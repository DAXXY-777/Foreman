"""Foreman graph schema v1: setup (indexes and uniqueness rules) and the core queries.

One FalkorDB graph per repo (= per tenant). Work-layer nodes carry run_id so evaluation runs never mix.
This file is plain Cypher in strings on purpose: the same queries go into the README for the judges.
"""
import os
import re
import time

from falkordb import FalkorDB

HOST = os.environ.get("FALKORDB_HOST", "localhost")
PORT = int(os.environ.get("FALKORDB_PORT", "6379"))


def connect():
    """Local Docker needs only host and port. FalkorDB Cloud also needs FALKORDB_USERNAME / FALKORDB_PASSWORD
    (and FALKORDB_SSL=1 if the cloud instance asks for TLS)."""
    extra = {}
    if os.environ.get("FALKORDB_USERNAME"):
        extra["username"] = os.environ["FALKORDB_USERNAME"]
    if os.environ.get("FALKORDB_PASSWORD"):
        extra["password"] = os.environ["FALKORDB_PASSWORD"]
    if os.environ.get("FALKORDB_SSL", "").lower() in ("1", "true", "yes"):
        extra["ssl"] = True
    return FalkorDB(host=HOST, port=PORT, **extra)


def graph_name(repo: str) -> str:
    """Each repo (tenant) gets its own graph, so memory is separate per tenant."""
    return "foreman_" + re.sub(r"[^A-Za-z0-9_]", "_", repo)


# ----------------------------------------------------------------------------------------------
# Setup
# ----------------------------------------------------------------------------------------------
INDEXES = {                      # label -> properties to look up quickly
    "File": ["path"], "Function": ["id"], "Test": ["id", "status"],
    "Agent": ["id"], "Task": ["run_id", "id", "status"], "Session": ["id"],
    "Message": ["session_id", "seq"], "Handoff": ["task_id"], "Result": ["task_id"],
    "Decision": ["id"], "Memory": ["id", "status", "kind"], "Counter": ["name"],
}
UNIQUE = {                       # label -> properties that together must be unique
    "File": ["path"], "Function": ["id"], "Test": ["id"], "Agent": ["id"],
    "Task": ["run_id", "id"], "Session": ["id"], "Message": ["session_id", "seq"],
    "Decision": ["id"], "Memory": ["id"], "Counter": ["name"],
}


def _ignore_exists(fn, *args):
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001
        if "already" not in str(exc).lower():
            raise


def create_schema(g, timeout=30):
    """Idempotent: safe to run again. Returns the number of uniqueness rules in place."""
    for label, props in INDEXES.items():
        _ignore_exists(g.create_node_range_index, label, *props)
    deadline = time.time() + timeout
    while time.time() < deadline:                       # indexes build in the background
        states = [r[0] for r in g.query("CALL db.indexes() YIELD status RETURN status").result_set]
        if all(s == "OPERATIONAL" for s in states):
            break
        time.sleep(0.2)
    for label, props in UNIQUE.items():
        _ignore_exists(g.create_node_unique_constraint, label, *props)
    while time.time() < deadline:                       # constraints also build in the background
        cons = g.list_constraints()
        if all(c["status"] == "OPERATIONAL" for c in cons):
            return len(cons)
        time.sleep(0.2)
    raise TimeoutError("schema rules did not become ready")


# ----------------------------------------------------------------------------------------------
# Core queries (each one is a single statement, so each one is atomic)
# ----------------------------------------------------------------------------------------------

# Safe claim: checks open + no active conflict, claims, and writes the decision, all in one step.
CLAIM = """
MATCH (a:Agent {id:$who})
MATCH (t:Task {run_id:$run, id:$id})
WHERE t.status = 'open'
OPTIONAL MATCH (t)-[:CONFLICTS_WITH]-(o:Task {run_id:$run, status:'claimed'})
WITH a, t, o
WHERE o IS NULL
SET t.status = 'claimed', t.claimed_by = a.id, t.claim_expires_at = $expires
WITH a, t
MERGE (c:Counter {name:'decision'})
ON CREATE SET c.v = 1
ON MATCH SET c.v = c.v + 1
CREATE (d:Decision {id: 'dec-' + toString(c.v), kind:'claim', run_id:$run, ts:$now, summary: a.id + ' claimed ' + t.id})
CREATE (d)-[:ABOUT]->(t), (d)-[:MADE_BY]->(a)
RETURN d.id
"""

