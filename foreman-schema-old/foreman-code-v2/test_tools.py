"""Checks the role scoping and one full pass through the tools (claim, fix, submit, review, remember).
Needs the database running with the TinyDB code loaded (load_code.py), and the seeded tree path:
    python test_tools.py --seeded ../tinydb --kit ../a2-eval-kit      (the kit is used only to borrow one reference fix)
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

import _paths  # noqa: F401
from graph_api import GraphAPI
from tools import ROLE_TOOLS, ToolBox
from worktrees import WorkTrees

bad = []


def check(cond, name):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        bad.append(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeded", required=True)
    ap.add_argument("--kit", required=True)
    a = ap.parse_args()

    root = os.path.join(tempfile.mkdtemp(), "work")
    wt = WorkTrees(root)
    wt.make_base(a.seeded)
    for n in ("w1", "w2", "rev", "base-check"):
        wt.create(n)
    baseline = {t: s for t, s in wt.run_tests("base-check")["results"].items()}

    api = GraphAPI("tinydb", "tools-test")
    api.reset_run()
    for who, role in (("w1", "worker"), ("w2", "worker"), ("rev", "reviewer"), ("fm", "foreman"), ("hi", "historian")):
        api.register_agent(who, role)
    tickets = {t["ticket_id"]: t for t in json.load(open(os.path.join(a.kit, "tickets.json")))}
    foreman = ToolBox(api, "foreman", "fm")
    worker = ToolBox(api, "worker", "w1", wt=wt, worktree="w1")
    worker2 = ToolBox(api, "worker", "w2", wt=wt, worktree="w2")
    reviewer = ToolBox(api, "reviewer", "rev", wt=wt, review_worktree="rev", baseline=baseline)
    historian = ToolBox(api, "historian", "hi")

    # --- role scoping ---
    check(foreman.call("create_task", {"ticket": tickets["T-02"]})["ok"], "foreman creates a task")
    check(worker.call("review_decision", {"task_id": "T-02", "approved": True})["error"] == "TOOL_NOT_ALLOWED",
          "a worker cannot approve")
    check(worker.call("record_outcome", {"task_id": "T-02", "kind": "episodic", "where": "x"})["error"] == "TOOL_NOT_ALLOWED",
          "a worker cannot write memory")
    check(reviewer.call("claim_task", {"task_id": "T-02"})["error"] == "TOOL_NOT_ALLOWED", "a reviewer cannot claim")
    check(historian.call("submit_result", {"task_id": "T-02", "summary": "x"})["error"] == "TOOL_NOT_ALLOWED",
          "a historian cannot submit")
    check(foreman.call("claim_task", {"task_id": "T-02"})["error"] == "TOOL_NOT_ALLOWED", "foreman cannot claim")
    for role, names in ROLE_TOOLS.items():
        box = ToolBox(api, role, "x")
        check({s["function"]["name"] for s in box.specs()} == set(names), f"{role} is shown exactly its own tools")
    check(len(worker.refused) == 2, "refusals are counted")

    # --- worker ---
    t = worker.call("get_task", {"task_id": "T-02"})
    check(t["ok"] and "flags" in t["description"] and t["failing_tests"] == tickets["T-02"]["failing_tests"],
          "worker can read the ticket and its failing tests")
    check(worker.call("claim_task", {"task_id": "T-02"})["ok"], "worker claims")
    check(worker2.call("claim_task", {"task_id": "T-02"})["reason"] == "already_claimed", "second worker is refused")
    check(worker.call("read_file", {"path": "../../etc/passwd"})["error"] == "FILE_ERROR", "worker cannot leave its folder")
    check(worker.call("edit_file", {"path": "tests/test_queries.py", "old": "a", "new": "b"})["error"] == "TESTS_READ_ONLY",
          "worker cannot edit tests")
    check(worker.call("submit_result", {"task_id": "T-02", "summary": "nothing"})["error"] == "NO_CHANGES",
          "cannot submit without changes")
    src = worker.call("search_code", {"text": "def search(self, regex"})["hits"]
    check("tinydb/queries.py" in src, "worker can search the code")
    wt.apply_patch("w1", open(os.path.join(a.kit, "fixes", "I3.patch")).read())      # stands in for the model's edit
    hw = worker.call("write_handoff", {"task_id": "T-02", "done": "found it", "remaining": "submit", "tried": "-"})
    check(hw["ok"], "handoff written (with the unfinished patch saved)")
    res = worker.call("submit_result", {"task_id": "T-02", "summary": "regex flags were ignored; now passed on"})
    check(res["ok"] and res["tests_passed"] and res["files_touched"] == ["tinydb/queries.py"], "submit records real test result")

    # --- reviewer ---
    rp = reviewer.call("review_patch", {"task_id": "T-02"})
    check(rp["ok"] and not rp["ticket_tests_failing"] and rp["regression_count"] == 0 and not rp["out_of_footprint"],
          "reviewer sees a clean patch")
    d = reviewer.call("review_decision", {"task_id": "T-02", "approved": True, "reasons": []})
    check(d["ok"] and api.get_task("T-02")["status"] == "approved", "reviewer approves")

    # --- an out-of-footprint patch is visible to the reviewer ---
    foreman.call("create_task", {"ticket": tickets["T-01"]})
    worker2.call("claim_task", {"task_id": "T-01"})
    wt.write("w2", "tinydb/middlewares.py", open(os.path.join(wt.path("w2"), "tinydb/middlewares.py")).read() + "\n# stray change\n")
    stray_patch = os.path.join(wt.root, "patches", "stray__T-01.patch")
    os.makedirs(os.path.dirname(stray_patch), exist_ok=True)
    open(stray_patch, "w").write(wt.diff("w2"))
    worker2.current_task = "T-01"
    sub = worker2.call("submit_result", {"task_id": "T-01", "summary": "unrelated edit"})
    check(sub.get("error") == "NO_CHANGES" and "middlewares" in sub["detail"], "a stray file outside the footprint is put back at submit")
    api.submit_result("T-01", "w2", "unrelated edit", False, ["tinydb/middlewares.py"], stray_patch)   # as if it slipped through
    rp2 = reviewer.call("review_patch", {"task_id": "T-01"})
    check(rp2["ok"] and rp2["ticket_tests_failing"] and "tinydb/middlewares.py" in rp2["out_of_footprint"],
          "reviewer sees tests still failing and a file outside the footprint")

    # --- historian ---
    refused = historian.call("record_outcome", {"task_id": "T-02", "kind": "episodic", "where": "Query.search", "symptom": "flags ignored"})
    check(refused.get("error") == "MISSING_FIELDS" and "cause" in refused["detail"] and "fix" in refused["detail"],
          "a lesson with empty fields is refused and says which ones")
    mem = historian.call("record_outcome", {"task_id": "T-02", "kind": "semantic", "where": "Query.search",
                                            "rule": "Query.search() must pass flags to re.search", "because": "callers rely on IGNORECASE"})
    check(mem["ok"], "historian saves a lesson")
    check("RULE: Query.search() must pass flags" in historian.call("recall_similar", {"task_id": "T-02"})["memories"][0]["summary"],
          "the saved lesson has the fixed WHERE | RULE | BECAUSE shape")
    check(historian.call("rate_memory", {"memory_id": mem["memory_id"], "worked": True})["ok"], "historian rates it")
    again = historian.call("recall_similar", {"task_id": "T-02"})
    check(any(m["memory_id"] == mem["memory_id"] for m in again["memories"]), "the lesson comes back for a similar task")

    api.reset_run()
    api.clear_memory()
    shutil.rmtree(os.path.dirname(root), ignore_errors=True)
    print(f"\n{'ALL PASSED' if not bad else str(len(bad)) + ' FAILED: ' + '; '.join(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
