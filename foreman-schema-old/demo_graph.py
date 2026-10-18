"""Builds a small EXAMPLE graph (made-up numbers, TinyDB-style names) and checks that every
Track 2 focus area works on it. Leaves the graph in place so you can look at it in the browser.

    python demo_graph.py            run everything, keep the graph
    python demo_graph.py --clean    run everything, then delete the demo graphs
"""
import sys
import threading
from datetime import datetime, timedelta, timezone

import schema
from schema import q

CLEAN = "--clean" in sys.argv
RUN = "demo-run"
NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def step(title):
    print(f"\n== {title}")


def fresh(db, repo):
    g = db.select_graph(schema.graph_name(repo))
    try:
        g.delete()
    except Exception:  # noqa: BLE001
        pass
    g = db.select_graph(schema.graph_name(repo))
    schema.create_schema(g)
    return g


db = schema.connect()

# ---------------------------------------------------------------------------------------------
step("1. Set up tenant A (one graph per repo) with indexes and uniqueness rules")
g = fresh(db, "demo_repo_a")
print("   uniqueness rules in place:", len(g.list_constraints()))
schema.create_schema(g)  # second run must not fail
print("   running setup twice is safe: yes")

# ---------------------------------------------------------------------------------------------
step("2. Code layer: files, functions, tests, and how they link (EXAMPLE data)")
q(g, """
CREATE (tb:File {path:'tinydb/table.py'}), (op:File {path:'tinydb/operations.py'}), (db:File {path:'tinydb/database.py'}),
 (upd:Function {id:'tinydb.table.Table.update', name:'update'}),
 (trn:Function {id:'tinydb.table.Table.truncate', name:'truncate'}),
 (nid:Function {id:'tinydb.table.Table._get_next_id', name:'_get_next_id'}),
 (ins:Function {id:'tinydb.table.Table.insert', name:'insert'}),
 (add:Function {id:'tinydb.operations.add', name:'add'}),
 (drp:Function {id:'tinydb.database.TinyDB.drop_tables', name:'drop_tables'}),
 (upd)-[:DEFINED_IN]->(tb), (trn)-[:DEFINED_IN]->(tb), (nid)-[:DEFINED_IN]->(tb), (ins)-[:DEFINED_IN]->(tb),
 (add)-[:DEFINED_IN]->(op), (drp)-[:DEFINED_IN]->(db),
 (upd)-[:CALLS]->(add), (ins)-[:CALLS]->(nid),
 (t1:Test {id:'tests/test_tinydb.py::test_update_ids_missing[json]', status:'fail'}),
 (t6:Test {id:'tests/test_operations.py::test_add_str[json]', status:'fail'}),
 (t3:Test {id:'tests/test_tinydb.py::test_insert_ids[json]', status:'fail'}),
 (t7:Test {id:'tests/test_tables.py::test_next_id[json]', status:'fail'}),
 (t1)-[:COVERS]->(upd), (t1)-[:COVERS]->(add), (t6)-[:COVERS]->(add),
 (t3)-[:COVERS]->(drp), (t3)-[:COVERS]->(ins), (t3)-[:COVERS]->(nid),
 (t7)-[:COVERS]->(trn), (t7)-[:COVERS]->(ins), (t7)-[:COVERS]->(nid)
""")
# passing tests: busy functions are run by many of them, specific ones by few
for i in range(6):
    q(g, "MATCH (ins:Function {id:'tinydb.table.Table.insert'}), (nid:Function {id:'tinydb.table.Table._get_next_id'}) "
         "CREATE (p:Test {id:$id, status:'pass'}), (p)-[:COVERS]->(ins), (p)-[:COVERS]->(nid)", {"id": f"tests/test_pass.py::test_busy_{i}"})
for i in range(3):
    q(g, "MATCH (upd:Function {id:'tinydb.table.Table.update'}) CREATE (p:Test {id:$id, status:'pass'}), (p)-[:COVERS]->(upd)",
      {"id": f"tests/test_pass.py::test_update_{i}"})
q(g, schema.SET_TEST_COUNTS)
n = q(g, "MATCH (f:Function) RETURN count(f)")[0][0]
print(f"   functions: {n}")

# ---------------------------------------------------------------------------------------------
step("3. Work layer: agents and tickets")
for i in range(1, 51):
    role = "foreman" if i == 1 else "worker"
    q(g, "CREATE (:Agent {id:$id, role:$role, status:'idle'})", {"id": f"agent-w{i}", "role": role})
