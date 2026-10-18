"""GraphAPI v1.2: the functions in the locked contract, each one running Cypher in FalkorDB.

    api = GraphAPI("tinydb", run_id="run-1")
    api.register_agent("agent-w1", "worker")
    api.claim_task("T-01", "agent-w1")

Rules (from the contract):
  - Every function returns a dict with `ok`. A business-rule failure is `ok: False` plus an `error` code.
    Exceptions are only for infrastructure failure (FalkorDB unreachable).
  - Where the contract says "list of ...", the list is returned under a named key:
    list_open_tasks -> tasks, find_conflicts -> conflicts, recall_similar -> memories, get_history -> messages.
  - Every function that makes a choice writes a Decision and returns its `decision_id`.
  - One FalkorDB graph per repo. `run_id` tags work-layer data so evaluation runs do not mix.
"""
import json
from datetime import datetime, timedelta, timezone

from falkordb import FalkorDB

import schema
from schema import q

ROLES = {"foreman", "worker", "reviewer", "historian"}
MESSAGE_ROLES = {"agent", "user", "tool"}
MEMORY_KINDS = {"episodic", "semantic", "procedural"}

NEW_DECISION = """
MERGE (c:Counter {name:'decision'})
ON CREATE SET c.v = 1
ON MATCH SET c.v = c.v + 1
CREATE (d:Decision {id: 'dec-' + toString(c.v), kind:$kind, run_id:$run, ts:$now, summary:$summary})
WITH d
OPTIONAL MATCH (t:Task {run_id:$run, id:$task})
OPTIONAL MATCH (a:Agent {id:$who})
FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | CREATE (d)-[:ABOUT]->(t))
FOREACH (_ IN CASE WHEN a IS NULL THEN [] ELSE [1] END | CREATE (d)-[:MADE_BY]->(a))
RETURN d.id
"""

NEXT_NUMBER = """
MERGE (c:Counter {name:$name})
ON CREATE SET c.v = 1
ON MATCH SET c.v = c.v + 1
RETURN c.v
"""


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"          # fixed width, so text order = time order


def _fail(error, **extra):
    return {"ok": False, "error": error, **extra}


