"""Saves one real run from the graph as a replay file the screen can play without any database.

    python export_replay.py --repo tinydb --run run-5          # writes ui/replay/run-5.json, ui/replay/index.json, ui/results.json

Run it on the machine where the run happened (it needs the FalkorDB that holds the run). Put the three files in the
deployed ui folder and the public screen replays that run for everyone, with no database and no model key needed.
Optional: before_after.json next to this script, e.g.
  [{"label": "Run 4, before our fixes", "approved": 6, "total": 10, "seconds": 5406},
   {"label": "Run 5, after", "approved": 10, "total": 10, "seconds": 1772}]
"""
import argparse
import json
import os
from datetime import datetime, timezone

import _paths  # noqa: F401
from results import build_results
from show import build_show


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="tinydb")
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui"))
    a = ap.parse_args()
    show = build_show(a.repo, a.run)
    if not show.get("ok"):
        raise SystemExit("nothing to export: " + str(show.get("error")))
    show["recorded"] = datetime.now(timezone.utc).strftime("%d %b %Y")
    os.makedirs(os.path.join(a.out, "replay"), exist_ok=True)
    path = os.path.join(a.out, "replay", a.run + ".json")
    json.dump(show, open(path, "w"), separators=(",", ":"))
    idx_path = os.path.join(a.out, "replay", "index.json")
    runs = []
    try:                                              # keep the runs already saved; the newest one becomes the default
        runs = [r for r in json.load(open(idx_path)).get("runs", []) if r != a.run + ".json" and os.path.exists(os.path.join(a.out, "replay", r))]
    except Exception:
        pass
    json.dump({"default": a.run + ".json", "runs": runs + [a.run + ".json"]}, open(idx_path, "w"))
    res = build_results()
    json.dump(res, open(os.path.join(a.out, "results.json"), "w"), indent=1)
    print(f"saved {path} ({os.path.getsize(path) // 1024} KB): {show['approved']} of {show['tasks_total']} approved, "
          f"{len(show['beats'])} moments, {show['duration']} s long")
    print("results.json has:", ", ".join(res) or "nothing yet")


if __name__ == "__main__":
    main()