tickets = [("T-01", "update() with unknown doc_ids raises KeyError", "tests/test_tinydb.py::test_update_ids_missing[json]"),
           ("T-06", "add() on a string field prepends instead of appending", "tests/test_operations.py::test_add_str[json]"),
           ("T-03", "IDs are not restarted after drop_tables()", "tests/test_tinydb.py::test_insert_ids[json]"),
           ("T-07", "IDs are not restarted after truncate()", "tests/test_tables.py::test_next_id[json]")]
for tid, title, test in tickets:
    q(g, schema.CREATE_TASK, {"run": RUN, "id": tid, "ticket_id": tid, "title": title, "description": title, "tests": [test]})
    schema.write_footprint(g, RUN, tid)
print("   tickets created, footprints written")

# ---------------------------------------------------------------------------------------------
step("4. Conflict detection from shared code (specific code counts more than busy code)")
for tid, *_ in tickets:
    for other, status, score, shared in q(g, schema.OVERLAP, {"run": RUN, "id": tid}):
        if tid < other and score >= schema.CONFLICT_THRESHOLD:
            names = [s_[0].split("tinydb.")[1] for s_ in shared]
            q(g, schema.MARK_CONFLICT, {"run": RUN, "a": tid, "b": other, "score": score, "shared": [s_[0] for s_ in shared]})
            print(f"   {tid} conflicts with {other}  score {score:.3f}  via {names}")
pairs = q(g, "MATCH (a:Task)-[c:CONFLICTS_WITH]->(b:Task) RETURN a.id, b.id ORDER BY a.id")
assert [tuple(p) for p in pairs] == [("T-01", "T-06")], pairs
low = [r for r in q(g, schema.OVERLAP, {"run": RUN, "id": "T-03"}) if r[0] == "T-07"][0]
print(f"   T-03 and T-07 share only busy code (insert, next id): score {low[2]:.3f}, not marked as conflicting")
assert low[2] < schema.CONFLICT_THRESHOLD

# ---------------------------------------------------------------------------------------------
step("5. Shared state: 50 workers grab conflicting tickets at the same instant")
barrier = threading.Barrier(50)
wins, lock = [], threading.Lock()


def try_claim(i):
    conn = schema.connect().select_graph(schema.graph_name("demo_repo_a"))
    target = "T-01" if i % 2 else "T-06"          # half go for each of two conflicting tickets
    barrier.wait()
    rows = q(conn, schema.CLAIM, {"run": RUN, "id": target, "who": f"agent-w{i}",
                                  "now": iso(NOW), "expires": iso(NOW + timedelta(minutes=5))})
    if rows:
        with lock:
            wins.append((i, target, rows[0][0]))


ts = [threading.Thread(target=try_claim, args=(i,)) for i in range(1, 51)]
[t.start() for t in ts]
[t.join() for t in ts]
claimed = q(g, "MATCH (t:Task {status:'claimed'}) RETURN t.id, t.claimed_by")
decisions = q(g, "MATCH (d:Decision {kind:'claim'}) RETURN count(d)")[0][0]
print(f"   winners: {len(wins)}   claimed tickets: {claimed}   claim decisions recorded: {decisions}")
assert len(wins) == 1 and len(claimed) == 1 and decisions == 1
winner, ticket, dec_id = wins[0]
other = "T-06" if ticket == "T-01" else "T-01"
why = q(g, schema.CLAIM_WHY, {"run": RUN, "id": other})[0]
print(f"   the conflicting ticket {other} is now blocked: status={why[0]}, blocked by {why[2]}")

# ---------------------------------------------------------------------------------------------
step("6. Explain the decision (who, which ticket, which code, what it blocked)")
kind, summary, agent, task, title, code, conflicts = q(g, schema.EXPLAIN, {"dec": dec_id})[0]
print(f"   {summary}. Ticket '{title}' touches {code}. Competing tickets: {conflicts}")
assert agent == f"agent-w{winner}" and task == ticket

# ---------------------------------------------------------------------------------------------
step("7. Conversation history: 30 messages written at once, none lost, none duplicated")
q(g, schema.START_SESSION, {"who": f"agent-w{winner}", "task": ticket, "run": RUN, "sid": "sess-1", "now": iso(NOW)})
b2 = threading.Barrier(30)


def say(i):
    conn = schema.connect().select_graph(schema.graph_name("demo_repo_a"))
    b2.wait()
    q(conn, schema.APPEND_MESSAGE, {"sid": "sess-1", "role": "worker", "content": f"note {i}", "now": iso(NOW)})


