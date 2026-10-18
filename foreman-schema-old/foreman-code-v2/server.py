"""Read-only web server for the live graph screen.

    uvicorn server:app --port 8000          then open http://localhost:8000

It only READS from the graph (nothing here can change data), so it is safe to put on the internet.
Settings (environment variables): FALKORDB_HOST, FALKORDB_PORT, FALKORDB_USERNAME, FALKORDB_PASSWORD, FALKORDB_SSL,
and optionally DEFAULT_REPO (default tinydb) and DEFAULT_RUN (default: the run with the newest activity).
"""
import json
import os
import threading

import _paths  # noqa: F401
import schema
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from graph_api import GraphAPI

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Foreman live view")
_apis, _lock = {}, threading.Lock()
_db = {}


def db():
    if "c" not in _db:
        _db["c"] = schema.connect()
    return _db["c"]


def graph(repo):
    return db().select_graph(schema.graph_name(repo))


def api_for(repo, run):
    with _lock:
        if (repo, run) not in _apis:
            _apis[(repo, run)] = GraphAPI(repo, run, ensure_schema=False)
        return _apis[(repo, run)]


def repos():
    names = [g for g in db().list_graphs() if g.startswith("foreman_")]
    return sorted(n[len("foreman_"):] for n in names)


def runs(repo):
    rows = schema.q(graph(repo), """MATCH (t:Task) WITH t.run_id AS run, count(t) AS tasks
                                    OPTIONAL MATCH (d:Decision {run_id: run})
                                    RETURN run, tasks, max(d.ts) AS last ORDER BY last DESC""")
    return [{"run": r[0], "tasks": r[1], "last_activity": r[2]} for r in rows]


def pick(repo, run):
    repo = repo or os.environ.get("DEFAULT_REPO", "tinydb")
    if repo not in repos():
        raise HTTPException(404, f"no graph for repo '{repo}'")
    if not run:
        run = os.environ.get("DEFAULT_RUN") or next((r["run"] for r in runs(repo)), None)
    if not run:
        raise HTTPException(404, "no runs yet in this graph")
    return repo, run


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/repos")
def api_repos():
    rs = repos()
    return {"repos": rs, "default": os.environ.get("DEFAULT_REPO", "tinydb") if os.environ.get("DEFAULT_REPO", "tinydb") in rs
            else (rs[0] if rs else None)}


@app.get("/api/runs")
def api_runs(repo: str = None):
    repo = repo or os.environ.get("DEFAULT_REPO", "tinydb")
    return {"repo": repo, "runs": runs(repo)}


@app.get("/api/state")
def api_state(repo: str = None, run: str = None):
    repo, run = pick(repo, run)
    api = api_for(repo, run)
    snap = api.get_graph_snapshot()
    g = graph(repo)
    involved = {r[0] for r in schema.q(g, "MATCH (a:Agent)-[:HAS_SESSION]->(s:Session {run_id:$run}) RETURN DISTINCT a.id", {"run": run})}
    involved |= {r[0] for r in schema.q(g, "MATCH (d:Decision {run_id:$run})-[:MADE_BY]->(a:Agent) RETURN DISTINCT a.id", {"run": run})}
    involved |= {n["claimed_by"] for n in snap["nodes"] if n["type"] == "Task" and n.get("claimed_by")}
    involved |= {n["id"] for n in snap["nodes"] if n["type"] == "Agent" and n.get("role") in ("foreman", "reviewer", "historian")}
    nodes = [n for n in snap["nodes"] if n["type"] != "Agent" or n["id"] in involved]
    ids = {n["id"] for n in nodes}
    edges = [e for e in snap["edges"] if e["from"] in ids and e["to"] in ids]
    return {"repo": repo, "run": run, "nodes": nodes, "edges": edges, "events": snap["events"],
            "metrics": api.get_run_metrics()}


@app.get("/api/explain/{decision_id}")
def api_explain(decision_id: str, repo: str = None, run: str = None):
    repo, run = pick(repo, run)
    res = api_for(repo, run).explain(decision_id)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error"))
    return res


