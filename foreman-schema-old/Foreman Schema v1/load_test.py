"""Load test for the Track 2 "high-concurrency reads and writes" focus area.

    python load_test.py             full run (about a minute)
    python load_test.py --quick     smaller run (about 15 seconds)

It uses the same GraphAPI the agents will use, so the numbers include the Python client. It checks the
rules while the load runs (nobody double-claims, conflicting tickets never run together, no message
lost) and fails loudly if any rule breaks. Results are written to load_test_results.json and .md.

Needs the real TinyDB graph first:  python load_code.py --repo ../tinydb
"""
import itertools
import json
import platform
import random
import statistics
import sys
import threading
import time
from datetime import datetime, timezone

import schema
from graph_api import GraphAPI
from schema import q

QUICK = "--quick" in sys.argv
WORKERS = 50
N_TASKS = 100 if QUICK else 300
MIXED_SECONDS = 4 if QUICK else 12
RACE_ROUNDS = 10 if QUICK else 40
TENANTS = 5
TENANT_TASKS = 40 if QUICK else 100

samples = {}                      # function name -> list of seconds
problems = []                     # rule violations found during the run
results = {}


class Timed:
    """Wraps a GraphAPI and records how long each call takes."""
    def __init__(self, api, bucket):
        self._api, self._bucket = api, bucket

    def __getattr__(self, name):
        fn = getattr(self._api, name)
        if not callable(fn):
            return fn

        def wrapper(*a, **k):
            t0 = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                self._bucket.setdefault(name, []).append(time.perf_counter() - t0)
        return wrapper


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] * 1000 if xs else 0.0


def summarize(bucket):
    return {k: {"calls": len(v), "p50_ms": round(pct(v, .50), 1), "p95_ms": round(pct(v, .95), 1),
                "p99_ms": round(pct(v, .99), 1), "max_ms": round(max(v) * 1000, 1)} for k, v in sorted(bucket.items())}


def run_threads(n, fn):
    errors = []

    def wrap(i):
        try:
            fn(i)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
    ts = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    t0 = time.perf_counter()
    for t in ts:
        t.daemon = True
    [t.start() for t in ts]
    for t in ts:
        t.join(timeout=90)
    stuck = sum(t.is_alive() for t in ts)
    if stuck:
        errors.append(f"{stuck} of {n} workers did not finish within 90 s (database not answering?)")
    return time.perf_counter() - t0, errors


def check(ok, message):
    if not ok:
        problems.append(message)
        print("   RULE BROKEN:", message)


def agents(api, n):
    for i in range(n):
        api.register_agent(f"lt-w{i}", "worker")
    api.register_agent("lt-reviewer", "reviewer")
    api.register_agent("lt-foreman", "foreman")
    api.register_agent("lt-historian", "historian")


def some_tests(api, n=1):
    return [r[0] for r in q(api.g, "MATCH (t:Test) RETURN t.id ORDER BY t.id LIMIT $n", {"n": n})]


# ------------------------------------------------------------------------------------------------
def phase_race():
    print(f"\n1. Claim race: {WORKERS * 2} workers grab one ticket at the same instant, {RACE_ROUNDS} rounds")
    bucket = {}
    api = GraphAPI("tinydb", "lt-race", claim_ttl_seconds=600)
    api.reset_run()
    agents(api, WORKERS * 2)
    test = some_tests(api)
    wins_per_round = []
    t_all = 0.0
    for r in range(RACE_ROUNDS):
        api.create_task({"ticket_id": f"R-{r}", "title": "race", "description": "d", "failing_tests": test})
        timed = Timed(api, bucket)
        barrier = threading.Barrier(WORKERS * 2)
        wins = []

        def go(i, tid=f"R-{r}"):
            barrier.wait()
            if timed.claim_task(tid, f"lt-w{i}")["ok"]:
                wins.append(i)
        dt, errs = run_threads(WORKERS * 2, go)
        t_all += dt
        wins_per_round.append(len(wins))
        print(f"   round {r + 1}/{RACE_ROUNDS} done in {dt:.1f} s", flush=True)
        check(not errs, f"race round {r}: errors {errs[:1]}")
    exact = sum(w == 1 for w in wins_per_round)
    print(f"   rounds with exactly one winner: {exact}/{RACE_ROUNDS}")
    check(exact == RACE_ROUNDS, "claim race produced a round without exactly one winner")
    m = api.get_run_metrics()
    check(m["double_claims"] == 0, "double claims recorded in race phase")
    s = summarize(bucket)["claim_task"]
    print(f"   claim latency with {WORKERS * 2} at once: p50 {s['p50_ms']} ms, p95 {s['p95_ms']} ms, p99 {s['p99_ms']} ms")
    results["claim_race"] = {"workers": WORKERS * 2, "rounds": RACE_ROUNDS, "rounds_exactly_one_winner": exact,
                             "claim_latency": s}
    api.reset_run()


