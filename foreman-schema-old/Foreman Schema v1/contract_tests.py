"""Contract tests for GraphAPI v1.2. Plain asserts, no extra packages.

    python contract_tests.py            builds its own tiny code graph and runs every test
    python contract_tests.py --real     also runs the checks on the real TinyDB graph (needs load_code.py first)

Each test prints PASS or FAIL. A later stub (in memory) must pass this same file.
"""
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone

import schema
from graph_api import GraphAPI
from schema import q

REPO = "contract_test"
T0 = datetime(2026, 10, 16, 9, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def build_code(api):
    """A tiny code layer (functions, tests) so the contract can be tested without the real repo."""
    g = api.g
    try:
        g.delete()
    except Exception:  # noqa: BLE001
        pass
    schema.create_schema(g)
    q(g, """
    CREATE (tb:File {path:'tinydb/table.py'}), (op:File {path:'tinydb/operations.py'}), (db:File {path:'tinydb/database.py'}),
     (upd:Function {id:'tinydb.table.Table.update', name:'update', file:'tinydb/table.py'}),
     (trn:Function {id:'tinydb.table.Table.truncate', name:'truncate', file:'tinydb/table.py'}),
     (nid:Function {id:'tinydb.table.Table._get_next_id', name:'_get_next_id', file:'tinydb/table.py'}),
     (add:Function {id:'tinydb.operations.add', name:'add', file:'tinydb/operations.py'}),
     (drp:Function {id:'tinydb.database.TinyDB.drop_tables', name:'drop_tables', file:'tinydb/database.py'}),
     (upd)-[:DEFINED_IN]->(tb), (trn)-[:DEFINED_IN]->(tb), (nid)-[:DEFINED_IN]->(tb), (add)-[:DEFINED_IN]->(op),
     (drp)-[:DEFINED_IN]->(db),
     (t1:Test {id:'t::update_missing', status:'fail'}), (t6:Test {id:'t::add_str', status:'fail'}),
     (t3:Test {id:'t::ids_after_drop', status:'fail'}), (t7:Test {id:'t::next_id', status:'fail'}),
     (t1)-[:COVERS]->(upd), (t1)-[:COVERS]->(add), (t6)-[:COVERS]->(add), (t3)-[:COVERS]->(drp), (t3)-[:COVERS]->(nid),
     (t7)-[:COVERS]->(trn), (t7)-[:COVERS]->(nid)""")
    for i in range(6):
        q(g, "MATCH (n:Function {id:'tinydb.table.Table._get_next_id'}) CREATE (p:Test {id:$id, status:'pass'}), (p)-[:COVERS]->(n)",
          {"id": f"t::busy_{i}"})
    for i in range(3):
        q(g, "MATCH (n:Function {id:'tinydb.table.Table.update'}) CREATE (p:Test {id:$id, status:'pass'}), (p)-[:COVERS]->(n)",
          {"id": f"t::upd_{i}"})
    q(g, schema.SET_TEST_COUNTS)


TICKETS = [
    {"ticket_id": "T-01", "title": "update() with unknown ids raises", "description": "d", "failing_tests": ["t::update_missing"]},
    {"ticket_id": "T-06", "title": "add() prepends strings", "description": "d", "failing_tests": ["t::add_str"]},
    {"ticket_id": "T-03", "title": "ids not restarted after drop_tables", "description": "d", "failing_tests": ["t::ids_after_drop"]},
    {"ticket_id": "T-07", "title": "ids not restarted after truncate", "description": "d", "failing_tests": ["t::next_id"]},
]


def fresh():
    """A clean run with agents and the four tickets, footprints and conflicts in place."""
    clock = Clock()
    api = GraphAPI(REPO, "run-1", clock=clock, claim_ttl_seconds=60)
    api.reset_run()
    api.clear_memory()
    for i in range(1, 51):
        assert api.register_agent(f"agent-w{i}", "worker")["ok"]
    assert api.register_agent("agent-f", "foreman")["ok"]
    assert api.register_agent("agent-r", "reviewer")["ok"]
    assert api.register_agent("agent-h", "historian")["ok"]
    for t in TICKETS:
        assert api.create_task(t)["ok"]
    for t in TICKETS:
        assert api.get_footprint(t["ticket_id"])["ok"]
        assert api.find_conflicts(t["ticket_id"])["ok"]
    return api, clock


# ------------------------------------------------------------------------------------------------
def test_register_and_reads():
    api, _ = fresh()
    assert api.register_agent("x", "wizard") == {"ok": False, "error": "bad_role", "allowed": ["foreman", "historian", "reviewer", "worker"]}
    assert api.register_agent("agent-w1", "worker")["ok"]                      # twice is fine
    t = api.get_task("T-01")
    assert t["ok"] and t["status"] == "open" and t["attempt_count"] == 0 and t["claimed_by"] is None
    assert api.get_task("nope") == {"ok": False, "error": "no_such_task"}
    assert api.get_result("T-01")["error"] == "no_result"


def test_create_task():
    api, _ = fresh()
    assert api.create_task({"ticket_id": "T-99", "title": "x", "description": "d", "failing_tests": ["t::missing"]}) == \
        {"ok": False, "error": "unknown_tests", "missing": ["t::missing"]}
    assert api.create_task({"ticket_id": "T-99"})["error"] == "bad_ticket"
    assert api.create_task(TICKETS[0])["ok"]                                    # same ticket twice is fine
    assert len(q(api.g, "MATCH (t:Task {run_id:'run-1', id:'T-01'}) RETURN t")) == 1


def test_footprint():
    api, _ = fresh()
    fp = api.get_footprint("T-01")
    assert fp["ok"] and fp["tests"] == ["t::update_missing"]
    ids = [f["id"] for f in fp["functions"]]
    assert "tinydb.operations.add" in ids and "tinydb.table.Table.update" in ids
    assert all(f["weight"] > 0 for f in fp["functions"])
    assert fp["functions"] == sorted(fp["functions"], key=lambda f: -f["weight"])
    assert "tinydb/table.py" in fp["files"]
    assert api.get_footprint("T-01")["decision_id"] == fp["decision_id"]        # one decision, not one per call
    assert api.get_footprint("nope")["error"] == "no_such_task"


def test_conflicts_and_order():
    api, _ = fresh()
    c = api.find_conflicts("T-01")
    assert [x["other_task_id"] for x in c["conflicts"]] == ["T-06"]
    assert c["conflicts"][0]["score"] >= 0.10 and c["conflicts"][0]["shared_nodes"][0]["type"] == "Function"
    assert api.find_conflicts("T-03")["conflicts"] == []                        # shares only busy code
    allc = api.find_conflicts("T-03", include_all=True)["conflicts"]
    assert allc and all(x["score"] < 0.10 for x in allc)
    r = api.set_task_order(["T-07", "T-01", "T-03"])
    assert r["ok"] and r["decision_id"].startswith("dec-")
    assert [t["task_id"] for t in api.list_open_tasks()["tasks"]] == ["T-07", "T-01", "T-03", "T-06"]
    assert api.set_task_order(["T-01", "ghost"])["error"] == "no_such_task"


def test_claim_reasons():
    api, _ = fresh()
    r = api.claim_task("T-01", "agent-w1")
    assert r["ok"] and r["reason"] == "claimed" and r["decision_id"].startswith("dec-") and r["claim_expires_at"]
    assert api.claim_task("T-01", "agent-w1")["reason"] == "already_yours"
    assert api.claim_task("T-01", "agent-w2")["reason"] == "already_claimed"
    r6 = api.claim_task("T-06", "agent-w2")                                     # conflicts with claimed T-01
    assert not r6["ok"] and r6["reason"] == "conflict_active" and r6["blocked_by"] == ["T-01"]
    assert api.list_open_tasks()["tasks"][0]["task_id"] in ("T-03", "T-06", "T-07")
    open_t6 = [t for t in api.list_open_tasks()["tasks"] if t["task_id"] == "T-06"][0]
    assert open_t6["conflicts_with_active"] == ["T-01"]
    assert api.claim_task("T-03", "agent-w3")["ok"]                              # unrelated work runs in parallel
    assert api.claim_task("T-07", "agent-w4")["ok"]                              # T-03 and T-07 share only busy code
    assert api.claim_task("T-01", "ghost")["reason"] == "unknown_agent"
    assert api.claim_task("nope", "agent-w1")["reason"] == "no_such_task"
    t = api.get_task("T-01")
    assert t["status"] == "claimed" and t["claimed_by"] == "agent-w1"


def test_concurrent_claim():
    api, _ = fresh()
    api.reset_run()
    clock = Clock()
    api = GraphAPI(REPO, "run-1", clock=clock)
    for t in TICKETS:
        api.create_task(t)
        api.get_footprint(t["ticket_id"])
        api.find_conflicts(t["ticket_id"])
    barrier = threading.Barrier(50)
    out, lock = [], threading.Lock()

    def go(i):
        barrier.wait()
        r = api.claim_task("T-01" if i % 2 else "T-06", f"agent-w{i}")
        with lock:
            out.append(r)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(1, 51)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    wins = [r for r in out if r["ok"]]
    assert len(out) == 50 and len(wins) == 1, (len(out), len(wins))
    assert {r["reason"] for r in out if not r["ok"]} <= {"already_claimed", "conflict_active"}
    assert q(api.g, "MATCH (d:Decision {run_id:'run-1', kind:'claim'}) RETURN count(d)")[0][0] == 1
    assert q(api.g, "MATCH (t:Task {run_id:'run-1', status:'claimed'}) RETURN count(t)")[0][0] == 1
    m = api.get_run_metrics()
    assert m["claims_granted"] == 1 and m["claims_attempted"] == 50 and m["double_claims"] == 0, m


def test_heartbeat_release_expire_handoff():
    api, clock = fresh()
    assert api.claim_task("T-01", "agent-w1")["ok"]
    clock.advance(40)
    hb = api.heartbeat("T-01", "agent-w1")
    assert hb["ok"] and api.heartbeat("T-01", "agent-w2")["error"] == "not_holder"
    clock.advance(30)                                                            # 70 s since claim, 30 s since heartbeat
    assert api.expire_stale_claims() == {"ok": True, "reopened": []}
    assert api.write_handoff("T-01", "agent-w1", {"done": "found it", "remaining": "run tests", "tried": "skip ids",
                                                  "files_touched": ["tinydb/table.py"]})["ok"]
    assert api.write_handoff("T-01", "agent-w1", {"done": "x"})["error"] == "bad_note"
    clock.advance(120)                                                           # worker went silent
    e = api.expire_stale_claims()
    assert e == {"ok": True, "reopened": ["T-01"]}
    t = api.get_task("T-01")
    assert t["status"] == "open" and t["claimed_by"] is None
    note = api.get_handoff("T-01")["note"]
    assert note["done"] == "found it" and note["written_by"] == "agent-w1" and note["files_touched"] == ["tinydb/table.py"]
    assert api.get_handoff("T-06")["note"] is None
    r = api.claim_task("T-01", "agent-w2")                                       # next worker takes over
    assert r["ok"]
    assert api.release_claim("T-01", "agent-w9")["error"] == "not_holder"
    assert api.release_claim("T-01", "agent-w2")["ok"] and api.get_task("T-01")["status"] == "open"
    m = api.get_run_metrics()
    assert m["handoffs_recovered"] == 1 and m["double_claims"] == 0, m


def test_submit_review_cycle():
    api, clock = fresh()
    assert api.submit_result("T-01", "agent-w1", "s", True, [], "p")["error"] == "not_holder"
    assert api.claim_task("T-01", "agent-w1")["ok"]
    assert api.submit_result("T-01", "agent-w2", "s", True, [], "p")["error"] == "not_holder"
    clock.advance(5)
    r = api.submit_result("T-01", "agent-w1", "skip unknown ids", {"passed": False, "failed": ["t::x"]},
                          ["tinydb/table.py", "tinydb/utils.py"], "patches/p1.diff")
    assert r["ok"] and api.get_task("T-01")["status"] == "submitted"
    res = api.get_result("T-01")
    assert res["ok"] and res["tests_passed"] is False and res["test_report"]["failed"] == ["t::x"]
    assert res["out_of_footprint"] == ["tinydb/utils.py"]
    assert api.claim_task("T-01", "agent-w3")["reason"] == "not_open"
    assert api.review_decision("T-03", "agent-r", True)["error"] == "not_submitted_or_unknown_reviewer"
    rej = api.review_decision("T-01", "agent-r", False, [{"code": "TESTS_FAIL", "detail": "1 failing"},
                                                         {"code": "OUT_OF_FOOTPRINT", "detail": "utils.py"}])
    assert rej["ok"]
    t = api.get_task("T-01")
    assert t["status"] == "open" and t["attempt_count"] == 1 and t["claimed_by"] is None
    ex = api.explain(rej["decision_id"])
    assert ex["ok"] and "TESTS_FAIL" in ex["summary"] and any(n["type"] == "Result" for n in ex["path"]["nodes"])
    assert api.claim_task("T-01", "agent-w1")["ok"]
    assert api.submit_result("T-01", "agent-w1", "fixed", True, ["tinydb/table.py"], "patches/p2.diff")["ok"]
    assert api.get_result("T-01")["summary"] == "fixed"                           # latest result wins
    assert api.review_decision("T-01", "agent-r", True, [])["ok"]
    t = api.get_task("T-01")
    assert t["status"] == "approved" and t["attempt_count"] == 1 and t["claimed_by"] == "agent-w1"
    assert api.get_run_metrics()["reworks"] == 1


def test_memory():
    api, _ = fresh()
    assert api.record_outcome("T-01", "x", "gossip")["error"] == "bad_kind"
    assert api.record_outcome("nope", "x", "episodic")["error"] == "no_such_task"
    e = api.record_outcome("T-01", "first attempt broke upsert", "episodic")
    s = api.record_outcome("T-01", "update must skip unknown ids", "semantic")
    p = api.record_outcome("T-01", "filter ids first, then update", "procedural",
                           {"functions": ["tinydb.table.Table.update"], "evidence_path": "patches/p2.diff"})
    assert e["ok"] and s["ok"] and p["ok"]
    rec = api.recall_similar("T-06")["memories"]                                  # T-06 shares add() with T-01
    assert {m["kind"] for m in rec} == {"episodic", "semantic"}, rec              # procedural one is only about update()
    rec = api.recall_similar("T-01")["memories"]
    assert {m["kind"] for m in rec} == {"episodic", "semantic", "procedural"}
    assert next(m for m in rec if m["kind"] == "procedural")["evidence_path"] == "patches/p2.diff"
    assert all("tinydb/table.py" in m["fix_files"] or "tinydb/operations.py" in m["fix_files"] for m in rec)
    assert api.rate_memory(p["memory_id"], True) == {"ok": True, "uses": 1, "successes": 1}
    assert api.rate_memory(p["memory_id"], True)["successes"] == 2
    assert api.rate_memory(p["memory_id"], True)["successes"] == 3
    assert api.rate_memory(s["memory_id"], False) == {"ok": True, "uses": 1, "successes": 0}
    assert api.rate_memory(s["memory_id"], False)["uses"] == 2
    top = [m["memory_id"] for m in api.recall_similar("T-01")["memories"]]
    assert top.index(p["memory_id"]) < top.index(s["memory_id"])                  # winner ranks above loser
    assert api.retire_fact(s["memory_id"], "replaced", superseded_by=p["memory_id"])["ok"]
    assert s["memory_id"] not in [m["memory_id"] for m in api.recall_similar("T-01")["memories"]]
    assert q(api.g, "MATCH (:Memory {id:$a})-[:SUPERSEDED_BY]->(:Memory {id:$b}) RETURN 1",
             {"a": s["memory_id"], "b": p["memory_id"]})
    assert api.retire_fact("mem-nope", "x")["error"] == "no_such_memory"
    assert api.rate_memory("mem-nope", True)["error"] == "no_such_memory"
    assert api.clear_memory()["ok"] and api.recall_similar("T-01")["memories"] == []


def test_history():
    api, _ = fresh()
    s = api.start_session("T-01", "agent-w1")
    assert s["ok"] and not s["resumed"]
    again = api.start_session("T-01", "agent-w1")                                 # restarted worker
    assert again["session_id"] == s["session_id"] and again["resumed"]
    assert api.start_session("T-01", "ghost")["error"] == "no_such_task_or_agent"
    sid = s["session_id"]
    assert api.log_message(sid, "wizard", "x")["error"] == "bad_role"
    assert api.log_message("sess-nope", "agent", "x")["error"] == "no_such_session"
    barrier = threading.Barrier(30)

    def say(i):
        barrier.wait()
        assert api.log_message(sid, "agent", f"note {i}")["ok"]
    ts = [threading.Thread(target=say, args=(i,)) for i in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    h = api.get_history(sid)["messages"]
    assert [m["seq"] for m in h] == list(range(1, 31)) and len({m["content"] for m in h}) == 30
    assert [m["seq"] for m in api.get_history(sid, after_seq=27)["messages"]] == [28, 29, 30]
    assert api.get_history("sess-nope")["error"] == "no_such_session"


def test_explain_snapshot_metrics():
    api, clock = fresh()
    mem = api.record_outcome("T-01", "update must skip unknown ids", "semantic")
    api.recall_similar("T-01")
    c = api.claim_task("T-01", "agent-w1")
    ex = api.explain(c["decision_id"])
    assert ex["ok"] and "agent-w1 claimed T-01" in ex["summary"] and "T-06" in ex["summary"]
    types = {n["type"] for n in ex["path"]["nodes"]}
    assert {"Decision", "Task", "Agent", "Function", "Memory"} <= types, types
    assert {e["type"] for e in ex["path"]["edges"]} >= {"ABOUT", "MADE_BY", "TOUCHES", "CONFLICTS_WITH", "RECALLED"}
    assert api.explain("dec-nope")["error"] == "no_such_decision"
    fp = api.explain(api.get_footprint("T-01")["decision_id"])
    assert fp["ok"] and fp["decision"]["kind"] == "footprint"
    snap = api.get_graph_snapshot()
    kinds = {n["type"] for n in snap["nodes"]}
    assert {"Agent", "Task", "Decision", "Memory", "Function"} <= kinds
    assert any(e["type"] == "CONFLICTS_WITH" for e in snap["edges"]) and any(e["type"] == "CLAIMED" for e in snap["edges"])
    n_events = len(snap["events"])
    assert n_events >= 5
    last = snap["events"][-1]["ts"]
    clock.advance(10)
    api.release_claim("T-01", "agent-w1")
    newer = api.get_graph_snapshot(since=last)["events"]
    assert len(newer) == 1 and newer[0]["type"] == "release"
    # collisions: two tickets worked on at once whose results touch the same file
    assert api.claim_task("T-03", "agent-w3")["ok"] and api.claim_task("T-07", "agent-w4")["ok"]
    assert api.submit_result("T-03", "agent-w3", "a", True, ["tinydb/table.py"], "p")["ok"]
    assert api.submit_result("T-07", "agent-w4", "b", True, ["tinydb/table.py"], "p")["ok"]
    assert api.get_run_metrics()["collisions"] == 1


def test_tenants_and_runs():
    api, _ = fresh()
    api.record_outcome("T-01", "private to tenant A", "semantic")
    other = GraphAPI("contract_test_b", "run-1")
    try:
        other.g.delete()
    except Exception:  # noqa: BLE001
        pass
    other = GraphAPI("contract_test_b", "run-1")
    assert q(other.g, "MATCH (m:Memory) RETURN count(m)")[0][0] == 0
    assert other.get_task("T-01")["error"] == "no_such_task"
    assert other.register_agent("agent-w1", "worker")["ok"]                      # same id in another tenant is fine
    other.g.delete()
    # two runs in one tenant do not see each other, and may reuse ticket ids
    run2 = GraphAPI(REPO, "run-2")
    run2.reset_run()
    assert run2.list_open_tasks()["tasks"] == []
    assert run2.create_task(TICKETS[0])["ok"]
    assert api.claim_task("T-01", "agent-w1")["ok"]
    assert run2.claim_task("T-01", "agent-w1")["ok"]                              # run 1's claim does not block run 2
    run2.reset_run()
    assert api.get_task("T-01")["status"] == "claimed"


def test_duplicates_refused():
    api, _ = fresh()
    for bad in ("CREATE (:Agent {id:'agent-w1'})", "CREATE (:Task {run_id:'run-1', id:'T-01'})"):
        try:
            q(api.g, bad)
        except Exception as exc:  # noqa: BLE001
            assert "unique" in str(exc).lower(), exc
        else:
            raise AssertionError("duplicate accepted: " + bad)


def test_real_tinydb():
    """On the real graph made by load_code.py: 10 tickets, footprints, conflicts."""
    import json
    api = GraphAPI("tinydb", "contract-real")
    api.reset_run()
    tickets = json.load(open("tickets.json"))
    for t in tickets:
        r = api.create_task(t)
        assert r["ok"], r
    for t in tickets:
        fp = api.get_footprint(t["ticket_id"])
        assert fp["ok"] and fp["functions"] and fp["files"]
    pairs = set()
    for t in tickets:
        for c in api.find_conflicts(t["ticket_id"])["conflicts"]:
            pairs.add(frozenset((t["ticket_id"], c["other_task_id"])))
    assert len(pairs) == 6, len(pairs)
    assert frozenset(("T-01", "T-06")) in pairs and frozenset(("T-03", "T-07")) in pairs
    api.reset_run()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and k != "test_real_tinydb"]


def main():
    api = GraphAPI(REPO, "setup")
    build_code(api)
    tests = TESTS + ([test_real_tinydb] if "--real" in sys.argv else [])
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed} of {len(tests)} passed")
    sys.exit(1 if failed else 0)


main()
