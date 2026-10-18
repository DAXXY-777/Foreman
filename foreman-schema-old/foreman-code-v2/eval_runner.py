"""The three-setup comparison. Same bugs, same model, same tools, same crash. Only the coordination changes.

  none   no plan, no safe claims, no claim expiry, no handoffs, no memory (the baseline)
  graph  the graph coordinates the crew (plan, safe claims, expiry, handoffs), no memory
  full   the product: graph coordination + memory

    python eval_runner.py --seeded ../tinydb --repeats 2

Each run is saved on its own in eval_runs/, so you can stop it and start it again: finished runs are skipped.
Results: eval_results.json and eval_results.md. After every run the approved fixes are merged into one fresh copy of
the code and ALL tests are run, so the score is what really works.
"""
import argparse
import json
import os
import statistics
import time

import _paths  # noqa: F401
from agents import Config
from run_crew import run_crew

SETUPS = ("none", "graph", "full")
LABEL = {"none": "No coordination", "graph": "Graph only", "full": "Full system (graph + memory)"}


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 1) if xs else None


def table(runs):
    rows = ["| Setup | Runs | Bugs fixed in the merged code (of 10) | Double claims | Clashing bugs worked on together | Duplicate work | "
            "Wasted worker sessions | Sent back by reviewer | Crashed bug recovered | Time (min) | Model calls | Tokens (million) |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in SETUPS:
        rs = [r for r in runs if r["mode"] == s]
        if not rs:
            continue
        e = [r["eval"] for r in rs]
        rec = [x["crash_recovered"] for x in e if x["crash_recovered"] is not None]
        rows.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            LABEL[s], len(rs), mean([x["fixed_in_merged_code"] for x in e]), mean([r["metrics"]["double_claims"] for r in rs]),
            mean([x["conflicting_tickets_worked_together"] for x in e]), mean([x["duplicate_work"] for x in e]),
            mean([x["wasted_sessions"] for x in e]), mean([x["sent_back_by_reviewer"] for x in e]),
            f"{sum(rec)} of {len(rec)}" if rec else "n/a", mean([r["seconds"] / 60 for r in rs]),
            mean([r["llm"].get("calls") for r in rs]), mean([x["tokens"] / 1e6 for x in e])))
    rows += ["", "Every number is an average over the runs of that setup. Per-run numbers:", "",
             "| Run | Fixed in merged code | Approved | Regressions | Double claims | Duplicate work | Stalled | Time (min) |",
             "|---|---|---|---|---|---|---|---|"]
    for r in runs:
        e = r["eval"]
        rows.append(f"| {r['run_id']} | {e['fixed_in_merged_code']} | {e['approved']} | {e['regressions_in_merged_code']} | "
                    f"{r['metrics']['double_claims']} | {e['duplicate_work']} | {r['stalled']} | {round(r['seconds'] / 60, 1)} |")
    return "\n".join(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeded", required=True)
    ap.add_argument("--tickets", default=os.path.join(_paths.HERE, "..", "foreman-schema", "tickets.json"))
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--setups", default=",".join(SETUPS))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--kill", default="w1:6", help="the same crash in every setup, e.g. w1:6 (worker w1 dies after 6 steps); empty = no crash")
    ap.add_argument("--claim-ttl", type=int, default=60)
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--timeout", type=int, default=3000, help="seconds per run")
    ap.add_argument("--stall", type=int, default=180, help="seconds with nobody able to make progress before a run is ended")
    ap.add_argument("--outdir", default="eval_runs")
    ap.add_argument("--repo-name", default="tinydb_eval", help="graph used for the comparison, so your real run's data is left alone")
    ap.add_argument("--pretend-kit", default="", help="test the runner itself with the pretend model (path to the eval kit)")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    from graph_api import GraphAPI
    n_fn = GraphAPI(a.repo_name, "check").g.query("MATCH (f:Function) RETURN count(f)").result_set[0][0]
    if not n_fn:
        raise SystemExit(f"The graph for '{a.repo_name}' has no code in it yet. Load it first:\n"
                         f"  python ../foreman-schema/load_code.py --repo {a.seeded} --repo-name {a.repo_name}")
    tickets = json.load(open(a.tickets))
    kill = {k: int(v) for k, v in (x.split(":") for x in a.kill.split(",") if x)}
    setups = [s for s in a.setups.split(",") if s]
    plan = [(r + 1, s) for r in range(a.repeats) for s in setups]       # interleaved, so any drift in the service hits all setups
    print(f"{len(plan)} runs planned: " + ", ".join(f"{s}-{r}" for r, s in plan), flush=True)
    for r, s in plan:
        run_id = f"eval-{s}-{r}"
        path = os.path.join(a.outdir, run_id + ".json")
        if os.path.exists(path):
            print(f"[skip] {run_id} already done", flush=True)
            continue
        print(f"\n=== {run_id} started {time.strftime('%H:%M:%S')} ===", flush=True)
        if a.pretend_kit:
            from test_crew import Pretend
            llm = Pretend(a.pretend_kit)
        else:
            from llm import LLMClient
            llm = LLMClient()
        cfg = Config(max_steps=a.max_steps, kill=dict(kill), verbose=False)
        report, _, _ = run_crew(llm, a.seeded, tickets, a.workers, run_id, os.path.join("work", run_id), cfg, a.claim_ttl,
                                a.timeout, mode=s, stall_seconds=a.stall, repo=a.repo_name)
        json.dump(report, open(path, "w"), indent=2, default=str)
        e = report["eval"]
        print(f"=== {run_id}: {e['fixed_in_merged_code']} of {len(tickets)} fixed in the merged code, {round(report['seconds'] / 60, 1)} min, "
              f"crash recovered: {e['crash_recovered']}, stalled: {report['stalled']} ===", flush=True)
        runs = [json.load(open(os.path.join(a.outdir, f))) for f in sorted(os.listdir(a.outdir)) if f.endswith(".json")]
        json.dump(runs, open("eval_results.json", "w"), indent=2, default=str)
        open("eval_results.md", "w").write("# Evaluation results\n\n" + table(runs) + "\n")
    runs = [json.load(open(os.path.join(a.outdir, f))) for f in sorted(os.listdir(a.outdir)) if f.endswith(".json")]
    print("\n" + table(runs))


if __name__ == "__main__":
    main()