def phase_lifecycle():
    print(f"\n2. Full ticket lifecycle: {WORKERS} workers push {N_TASKS} tickets from open to approved")
    bucket = {}
    api = GraphAPI("tinydb", "lt-life", claim_ttl_seconds=600)
    api.reset_run()
    agents(api, WORKERS)
    test = some_tests(api)
    for i in range(N_TASKS):
        api.create_task({"ticket_id": f"L-{i:04d}", "title": f"ticket {i}", "description": "d", "failing_tests": test})
    timed = Timed(api, bucket)
    done = itertools.count()
    lost = itertools.count()
    total_done = [0]

    def worker(i):
        me = f"lt-w{i}"
        rnd = random.Random(i)
        while total_done[0] < N_TASKS:
            tasks = timed.list_open_tasks()["tasks"]
            if not tasks:
                time.sleep(0.01)
                continue
            tid = rnd.choice(tasks[:25])["task_id"]
            if not timed.claim_task(tid, me)["ok"]:
                next(lost)
                continue
            sid = timed.start_session(tid, me)["session_id"]
            for k in range(3):
                timed.log_message(sid, "agent", f"step {k} on {tid}")
            timed.write_handoff(tid, me, {"done": "a", "remaining": "b", "tried": "c", "files_touched": ["tinydb/table.py"]})
            timed.submit_result(tid, me, "fixed", True, ["tinydb/table.py"], "p")
            timed.review_decision(tid, "lt-reviewer", True, [])
            next(done)
            total_done[0] += 1
    dt, errs = run_threads(WORKERS, worker)
    n_done = total_done[0]
    ops = sum(len(v) for v in bucket.values())
    print(f"   {n_done} tickets finished in {dt:.1f} s = {n_done / dt:.1f} tickets/s, {ops / dt:.0f} database calls/s")
    print(f"   claim attempts lost to another worker: {next(lost)}   errors: {len(errs)}")
    check(not errs, f"lifecycle errors: {errs[:2]}")
    check(n_done == N_TASKS, "not every ticket finished")
    approved = q(api.g, "MATCH (t:Task {run_id:'lt-life', status:'approved'}) RETURN count(t)")[0][0]
    grants = q(api.g, "MATCH (d:Decision {run_id:'lt-life', kind:'claim'}) RETURN count(d)")[0][0]
    check(approved == N_TASKS, f"approved {approved} of {N_TASKS}")
    check(grants == N_TASKS, f"{grants} claim decisions for {N_TASKS} tickets (each ticket should be claimed once)")
    m = api.get_run_metrics()
    check(m["double_claims"] == 0, "double claims in lifecycle phase")
    msgs = q(api.g, "MATCH (m:Message) RETURN count(m)")[0][0]
    check(msgs >= N_TASKS * 3, f"messages stored {msgs}, expected at least {N_TASKS * 3}")
    print(f"   rules: every ticket claimed exactly once ({grants}), double claims {m['double_claims']}, messages stored {msgs}")
    results["lifecycle"] = {"workers": WORKERS, "tickets": N_TASKS, "seconds": round(dt, 1),
                            "tickets_per_second": round(n_done / dt, 1), "calls_per_second": round(ops / dt),
                            "errors": len(errs), "latency": summarize(bucket)}
    api.reset_run()


