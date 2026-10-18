"""End-to-end check of the whole crew with a PRETEND model (no internet, no key).

The pretend model makes the same tool calls a real model would: it reads, edits with edit_file, runs tests and
submits, using the reference fixes from the evaluation kit. One worker is killed halfway and one first attempt is
deliberately wrong, so the takeover, the rejection and the rework paths all run.

    python test_crew.py --seeded <seeded tinydb folder> --kit ../a2-eval-kit
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading

import _paths  # noqa: F401
from agents import Config
from llm import LLMReply
from run_crew import run_crew
from worktrees import WorkTrees

bad = []


def check(cond, name):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        bad.append(name)


def hunks(patch_text):
    """[(path, old_text, new_text)] from a unified diff, using context lines so `old` is unique."""
    out, path, cur = [], None, None
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            cur = {"old": [], "new": []}
            out.append((path, cur))
        elif cur is not None and line[:1] in " -+":
            if line[0] in " -":
                cur["old"].append(line[1:])
            if line[0] in " +":
                cur["new"].append(line[1:])
    return [(p, "\n".join(h["old"]), "\n".join(h["new"])) for p, h in out]


class Pretend:
    """Stateless fake model: decides its next move from the conversation so far."""
    primary = backup = "pretend"

    def __init__(self, kit, bad_first=(), memory_check=None):
        key = json.load(open(os.path.join(kit, "bugs.json")))["bugs"]
        self.fix = {b["ticket_id"]: hunks(open(os.path.join(kit, "fixes", b["id"] + ".patch")).read()) for b in key}
        self.bad_first = set(bad_first)
        self.stats = {"calls": 0, "cached": 0, "retries": 0, "fallbacks": 0}
        self.lock = threading.Lock()
        self.saw_lessons = []                     # tickets whose worker prompt showed saved lessons

    def chat(self, messages, tools=None, **_):
        with self.lock:
            self.stats["calls"] += 1
        system, user = messages[0]["content"], messages[1]["content"]
        done = [m for m in messages if m["role"] == "assistant"]
        n = len(done)
        if "senior debugging specialist" in system:
            call = self.worker_move(user, n)
        elif "principal engineer" in system:
            call = self.reviewer_move(user, messages, n)
        else:
            call = self.historian_move(user, n)
        return self.reply(call, n)

    def reply(self, call, n):
        calls = [] if call is None else [{"id": f"p{n}", "name": call[0], "arguments": call[1]}]
        text = "Done." if call is None else ""
        msg = {"role": "assistant", "content": text or None}
        if calls:
            msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]
        return LLMReply(text, calls, msg, "pretend", False, {"total_tokens": 100})

    def worker_move(self, user, n):
        tid = re.search(r"Ticket (T-\d+)", user).group(1)
        if "Lessons saved" in user:
            self.saw_lessons.append(tid)
        edits = self.fix[tid]
        applied = "unfinished changes have been applied" in user
        wrong = tid in self.bad_first and "rejected by the reviewer" not in user
        tests = re.findall(r"^  (tests/\S+)$", user, re.M)
        plan = [("search_code", {"text": "def "})]
        if wrong:
            plan += [("edit_file", {"path": "tinydb/middlewares.py", "old": "class Middleware:",
                                    "new": "# a stray comment\nclass Middleware:"})]
        elif not applied:
            plan += [("edit_file", {"path": p, "old": o, "new": nw}) for p, o, nw in edits]
        plan += [("run_tests", {"targets": tests}), ("submit_result", {"task_id": tid, "summary":
                 f"{tid}: fixed the cause in the code"})]
        return plan[n] if n < len(plan) else None

    def reviewer_move(self, user, messages, n):
        tid = re.search(r"task (T-\d+)", user).group(1)
        if n == 0:
            return ("review_patch", {"task_id": tid})
        if n == 1:
            facts = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))
            reasons = []
            if facts["out_of_footprint"]:
                reasons.append({"code": "OUT_OF_FOOTPRINT", "detail": "changed " + ", ".join(facts["out_of_footprint"])})
            if facts["ticket_tests_failing"]:
                reasons.append({"code": "TESTS_FAIL", "detail": "the ticket's tests still fail"})
            if facts["regression_count"]:
                reasons.append({"code": "REGRESSION", "detail": "other tests broke"})
            return ("review_decision", {"task_id": tid, "approved": not reasons, "reasons": reasons})
        return None

    def historian_move(self, user, n):
        tid = re.search(r"Task (T-\d+)", user).group(1)
        moves = [("record_outcome", {"task_id": tid, "kind": "episodic", "where": "Table.update", "symptom": f"{tid} failed",
                                      "cause": "a check was missing", "fix": "added the check"}),
                 ("record_outcome", {"task_id": tid, "kind": "semantic", "where": "Table.update",
                                      "rule": "Table.update() must skip unknown ids", "because": "callers pass stale lists"})]
        return moves[n] if n < len(moves) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeded", required=True)
    ap.add_argument("--kit", required=True)
    a = ap.parse_args()
    tickets = json.load(open(os.path.join(a.kit, "tickets.json")))
    llm = Pretend(a.kit, bad_first={"T-05"})
    work = os.path.join(tempfile.mkdtemp(), "work")
    cfg = Config(kill={"w1": 3}, checkpoint_every=2, max_attempts=3)
    report, wt, api = run_crew(llm, a.seeded, tickets, workers=3, run_id="crew-test", work_dir=work, cfg=cfg,
                               claim_ttl=4, timeout=300, keep_work=False)

    check(report["approved"] == 10, f"all 10 tickets approved ({report['approved']} of {report['total']})")
    check(len(report["crashed"]) == 1 and report["crashed"][0]["worker"] == "w1", "one worker was killed")
    killed_task = report["crashed"][0]["task"] if report["crashed"] else None
    took = [w for w in report["work"] if w["task"] == killed_task and w["worker"] != "w1"]
    check(bool(took), f"another worker finished the killed worker's task ({killed_task})")
    check(report["metrics"]["double_claims"] == 0, "no double claims")
    check(max(v["attempts"] for v in report["tasks"].values()) >= 2, "a rejected patch was reworked")
    check(any(r["status"] != "approved" and r["reasons"] for r in report["reviews"]), "the reviewer rejected a bad patch with reasons")
    check(len(report["memory"]) == 10, "the historian wrote lessons for every approved ticket")
    check(not report["errors"], "no errors in any agent thread: " + (report["errors"][0] if report["errors"] else ""))
    check(report["tool_refusals"] == 0, "no worker tried a tool outside its role")

    # the approved patches together should fix everything
    fin = WorkTrees(os.path.join(tempfile.mkdtemp(), "fin"))
    fin.make_base(a.seeded)
    fin.create("final")
    for tid, path in report["patches"].items():
        pass
    latest = {}
    for tid, path in report["patches"].items():
        latest[tid] = path
    for tid, path in sorted(latest.items()):
        fin.apply_patch("final", open(path).read())
    run = fin.run_tests("final")
    print(list(run["failures"].items())[:4] if isinstance(run["failures"], dict) else run["failures"][:4])
    check(run["failed"] == 0 and run["passed"] >= 228, f"combined patches: {run['passed']} pass, {run['failed']} fail")

    # second pass: same tickets again, lessons are kept, so workers should be shown them
    llm2 = Pretend(a.kit)
    report2, *_ = run_crew(llm2, a.seeded, tickets, workers=3, run_id="crew-test2", work_dir=work + "2",
                           cfg=Config(), claim_ttl=30, timeout=300, fresh_memory=False)
    check(report2["approved"] == 10 and len(set(llm2.saw_lessons)) >= 8, f"saved lessons were shown to workers on the next run ({len(set(llm2.saw_lessons))} of 10 tickets)")

    api.reset_run()
    api.clear_memory()
    print(f"\n{'ALL PASSED' if not bad else str(len(bad)) + ' FAILED'}")
    for k in ("work", "reviews"):
        pass
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
