"""Checks the three evaluation setups with the PRETEND model (no internet, no key).

    python test_eval.py --seeded <seeded tinydb folder> --kit ../a2-eval-kit
"""
import argparse
import json
import os
import sys
import tempfile
import threading

import _paths  # noqa: F401
from agents import Config
from graph_api import GraphAPI
from run_crew import run_crew
from test_crew import Pretend

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
    tickets = json.load(open(os.path.join(a.kit, "tickets.json")))
    work = tempfile.mkdtemp()
    out = {}
    for mode in ("full", "graph", "none"):
        llm = Pretend(a.kit)
        cfg = Config(kill={"w1": 3}, checkpoint_every=2, max_attempts=3, verbose=False)
        rep, wt, api = run_crew(llm, a.seeded, tickets, workers=3, run_id=f"eval-{mode}", work_dir=os.path.join(work, mode),
                                cfg=cfg, claim_ttl=4, timeout=240, mode=mode, stall_seconds=6, naive_gap=0.02)
        out[mode] = rep
        e = rep["eval"]
        print(f"  [{mode}] approved {e['approved']}, merged-fixed {e['fixed_in_merged_code']}, regressions {e['regressions_in_merged_code']}, "
              f"stalled {rep['stalled']}, memory notes {len(rep['memory'])}, duplicate {e['duplicate_work']}, recovered {e['crash_recovered']}")
        check(not rep["errors"], f"{mode}: no errors in agent threads " + (rep["errors"][0] if rep["errors"] else ""))
    f, g, n = out["full"], out["graph"], out["none"]
    check(f["eval"]["approved"] == 10 and f["eval"]["fixed_in_merged_code"] == 10 and f["eval"]["regressions_in_merged_code"] == 0,
          "full: 10 approved and all 10 fixed in the merged code")
    check(len(f["memory"]) == 10, "full: memory notes written")
    check(g["eval"]["approved"] == 10 and g["eval"]["fixed_in_merged_code"] == 10, "graph: 10 approved, memory off")
    check(len(g["memory"]) == 0 and g["llm"]["calls"] < f["llm"]["calls"], "graph: no historian calls")
    check(f["eval"]["crash_recovered"] is True and g["eval"]["crash_recovered"] is True, "full and graph: the killed worker's ticket was recovered")
    check(n["stalled"] is True and n["eval"]["crash_recovered"] is False, "none: the killed worker's ticket was never recovered, the run stalled")
    check(n["eval"]["approved"] == 9 and n["eval"]["fixed_in_merged_code"] == 9, "none: the other 9 tickets were still fixed")
    check(len(n["memory"]) == 0, "none: no memory")
    check(f["metrics"]["double_claims"] == 0 and g["metrics"]["double_claims"] == 0, "full and graph: zero double claims")

    # the claim race: 12 workers take the same ticket at once, 10 rounds
    atomic_win, naive_win = [], []
    for kind in ("atomic", "naive"):
        for rnd in range(10):
            api = GraphAPI("tinydb", f"race-{kind}-{rnd}", claim_ttl_seconds=60)
            api.reset_run()
            api.create_task(tickets[0])
            names = [f"r{i}" for i in range(12)]
            for nm in names:
                api.register_agent(nm, "worker")
            wins = []
            gate = threading.Barrier(12)

            def go(nm):
                gate.wait()
                r = api.claim_task(tickets[0]["ticket_id"], nm) if kind == "atomic" else api.naive_claim(tickets[0]["ticket_id"], nm, 0.02)
                if r.get("ok"):
                    wins.append(nm)
            ts = [threading.Thread(target=go, args=(nm,)) for nm in names]
            [t.start() for t in ts]
            [t.join() for t in ts]
            (atomic_win if kind == "atomic" else naive_win).append(len(wins))
            api.reset_run()
    print(f"  race: atomic winners per round {atomic_win}; naive winners per round {naive_win}")
    check(all(w == 1 for w in atomic_win), "atomic claim: exactly one winner in every round")
    check(sum(naive_win) > len(naive_win), "naive claim: more than one worker is told yes in at least some rounds")
    print(f"\n{'ALL PASSED' if not bad else str(len(bad)) + ' FAILED'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
