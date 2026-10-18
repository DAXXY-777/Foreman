"""Run the whole crew on a set of tickets.

    python run_crew.py --seeded ../tinydb --workers 3

Needs: the database running with the TinyDB code loaded (load_code.py), and NVIDIA_API_KEY in .env.
It creates the tasks, starts Foreman's timer, N workers, one Reviewer and one Historian, and stops when every
ticket is approved or given up on. A report is written to run_report.json.
"""
import argparse
import dataclasses
import json
import os
import queue
import threading
import time
import traceback

import _paths  # noqa: F401
from personas import PLAYBOOK
from agents import Config, Crash, Foreman, Historian, Reviewer, Worker, say
from graph_api import GraphAPI
from tools import ToolBox
from worktrees import WorkTrees


def run_crew(llm, seeded, tickets, workers=3, run_id="run-1", work_dir="work", cfg=None, claim_ttl=180,
             timeout=3600, fresh_memory=True, repo="tinydb", keep_work=False, mode="full", stall_seconds=180,
             naive_gap=0.05):
    """mode: "full" = graph coordination + memory (the product); "graph" = coordination, no memory;
    "none" = no coordination (evaluation baseline: no plan, no safe claims, no expiry, no handoffs, no memory)."""
    if mode not in ("full", "graph", "none"):
        raise ValueError("mode must be full, graph or none")
    cfg = dataclasses.replace(cfg or Config(), use_memory=(mode == "full") and (cfg.use_memory if cfg else True),
                              coordination="none" if mode == "none" else "graph")
    t_start = time.time()
    wt = WorkTrees(work_dir)
    wt.make_base(seeded, force=True)
    names = [f"w{i + 1}" for i in range(workers)]
    for n in names + ["rev", "basecheck"]:
        wt.create(n)
    baseline = dict(wt.run_tests("basecheck")["results"])
    api = GraphAPI(repo, run_id, claim_ttl_seconds=claim_ttl)
    api.reset_run()
    if fresh_memory:
        api.clear_memory()
    if cfg.use_memory and not api.get_playbook(" ".join(k for e in PLAYBOOK for k in e["keywords"]), k=1)["hints"]:
        api.add_playbook(PLAYBOOK)                    # general debugging know-how, stored as procedural memory
    for n in names:
        api.register_agent(n, "worker")
    for n, r in (("foreman", "foreman"), ("reviewer", "reviewer"), ("historian", "historian")):
        api.register_agent(n, r)

    foreman = Foreman(ToolBox(api, "foreman", "foreman"), cfg)
    plan = foreman.setup(tickets, plan=(mode != "none"))
    ids = [t["ticket_id"] for t in tickets]
    wks = [Worker(llm, ToolBox(api, "worker", n, wt=wt, worktree=n, baseline=baseline,
                               naive_claim=(mode == "none"), naive_gap=naive_gap), cfg, n) for n in names]
    reviewer = Reviewer(llm, ToolBox(api, "reviewer", "reviewer", wt=wt, review_worktree="rev", baseline=baseline), cfg)
    historian = Historian(llm, ToolBox(api, "historian", "historian"), cfg)
    hist_box = historian.box

    stop = threading.Event()
    review_q, hist_q = queue.Queue(), queue.Queue()
    lock = threading.Lock()
    rep = {"work": [], "reviews": [], "memory": [], "crashed": [], "errors": []}
    state = {"last_active": time.time(), "working": 0}

    def finished():
        for i in ids:
            t = api.get_task(i)
            if t["status"] == "approved" or (t["status"] == "open" and t["attempt_count"] >= cfg.max_attempts):
                continue
            return False
        return review_q.unfinished_tasks == 0 and hist_q.unfinished_tasks == 0

    def worker_loop(w):
        fails = 0
        idle = 0
        while not stop.is_set():
            tid = w.claim_next()
            if tid is None:
                idle += 1                                       # nothing to claim: look less and less often
                stop.wait(min(1.5 * idle, 10))
                continue
            idle = 0
            with lock:
                state["working"] += 1
            try:
                rec = w.work(tid)
            except Crash as exc:
                say(cfg, w.name, f"CRASHED ({exc}); its claim on {tid} is left to expire")
                with lock:
                    rep["crashed"].append({"worker": w.name, "task": tid, "at": round(time.time(), 2)})
                    state["working"] -= 1
                return
            except Exception:                                   # noqa: BLE001
                with lock:
                    state["working"] -= 1
                fails += 1
                rep["errors"].append(traceback.format_exc()[-400:])
                w.box.call("release_claim", {"task_id": tid})
                if fails >= 3:
                    return
                continue
            with lock:
                rep["work"].append(rec)
                state["working"] -= 1
                state["last_active"] = time.time()
            if rec["task_status"] == "submitted":
                review_q.put((tid, rec["used_memory"]))

    def reviewer_loop():
        while not stop.is_set():
            try:
                tid, used = review_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                r = reviewer.review(tid)
                with lock:
                    rep["reviews"].append(r)
                state["last_active"] = time.time()
                if r["status"] == "approved":
                    if mode == "full":
                        hist_q.put((tid, used))
                else:
                    for mid in used if mode == "full" else []:  # the lesson did not lead to a good patch
                        hist_box.call("rate_memory", {"memory_id": mid, "worked": False})
                    if r["status"] == "submitted":              # no decision was made: try again once
                        r2 = reviewer.review(tid)
                        with lock:
                            rep["reviews"].append(r2)
                        if r2["status"] == "approved" and mode == "full":
                            hist_q.put((tid, used))
            except Exception:                                   # noqa: BLE001
                rep["errors"].append(traceback.format_exc()[-400:])
            finally:
                review_q.task_done()

    def historian_loop():
        while not stop.is_set():
            try:
                tid, used = hist_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                m = historian.remember(tid, used)
                with lock:
                    rep["memory"].append(m)
            except Exception:                                   # noqa: BLE001
                rep["errors"].append(traceback.format_exc()[-400:])
            finally:
                hist_q.task_done()

    def foreman_loop():
        while not stop.is_set():
            if mode != "none":                                  # "none" has no claim expiry
                foreman.tick()
            stop.wait(3)

    threads = [threading.Thread(target=f, daemon=True) for f in (reviewer_loop, historian_loop, foreman_loop)]
    wthreads = [threading.Thread(target=worker_loop, args=(w,), daemon=True) for w in wks]
    for t in threads + wthreads:
        t.start()
    timed_out = stalled = False
    while True:
        time.sleep(1)
        if finished():
            break
        if time.time() - t_start > timeout:
            timed_out = True
            break
        if (state["working"] == 0 and review_q.unfinished_tasks == 0 and hist_q.unfinished_tasks == 0
                and time.time() - state["last_active"] > stall_seconds):
            stalled = True                                      # nobody can make progress (for example a dead worker's claim never expires)
            break
        if not any(t.is_alive() for t in wthreads) and not any(
                api.get_task(i)["status"] in ("claimed", "submitted") for i in ids):
            break                                               # every worker is gone and nothing is in flight
    stop.set()
    for t in threads + wthreads:
        t.join(timeout=5)

    tasks = {}
    for i in ids:
        t = api.get_task(i)
        tasks[i] = {"status": t["status"], "attempts": t["attempt_count"] + (1 if t["status"] == "approved" else 0)}
    report = {"run_id": run_id, "mode": mode, "workers": workers, "seconds": round(time.time() - t_start, 1), "timed_out": timed_out,
              "stalled": stalled,
              "approved": sum(v["status"] == "approved" for v in tasks.values()), "total": len(ids), "tasks": tasks,
              "plan": plan, "metrics": api.get_run_metrics(), "llm": getattr(llm, "stats", {}),
              "tool_refusals": sum(len(w.box.refused) for w in wks), **rep}
    report["eval"] = score_run(wt, api, seeded, tickets, baseline, report)
    if not keep_work:
        report["patches"] = {t: p for t, p in _collect_patches(wt, ids)}
    return report, wt, api