@app.get("/api/sessions")
def api_sessions(repo: str = None, run: str = None, task: str = None):
    repo, run = pick(repo, run)
    rows = schema.q(graph(repo), """
        MATCH (a:Agent)-[:HAS_SESSION]->(s:Session)-[:FOR]->(t:Task {run_id:$run})
        WHERE $task IS NULL OR t.id = $task
        RETURN s.id, a.id, a.role, t.id, s.last_seq, s.started_at ORDER BY s.started_at""", {"run": run, "task": task})
    return {"sessions": [{"id": r[0], "agent": r[1], "role": r[2], "task": r[3], "messages": r[4], "started_at": r[5]}
                         for r in rows]}


@app.get("/api/session/{session_id}")
def api_session(session_id: str, repo: str = None, run: str = None):
    repo, run = pick(repo, run)
    res = api_for(repo, run).get_history(session_id)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error"))
    out = []
    for m in res["messages"]:
        item = {"seq": m["seq"], "role": m["role"], "ts": m["ts"], "text": m["content"], "calls": []}
        if m["role"] == "agent":
            try:
                body = json.loads(m["content"])
                item["text"], item["calls"] = body.get("text") or "", body.get("calls") or []
            except ValueError:
                pass
        elif m["role"] == "tool":
            try:
                body = json.loads(m["content"])
                item["text"] = body.get("name", "tool")
                item["result_ok"] = bool((body.get("result") or {}).get("ok", True))
                item["calls"] = []
            except ValueError:
                pass
        out.append(item)
    return {"messages": out}


@app.get("/api/live")
def api_live(repo: str = None, run: str = None):
    """What each agent said or did most recently (its newest messages), for the speech bubbles."""
    repo, run = pick(repo, run)
    rows = schema.q(graph(repo), """
        MATCH (a:Agent)-[:HAS_SESSION]->(s:Session {run_id:$run})-[:HAS_MESSAGE]->(m:Message {role:'agent'})
        OPTIONAL MATCH (s)-[:FOR]->(t:Task)
        RETURN a.id, t.id, m.content, m.ts ORDER BY m.ts DESC LIMIT 200""", {"run": run})
    out = {}
    for agent, task, content, ts in rows:
        lst = out.setdefault(agent, [])
        if len(lst) >= 6:
            continue
        text, calls = content, []
        try:
            body = json.loads(content)
            text, calls = body.get("text") or "", body.get("calls") or []
        except ValueError:
            pass
        lst.append({"ts": ts, "task": task, "text": text[:400], "calls": [
            {"name": c.get("name"), "args": {k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:160])
                                             for k, v in (c.get("arguments") or {}).items()}} for c in calls[:3]]})
    return {"agents": out}


@app.get("/api/memories")
def api_memories(repo: str = None):
    repo = repo or os.environ.get("DEFAULT_REPO", "tinydb")
    g = graph(repo)
    rows = schema.q(g, """MATCH (m:Memory)
                          OPTIONAL MATCH (m)-[:LEARNED_FROM]->(t:Task)
                          OPTIONAL MATCH (m)-[:SUPERSEDED_BY]->(n:Memory)
                          OPTIONAL MATCH (m)-[:ABOUT]->(f:Function)
                          RETURN m.id, m.kind, m.summary, m.status, m.uses, m.successes, m.created_at, t.id,
                                 n.id, m.retired_reason, collect(DISTINCT f.file), m.origin
                          ORDER BY m.created_at DESC""")
    return {"memories": [{"id": r[0], "kind": r[1], "summary": r[2], "status": r[3], "uses": r[4], "successes": r[5],
                          "created_at": r[6], "task": r[7], "superseded_by": r[8], "retired_reason": r[9],
                          "files": sorted(r[10] or []), "origin": r[11] or "learned"} for r in rows]}


ui_dir = os.path.join(HERE, "ui")


@app.get("/")
def index():
    return FileResponse(os.path.join(ui_dir, "index.html"))


app.mount("/", StaticFiles(directory=ui_dir), name="ui")
