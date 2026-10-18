"""The claim race, no model needed: 50 workers take the same ticket at the same instant, 20 rounds.
Compares the real atomic claim with a naive "look, then take" claim.   python race_test.py"""
import json
import os
import threading
import time

import _paths  # noqa: F401
from graph_api import GraphAPI

N, ROUNDS = 50, 20
REPO = os.environ.get("FOREMAN_RACE_REPO", "tinydb_eval")     # its own graph, so no real run is touched
TICKET = json.load(open(os.path.join(_paths.HERE, "..", "foreman-schema", "tickets.json")))[0]
TID = TICKET["ticket_id"]


def one_round(kind, rnd):
    api = GraphAPI(REPO, f"race-{kind}-{rnd}", claim_ttl_seconds=60)
    api.reset_run()
    api.create_task(TICKET)
    names = [f"r{i}" for i in range(N)]
    for nm in names:
        api.register_agent(nm, "worker")
    wins, lat = [], []
    gate = threading.Barrier(N)

    def go(nm):
        gate.wait()
        t0 = time.perf_counter()
        r = api.claim_task(TID, nm) if kind == "atomic" else api.naive_claim(TID, nm, 0.05)
        lat.append((time.perf_counter() - t0) * 1000)
        if r.get("ok"):
            wins.append(nm)
    ts = [threading.Thread(target=go, args=(nm,)) for nm in names]
    [t.start() for t in ts]
    [t.join() for t in ts]
    api.reset_run()
    try:
        api.g.client.close()                      # give the 50 connections back, or a Mac runs out of open files
    except Exception:
        pass
    return len(wins), lat


def main():
    out = {}
    for kind in ("atomic", "naive"):
        wins, lat = [], []
        for rnd in range(ROUNDS):
            w, l = one_round(kind, rnd)
            wins.append(w)
            lat += l
        lat.sort()
        out[kind] = {"winners_per_round": wins, "rounds_with_exactly_one_winner": sum(w == 1 for w in wins),
                     "worst_round": max(wins), "p50_ms": round(lat[len(lat) // 2], 1), "p95_ms": round(lat[int(len(lat) * .95)], 1)}
    json.dump(out, open("race_results.json", "w"), indent=2)
    md = [f"# Claim race: {N} workers, one ticket, {ROUNDS} rounds", "",
          "| Claim | Rounds with exactly one winner | Most workers told 'yours' in one round | p50 ms | p95 ms |", "|---|---|---|---|---|"]
    for kind, label in (("atomic", "Atomic claim (the product)"), ("naive", "Naive look-then-take")):
        o = out[kind]
        md.append(f"| {label} | {o['rounds_with_exactly_one_winner']} of {ROUNDS} | {o['worst_round']} | {o['p50_ms']} | {o['p95_ms']} |")
    open("race_results.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