def phase_mixed():
    print(f"\n3. Mixed reads and writes: {WORKERS} threads for {MIXED_SECONDS} s (80% reads, 20% writes)")
    bucket = {}
    api = GraphAPI("tinydb", "lt-mixed", claim_ttl_seconds=600)
    api.reset_run()
    agents(api, WORKERS)
    tickets = json.load(open("tickets.json"))
    for t in tickets:
        api.create_task(t)
        api.get_footprint(t["ticket_id"])
    for t in tickets:
        api.find_conflicts(t["ticket_id"])
    mems = []
    for i, t in enumerate(tickets):
        for kind in ("episodic", "semantic", "procedural"):
            mems.append(api.record_outcome(t["ticket_id"], f"{kind} lesson {i}", kind)["memory_id"])
    sessions = [api.start_session(t["ticket_id"], f"lt-w{i}")["session_id"] for i, t in enumerate(tickets)]
    for s in sessions:
        for k in range(20):
            api.log_message(s, "agent", f"seed {k}")
    timed = Timed(api, bucket)
    stop = time.time() + MIXED_SECONDS
    counts = {"reads": 0, "writes": 0}
    seq_before = {s: len(api.get_history(s)["messages"]) for s in sessions}
    written = {s: itertools.count() for s in sessions}

    def worker(i):
        rnd = random.Random(i)
        while time.time() < stop:
            x = rnd.random()
            t = rnd.choice(tickets)["ticket_id"]
            if x < 0.80:
                kind = rnd.choice(("recall", "task", "open", "history"))
                if kind == "recall":
                    timed.recall_similar(t)
                elif kind == "task":
                    timed.get_task(t)
                elif kind == "open":
                    timed.list_open_tasks()
                else:
                    timed.get_history(rnd.choice(sessions), after_seq=15)
                counts["reads"] += 1
            else:
                if rnd.random() < 0.6:
                    s = rnd.choice(sessions)
                    next(written[s])
                    timed.log_message(s, "agent", "load")
                else:
                    timed.rate_memory(rnd.choice(mems), rnd.random() < 0.5)
                counts["writes"] += 1
    dt, errs = run_threads(WORKERS, worker)
    total = counts["reads"] + counts["writes"]
    print(f"   {total} calls in {dt:.1f} s = {total / dt:.0f} calls/s  (reads {counts['reads']}, writes {counts['writes']}), errors {len(errs)}")
    check(not errs, f"mixed errors: {errs[:2]}")
    lost = 0
    for s in sessions:
        h = api.get_history(s)["messages"]
        expect = seq_before[s] + next(written[s])
        if [m["seq"] for m in h] != list(range(1, expect + 1)):
            lost += 1
    check(lost == 0, f"{lost} sessions have missing or repeated message numbers")
    print(f"   message numbering intact in all {len(sessions)} sessions: {lost == 0}")
    results["mixed"] = {"threads": WORKERS, "seconds": round(dt, 1), "calls": total, "calls_per_second": round(total / dt),
                        "reads": counts["reads"], "writes": counts["writes"], "errors": len(errs), "latency": summarize(bucket)}
    api.reset_run()
    api.clear_memory()