def _overlap(a, b):
    return a["t_start"] < b["t_end"] and b["t_start"] < a["t_end"]


def score_run(wt, api, seeded, tickets, baseline, report):
    """The same objective check for every setup. Merge every approved fix, in approval order, into one fresh copy of
    the seeded code, then run all the tests. This is what really works, whatever the agents said."""
    ids = [t["ticket_id"] for t in tickets]
    approved = []
    for r in report["reviews"]:
        if r["status"] == "approved" and r["task"] not in approved:
            approved.append(r["task"])
    wt.create("land")
    landed, failed_to_land = [], []
    for tid in approved:
        ref = (api.get_result(tid) or {}).get("patch_ref")
        if not ref or not os.path.exists(ref):
            failed_to_land.append(tid)
            continue
        try:
            wt.apply_patch("land", open(ref).read())
            landed.append(tid)
        except Exception:                                       # noqa: BLE001  (the patch no longer fits the merged code)
            failed_to_land.append(tid)
    run = wt.run_tests("land")
    res = run["results"]
    solved = [t["ticket_id"] for t in tickets if t["failing_tests"] and all(res.get(x) == "passed" for x in t["failing_tests"])]
    regress = sorted(t for t, s in baseline.items() if s == "passed" and res.get(t) != "passed")
    work = report["work"]
    duplicate = sum(1 for i, a in enumerate(work) for b in work[i + 1:]
                    if a["task"] == b["task"] and a["worker"] != b["worker"] and _overlap(a, b))
    conflicts = (report.get("plan") or {}).get("conflicts", {})
    clash = 0
    for i, a in enumerate(work):
        for b in work[i + 1:]:
            if a["task"] != b["task"] and b["task"] in conflicts.get(a["task"], []) and _overlap(a, b):
                clash += 1
    tokens = sum(w["tokens"] for w in work) + sum(r["tokens"] for r in report["reviews"]) + sum(m["tokens"] for m in report["memory"])
    crashed = report["crashed"][0]["task"] if report["crashed"] else None
    return {"approved": len(approved), "merged_ok": len(landed), "failed_to_merge": failed_to_land,
            "fixed_in_merged_code": len(solved), "fixed_ids": solved, "regressions_in_merged_code": len(regress),
            "tests_passing_merged": run["passed"], "tests_failing_merged": run["failed"],
            "duplicate_work": duplicate, "conflicting_tickets_worked_together": clash,
            "worker_sessions": len(work), "wasted_sessions": max(0, len(work) - len(approved)),
            "sent_back_by_reviewer": sum(r["status"] != "approved" for r in report["reviews"]),
            "tokens": tokens, "crashed_task": crashed,
            "crash_recovered": (report["tasks"].get(crashed, {}).get("status") == "approved") if crashed else None}