class GraphAPI:
    def __init__(self, repo, run_id, host=None, port=None, claim_ttl_seconds=300,
                 conflict_threshold=schema.CONFLICT_THRESHOLD, clock=None, ensure_schema=True):
        self.repo, self.run = repo, run_id
        self.ttl = claim_ttl_seconds
        self.threshold = conflict_threshold
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        db = FalkorDB(host=host or schema.HOST, port=port or schema.PORT)
        self.g = db.select_graph(schema.graph_name(repo))
        if ensure_schema:
            schema.create_schema(self.g)

    # ---- helpers -------------------------------------------------------------------------------
    def _now(self):
        return _iso(self.clock())

    def _number(self, name):
        return q(self.g, NEXT_NUMBER, {"name": name})[0][0]

    def _decision(self, kind, summary, task=None, who=None):
        return q(self.g, NEW_DECISION, {"kind": kind, "run": self.run, "now": self._now(), "summary": summary,
                                        "task": task, "who": who})[0][0]

    def _task_row(self, task_id):
        rows = q(self.g, """MATCH (t:Task {run_id:$run, id:$id})
                            RETURN t.id, t.title, t.status, t.claimed_by, t.claim_expires_at, t.attempt_count,
                                   t.footprint_decision, t.footprint_weight""",
                 {"run": self.run, "id": task_id})
        return rows[0] if rows else None

    # ---- setup and reads -----------------------------------------------------------------------
    def register_agent(self, agent_id, role):
        if role not in ROLES:
            return _fail("bad_role", allowed=sorted(ROLES))
        q(self.g, """MERGE (a:Agent {id:$id})
                     ON CREATE SET a.role = $role, a.status = 'idle', a.registered_at = $now
                     ON MATCH SET a.role = $role, a.last_seen = $now""",
          {"id": agent_id, "role": role, "now": self._now()})
        return {"ok": True}

    def get_task(self, task_id):
        row = self._task_row(task_id)
        if row is None:
            return _fail("no_such_task")
        return {"ok": True, "task_id": row[0], "title": row[1], "status": row[2], "claimed_by": row[3],
                "claim_expires_at": row[4], "attempt_count": row[5]}

    def list_open_tasks(self):
        rows = q(self.g, """MATCH (t:Task {run_id:$run, status:'open'})
                            OPTIONAL MATCH (t)-[:CONFLICTS_WITH]-(o:Task {run_id:$run, status:'claimed'})
                            RETURN t.id, t.title, t.rank, collect(DISTINCT o.id)""", {"run": self.run})
        tasks = [{"task_id": r[0], "title": r[1], "rank": r[2], "conflicts_with_active": sorted(r[3])} for r in rows]
        tasks.sort(key=lambda t: (t["rank"] is None, t["rank"] if t["rank"] is not None else 0, t["task_id"]))
        return {"ok": True, "tasks": tasks}

    def get_result(self, task_id):
        rows = q(self.g, """MATCH (r:Result {run_id:$run, task_id:$id})
                            RETURN r.id, r.summary, r.tests_passed, r.test_report, r.files_touched, r.patch_ref, r.ts
                            ORDER BY r.n DESC LIMIT 1""", {"run": self.run, "id": task_id})
        if not rows:
            return _fail("no_result")
        r = rows[0]
        inside = {f for f in (self._footprint_files(task_id) or [])}
        outside = sorted(f for f in (r[4] or []) if f not in inside) if inside else []
        return {"ok": True, "result_id": r[0], "summary": r[1], "tests_passed": r[2],
                "test_report": json.loads(r[3]) if r[3] else None, "files_touched": r[4] or [],
                "patch_ref": r[5], "submitted_at": r[6], "out_of_footprint": outside}

    # ---- foreman -------------------------------------------------------------------------------
    def create_task(self, ticket):
        need = ("ticket_id", "title", "description", "failing_tests")
        if any(k not in ticket for k in need) or not ticket["failing_tests"]:
            return _fail("bad_ticket", required=list(need))
        known = {r[0] for r in q(self.g, "UNWIND $t AS x MATCH (n:Test {id:x}) RETURN n.id", {"t": ticket["failing_tests"]})}
        missing = [t for t in ticket["failing_tests"] if t not in known]
        if missing:
            return _fail("unknown_tests", missing=missing)
        q(self.g, schema.CREATE_TASK, {"run": self.run, "id": ticket["ticket_id"], "ticket_id": ticket["ticket_id"],
                                       "title": ticket["title"], "description": ticket["description"],
                                       "tests": ticket["failing_tests"]})
        return {"ok": True, "task_id": ticket["ticket_id"]}

    def _footprint_rows(self, task_id):
        return q(self.g, """MATCH (:Task {run_id:$run, id:$id})-[r:TOUCHES]->(f:Function)
                            WHERE r.weight > 0
                            RETURN f.id, f.file, r.weight ORDER BY r.weight DESC""", {"run": self.run, "id": task_id})

    def _footprint_files(self, task_id):
        return sorted({r[1] for r in self._footprint_rows(task_id)})

    def get_footprint(self, task_id):
        row = self._task_row(task_id)
        if row is None:
            return _fail("no_such_task")
        decision_id = row[6]
        if decision_id is None:
            schema.write_footprint(self.g, self.run, task_id)
            decision_id = self._decision("footprint", f"footprint of {task_id} from its failing tests", task=task_id)
            q(self.g, "MATCH (t:Task {run_id:$run, id:$id}) SET t.footprint_decision = $d",
              {"run": self.run, "id": task_id, "d": decision_id})
        rows = self._footprint_rows(task_id)
        tests = [r[0] for r in q(self.g, "MATCH (:Task {run_id:$run, id:$id})-[:FAILS_ON]->(x:Test) RETURN x.id ORDER BY x.id",
                                 {"run": self.run, "id": task_id})]
        return {"ok": True, "files": sorted({r[1] for r in rows}),
                "functions": [{"id": r[0], "weight": r[2]} for r in rows], "tests": tests, "decision_id": decision_id}

    def _ensure_footprints(self):
        for (tid,) in q(self.g, "MATCH (t:Task {run_id:$run}) WHERE t.footprint_weight IS NULL RETURN t.id", {"run": self.run}):
            schema.write_footprint(self.g, self.run, tid)

    def find_conflicts(self, task_id, include_all=False):
        if self._task_row(task_id) is None:
            return _fail("no_such_task")
        self._ensure_footprints()
        conflicts = []
        for other, status, score, shared in q(self.g, schema.OVERLAP, {"run": self.run, "id": task_id}):
            if score < self.threshold and not include_all:
                continue
            if score >= self.threshold:
                a, b = sorted((task_id, other))
                q(self.g, schema.MARK_CONFLICT, {"run": self.run, "a": a, "b": b, "score": score,
                                                 "shared": [s[0] for s in shared]})
            conflicts.append({"other_task_id": other, "other_status": status, "score": score,
                              "shared_nodes": [{"id": s[0], "type": "Function", "weight": s[1]}
                                               for s in sorted(shared, key=lambda s: -s[1])]})
        return {"ok": True, "conflicts": conflicts}

    def set_task_order(self, ordered_task_ids):
        known = {r[0] for r in q(self.g, "UNWIND $ids AS x MATCH (t:Task {run_id:$run, id:x}) RETURN t.id",
                                 {"ids": ordered_task_ids, "run": self.run})}
        missing = [t for t in ordered_task_ids if t not in known]
        if missing:
            return _fail("no_such_task", missing=missing)
        q(self.g, """UNWIND range(0, size($ids) - 1) AS i
                     MATCH (t:Task {run_id:$run, id:$ids[i]}) SET t.rank = i + 1""", {"ids": ordered_task_ids, "run": self.run})
        did = self._decision("order", "task order set: " + ", ".join(ordered_task_ids))
        return {"ok": True, "decision_id": did}

    def expire_stale_claims(self):
        rows = q(self.g, """
            MATCH (t:Task {run_id:$run, status:'claimed'})
            WHERE t.claim_expires_at < $now
            WITH t, t.claimed_by AS prev
            SET t.status = 'open', t.claimed_by = null, t.claim_expires_at = null
            WITH t, prev
            MERGE (c:Counter {name:'decision'})
            ON CREATE SET c.v = 1
            ON MATCH SET c.v = c.v + 1
            CREATE (d:Decision {id: 'dec-' + toString(c.v), kind:'expire', run_id:$run, ts:$now,
                                summary: 'claim by ' + prev + ' on ' + t.id + ' expired, ticket reopened'})
            CREATE (d)-[:ABOUT]->(t)
            RETURN t.id""", {"run": self.run, "now": self._now()})
        return {"ok": True, "reopened": sorted(r[0] for r in rows)}

    # ---- workers -------------------------------------------------------------------------------
    def claim_task(self, task_id, agent_id):
        now = self.clock()
        expires = _iso(now + timedelta(seconds=self.ttl))
        rows = q(self.g, schema.CLAIM, {"run": self.run, "id": task_id, "who": agent_id,
                                        "now": _iso(now), "expires": expires})
        if rows:
            return {"ok": True, "reason": "claimed", "claim_expires_at": expires, "decision_id": rows[0][0]}
        if not q(self.g, "MATCH (a:Agent {id:$id}) RETURN a.id", {"id": agent_id}):
            return _fail("unknown_agent", reason="unknown_agent")
        why = q(self.g, schema.CLAIM_WHY, {"run": self.run, "id": task_id})
        if not why:
            return _fail("no_such_task", reason="no_such_task")
        status, holder, blockers = why[0]
        if status == "claimed" and holder == agent_id:
            task = self._task_row(task_id)
            return {"ok": True, "reason": "already_yours", "claim_expires_at": task[4], "decision_id": None}
        reason = ("already_claimed" if status == "claimed" else
                  "conflict_active" if status == "open" and blockers else "not_open")
        q(self.g, NEXT_NUMBER, {"name": f"refused:{self.run}:{reason}"})
        return {"ok": False, "reason": reason, "error": reason, "claimed_by": holder, "blocked_by": sorted(blockers)}

    def heartbeat(self, task_id, agent_id):
        expires = _iso(self.clock() + timedelta(seconds=self.ttl))
        rows = q(self.g, schema.HEARTBEAT, {"run": self.run, "id": task_id, "who": agent_id, "expires": expires})
        if not rows:
            return _fail("not_holder")
        return {"ok": True, "claim_expires_at": expires}

    def release_claim(self, task_id, agent_id):
        rows = q(self.g, """
            MATCH (t:Task {run_id:$run, id:$id, status:'claimed', claimed_by:$who})
            SET t.status = 'open', t.claimed_by = null, t.claim_expires_at = null
            WITH t
            MERGE (c:Counter {name:'decision'})
            ON CREATE SET c.v = 1
            ON MATCH SET c.v = c.v + 1
            CREATE (d:Decision {id: 'dec-' + toString(c.v), kind:'release', run_id:$run, ts:$now,
                                summary: $who + ' released ' + t.id})
            CREATE (d)-[:ABOUT]->(t)
            RETURN t.id""", {"run": self.run, "id": task_id, "who": agent_id, "now": self._now()})
        return {"ok": True} if rows else _fail("not_holder")

    def write_handoff(self, task_id, agent_id, note):
        if any(k not in note for k in ("done", "remaining", "tried", "files_touched")):
            return _fail("bad_note", required=["done", "remaining", "tried", "files_touched"])
        n = self._number("handoff")
        rows = q(self.g, """
            MATCH (t:Task {run_id:$run, id:$task})
            MATCH (a:Agent {id:$who})
            CREATE (h:Handoff {id:$id, n:$n, run_id:$run, task_id:$task, done:$done, remaining:$remaining,
                               tried:$tried, files_touched:$files, ts:$now})
            CREATE (h)-[:FOR]->(t), (a)-[:WROTE]->(h)
            RETURN h.id""",
                 {"run": self.run, "task": task_id, "who": agent_id, "id": f"hand-{n}", "n": n, "now": self._now(),
                  "done": note["done"], "remaining": note["remaining"], "tried": note["tried"],
                  "files": list(note["files_touched"])})
        return {"ok": True} if rows else _fail("no_such_task_or_agent")

    def get_handoff(self, task_id):
        rows = q(self.g, """MATCH (h:Handoff {run_id:$run, task_id:$id})
                            OPTIONAL MATCH (a:Agent)-[:WROTE]->(h)
                            RETURN h.done, h.remaining, h.tried, h.files_touched, a.id, h.ts
                            ORDER BY h.n DESC LIMIT 1""", {"run": self.run, "id": task_id})
        if not rows:
            return {"ok": True, "note": None}
        r = rows[0]
        return {"ok": True, "note": {"done": r[0], "remaining": r[1], "tried": r[2], "files_touched": r[3],
                                     "written_by": r[4], "written_at": r[5]}}

    def recall_similar(self, task_id, k=5):
        if self._task_row(task_id) is None:
            return _fail("no_such_task")
        rows = q(self.g, """
            MATCH (t:Task {run_id:$run, id:$id})-[r:TOUCHES]->(f:Function)<-[:ABOUT]-(m:Memory {status:'active'})
            WITH m, count(DISTINCT f) AS shared, sum(r.weight) AS weight, collect(DISTINCT f.file) AS files
            MATCH (m)-[:ABOUT]->(a:Function)
            WITH m, shared, weight, files, count(a) AS total
            RETURN m.id, m.kind, m.summary, files, m.evidence_path, toFloat(shared) / total AS relevance,
                   (m.successes + 1.0) / (m.uses + 2.0) AS score, weight
            ORDER BY relevance * score DESC, weight DESC, m.id
            LIMIT $k""", {"run": self.run, "id": task_id, "k": k})
        if rows:
            q(self.g, """UNWIND $ids AS mid
                         MATCH (t:Task {run_id:$run, id:$id}), (m:Memory {id:mid})
                         MERGE (t)-[:RECALLED]->(m)""", {"ids": [r[0] for r in rows], "run": self.run, "id": task_id})
        return {"ok": True, "memories": [{"memory_id": r[0], "kind": r[1], "summary": r[2], "fix_files": sorted(r[3]),
                                          "evidence_path": r[4], "relevance": r[5], "score": r[6]} for r in rows]}

    def submit_result(self, task_id, agent_id, summary, tests_passed, files_touched, patch_ref):
        if isinstance(tests_passed, dict):
            passed, report = bool(tests_passed.get("passed")), json.dumps(tests_passed)
        else:
            passed, report = bool(tests_passed), None
        n = self._number("result")
        rows = q(self.g, """
            MATCH (a:Agent {id:$who})
            MATCH (t:Task {run_id:$run, id:$id, status:'claimed', claimed_by:$who})
            SET t.status = 'submitted', t.claim_expires_at = null
            CREATE (r:Result {id:$rid, n:$n, run_id:$run, task_id:$id, summary:$summary, tests_passed:$passed,
                              test_report:$report, files_touched:$files, patch_ref:$patch, ts:$now})
            CREATE (r)-[:FOR]->(t), (r)-[:SUBMITTED_BY]->(a)
            RETURN r.id""",
                 {"run": self.run, "id": task_id, "who": agent_id, "rid": f"res-{n}", "n": n, "summary": summary,
                  "passed": passed, "report": report, "files": list(files_touched), "patch": patch_ref,
                  "now": self._now()})
        return {"ok": True, "result_id": rows[0][0]} if rows else _fail("not_holder")

    # ---- reviewer ------------------------------------------------------------------------------
    def review_decision(self, task_id, reviewer_id, approved, reasons=()):
        reasons = list(reasons)
        rows = q(self.g, """
            MATCH (a:Agent {id:$who})
            MATCH (t:Task {run_id:$run, id:$id, status:'submitted'})
            SET t.status = CASE WHEN $approved THEN 'approved' ELSE 'open' END,
                t.attempt_count = CASE WHEN $approved THEN t.attempt_count ELSE t.attempt_count + 1 END,
                t.claimed_by = CASE WHEN $approved THEN t.claimed_by ELSE null END
            WITH a, t
            MERGE (c:Counter {name:'decision'})
            ON CREATE SET c.v = 1
            ON MATCH SET c.v = c.v + 1
            CREATE (d:Decision {id: 'dec-' + toString(c.v), kind:'review', run_id:$run, ts:$now, approved:$approved,
                                reason_codes:$codes, reasons:$reasons_json,
                                summary: a.id + CASE WHEN $approved THEN ' approved ' ELSE ' rejected ' END + t.id})
            CREATE (d)-[:ABOUT]->(t), (d)-[:MADE_BY]->(a)
            RETURN d.id""",
                 {"run": self.run, "id": task_id, "who": reviewer_id, "approved": bool(approved), "now": self._now(),
                  "codes": [r["code"] for r in reasons], "reasons_json": json.dumps(reasons)})
        if not rows:
            return _fail("not_submitted_or_unknown_reviewer")
        q(self.g, """MATCH (d:Decision {id:$d}), (r:Result {run_id:$run, task_id:$id})
                     WITH d, r ORDER BY r.n DESC LIMIT 1
                     CREATE (d)-[:ABOUT]->(r)""", {"d": rows[0][0], "run": self.run, "id": task_id})
        return {"ok": True, "decision_id": rows[0][0]}

    # ---- historian -----------------------------------------------------------------------------
    def record_outcome(self, task_id, summary, kind, details=None):
        if kind not in MEMORY_KINDS:
            return _fail("bad_kind", allowed=sorted(MEMORY_KINDS))
        if self._task_row(task_id) is None:
            return _fail("no_such_task")
        details = details or {}
        functions = details.get("functions")
        if functions is None:                         # default: the ticket's most suspicious functions
            functions = [r[0] for r in self._footprint_rows(task_id)[:5]]
        n = self._number("memory")
        q(self.g, """
            MATCH (t:Task {run_id:$run, id:$task})
            CREATE (m:Memory {id:$id, kind:$kind, summary:$summary, status:'active', uses:0, successes:0,
                              created_at:$now, evidence_path:$evidence, details:$details})
            CREATE (m)-[:LEARNED_FROM]->(t)
            WITH m
            UNWIND $functions AS fid
            MATCH (f:Function {id:fid})
            CREATE (m)-[:ABOUT]->(f)""",
          {"run": self.run, "task": task_id, "id": f"mem-{n}", "kind": kind, "summary": summary, "now": self._now(),
           "evidence": details.get("evidence_path"), "details": json.dumps(details), "functions": list(functions)})
        return {"ok": True, "memory_id": f"mem-{n}"}

    def retire_fact(self, memory_id, reason, superseded_by=None):
        rows = q(self.g, schema.RETIRE_MEMORY, {"old": memory_id, "new": superseded_by, "reason": reason})
        return {"ok": True} if rows else _fail("no_such_memory")

    def rate_memory(self, memory_id, worked):
        rows = q(self.g, schema.MEMORY_FEEDBACK, {"id": memory_id, "worked": 1 if worked else 0, "now": self._now()})
        if not rows:
            return _fail("no_such_memory")
        return {"ok": True, "uses": rows[0][0], "successes": rows[0][1]}

    # ---- conversation history ------------------------------------------------------------------
    def start_session(self, task_id, agent_id):
        old = q(self.g, """MATCH (:Agent {id:$who})-[:HAS_SESSION]->(s:Session)-[:FOR]->(:Task {run_id:$run, id:$task})
                           RETURN s.id ORDER BY s.started_at DESC LIMIT 1""",
                {"who": agent_id, "run": self.run, "task": task_id})
        if old:                                       # a restarted agent picks up its old conversation
            return {"ok": True, "session_id": old[0][0], "resumed": True}
        n = self._number("session")
        rows = q(self.g, schema.START_SESSION, {"who": agent_id, "task": task_id, "run": self.run,
                                                "sid": f"sess-{n}", "now": self._now()})
        if not rows:
            return _fail("no_such_task_or_agent")
        return {"ok": True, "session_id": rows[0][0], "resumed": False}

    def log_message(self, session_id, role, content):
        if role not in MESSAGE_ROLES:
            return _fail("bad_role", allowed=sorted(MESSAGE_ROLES))
        rows = q(self.g, schema.APPEND_MESSAGE, {"sid": session_id, "role": role, "content": content, "now": self._now()})
        return {"ok": True, "seq": rows[0][0]} if rows else _fail("no_such_session")

    def get_history(self, session_id, after_seq=0):
        if not q(self.g, "MATCH (s:Session {id:$id}) RETURN s.id", {"id": session_id}):
            return _fail("no_such_session")
        rows = q(self.g, """MATCH (:Session {id:$id})-[:HAS_MESSAGE]->(m:Message) WHERE m.seq > $after
                            RETURN m.seq, m.role, m.content, m.ts ORDER BY m.seq""", {"id": session_id, "after": after_seq})
        return {"ok": True, "messages": [{"seq": r[0], "role": r[1], "content": r[2], "ts": r[3]} for r in rows]}

    # ---- explain, UI and evaluation ------------------------------------------------------------
    def explain(self, decision_id):
        rows = q(self.g, """MATCH (d:Decision {id:$id})
                            RETURN d.kind, d.summary, d.ts, d.approved, d.reason_codes""", {"id": decision_id})
        if not rows:
            return _fail("no_such_decision")
        kind, summary, ts, approved, codes = rows[0]
        nodes = {decision_id: {"id": decision_id, "type": "Decision", "label": summary}}
        edges = []

        def add(node_id, typ, label):
            nodes.setdefault(node_id, {"id": node_id, "type": typ, "label": label})

        for typ, nid, title in q(self.g, """MATCH (:Decision {id:$id})-[:MADE_BY]->(a:Agent) RETURN 'Agent', a.id, a.role""",
                                 {"id": decision_id}):
            add(nid, typ, f"{nid} ({title})")
            edges.append({"from": decision_id, "to": nid, "type": "MADE_BY"})
        tasks = []
        for typ, nid, title in q(self.g, """MATCH (:Decision {id:$id})-[:ABOUT]->(x)
                                            RETURN labels(x)[0], x.id, coalesce(x.title, x.summary)""", {"id": decision_id}):
            add(nid, typ, title or nid)
            edges.append({"from": decision_id, "to": nid, "type": "ABOUT"})
            if typ == "Task":
                tasks.append(nid)
        sentences = [summary + "."]
        for tid in tasks:
            funcs = q(self.g, """MATCH (:Task {run_id:$run, id:$id})-[r:TOUCHES]->(f:Function) WHERE r.weight > 0
                                 RETURN f.id, r.weight ORDER BY r.weight DESC LIMIT 5""", {"run": self.run, "id": tid})
            for fid, w in funcs:
                add(fid, "Function", fid)
                edges.append({"from": tid, "to": fid, "type": "TOUCHES", "weight": w})
            if funcs and kind in ("claim", "footprint"):
                sentences.append(f"{tid} touches " + ", ".join(f[0] for f in funcs[:3]) + ".")
            others = q(self.g, """MATCH (:Task {run_id:$run, id:$id})-[c:CONFLICTS_WITH]-(o:Task {run_id:$run})
                                  RETURN o.id, o.status, c.score""", {"run": self.run, "id": tid})
            for oid, ostatus, score in others:
                add(oid, "Task", oid)
                edges.append({"from": tid, "to": oid, "type": "CONFLICTS_WITH", "score": score})
            if others and kind in ("claim", "release", "expire"):
                sentences.append("It shares code with " + ", ".join(f"{o[0]} (score {o[2]:.2f}, {o[1]})" for o in others)
                                 + ", so those tickets cannot run at the same time.")
            for mid, mkind, msum in q(self.g, """MATCH (:Task {run_id:$run, id:$id})-[:RECALLED]->(m:Memory)
                                                 RETURN m.id, m.kind, m.summary""", {"run": self.run, "id": tid}):
                add(mid, "Memory", msum)
                edges.append({"from": tid, "to": mid, "type": "RECALLED"})
        if kind == "review" and codes:
            sentences.append("Reasons: " + ", ".join(codes) + ".")
        return {"ok": True, "decision": {"id": decision_id, "kind": kind, "ts": ts, "approved": approved},
                "summary": " ".join(sentences), "path": {"nodes": list(nodes.values()), "edges": edges}}

    def get_graph_snapshot(self, since=None):
        run = {"run": self.run}
        nodes, edges = [], []
        for r in q(self.g, "MATCH (a:Agent) RETURN a.id, a.role"):
            nodes.append({"id": r[0], "type": "Agent", "label": r[0], "role": r[1]})
        for r in q(self.g, "MATCH (t:Task {run_id:$run}) RETURN t.id, t.title, t.status, t.claimed_by", run):
            nodes.append({"id": r[0], "type": "Task", "label": r[1], "status": r[2], "claimed_by": r[3]})
            if r[3]:
                edges.append({"from": r[3], "to": r[0], "type": "CLAIMED"})
        for r in q(self.g, "MATCH (d:Decision {run_id:$run}) RETURN d.id, d.kind, d.summary", run):
            nodes.append({"id": r[0], "type": "Decision", "label": r[2], "kind": r[1]})
        for r in q(self.g, "MATCH (m:Memory) RETURN m.id, m.kind, m.summary, m.status"):
            nodes.append({"id": r[0], "type": "Memory", "label": r[2], "kind": r[1], "status": r[3]})
        for r in q(self.g, "MATCH (x:Result {run_id:$run}) RETURN x.id, x.summary", run):
            nodes.append({"id": r[0], "type": "Result", "label": r[1]})
        for r in q(self.g, """MATCH (:Task {run_id:$run})-[r:TOUCHES]->(f:Function) WHERE r.weight > 0
                              RETURN DISTINCT f.id""", run):
            nodes.append({"id": r[0], "type": "Function", "label": r[0]})
        for typ, cypher in (("TOUCHES", "MATCH (t:Task {run_id:$run})-[r:TOUCHES]->(f:Function) WHERE r.weight > 0 RETURN t.id, f.id"),
                            ("CONFLICTS_WITH", "MATCH (a:Task {run_id:$run})-[:CONFLICTS_WITH]->(b:Task) RETURN a.id, b.id"),
                            ("ABOUT", "MATCH (d:Decision {run_id:$run})-[:ABOUT]->(x) RETURN d.id, x.id"),
                            ("MADE_BY", "MATCH (d:Decision {run_id:$run})-[:MADE_BY]->(a:Agent) RETURN d.id, a.id"),
                            ("ABOUT", "MATCH (m:Memory)-[:ABOUT]->(f:Function) RETURN m.id, f.id"),
                            ("LEARNED_FROM", "MATCH (m:Memory)-[:LEARNED_FROM]->(t:Task {run_id:$run}) RETURN m.id, t.id"),
                            ("RECALLED", "MATCH (t:Task {run_id:$run})-[:RECALLED]->(m:Memory) RETURN t.id, m.id"),
                            ("FOR", "MATCH (x:Result {run_id:$run})-[:FOR]->(t:Task) RETURN x.id, t.id")):
            for a, b in q(self.g, cypher, run):
                edges.append({"from": a, "to": b, "type": typ})
        since = since or ""
        events = [{"ts": r[0], "type": r[1], "id": r[2], "summary": r[3]} for r in q(self.g, """
            MATCH (d:Decision {run_id:$run}) WHERE d.ts > $since RETURN d.ts AS ts, d.kind AS type, d.id AS id, d.summary AS summary
            UNION ALL
            MATCH (x:Result {run_id:$run}) WHERE x.ts > $since RETURN x.ts AS ts, 'result' AS type, x.id AS id, x.summary AS summary
            UNION ALL
            MATCH (h:Handoff {run_id:$run}) WHERE h.ts > $since RETURN h.ts AS ts, 'handoff' AS type, h.id AS id, h.done AS summary""",
                                                                                      {"run": self.run, "since": since})]
        events.sort(key=lambda e: e["ts"])
        return {"ok": True, "nodes": nodes, "edges": edges, "events": events}

    def get_run_metrics(self):
        run = {"run": self.run}
        grants = q(self.g, "MATCH (d:Decision {run_id:$run, kind:'claim'}) RETURN count(d)", run)[0][0]
        refused = {r[0].split(":")[-1]: r[1] for r in q(
            self.g, "MATCH (c:Counter) WHERE c.name STARTS WITH $p RETURN c.name, c.v", {"p": f"refused:{self.run}:"})}
        per_task = q(self.g, """
            MATCH (t:Task {run_id:$run})
            OPTIONAL MATCH (d:Decision {run_id:$run})-[:ABOUT]->(t)
            WITH t, sum(CASE WHEN d.kind = 'claim' THEN 1 ELSE 0 END) AS g,
                    sum(CASE WHEN d.kind IN ['expire', 'release'] OR (d.kind = 'review' AND d.approved = false)
                             THEN 1 ELSE 0 END) AS r
            RETURN t.id, g, r, t.attempt_count""", run)
        double_claims = sum(max(0, g - 1 - r) for _, g, r, _ in per_task)    # held twice with no reopen in between
        reworks = sum(a or 0 for *_, a in per_task)
        recovered = q(self.g, """
            MATCH (h:Handoff {run_id:$run})-[:FOR]->(t:Task)
            MATCH (d:Decision {run_id:$run, kind:'claim'})-[:ABOUT]->(t)
            WHERE d.ts > h.ts
            RETURN count(DISTINCT t)""", run)[0][0]
        # collisions: two tickets whose results touched the same file while both were being worked on
        spans = {}
        for tid, ts in q(self.g, """MATCH (d:Decision {run_id:$run, kind:'claim'})-[:ABOUT]->(t:Task)
                                    RETURN t.id, min(d.ts)""", run):
            spans[tid] = [ts, None, set()]
        for tid, ts, files in q(self.g, "MATCH (x:Result {run_id:$run}) RETURN x.task_id, max(x.ts), collect(x.files_touched)", run):
            if tid in spans:
                spans[tid][1] = ts
                spans[tid][2] = {f for fl in files for f in (fl or [])}
        collisions = 0
        ids = sorted(spans)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                (sa, ea, fa), (sb, eb, fb) = spans[a], spans[b]
                ea, eb = ea or "9999", eb or "9999"
                if fa & fb and sa <= eb and sb <= ea:
                    collisions += 1
        return {"ok": True, "claims_granted": grants,
                "claims_refused": refused, "claims_attempted": grants + sum(refused.values()),
                "double_claims": double_claims, "collisions": collisions, "reworks": reworks,
                "handoffs_recovered": recovered}

    # ---- evaluation housekeeping (not part of the agent-facing contract) -----------------------
    def clear_memory(self):
        q(self.g, "MATCH (m:Memory) DETACH DELETE m")
        return {"ok": True}

    def reset_run(self):
        q(self.g, "MATCH (:Session {run_id:$run})-[:HAS_MESSAGE]->(m:Message) DETACH DELETE m", {"run": self.run})
        for label in ("Decision", "Handoff", "Result", "Task", "Session"):
            q(self.g, f"MATCH (n:{label} {{run_id:$run}}) DETACH DELETE n", {"run": self.run})
        q(self.g, "MATCH (c:Counter) WHERE c.name STARTS WITH $p DELETE c", {"p": f"refused:{self.run}:"})
        return {"ok": True}
