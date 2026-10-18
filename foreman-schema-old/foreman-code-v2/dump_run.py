"""Prints what the agents did on one ticket, as plain text you can paste into chat.

    python3 dump_run.py --task T-01 [--run run-1] [--repo tinydb] [--last 60]
"""
import argparse
import json

import _paths  # noqa: F401
import schema
from graph_api import GraphAPI


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--run", default="run-1")
    ap.add_argument("--repo", default="tinydb")
    ap.add_argument("--last", type=int, default=60, help="only the last N messages of each conversation")
    a = ap.parse_args()
    api = GraphAPI(a.repo, a.run, ensure_schema=False)
    g = schema.connect().select_graph(schema.graph_name(a.repo))
    rows = schema.q(g, """MATCH (ag:Agent)-[:HAS_SESSION]->(s:Session)-[:FOR]->(t:Task {run_id:$run, id:$task})
                          RETURN s.id, ag.id, ag.role, s.started_at ORDER BY s.started_at""", {"run": a.run, "task": a.task})
    if not rows:
        print("no conversations found for", a.task, "in", a.run)
        return
    for sid, agent, role, started in rows:
        msgs = api.get_history(sid)["messages"]
        print(f"\n===== {agent} ({role}) started {started}, {len(msgs)} messages =====")
        for m in msgs[-a.last:]:
            body = m["content"]
            if m["role"] == "agent":
                try:
                    d = json.loads(body)
                    calls = "; ".join(f"{c['name']}({json.dumps(c['arguments'])[:300]})" for c in d.get("calls", []))
                    body = (d.get("text") or "") + (" CALL " + calls if calls else "")
                except ValueError:
                    pass
            elif m["role"] == "tool":
                try:
                    d = json.loads(body)
                    r = json.dumps(d.get("result"))[:400]
                    body = f"{d.get('name')} -> {r}"
                except ValueError:
                    body = body[:400]
            else:
                body = body[:1500]
            print(f"[{m['seq']}] {m['role']}: {body}")


if __name__ == "__main__":
    main()