# Why a claim failed (only run after CLAIM returned nothing).
CLAIM_WHY = """
MATCH (t:Task {run_id:$run, id:$id})
OPTIONAL MATCH (t)-[:CONFLICTS_WITH]-(o:Task {run_id:$run, status:'claimed'})
RETURN t.status, t.claimed_by, collect(o.id)
"""

HEARTBEAT = """
MATCH (t:Task {run_id:$run, id:$id, status:'claimed', claimed_by:$who})
SET t.claim_expires_at = $expires
RETURN t.id
"""

# A claim that ran out of time goes back to open. The handoff note stays in the graph.
EXPIRE_STALE = """
MATCH (t:Task {run_id:$run, status:'claimed'})
WHERE t.claim_expires_at < $now
SET t.status = 'open', t.claimed_by = null, t.claim_expires_at = null
RETURN t.id
"""

# Create a ticket and link it to its failing tests.
CREATE_TASK = """
MERGE (t:Task {run_id:$run, id:$id})
ON CREATE SET t.ticket_id = $ticket_id, t.title = $title, t.description = $description,
              t.status = 'open', t.attempt_count = 0
WITH t
UNWIND $tests AS tid
MATCH (x:Test {id:tid})
MERGE (t)-[:FAILS_ON]->(x)
RETURN count(x)
"""

# After the code is loaded: how many tests run each function (hub functions get a low weight later).
SET_TEST_COUNTS = """
MATCH (f:Function)
OPTIONAL MATCH (x:Test)-[:COVERS]->(f)
WITH f, count(x) AS c
SET f.test_count = c
"""

# Footprint. For every function the ticket's failing tests ran, weight = suspicious x specific:
#   suspicious = ef / sqrt(tf * (ef + ep))   (ef: this ticket's failing tests that ran it, tf: how many
#                                             failing tests the ticket has, ep: passing tests that ran it)
#   specific   = log(all tests / tests that ran it)   (a function every test runs says nothing)
WRITE_FOOTPRINT = """
MATCH (t:Task {run_id:$run, id:$id})-[:FAILS_ON]->(ft:Test)
WITH t, count(ft) AS tf
MATCH (t)-[:FAILS_ON]->(x:Test)-[:COVERS]->(f:Function)
WITH t, tf, f, count(DISTINCT x) AS ef
OPTIONAL MATCH (p:Test {status:'pass'})-[:COVERS]->(f)
WITH t, tf, f, ef, count(DISTINCT p) AS ep
MERGE (t)-[r:TOUCHES]->(f)
SET r.weight = (ef / sqrt(tf * (ef + ep + 0.0))) * log(toFloat($n_tests) / f.test_count)
"""

TOTAL_WEIGHT = """
MATCH (t:Task {run_id:$run, id:$id})-[r:TOUCHES]->(:Function)
WITH t, sum(r.weight) AS w
SET t.footprint_weight = w
RETURN w
"""

# Overlap between this ticket and every other one: shared weight divided by combined weight (0 to 1).
OVERLAP = """
MATCH (a:Task {run_id:$run, id:$id})-[ra:TOUCHES]->(f:Function)<-[rb:TOUCHES]-(b:Task {run_id:$run})
WHERE b.id <> a.id
WITH a, b, f, CASE WHEN ra.weight < rb.weight THEN ra.weight ELSE rb.weight END AS m
WITH a, b, sum(m) AS inter, collect([f.id, m]) AS shared
RETURN b.id, b.status,
       CASE WHEN a.footprint_weight + b.footprint_weight - inter > 0
            THEN inter / (a.footprint_weight + b.footprint_weight - inter) ELSE 0.0 END AS score, shared
ORDER BY score DESC
"""

CONFLICT_THRESHOLD = 0.10   # two tickets at or above this overlap are marked as conflicting


def write_footprint(g, run, task_id):
    """Compute and store a ticket's weighted footprint. Returns its total weight."""
    n_tests = q(g, "MATCH (x:Test) RETURN count(x)")[0][0]
    q(g, WRITE_FOOTPRINT, {"run": run, "id": task_id, "n_tests": n_tests})
    rows = q(g, TOTAL_WEIGHT, {"run": run, "id": task_id})
    if rows:
        return rows[0][0]
    q(g, "MATCH (t:Task {run_id:$run, id:$id}) SET t.footprint_weight = 0.0", {"run": run, "id": task_id})
    return 0.0