def phase_conflicts():
    print("\n4. Conflict rule under load: the 10 real tickets, workers hold claims briefly and keep trying")
    api = GraphAPI("tinydb", "lt-conf", claim_ttl_seconds=600)
    api.reset_run()
    agents(api, WORKERS)
    tickets = json.load(open("tickets.json"))
    for t in tickets:
        api.create_task(t)
        api.get_footprint(t["ticket_id"])
    for t in tickets:
        api.find_conflicts(t["ticket_id"])
    ids = [t["ticket_id"] for t in tickets]
    edges = {frozenset((a, b)) for a, b in q(api.g, "MATCH (a:Task {run_id:'lt-conf'})-[:CONFLICTS_WITH]->(b:Task) RETURN a.id, b.id")}
    best = max(len(s) for r in range(len(ids) + 1) for s in itertools.combinations(ids, r)
               if not any(frozenset(p) in edges for p in itertools.combinations(s, 2)))
    print(f"   {len(edges)} conflicting pairs, so at most {best} of the 10 tickets can be worked on at the same time")
    stop_monitor = threading.Event()
    seen = {"max_claimed": 0, "violations": 0, "samples": 0}

    def monitor():
        mon = GraphAPI("tinydb", "lt-conf", ensure_schema=False)
        while not stop_monitor.is_set():
            v = q(mon.g, """MATCH (a:Task {run_id:'lt-conf', status:'claimed'})-[:CONFLICTS_WITH]-(b:Task {run_id:'lt-conf', status:'claimed'})
                            RETURN count(a)""")[0][0]
            c = q(mon.g, "MATCH (t:Task {run_id:'lt-conf', status:'claimed'}) RETURN count(t)")[0][0]
            seen["violations"] += v
            seen["max_claimed"] = max(seen["max_claimed"], c)
            seen["samples"] += 1
    mt = threading.Thread(target=monitor)
    mt.start()
    refused = itertools.count()

    def worker(i):
        me, rnd = f"lt-w{i}", random.Random(i)
        while q(api.g, "MATCH (t:Task {run_id:'lt-conf'}) WHERE t.status <> 'approved' RETURN count(t)")[0][0] > 0:
            tid = rnd.choice(ids)
            r = api.claim_task(tid, me)
            if not r["ok"]:
                next(refused)
                continue
            time.sleep(0.05)                                       # "working"
            api.submit_result(tid, me, "done", True, ["tinydb/table.py"], "p")
            api.review_decision(tid, "lt-reviewer", True, [])
    dt, errs = run_threads(WORKERS, worker)
    stop_monitor.set()
    mt.join()
    print(f"   all done in {dt:.1f} s. Highest number of tickets held at once: {seen['max_claimed']} (limit {best})")
    print(f"   {seen['samples']} checks while running, times two conflicting tickets were held together: {seen['violations']}")
    print(f"   claim attempts refused (already taken or conflicting): {next(refused)}   errors: {len(errs)}")
    check(not errs, f"conflict phase errors: {errs[:2]}")
    check(seen["violations"] == 0, "two conflicting tickets were held at the same time")
    check(seen["max_claimed"] <= best, "more tickets held at once than the conflict rules allow")
    m = api.get_run_metrics()
    check(m["double_claims"] == 0, "double claims in conflict phase")
    results["conflicts"] = {"pairs": len(edges), "max_allowed_at_once": best, "max_held_at_once": seen["max_claimed"],
                            "checks": seen["samples"], "violations": seen["violations"], "double_claims": m["double_claims"],
                            "claims_refused": m["claims_refused"], "seconds": round(dt, 1)}
    api.reset_run()