def _collect_patches(wt, ids):
    pdir = os.path.join(wt.root, "patches")
    for i in ids:
        if os.path.isdir(pdir):
            for f in sorted(os.listdir(pdir), key=lambda x: os.path.getmtime(os.path.join(pdir, x))):   # oldest first, so the newest wins
                if f.endswith(f"__{i}.patch") and not f.startswith(("handoff__",)):
                    yield i, os.path.join(pdir, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeded", required=True, help="the seeded TinyDB folder (the tree with the bugs)")
    ap.add_argument("--tickets", default=os.path.join(_paths.HERE, "..", "foreman-schema", "tickets.json"))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--run-id", default="run-1")
    ap.add_argument("--work-dir", default="work")
    ap.add_argument("--claim-ttl", type=int, default=180)
    ap.add_argument("--timeout", type=int, default=3600, help="seconds")
    ap.add_argument("--max-tasks", type=int, default=None, help="only the first N tickets (for a quick try)")
    ap.add_argument("--kill", default="", help="e.g. w1:4 makes worker w1 stop after 4 steps")
    ap.add_argument("--keep-memory", action="store_true", help="do not clear saved lessons first")
    ap.add_argument("--no-memory", action="store_true", help="workers do not recall lessons")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--mode", default="full", choices=["full", "graph", "none"],
                    help="full = coordination + memory, graph = coordination only, none = no coordination (baseline)")
    ap.add_argument("--out", default="run_report.json")
    a = ap.parse_args()

    from llm import LLMClient
    tickets = json.load(open(a.tickets))[: a.max_tasks]
    kill = {k: int(v) for k, v in (x.split(":") for x in a.kill.split(",") if x)}
    cfg = Config(max_steps=a.max_steps, use_memory=not a.no_memory, kill=kill)
    report, _, _ = run_crew(LLMClient(), a.seeded, tickets, a.workers, a.run_id, a.work_dir, cfg, a.claim_ttl,
                            a.timeout, fresh_memory=not a.keep_memory, mode=a.mode)
    json.dump(report, open(a.out, "w"), indent=2, default=str)
    print(f"\n{report['approved']} of {report['total']} tickets approved in {report['seconds']} s. "
          f"Report: {a.out}")
    for t, v in report["tasks"].items():
        print(f"  {t}: {v['status']} (attempts {v['attempts']})")


if __name__ == "__main__":
    main()
