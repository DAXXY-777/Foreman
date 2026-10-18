"""Create the 10 tickets in the graph, compute each ticket's weighted footprint, score every pair, mark
conflicts, and (EVALUATION ONLY, optional) compare against the answer key.

    python tune_conflicts.py                       just do it and show the conflicts
    python tune_conflicts.py --key bugs.json       also check against the answer key (never give workers this file)
    python tune_conflicts.py --threshold 0.08      try a different cut-off
"""
import argparse
import itertools
import json

import schema
from schema import q

ap = argparse.ArgumentParser()
ap.add_argument("--repo-name", default="tinydb")
ap.add_argument("--tickets", default="tickets.json")
ap.add_argument("--key")
ap.add_argument("--threshold", type=float, default=schema.CONFLICT_THRESHOLD)
args = ap.parse_args()
RUN = "tune-run"

g = schema.connect().select_graph(schema.graph_name(args.repo_name))
q(g, "MATCH (t:Task {run_id:$run}) DETACH DELETE t", {"run": RUN})
tickets = json.load(open(args.tickets))
for t in tickets:
    linked = q(g, schema.CREATE_TASK, {"run": RUN, "id": t["ticket_id"], "ticket_id": t["ticket_id"], "title": t["title"],
                                       "description": t["description"], "tests": t["failing_tests"]})
    assert linked and linked[0][0] == len(t["failing_tests"]), (t["ticket_id"], linked)
    schema.write_footprint(g, RUN, t["ticket_id"])
print(f"{len(tickets)} tickets created and footprints written")

score = {}
for t in tickets:
    for other, status, s, shared in q(g, schema.OVERLAP, {"run": RUN, "id": t["ticket_id"]}):
        score[frozenset((t["ticket_id"], other))] = (s, shared)
order = sorted(score, key=lambda k: -score[k][0])
print(f"\nTop pairs by overlap (cut-off {args.threshold}):")
for k in order[:8]:
    s, shared = score[k]
    top = sorted(shared, key=lambda x: -x[1])[:2]
    print(f"  {'+'.join(sorted(k)):12s} {s:.3f} {'CONFLICT' if s >= args.threshold else '        '}  "
          f"main shared code: {[x[0].split('tinydb.')[1] for x in top]}")
n = 0
for k in score:
    s, shared = score[k]
    if s >= args.threshold:
        a, b = sorted(k)
        q(g, schema.MARK_CONFLICT, {"run": RUN, "a": a, "b": b, "score": s, "shared": [x[0] for x in shared]})
        n += 1
print(f"\n{n} of {len(score)} ticket pairs marked as conflicting")

if args.key:
    key = json.load(open(args.key))["bugs"]
    tid_of = {b["id"]: b["ticket_id"] for b in key}
    fix = {b["ticket_id"]: b["fix_functions"][0] for b in key}
    intended = {frozenset((tid_of[b["id"]], tid_of[o])) for b in key for o in b["intended_overlaps_with"]}
    print("\n--- check against the answer key (evaluation only) ---")
    for k in sorted(intended, key=lambda k: sorted(k)):
        s, _ = score[k]
        print(f"  intended pair {'+'.join(sorted(k)):12s} score {s:.3f}  rank {order.index(k) + 1} of {len(order)}  "
              f"{'found' if s >= args.threshold else 'NOT found'}")
    extra = [k for k in score if score[k][0] >= args.threshold and k not in intended]
    print(f"  extra pairs flagged that the key did not intend: {len(extra)}")
    inside = 0
    for tid, fn in fix.items():
        hit = q(g, "MATCH (:Task {run_id:$run, id:$id})-[r:TOUCHES]->(f:Function {id:$fn}) RETURN r.weight",
                {"run": RUN, "id": tid, "fn": fn})
        inside += bool(hit)
    print(f"  the real fix function is inside the footprint: {inside} of {len(fix)} tickets")