# Store a conflict as a link, so CLAIM can check it in the same step.
MARK_CONFLICT = """
MATCH (a:Task {run_id:$run, id:$a}), (b:Task {run_id:$run, id:$b})
MERGE (a)-[c:CONFLICTS_WITH]->(b) SET c.score = $score, c.shared = $shared
"""

# Memory about the same code the task touches. Rank = how targeted (share of the memory's functions the ticket touches) times how reliable (success score).
RECALL = """
MATCH (t:Task {run_id:$run, id:$id})-[r:TOUCHES]->(f:Function)<-[:ABOUT]-(m:Memory {status:'active'})
WITH m, count(DISTINCT f) AS shared, sum(r.weight) AS weight
MATCH (m)-[:ABOUT]->(a:Function)
WITH m, shared, weight, count(a) AS total
RETURN m.id, m.kind, m.summary, toFloat(shared) / total AS relevance, (m.successes + 1.0) / (m.uses + 2.0) AS score
ORDER BY relevance * score DESC, weight DESC, m.id
LIMIT $k
"""

RECORD_MEMORY = """
MATCH (t:Task {run_id:$run, id:$task})
CREATE (m:Memory {id:$id, kind:$kind, summary:$summary, status:'active', uses:0, successes:0, created_at:$now})
CREATE (m)-[:LEARNED_FROM]->(t)
WITH m
UNWIND $functions AS fid
MATCH (f:Function {id:fid})
CREATE (m)-[:ABOUT]->(f)
"""

MEMORY_FEEDBACK = """
MATCH (m:Memory {id:$id})
SET m.uses = m.uses + 1, m.successes = m.successes + $worked, m.updated_at = $now
RETURN m.uses, m.successes
"""

RETIRE_MEMORY = """
MATCH (old:Memory {id:$old})
SET old.status = 'retired', old.retired_reason = $reason
WITH old
OPTIONAL MATCH (new:Memory {id:$new})
FOREACH (_ IN CASE WHEN new IS NULL THEN [] ELSE [1] END | CREATE (old)-[:SUPERSEDED_BY]->(new))
RETURN old.id
"""

# Conversation history: the counter and the message are written in one step, so numbers never repeat.
START_SESSION = """
MATCH (a:Agent {id:$who})
MATCH (t:Task {run_id:$run, id:$task})
CREATE (s:Session {id:$sid, run_id:$run, started_at:$now, last_seq:0})
CREATE (a)-[:HAS_SESSION]->(s), (s)-[:FOR]->(t)
RETURN s.id
"""

APPEND_MESSAGE = """
MATCH (s:Session {id:$sid})
SET s.last_seq = s.last_seq + 1
CREATE (m:Message {session_id:$sid, seq:s.last_seq, role:$role, content:$content, ts:$now})
CREATE (s)-[:HAS_MESSAGE]->(m)
RETURN m.seq
"""

READ_HISTORY = """
MATCH (:Session {id:$sid})-[:HAS_MESSAGE]->(m:Message)
RETURN m.seq, m.role, m.content
ORDER BY m.seq
"""

# Handoff: a note that stays on the ticket when a worker stops or changes.
WRITE_HANDOFF = """
MATCH (t:Task {run_id:$run, id:$task})
MATCH (a:Agent {id:$who})
CREATE (h:Handoff {id:$id, run_id:$run, task_id:$task, done:$done, remaining:$remaining,
                   tried:$tried, files_touched:$files, ts:$now})
CREATE (h)-[:FOR]->(t), (a)-[:WROTE]->(h)
RETURN h.id
"""

LATEST_HANDOFF = """
MATCH (h:Handoff {run_id:$run, task_id:$task})
RETURN h.done, h.remaining, h.tried, h.files_touched
ORDER BY h.ts DESC
LIMIT 1
"""

# Explain a decision: who, which ticket, which code, which competing tickets.
EXPLAIN = """
MATCH (d:Decision {id:$dec})
OPTIONAL MATCH (d)-[:MADE_BY]->(a:Agent)
OPTIONAL MATCH (d)-[:ABOUT]->(t:Task)
OPTIONAL MATCH (t)-[:TOUCHES]->(f:Function)
OPTIONAL MATCH (t)-[c:CONFLICTS_WITH]-(o:Task)
RETURN d.kind, d.summary, a.id, t.id, t.title,
       collect(DISTINCT f.id) AS code, collect(DISTINCT [o.id, o.status, c.score]) AS conflicts
"""


def q(g, query, params=None):
    """Run a query and return plain rows."""
    return g.query(query, params or {}).result_set