ts = [threading.Thread(target=say, args=(i,)) for i in range(30)]
[t.start() for t in ts]
[t.join() for t in ts]
hist = q(g, schema.READ_HISTORY, {"sid": "sess-1"})
print(f"   messages stored: {len(hist)}   numbered {hist[0][0]} to {hist[-1][0]}")
assert [h[0] for h in hist] == list(range(1, 31))

# ---------------------------------------------------------------------------------------------
step("8. Handoff: worker stops, ticket reopens, the next worker finds the note")
q(g, schema.WRITE_HANDOFF, {"run": RUN, "task": ticket, "who": f"agent-w{winner}", "id": "hand-1", "now": iso(NOW),
                            "done": "found the bug in the ids branch", "remaining": "run the full tests",
                            "tried": "skip unknown ids", "files": ["tinydb/table.py"]})
late = iso(NOW + timedelta(minutes=30))
reopened = q(g, schema.EXPIRE_STALE, {"run": RUN, "now": late})
print("   reopened after timeout:", [r[0] for r in reopened])
print("   note waiting for next worker:", q(g, schema.LATEST_HANDOFF, {"run": RUN, "task": ticket})[0])
assert [r[0] for r in reopened] == [ticket]

# ---------------------------------------------------------------------------------------------
step("9. Memory: episodic, semantic, procedural; recall; update; retire")
fn = ["tinydb.table.Table.update"]
q(g, schema.RECORD_MEMORY, {"run": RUN, "task": "T-01", "id": "mem-0001", "kind": "episodic", "now": iso(NOW),
                            "summary": "Attempt on T-01 failed: skipping ids broke upsert", "functions": fn})
q(g, schema.RECORD_MEMORY, {"run": RUN, "task": "T-01", "id": "mem-0002", "kind": "semantic", "now": iso(NOW),
                            "summary": "Table.update with doc_ids must skip ids that do not exist", "functions": fn})
q(g, schema.RECORD_MEMORY, {"run": RUN, "task": "T-01", "id": "mem-0003", "kind": "procedural", "now": iso(NOW),
                            "summary": "For KeyError in a doc_ids branch: filter ids first, then update", "functions": fn})
q(g, schema.MEMORY_FEEDBACK, {"id": "mem-0003", "worked": 1, "now": iso(NOW)})
q(g, schema.MEMORY_FEEDBACK, {"id": "mem-0003", "worked": 1, "now": iso(NOW)})
rec = q(g, schema.RECALL, {"run": RUN, "id": "T-01", "k": 5})
for r in rec:
    print(f"   recalled {r[0]} ({r[1]}): {r[2]}  [score {r[4]:.2f}]")
assert {r[1] for r in rec} == {"episodic", "semantic", "procedural"}
q(g, schema.RETIRE_MEMORY, {"old": "mem-0002", "new": "mem-0003", "reason": "replaced by a recipe"})
rec2 = q(g, schema.RECALL, {"run": RUN, "id": "T-01", "k": 5})
assert "mem-0002" not in [r[0] for r in rec2]
print("   retired fact no longer recalled: yes")

# ---------------------------------------------------------------------------------------------
step("10. Separate memory per tenant: a secret in tenant A must be invisible to tenant B")
q(g, "CREATE (:Memory {id:'mem-secret', kind:'semantic', summary:'tenant A private', status:'active', uses:0, successes:0})")
gb = fresh(db, "demo_repo_b")
seen_b = q(gb, "MATCH (m:Memory) RETURN count(m)")[0][0]
seen_a = q(g, "MATCH (m:Memory {id:'mem-secret'}) RETURN count(m)")[0][0]
print(f"   tenant A sees its secret: {seen_a}   tenant B sees any memory: {seen_b}")
assert seen_a == 1 and seen_b == 0

# ---------------------------------------------------------------------------------------------
step("11. Duplicates are refused by the database itself")
try:
    q(g, "CREATE (:Agent {id:'agent-w1'})")
    print("   NOT OK: duplicate agent was accepted")
    sys.exit(1)
except Exception as exc:  # noqa: BLE001
    print("   duplicate agent refused:", str(exc)[:70])

print("\nALL CHECKS PASSED")
if CLEAN:
    g.delete()
    gb.delete()
    print("demo graphs deleted")
else:
    print("Graphs kept. Open http://localhost:3000, pick graph 'foreman_demo_repo_a', run:")
    print("   MATCH (n)-[r]->(m) RETURN n, r, m")
