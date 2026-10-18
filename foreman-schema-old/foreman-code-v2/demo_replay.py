"""Fills the graph with a full pretend run (no model, no key) so the live board has something to show.
Slow on purpose, so you can watch it. Nothing is cleaned up afterwards.

    python demo_replay.py --seeded <seeded tinydb folder> --kit ../a2-eval-kit [--delay 0.6]
"""
import argparse
import json
import os
import tempfile
import time

import _paths  # noqa: F401
from agents import Config
from run_crew import run_crew
from test_crew import Pretend


class Slow(Pretend):
    def __init__(self, *a, delay=0.6, **k):
        super().__init__(*a, **k)
        self.delay = delay

    def chat(self, *a, **k):
        time.sleep(self.delay)
        return super().chat(*a, **k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeded", required=True)
    ap.add_argument("--kit", required=True)
    ap.add_argument("--delay", type=float, default=0.6)
    ap.add_argument("--run-id", default="demo")
    a = ap.parse_args()
    tickets = json.load(open(os.path.join(a.kit, "tickets.json")))
    llm = Slow(a.kit, bad_first={"T-05"}, delay=a.delay)
    work = os.path.join(tempfile.mkdtemp(), "work")
    report, *_ = run_crew(llm, a.seeded, tickets, workers=3, run_id=a.run_id, work_dir=work,
                          cfg=Config(kill={"w1": 3}, checkpoint_every=2), claim_ttl=6, timeout=600)
    print(report["approved"], "of", report["total"], "approved")


if __name__ == "__main__":
    main()
