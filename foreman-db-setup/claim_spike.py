"""Claim spike: can FalkorDB hand out a task to exactly one of many simultaneous claimants?

Three experiments, many rounds each, N threads per round, every thread on its own connection:
  1. naive        read the status, then write it (check-then-act)          -> expect duplicates
  2. atomic       one query that checks 'open' and sets 'claimed' together  -> expect exactly 1
  3. conflict     two tasks that conflict; half the threads claim each one   -> expect exactly 1 in total
"""
import os
import statistics
import sys
import threading
import time

from falkordb import FalkorDB

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("FALKORDB_PORT", "6379"))
N = int(sys.argv[2]) if len(sys.argv) > 2 else 50
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 30

NAIVE_READ = "MATCH (t:Task {id:$id}) RETURN t.status"
NAIVE_WRITE = "MATCH (t:Task {id:$id}) SET t.status = 'claimed', t.claimed_by = $who"

ATOMIC = """
MATCH (t:Task {id:$id})
WHERE t.status = 'open'
SET t.status = 'claimed', t.claimed_by = $who
RETURN t.id
"""

CONFLICT = """
MATCH (t:Task {id:$id})
WHERE t.status = 'open'
OPTIONAL MATCH (t)-[:CONFLICTS_WITH]-(o:Task {status:'claimed'})
WITH t, o
WHERE o IS NULL
SET t.status = 'claimed', t.claimed_by = $who
RETURN t.id
"""


def graph(name="claim_spike"):
    return FalkorDB(host="localhost", port=PORT).select_graph(name)


def fire(jobs):
    """Run all jobs at the same instant. jobs: list of callables returning a value."""
    barrier = threading.Barrier(len(jobs))
    out = [None] * len(jobs)
    lat = [0.0] * len(jobs)

    def run(i):
        g_job = jobs[i]
        barrier.wait()
        t0 = time.perf_counter()
        try:
            out[i] = g_job()
        except Exception as exc:  # noqa: BLE001 - we want to see every failure
            out[i] = exc
        lat[i] = (time.perf_counter() - t0) * 1000

    ts = [threading.Thread(target=run, args=(i,)) for i in range(len(jobs))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out, lat


def main():
    admin = graph()
    try:
        admin.delete()
    except Exception:
        pass
    admin = graph()
    conns = [graph() for _ in range(N)]          # one connection per simulated worker
    errors = 0

    # ---------- 1. naive ----------
    winners, lats = [], []
    for r in range(ROUNDS):
        tid = f"naive-{r}"
        admin.query("CREATE (:Task {id:$id, status:'open'})", {"id": tid})

        def job(i, tid=tid):
            def go():
                st = conns[i].query(NAIVE_READ, {"id": tid}).result_set[0][0]
                if st == "open":
                    conns[i].query(NAIVE_WRITE, {"id": tid, "who": f"w{i}"})
                    return True
                return False
            return go
        out, lat = fire([job(i) for i in range(N)])
        errors += sum(isinstance(o, Exception) for o in out)
        winners.append(sum(o is True for o in out)); lats += lat
    print(f"1 naive    : winners per round -> min {min(winners)}, max {max(winners)}, "
          f"rounds with exactly 1 winner: {sum(w == 1 for w in winners)}/{ROUNDS}")

    # ---------- 2. atomic ----------
    winners, lats = [], []
    for r in range(ROUNDS):
        tid = f"atomic-{r}"
        admin.query("CREATE (:Task {id:$id, status:'open'})", {"id": tid})

        def job(i, tid=tid):
            return lambda: len(conns[i].query(ATOMIC, {"id": tid, "who": f"w{i}"}).result_set) > 0
        out, lat = fire([job(i) for i in range(N)])
        errors += sum(isinstance(o, Exception) for o in out)
        winners.append(sum(o is True for o in out)); lats += lat
        who = admin.query("MATCH (t:Task {id:$id}) RETURN t.claimed_by", {"id": tid}).result_set[0][0]
        assert who is not None
    print(f"2 atomic   : winners per round -> min {min(winners)}, max {max(winners)}, "
          f"rounds with exactly 1 winner: {sum(w == 1 for w in winners)}/{ROUNDS}")
    lat_s = sorted(lats)
    print(f"             claim latency with {N} at once: median {statistics.median(lat_s):.0f} ms, "
          f"p95 {lat_s[int(len(lat_s) * .95)]:.0f} ms, max {lat_s[-1]:.0f} ms")

    # ---------- 3. conflict-aware ----------
    winners = []
    for r in range(ROUNDS):
        a, b = f"cA-{r}", f"cB-{r}"
        admin.query("CREATE (:Task {id:$a, status:'open'})-[:CONFLICTS_WITH]->(:Task {id:$b, status:'open'})",
                    {"a": a, "b": b})

        def job(i, a=a, b=b):
            tid = a if i % 2 == 0 else b
            return lambda: len(conns[i].query(CONFLICT, {"id": tid, "who": f"w{i}"}).result_set) > 0
        out, lat = fire([job(i) for i in range(N)])
        errors += sum(isinstance(o, Exception) for o in out)
        winners.append(sum(o is True for o in out))
        held = admin.query("MATCH (t:Task) WHERE t.id IN [$a,$b] AND t.status='claimed' RETURN count(t)",
                           {"a": a, "b": b}).result_set[0][0]
        assert held == winners[-1], (held, winners[-1])
    print(f"3 conflict : two conflicting tasks, {N} claimants -> winners per round: "
          f"min {min(winners)}, max {max(winners)}, rounds with exactly 1: {sum(w == 1 for w in winners)}/{ROUNDS}")
    print(f"errors: {errors}")
    admin.delete()


main()
