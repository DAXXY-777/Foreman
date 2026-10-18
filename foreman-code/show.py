"""Turns one run in the graph into a "show": a list of timed beats the screen can play, plus the graph to draw.

    from show import build_show
    show = build_show("tinydb", "run-5")          # a dict, ready for json.dump

The same function feeds the live screen (server.py, /api/show) and the saved replay (export_replay.py), so the replay
is exactly what the live screen would have shown. It only READS from the graph.

Every beat comes from something really stored in the graph: a decision (claim, release, expire, review, order,
footprint), a submitted result, a handoff note, a saved lesson, a recall, or an agent's own message. Nothing is invented.
Two things are worked out from graph facts instead of read directly:
  * "held back": when a worker takes a ticket, every open ticket that shares code with it (CONFLICTS_WITH) must wait.
  * "recall": the RECALLED link has no time on it, so it is shown at the moment the worker took that ticket.
"""
import json
import re
from datetime import datetime

import _paths  # noqa: F401
import schema
from graph_api import GraphAPI
from personas import FOREMAN_NAME, HISTORIAN_NAME, REVIEWER_NAME, worker_name

STAGE = {"reviewer": "rev", "historian": "hist"}          # real agent id -> id used by the screen
q = schema.q


def stage(a):
    return STAGE.get(a, a)


def secs(ts, t0):
    return round((datetime.fromisoformat(ts.replace("Z", "+00:00")) - t0).total_seconds(), 1)


def short(text, n=78):
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def fn_label(fid):
    parts = fid.split(".")
    return (parts[-2] + "." + parts[-1] if len(parts) > 2 and parts[-2][:1].isupper() else parts[-1]) + "()"


def node_label(fid):
    name = fid.split(".")[-1]
    if name.startswith("__") and fid.count(".") > 2:
        name = fid.split(".")[-2] + "." + name
    name = name + "()"
    return name if len(name) <= 16 else name[:15] + "…"


def plain(text):
    """Graph summary -> sentence for people: names instead of ids, no internal scores."""
    text = re.sub(r"tinydb(?:\.\w+)+", lambda m: fn_label(m.group(0)), text)
    text = re.sub(r"\s*\(score [\d.]+, \w+\)", "", text)
    text = re.sub(r"\bw(\d+)\b", lambda m: worker_name("w" + m.group(1)), text)
    text = text.replace("reviewer ", REVIEWER_NAME + " ")
    return text


def memory_text(summary):
    return re.sub(r"\s*\|\s*", " · ", summary or "")


def crew_names(workers):
    c = {"foreman": {"name": FOREMAN_NAME, "role": "Foreman"}, "rev": {"name": REVIEWER_NAME, "role": "Reviewer"},
         "hist": {"name": HISTORIAN_NAME, "role": "Historian"}}
    for w in workers:
        c[w] = {"name": worker_name(w), "role": "Worker"}
    return c


def layout(tasks, funcs, mems, comps, links):
    """x,y for every node: tickets in a row (clashing ones side by side), functions under the tickets they belong to."""
    order = sorted(tasks, key=lambda t: (comps[t], t))
    pos = {t: (60 + i * 92, 50) for i, t in enumerate(order)}
    lane = {}
    xs = {}
    for f in funcs:
        near = [pos[t][0] for t in links.get(f, []) if t in pos]
        xs[f] = sum(near) / len(near) if near else 60
    for row, fl in enumerate([sorted(funcs, key=lambda f: xs[f])]):
        pass
    fl = sorted(funcs, key=lambda f: xs[f])
    last = [-1e9] * 3
    for i, f in enumerate(fl):                               # three staggered rows so labels do not collide
        r = i % 3
        x = max(xs[f], last[r] + 112)
        last[r] = x
        pos[f] = (x, 135 + r * 58)
    ml = sorted(mems, key=lambda m: sum(pos[n][0] for n in links.get(m, []) if n in pos) / max(1, len([n for n in links.get(m, []) if n in pos])))
    last = -1e9
    for m in ml:
        near = [pos[n][0] for n in links.get(m, []) if n in pos]
        x = max(sum(near) / len(near) if near else 60, last + 92)
        last = x
        pos[m] = (x, 330)
    return pos


