"""Checks that a finished run in the graph turns into a sensible show for the screen (needs the database, no model key).

    python test_show.py [--repo tinydb] [--run RUN]     (default: the newest run)
"""
import argparse

import _paths  # noqa: F401
import schema
from show import build_show

ap = argparse.ArgumentParser()
ap.add_argument("--repo", default="tinydb")
ap.add_argument("--run")
a = ap.parse_args()
g = schema.connect().select_graph(schema.graph_name(a.repo))
run = a.run or schema.q(g, "MATCH (d:Decision) RETURN d.run_id, max(d.ts) AS m ORDER BY m DESC LIMIT 1")[0][0]
s = build_show(a.repo, run)
bad = []


def check(c, name):
    print(("PASS  " if c else "FAIL  ") + name)
    if not c:
        bad.append(name)


B = s["beats"]
check(s["ok"] and len(B) > 10, f"{run}: {len(B)} moments")
check(all(B[i]["t"] <= B[i + 1]["t"] for i in range(len(B) - 1)), "moments are in time order")
check(sum(1 for b in B for c in b["claims"] if c.get("mode") == "held") >= s["tasks_total"], "every ticket was claimed on screen")
check(sum(1 for b in B if b.get("approved")) == s["approved"], "approved moments match the approved tickets")
ids = {n["id"] for n in s["graph_nodes"]}
check(all(e[1] in ids and e[2] in ids for e in s["graph_edges"]), "every graph edge joins two drawn nodes")
check(all(n in ids for d in s["explain"].values() for st in d["steps"] for n in st["nodes"]), "every 'why' step points at a drawn node")
check(any(b.get("held") for b in B) or not s["conflicts"], "clashing tickets are shown as held back")
print("\nALL PASSED" if not bad else f"\n{len(bad)} FAILED")
raise SystemExit(1 if bad else 0)
