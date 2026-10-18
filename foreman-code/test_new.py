"""Checks for: read_function, the playbook, the text-only nudge, forgiving edits, and the names.
    python test_new.py --seeded ../tinydb
"""
import argparse
import os
import sys
import tempfile

import _paths  # noqa: F401
from agents import NEED_TOOL, run_loop
from graph_api import GraphAPI
from llm import ScriptedLLM
from personas import PLAYBOOK, worker_name
from tools import ToolBox
from worktrees import WorkTrees

bad = []


def check(cond, name):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        bad.append(name)


ap = argparse.ArgumentParser()
ap.add_argument("--seeded", required=True)
a = ap.parse_args()
wt = WorkTrees(os.path.join(tempfile.mkdtemp(), "work"))
wt.make_base(a.seeded)
wt.create("w1")
api = GraphAPI("tinydb", "new-test")
api.reset_run()
api.clear_memory()
api.register_agent("w1", "worker")
box = ToolBox(api, "worker", "w1", wt=wt, worktree="w1")

# names
check([worker_name(x) for x in ("w1", "w2", "w3")] == ["Mina", "Jo", "Kai"], "worker names come from the slot")

# read_function
r = box.call("read_function", {"function": "tinydb.table.Table.update"})
check(r.get("ok") and "def update" in r["text"] and r["text"].split("\n")[0].split("|")[0].isdigit(), "read_function reads a whole method by id")
r2 = box.call("read_function", {"function": "Table.update"})
check(r2.get("ok") and r2["function"] == "tinydb.table.Table.update", "read_function accepts Class.method")
check(box.call("read_function", {"function": "nope_nothing"}).get("error") == "NO_SUCH_FUNCTION", "read_function reports an unknown name")
r3 = box.call("read_function", {"function": "__init__"})
check(r3.get("error") == "AMBIGUOUS" and r3["matches"], "read_function lists matches when the name is ambiguous")
# still works after an edit that shifts lines
box.call("edit_file", {"path": "tinydb/table.py", "old": "class Table:", "new": "# one\n# two\n# three\nclass Table:"})
r4 = box.call("read_function", {"function": "tinydb.table.Table.update"})
check(r4.get("ok") and "def update" in r4["text"], "read_function still finds the method after lines moved")

# forgiving edits
src = box.call("read_file", {"path": "tinydb/table.py", "start": 1, "end": 3})["text"]
check("|" in src.split("\n")[0], "read_file shows <number>|<line>")
line = [l for l in open(os.path.join(wt.path("w1"), "tinydb/table.py")).read().split("\n") if "def upsert(" in l][0]
ok = box.call("edit_file", {"path": "tinydb/table.py", "old": " " + line, "new": " " + line + "  # touched"})
check(ok.get("ok"), "an edit with one extra leading space is still applied")

# playbook
api.add_playbook(PLAYBOOK)
h = api.get_playbook("update() with unknown doc_ids raises KeyError")["hints"]
check(h and "id" in h[0]["summary"].lower(), "the playbook returns a hint for a missing-id ticket")
check(api.get_playbook("something completely unrelated xyz")["hints"] == [], "no hint when nothing matches")
check(len(api.get_playbook("keyerror stale cache flush argument ignored boundary directory", k=2)["hints"]) == 2, "at most k hints")
snap = api.get_graph_snapshot()
check(not any(n["type"] == "Memory" for n in snap["nodes"]), "playbook entries stay out of the graph picture")

# the text-only nudge
llm = ScriptedLLM(["I think I am done.", "Still thinking.", "Third time.",
                   {"text": "", "calls": [{"name": "run_tests", "arguments": {"targets": []}}]}])
res = run_loop(llm, ToolBox(api, "worker", "w1", wt=wt, worktree="w1"), "sys", "user", api, "T-01",
               only=["run_tests"], max_steps=6, needs_tool=True)
check(any(NEED_TOOL in str(m) for m in llm.seen[1]), "a text-only reply gets the 'you must call a tool' message")
check(llm.i == 3 and res["status"] == "finished", "after two nudges a text-only model is let go (no endless loop)")

# --- footprint enforcement and run_tests guidance ---
import json as _json
baseline = dict(wt.run_tests("w1")["results"])
api.create_task(_json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "foreman-schema", "tickets.json")))[9])  # T-10
api.register_agent("w9", "worker")
wt.create("w9")
box9 = ToolBox(api, "worker", "w9", wt=wt, worktree="w9", baseline=baseline)
check(api.claim_task("T-10", "w9").get("ok"), "claim T-10")
box9.current_task = "T-10"
r = box9.call("write_file", {"path": "debug.py", "content": "print(1)"})
check(r.get("error") == "OUT_OF_FOOTPRINT" and "storages.py" in r["detail"], "creating a new file outside the footprint is refused")
r = box9.call("edit_file", {"path": "tinydb/table.py", "old": "class Table", "new": "class Table"})
check(r.get("error") == "OUT_OF_FOOTPRINT", "editing a file outside the footprint is refused")
full = box9.call("run_tests", {})
check(full["ok"] and full.get("ticket_tests_still_failing") and not full["newly_broken_by_you"] and "other_failures_ignore" in full,
      "full run: ticket tests still failing, nothing newly broken, old failures marked as ignorable")
check(not any(f["test"] not in full["ticket_tests_still_failing"] for f in full["failures"]), "only the ticket's own failures are listed")
src = os.path.join(wt.path("w9"), "tinydb/storages.py")
txt = open(src).read().replace("if base_dir and os.path.exists(base_dir):", "if base_dir and not os.path.exists(base_dir):")
open(src, "w").write(txt)
tg = box9.call("run_tests", {"targets": ["tests/test_storages.py::test_create_dirs"]})
check("full test suite" in tg.get("next_step", ""), "ticket tests pass on a targeted run: told to run the full suite once")
full = box9.call("run_tests", {})
check(not full["newly_broken_by_you"] and "submit_result now" in full.get("next_step", ""), "full run clean: told to submit now")
open(os.path.join(wt.path("w9"), "scratch.py"), "w").write("x = 1\n")        # a leftover that must not reach the reviewer
sub = box9.call("submit_result", {"task_id": "T-10", "summary": "created the folder first"})
check(sub["ok"] and sub["files_touched"] == ["tinydb/storages.py"] and "scratch.py" in sub.get("put_back", ""),
      "submit puts stray files back and keeps only the real fix")

api.clear_memory()
print(f"\n{'ALL PASSED' if not bad else str(len(bad)) + ' FAILED'}")
sys.exit(1 if bad else 0)