def phase_tenants():
    print(f"\n5. Many tenants at once: {TENANTS} repos, each with its own graph, {WORKERS // TENANTS} workers each")
    from contract_tests import build_code  # noqa: PLC0415  (tiny code layer for the other tenants)

    def run_tenants(count):
        apis = []
        for t in range(count):
            a = GraphAPI(f"lt_tenant_{t}", "lt-ten", claim_ttl_seconds=600)
            build_code(a)
            a.reset_run()
            for i in range(WORKERS // TENANTS):
                a.register_agent(f"w{i}", "worker")
            a.register_agent("rev", "reviewer")
            for i in range(TENANT_TASKS):
                a.create_task({"ticket_id": f"X-{i}", "title": "x", "description": "d", "failing_tests": ["t::add_str"]})
            apis.append(a)
        left = [TENANT_TASKS] * count

        def worker(k):
            t, i = divmod(k, WORKERS // TENANTS)
            a, rnd = apis[t], random.Random(k)
            while left[t] > 0:
                tasks = a.list_open_tasks()["tasks"]
                if not tasks:
                    time.sleep(0.005)
                    continue
                tid = rnd.choice(tasks[:15])["task_id"]
                if a.claim_task(tid, f"w{i}")["ok"]:
                    a.submit_result(tid, f"w{i}", "s", True, [], "p")
                    a.review_decision(tid, "rev", True, [])
                    left[t] -= 1
        dt, errs = run_threads(count * (WORKERS // TENANTS), worker)
        done = sum(q(a.g, "MATCH (t:Task {run_id:'lt-ten', status:'approved'}) RETURN count(t)")[0][0] for a in apis)
        # isolation: a tenant never sees another tenant's tickets
        seen = [q(a.g, "MATCH (t:Task) RETURN count(t)")[0][0] for a in apis]
        for a in apis:
            a.g.delete()
        return dt, errs, done, seen
    dt1, e1, d1, s1 = run_tenants(1)
    dtn, en, dn, sn = run_tenants(TENANTS)
    check(not e1 and not en, f"tenant errors {(e1 + en)[:2]}")
    check(d1 == TENANT_TASKS and dn == TENANT_TASKS * TENANTS, "not every tenant ticket finished")
    check(all(x == TENANT_TASKS for x in sn), f"a tenant saw the wrong number of tickets: {sn}")
    print(f"   1 tenant : {d1} tickets in {dt1:.1f} s = {d1 / dt1:.1f} tickets/s")
    print(f"   {TENANTS} tenants: {dn} tickets in {dtn:.1f} s = {dn / dtn:.1f} tickets/s in total ({dn / dtn / TENANTS:.1f} per tenant)")
    print(f"   every tenant saw exactly its own {TENANT_TASKS} tickets: {all(x == TENANT_TASKS for x in sn)}")
    results["tenants"] = {"tickets_each": TENANT_TASKS, "one_tenant_tickets_per_second": round(d1 / dt1, 1),
                          "tenants": TENANTS, "all_tenants_tickets_per_second": round(dn / dtn, 1),
                          "isolation_ok": all(x == TENANT_TASKS for x in sn)}


def write_report():
    results["environment"] = {"python": platform.python_version(), "machine": platform.platform(),
                              "when": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                              "quick_run": QUICK, "problems": problems}
    json.dump(results, open("load_test_results.json", "w"), indent=1)
    L, M, C, T, R = (results.get(k) for k in ("lifecycle", "mixed", "conflicts", "tenants", "claim_race"))
    md = ["# Load test results", "",
          f"Run on {results['environment']['when']}, {results['environment']['machine']}, Python {results['environment']['python']}.",
          "Numbers include the Python client, so they are end to end. Problems found: " + (str(len(problems)) if problems else "none") + ".", ""]
    if R:
        md += ["## Safe claiming", f"- {R['workers']} workers claiming one ticket at the same instant, {R['rounds']} rounds: "
               f"exactly one winner in {R['rounds_exactly_one_winner']} of {R['rounds']}.",
               f"- Claim latency at that load: p50 {R['claim_latency']['p50_ms']} ms, p95 {R['claim_latency']['p95_ms']} ms, "
               f"p99 {R['claim_latency']['p99_ms']} ms.", ""]
    if L:
        md += ["## Full lifecycle", f"- {L['workers']} workers took {L['tickets']} tickets from open to approved in {L['seconds']} s "
               f"({L['tickets_per_second']} tickets/s, {L['calls_per_second']} database calls/s), errors {L['errors']}.", ""]
    if M:
        md += ["## Mixed reads and writes", f"- {M['threads']} threads, {M['calls']} calls in {M['seconds']} s "
               f"({M['calls_per_second']} calls/s, {M['reads']} reads, {M['writes']} writes), errors {M['errors']}.", ""]
    if C:
        md += ["## Conflict rule under load", f"- {C['pairs']} conflicting pairs among the 10 real tickets, so at most {C['max_allowed_at_once']} can run at once. "
               f"Most held at once: {C['max_held_at_once']}. Times two conflicting tickets were held together: {C['violations']} "
               f"in {C['checks']} checks.", ""]
    if T:
        md += ["## Many tenants", f"- 1 tenant: {T['one_tenant_tickets_per_second']} tickets/s. {T['tenants']} tenants at once: "
               f"{T['all_tenants_tickets_per_second']} tickets/s in total. Every tenant saw only its own tickets: {T['isolation_ok']}.", ""]
    md += ["## Per-call latency (lifecycle run)", "", "| Call | Count | p50 ms | p95 ms | p99 ms |", "|---|---|---|---|---|"]
    for k, v in (L or {}).get("latency", {}).items():
        md.append(f"| `{k}` | {v['calls']} | {v['p50_ms']} | {v['p95_ms']} | {v['p99_ms']} |")
    open("load_test_results.md", "w").write("\n".join(md) + "\n")


def main():
    if not q(schema.connect().select_graph(schema.graph_name("tinydb")), "MATCH (t:Test) RETURN count(t)")[0][0]:
        raise SystemExit("NOT OK: the real TinyDB graph is empty. Run: python load_code.py --repo ../tinydb")
    t0 = time.time()
    for phase in (phase_race, phase_lifecycle, phase_mixed, phase_conflicts, phase_tenants):
        phase()
    write_report()
    print(f"\nFinished in {time.time() - t0:.0f} s. Results written to load_test_results.md and .json")
    if problems:
        print(f"{len(problems)} RULE(S) BROKEN:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("ALL RULES HELD UNDER LOAD")


# contract_tests runs its own main() when imported, so load only its code-builder
import types  # noqa: E402
_src = open("contract_tests.py").read().rsplit("\nmain()", 1)[0]
_mod = types.ModuleType("contract_tests")
exec(compile(_src, "contract_tests.py", "exec"), _mod.__dict__)
sys.modules["contract_tests"] = _mod

main()