def build_show(repo, run, api=None, with_explain=True):
    api = api or GraphAPI(repo, run, ensure_schema=False)
    g = api.g
    R = {"r": run}

    # ---- the tickets -------------------------------------------------------------------------------------
    tasks = {}
    for tid, title, status, by, attempts in q(g, "MATCH (t:Task {run_id:$r}) RETURN t.id, t.title, t.status, t.claimed_by, t.attempt_count", R):
        tasks[tid] = {"title": title, "status": status, "attempts": attempts or 0}
    conflicts = {}
    for a, b, s in q(g, "MATCH (a:Task {run_id:$r})-[c:CONFLICTS_WITH]->(b:Task) RETURN a.id, b.id, c.score", R):
        conflicts.setdefault(a, {})[b] = s
        conflicts.setdefault(b, {})[a] = s
    touches = {}
    for tid, fid, w in q(g, "MATCH (t:Task {run_id:$r})-[x:TOUCHES]->(f:Function) WHERE x.weight > 0 RETURN t.id, f.id, x.weight", R):
        touches.setdefault(tid, []).append((w, fid))
    users = {}
    for tid, fl in touches.items():
        for _, f in fl:
            users.setdefault(f, set()).add(tid)
    wt = {(t, f): w for t, fl in touches.items() for w, f in fl}
    shown = set()                                           # functions to draw: each ticket's top 2, and where clashing tickets meet
    for tid, fl in touches.items():
        shown |= {f for _, f in sorted(fl, reverse=True)[:2]}
    meet = {}                                               # (ticket, ticket) -> the function where they meet
    for a, row in conflicts.items():
        for b in row:
            if a < b:
                common = [(min(wt[(a, f)], wt[(b, f)]), f) for f in users if a in users[f] and b in users[f]]
                if common:
                    meet[(a, b)] = max(common)[1]
    shown |= set(meet.values())

    # union-find on conflicts so clashing tickets sit together
    comp = {t: t for t in tasks}

    def find(x):
        while comp[x] != x:
            comp[x] = comp[comp[x]]
            x = comp[x]
        return x
    for a, row in conflicts.items():
        for b in row:
            if a in comp and b in comp:
                comp[find(a)] = find(b)
    comps = {t: find(t) for t in tasks}
    sizes = {}
    for t, c in comps.items():
        sizes[c] = sizes.get(c, 0) + 1
    comps = {t: (-sizes[c], c) for t, c in comps.items()}

    # ---- decisions, results, handoffs, memory ------------------------------------------------------------
    dec = q(g, """MATCH (d:Decision {run_id:$r})
                  OPTIONAL MATCH (d)-[:ABOUT]->(t:Task)
                  OPTIONAL MATCH (d)-[:MADE_BY]->(a:Agent)
                  RETURN d.id, d.kind, d.ts, d.summary, d.approved, d.reason_codes, head(collect(t.id)), head(collect(a.id))""", R)
    res = q(g, """MATCH (x:Result {run_id:$r}) OPTIONAL MATCH (x)-[:SUBMITTED_BY]->(a:Agent)
                  RETURN x.id, x.ts, x.task_id, x.summary, head(collect(a.id))""", R)
    hands = q(g, """MATCH (h:Handoff {run_id:$r}) OPTIONAL MATCH (a:Agent)-[:WROTE]->(h)
                    RETURN h.id, h.ts, h.task_id, h.done, head(collect(a.id))""", R)
    mems = q(g, """MATCH (m:Memory) WHERE coalesce(m.origin, '') <> 'playbook'
                   OPTIONAL MATCH (m)-[:LEARNED_FROM]->(t:Task {run_id:$r})
                   RETURN m.id, m.kind, m.summary, m.created_at, m.uses, m.successes, head(collect(t.id))""", R)
    recalled = {}
    for tid, mid in q(g, "MATCH (t:Task {run_id:$r})-[:RECALLED]->(m:Memory) RETURN t.id, m.id", R):
        recalled.setdefault(tid, []).append(mid)
    stamps = [d[2] for d in dec] + [x[1] for x in res] + [h[1] for h in hands]
    if not stamps:
        return {"ok": False, "error": "no_activity"}
    t0 = min(datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps)
    tfin = max(datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps + [m[3] for m in mems if m[6]])
    duration = round((tfin - t0).total_seconds() + 3, 1)

    workers = sorted({d[7] for d in dec if d[7] and d[7].startswith("w")} | {x[4] for x in res if x[4]})
    workers = [w for w in workers if w and w.startswith("w")] or ["w1", "w2", "w3"]

    # ---- the beats ---------------------------------------------------------------------------------------
    beats = []
    holder = {}                                             # ticket -> worker now holding it
    last_owner = {}                                         # ticket -> worker who last held it (for takeover)
    handed = {h[2]: (secs(h[1], t0), h[4]) for h in hands}
    done = set()

    def beat(t, acts=None, claims=None, log=None, say=None, mem=None, hl=None, did=None, **extra):
        b = {"t": t, "acts": acts or [], "claims": claims or [], "log": log, "say": say, "mem": mem, "hl": hl, "did": did}
        b.update(extra)
        beats.append(b)

    def title(t):
        return short(tasks.get(t, {}).get("title", t), 46)

    fp_times = sorted(secs(d[2], t0) for d in dec if d[1] == "footprint")
    if fp_times:
        beat(fp_times[0], [{"a": "foreman", "s": "planning", "b": f"Mapping what {len(fp_times)} tickets touch…"}],
             log={"call": "footprint ×" + str(len(fp_times)), "res": "each ticket's failing tests mapped to the code they run", "k": "info"},
             say=f"{FOREMAN_NAME} maps which code each of the {len(tasks)} tickets touches.")
    for did, kind, ts, summary, approved, codes, tid, agent in sorted(dec, key=lambda d: d[2]):
        t = secs(ts, t0)
        a = stage(agent) if agent else None
        if kind == "order":
            clash = sorted({(x, y) for x, row in conflicts.items() for y in row if x < y})
            beat(t, [{"a": "foreman", "s": "planning", "b": f"{len(clash)} clashing pairs. Those go first."}],
                 log={"call": "set_task_order()", "res": short(summary.replace("task order set: ", "order: "), 120), "k": "ok"},
                 say=f"{FOREMAN_NAME} sets the order. Tickets that share code start first, so they never wait at the end.", did=did)
        elif kind == "claim" and tid:
            w = stage(agent)
            again = tid in last_owner and tid in handed
            holder[tid] = w
            acts = [{"a": w, "s": "takeover" if again else "working", "task": tid,
                     "b": (f"Reading {worker_name(last_owner[tid])}'s note" if again else f"Fixing {tid}: {title(tid)}")}]
            held = [o for o in conflicts.get(tid, {}) if tasks.get(o) and o not in done and o not in holder]
            if held:
                acts.append({"a": "foreman", "s": "idle", "b": f"{', '.join(sorted(held))} wait{'s' if len(held) == 1 else ''} for {tid}"})
            beat(t, acts, claims=[{"task": tid, "agent": w, "mode": "held"}],
                 log={"call": f"claim_task({tid}, {agent})", "res": "claimed" + (f". Waiting for it: {', '.join(sorted(held))}" if held else ""), "k": "ok"},
                 say=(f"{worker_name(agent)} takes {tid}. " + (f"{', '.join(sorted(held))} share code with it, so they must wait." if held else "No other ticket shares its code.")),
                 hl={"nodes": [tid] + held, "edges": [], "kind": "accent"} if held else None, did=did, held=held)
            if again:
                beat(round(t + 5, 1), [{"a": w, "s": "working", "task": tid, "b": f"Fixing {tid}: {title(tid)}"}])
            for mid in recalled.get(tid, []):
                pass
            if recalled.get(tid):
                beat(round(t + 1.5, 1), [{"a": w, "s": "recalling", "task": tid, "b": f"{len(recalled[tid])} saved lesson{'s' if len(recalled[tid]) > 1 else ''} for {tid}"},
                                         {"a": "hist", "s": "recalling", "b": f"Handing {worker_name(agent)} {len(recalled[tid])} lesson{'s' if len(recalled[tid]) > 1 else ''}"}],
                     log={"call": f"recall_similar({tid})", "res": f"{len(recalled[tid])} lessons, ranked by relevance and how often they helped", "k": "mem"},
                     say=f"The Historian already knows this kind of fix. {len(recalled[tid])} lesson{'s' if len(recalled[tid]) > 1 else ''} go to {worker_name(agent)}.",
                     hl={"nodes": [tid] + recalled[tid], "edges": [], "kind": "accent"})
                beat(round(t + 6, 1), [{"a": "hist", "s": "idle", "b": ""}])
        elif kind == "expire" and tid:
            w = stage(holder.pop(tid, None) or agent)
            last_owner[tid] = w
            beat(t, [{"a": w, "s": "dead", "task": tid, "b": "No heartbeat"}, {"a": "foreman", "s": "idle", "b": f"{tid} is open again"}],
                 claims=[{"task": tid, "agent": w, "mode": "dead"}],
                 log={"call": f"expire_claims()", "res": short(summary, 110), "k": "bad"},
                 say=f"{worker_name(w)} stopped sending heartbeats. The graph reopens {tid} and keeps the handoff note.", did=did)
            beat(round(t + 2.5, 1), [], claims=[{"task": tid, "agent": None}])
        elif kind == "release" and tid:
            w = stage(holder.pop(tid, None) or agent)
            last_owner[tid] = w
            beat(t, [{"a": w, "s": "idle", "task": "", "b": f"Gave up {tid}, note saved"}], claims=[{"task": tid, "agent": None}],
                 log={"call": f"release_task({tid})", "res": short(summary, 110), "k": "warn"},
                 say=f"{worker_name(w)} ran out of steps on {tid} and handed it back with a note.", did=did)
        elif kind == "review" and tid:
            w = holder.get(tid) or stage(agent)
            if approved:
                done.add(tid)
                holder.pop(tid, None)
                beat(t, [{"a": "rev", "s": "approved", "b": f"{tid} passes"}, {"a": w, "s": "approved", "task": "", "b": f"{tid} approved"}],
                     claims=[{"task": tid, "agent": None}],
                     log={"call": f"review_decision({tid}, approved)", "res": "tests pass, patch stays in its footprint", "k": "ok"},
                     say=f"{REVIEWER_NAME} approves {tid}.", did=did, approved=tid)
                beat(round(t + 4, 1), [{"a": "rev", "s": "idle", "b": ""}, {"a": w, "s": "idle", "b": ""}])
            else:
                holder.pop(tid, None)
                last_owner[tid] = w
                why = ", ".join(codes or []) or "rejected"
                beat(t, [{"a": "rev", "s": "rejecting", "b": f"Not yet: {why}"}, {"a": w, "s": "sentback", "task": tid, "b": f"{tid} sent back"}],
                     claims=[{"task": tid, "agent": None}],
                     log={"call": f"review_decision({tid}, rejected)", "res": why, "k": "warn"},
                     say=f"{REVIEWER_NAME} sends {tid} back: {why}.", did=did)
        elif kind not in ("footprint", "claim", "review"):
            pass
    for rid, ts, tid, summary, agent in res:
        t = secs(ts, t0)
        w = stage(agent) if agent else holder.get(tid)
        beat(t, [{"a": w, "s": "waiting", "task": tid, "b": f"{tid} in review"}, {"a": "rev", "s": "reviewing", "b": f"Checking {tid}"}] if w else [],
             log={"call": f"submit_result({tid})", "res": short(summary, 110), "k": "info"}, say=None)
    recovered_tasks = {d[6] for d in dec if d[1] in ("expire", "release")}
    for hid, ts, tid, note, agent in hands:
        if tid not in recovered_tasks:
            continue
        beat(secs(ts, t0), [], log={"call": f"write_handoff({tid})", "res": short(note, 120), "k": "info"})
    by_task = {}
    for mid, kind, summary, created, uses, succ, tid in sorted(mems, key=lambda m: m[3]):
        if tid:
            by_task.setdefault(tid, []).append((mid, kind, summary, created))
    for tid, items in by_task.items():
        t = secs(items[0][3], t0)
        kinds = sorted({i[1] for i in items})
        beat(t, [{"a": "hist", "s": "saved", "b": f"{len(items)} lesson{'s' if len(items) > 1 else ''} saved from {tid}"}], mem=[i[0] for i in items],
             log={"call": f"record_outcome({tid}) ×{len(items)}", "res": short(memory_text(items[0][2]), 120), "k": "mem"},
             say=f"{HISTORIAN_NAME} saves {len(items)} lesson{'s' if len(items) > 1 else ''} ({', '.join(kinds)}) from {tid}.")
        beat(round(t + 3, 1), [{"a": "hist", "s": "idle", "b": ""}])
    approved_n = sum(1 for v in tasks.values() if v["status"] == "approved")
    beat(duration - 1, [{"a": "foreman", "s": "explaining", "b": f"{approved_n} of {len(tasks)} approved"}],
         log={"call": "run finished", "res": f"{approved_n} of {len(tasks)} tickets approved", "k": "ok"},
         say=f"Done: {approved_n} of {len(tasks)} tickets approved.", finished=True)
    beats.sort(key=lambda b: b["t"])

    # ---- messages: what agents actually said, shown as speech while they work -----------------------------
    msgs = q(g, """MATCH (a:Agent)-[:HAS_SESSION]->(s:Session {run_id:$r})-[:HAS_MESSAGE]->(m:Message {role:'agent'})
                   RETURN a.id, m.ts, m.content ORDER BY m.ts""", R)
    lastsay = {}
    talk = []
    for agent, ts, content in msgs:
        try:
            text = json.loads(content).get("text") or ""
        except ValueError:
            text = content or ""
        text = short(text.split("\n")[0], 82)
        a = stage(agent)
        if len(text) < 12 or agent in ("foreman", "historian"):
            continue
        t = secs(ts, t0)
        if t - lastsay.get(a, -99) < 25:
            continue
        lastsay[a] = t
        talk.append({"t": t, "acts": [{"a": a, "say": text}], "claims": [], "log": None, "say": None, "mem": None, "hl": None, "did": None})
    beats = sorted(beats + talk, key=lambda b: b["t"])

    # ---- the graph to draw -------------------------------------------------------------------------------
    recalled_ids = {m for ms in recalled.values() for m in ms}
    memo = [m for m in mems if m[0] in recalled_ids]
    mlink = {}
    for mid, fid in q(g, "MATCH (m:Memory)-[:ABOUT]->(f:Function) RETURN m.id, f.id"):
        mlink.setdefault(mid, []).append(fid)
    links = {f: [t for t in users.get(f, ()) if t in tasks] for f in shown}
    for m in memo:
        links[m[0]] = [f for f in mlink.get(m[0], []) if f in shown] + ([m[6]] if m[6] else [])
    pos = layout(list(tasks), sorted(shown), [m[0] for m in memo], comps, links)
    nodes = [{"id": t, "k": "task", "label": t, "sub": title(t), "x": pos[t][0], "y": pos[t][1]} for t in sorted(tasks, key=lambda t: pos[t][0])]
    nodes += [{"id": f, "k": "code", "label": node_label(f), "sub": f.split(".")[1] + ".py" if f.count(".") > 1 else "", "x": pos[f][0], "y": pos[f][1],
               "pos": "down", "full": f} for f in sorted(shown, key=lambda f: pos[f][0])]
    mt = {"episodic": "episodic", "semantic": "semantic", "procedural": "procedural"}
    nodes += [{"id": m[0], "k": "mem", "mk": mt.get(m[1], "semantic"), "label": m[0], "x": pos[m[0]][0], "y": pos[m[0]][1]} for m in memo]
    edges = []
    for tid, fl in touches.items():
        for w, f in fl:
            if f in shown:
                edges.append([f"{tid}>{f}", tid, f, "touches"])
    for a, b in q(g, "MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.id, b.id"):
        if a in shown and b in shown:
            edges.append([f"{a}>{b}", a, b, "calls"])
    for m in memo:
        for f in mlink.get(m[0], []):
            if f in shown:
                edges.append([f"{m[0]}>{f}", m[0], f, "fixes"])
        if m[6] and m[6] in tasks:
            edges.append([f"{m[0]}>{m[6]}", m[0], m[6], "from"])
    seen = set()
    for a, row in conflicts.items():
        for b in row:
            if a < b and (a, b) not in seen:
                seen.add((a, b))
                edges.append([f"{a}~{b}", a, b, "clash"])
    eids = {e[0] for e in edges}

    # ---- "ask the graph why" -----------------------------------------------------------------------------
    explain = {}
    if with_explain:
        want = [d for d in dec if d[1] in ("review", "expire", "release", "order")
                and not (d[1] == "review" and d[4])]
        claims_with_clash = [d for d in dec if d[1] == "claim" and d[6] and conflicts.get(d[6])]
        want += claims_with_clash[:4]
        for did, kind, ts, summary, approved, codes, tid, agent in sorted(want, key=lambda d: d[2])[:14]:
            ex = api.explain(did)
            if not ex.get("ok"):
                continue
            label = {n["id"]: n["label"] for n in ex["path"]["nodes"]}
            steps = []
            for e in ex["path"]["edges"]:
                if e["type"] in ("MADE_BY",) or e["from"] == did:
                    continue
                a, b = e["from"], e["to"]
                if e["type"] == "CONFLICTS_WITH":
                    k = (a, b) if a < b else (b, a)
                    f = meet.get(k)
                    if f and f in {n["id"] for n in nodes}:
                        steps.append({"t": f"{a} and {b} both touch {fn_label(f)}, so they cannot run together.",
                                      "nodes": [a, f, b], "edges": [x[0] for x in edges if (x[1], x[2]) in ((a, f), (b, f), (f, a), (f, b))]})
                        continue
                if (a not in {n["id"] for n in nodes}) or (b not in {n["id"] for n in nodes}):
                    continue
                eid = next((x[0] for x in edges if x[1] == a and x[2] == b), None) or next((x[0] for x in edges if x[1] == b and x[2] == a), None)
                verb = {"TOUCHES": "touches", "CONFLICTS_WITH": "shares code with", "RECALLED": "was handed lesson"}.get(e["type"], e["type"].lower())
                if e["type"] == "TOUCHES" and sum(1 for x in steps if x["t"].startswith(a + " touches")) >= 2:
                    continue
                label_b = fn_label(b) if b in shown else b
                steps.append({"t": f"{a} {verb} {label_b}.", "nodes": [a, b], "edges": [eid] if eid else []})
            qtext = {"review": f"Why was {tid} sent back?", "expire": f"Why did {tid} reopen?", "release": f"Why did {tid} change hands?",
                     "order": "Why this order?", "claim": f"Why did {worker_name(agent)} get {tid}?"}.get(kind, f"Why {kind}?")
            who = {"review": "rev", "order": "foreman", "claim": "foreman", "expire": "foreman", "release": "foreman"}[kind]
            explain[did] = {"q": qtext, "who": who, "t": secs(ts, t0), "steps": steps[:7], "a": plain(ex["summary"])}
    # ---- memory list -------------------------------------------------------------------------------------
    memory = [{"id": m[0], "kind": m[1], "text": memory_text(m[2]), "by": f"{HISTORIAN_NAME}" + (f", from {m[6]}" if m[6] else ""),
               "t": secs(m[3], t0) if m[6] else 0, "uses": m[4] or 0, "helped": m[5] or 0} for m in mems]
    plan = next(({"summary": d[3]} for d in dec if d[1] == "order"), None)
    return {"ok": True, "repo": repo, "run": run, "graph": schema.graph_name(repo), "duration": duration,
            "crew": crew_names(workers), "tasks": tasks, "conflicts": {a: sorted(r) for a, r in conflicts.items()},
            "beats": beats, "graph_nodes": nodes, "graph_edges": edges, "explain": explain, "memory": memory,
            "metrics": api.get_run_metrics(), "plan": plan, "tasks_total": len(tasks), "approved": approved_n}


if __name__ == "__main__":
    import sys
    s = build_show(sys.argv[1] if len(sys.argv) > 1 else "tinydb", sys.argv[2] if len(sys.argv) > 2 else "run-1")
    print(json.dumps(s, indent=1)[:3000])
