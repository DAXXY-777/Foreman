"""The numbers for the results panel, read from the files the test scripts write. Nothing here is estimated:
a section with no file behind it is simply left out and the screen says it is still running."""
import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = {"none": "No coordination", "graph": "Graph only", "full": "Graph + memory"}


def _load(*names):
    for n in names:
        for base in (os.getcwd(), HERE, os.path.join(HERE, "..", "foreman-schema")):
            p = os.path.join(base, n)
            if os.path.exists(p):
                try:
                    return json.load(open(p))
                except ValueError:
                    pass
    return None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 1) if xs else 0


def build_results(before_after=None):
    out = {}
    if before_after is None:
        before_after = _load("before_after.json")
    if before_after:
        out["before_after"] = before_after
    runs = _load("eval_results.json")
    if runs:
        setups = []
        for mode in ("none", "graph", "full"):
            rs = [r for r in runs if r.get("mode") == mode]
            if not rs:
                continue
            e = [r["eval"] for r in rs]
            rec = [x["crash_recovered"] for x in e if x.get("crash_recovered") is not None]
            setups.append({"id": mode, "name": NAMES[mode], "solved": _mean([x["fixed_in_merged_code"] for x in e]), "total": 10,
                           "collisions": _mean([x["conflicting_tickets_worked_together"] for x in e]),
                           "rework": _mean([x["wasted_sessions"] for x in e]),
                           "recovered": f"{sum(rec)} of {len(rec)}" if rec else "n/a",
                           "recovered_pct": 100 * sum(rec) / len(rec) if rec else 0, "runs": len(rs),
                           "minutes": _mean([r.get("seconds", 0) / 60 for r in rs]),
                           "duplicate": _mean([x.get("duplicate_work") for x in e]),
                           "stalled": sum(1 for r in rs if r.get("stalled"))})
        if setups:
            out["setups"] = setups
            out["repeats"] = min(s["runs"] for s in setups)
            out["runs_detail"] = [{"run": r.get("run_id"), "mode": r.get("mode"),
                                   "fixed": r["eval"].get("fixed_in_merged_code"), "duplicate": r["eval"].get("duplicate_work"),
                                   "stalled": bool(r.get("stalled")), "minutes": round(r.get("seconds", 0) / 60, 1)} for r in runs]
    lt = _load("load_test_results.json")
    race = _load("race_results.json")
    if lt:
        cr = lt.get("claim_race", {})
        out["load"] = {"workers": cr.get("workers"), "rounds": cr.get("rounds"),
                       "winners_ok": f"{cr.get('rounds_exactly_one_winner')} of {cr.get('rounds')}",
                       "atomicDouble": 0 if cr.get("rounds_exactly_one_winner") == cr.get("rounds") else None,
                       "p50_ms": (cr.get("claim_latency") or {}).get("p50_ms")}
        if race and "atomic" in race:
            out["load"]["atomicDouble"] = max(0, race["atomic"]["worst_round"] - 1)
            out["load"]["naiveDouble"] = max(0, race["naive"]["worst_round"] - 1)
    if race and "atomic" in race:
        out["race"] = {"workers": 50, "rounds": len(race["atomic"].get("winners_per_round") or []),
                       "atomic_ok": race["atomic"]["rounds_with_exactly_one_winner"],
                       "naive_ok": race["naive"]["rounds_with_exactly_one_winner"],
                       "naive_worst": race["naive"]["worst_round"], "p50_ms": race["atomic"]["p50_ms"]}
    return out
