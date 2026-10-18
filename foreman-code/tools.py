"""Role-scoped tools. Each agent gets a ToolBox that only knows the tools its role is allowed to use.

    box = ToolBox(api, role="worker", agent_id="w1", wt=worktrees, worktree="w1")
    box.specs()                       # the tool list to show the model (only this role's tools)
    box.call("claim_task", {"task_id": "T-01"})     # -> a plain dict, never raises
    box.call("review_decision", {...})              # -> {"ok": False, "error": "TOOL_NOT_ALLOWED"} for a worker

Rules this file enforces (the database enforces the rest):
  - A tool outside the role's list is refused, and the refusal is counted (box.refused).
  - Who is acting is fixed by the box (`agent_id`), never typed by the model, so no agent can act as another.
  - Workers cannot edit test files, and cannot read or write outside their own worktree.
  - A worker cannot claim its tests pass: `submit_result` runs the ticket's tests itself and records the real answer.
"""
import json
import os
import re

import _paths  # noqa: F401
from worktrees import WorkTreeError

FOREMAN_TOOLS = ["create_task", "get_footprint", "find_conflicts", "set_task_order", "expire_stale_claims",
                 "list_open_tasks", "get_task", "recall_similar"]
WORKER_GRAPH = ["list_open_tasks", "get_task", "claim_task", "heartbeat", "release_claim", "write_handoff",
                "get_handoff", "recall_similar", "submit_result", "get_footprint"]
WORKER_FILES = ["list_files", "read_file", "read_function", "search_code", "edit_file", "write_file", "run_tests",
                "apply_handoff_patch"]
REVIEWER_TOOLS = ["get_task", "get_result", "get_footprint", "review_decision", "get_handoff", "review_patch"]
HISTORIAN_TOOLS = ["get_task", "get_result", "record_outcome", "retire_fact", "recall_similar", "rate_memory"]

ROLE_TOOLS = {
    "foreman": FOREMAN_TOOLS,
    "worker": WORKER_GRAPH + WORKER_FILES,
    "reviewer": REVIEWER_TOOLS,
    "historian": HISTORIAN_TOOLS,
}

TASK_ID = {"task_id": {"type": "string", "description": "The ticket id, for example T-03."}}

# name -> (description, properties, required)
SPECS = {
    "create_task": ("Create a task from a ticket.", {"ticket": {"type": "object", "description":
                    "{ticket_id, title, description, failing_tests: [test ids]}"}}, ["ticket"]),
    "get_footprint": ("Files, functions and tests this task is likely to touch, most suspicious first.", TASK_ID, ["task_id"]),
    "find_conflicts": ("Other tasks whose footprint overlaps this one.", TASK_ID, ["task_id"]),
    "set_task_order": ("Record the order tasks should be worked in.", {"ordered_task_ids": {
        "type": "array", "items": {"type": "string"}}}, ["ordered_task_ids"]),
    "expire_stale_claims": ("Reopen tasks whose worker stopped reporting.", {}, []),
    "list_open_tasks": ("Open tasks in the order to take them. `conflicts_with_active` lists claimed tasks that "
                        "overlap; a task with any is likely to be refused.", {}, []),
    "get_task": ("A task's title, description, status, owner and failing tests.", TASK_ID, ["task_id"]),
    "get_result": ("The latest submitted result for a task, with files touched and any out-of-footprint files.",
                   TASK_ID, ["task_id"]),
    "claim_task": ("Claim a task so only you work on it. Refused if someone holds it or an overlapping task is "
                   "being worked on.", TASK_ID, ["task_id"]),
    "heartbeat": ("Tell the system you are still working, so your claim does not expire.", TASK_ID, ["task_id"]),
    "release_claim": ("Give up your claim and put the task back in the queue.", TASK_ID, ["task_id"]),
    "write_handoff": ("Leave a note so another worker could take over: what is done, what is left, what you tried.",
                      {**TASK_ID, "done": {"type": "string"}, "remaining": {"type": "string"},
                       "tried": {"type": "string"}}, ["task_id", "done", "remaining", "tried"]),
    "get_handoff": ("The latest handoff note for a task, or null.", TASK_ID, ["task_id"]),
    "recall_similar": ("Saved lessons and fixes from earlier tasks that touched the same code.", TASK_ID, ["task_id"]),
    "submit_result": ("Submit your fix for review. The system runs the ticket's tests itself and records the real "
                      "result and your changed files.", {**TASK_ID, "summary": {"type": "string",
                      "description": "What was wrong and what you changed, in two or three sentences."}},
                      ["task_id", "summary"]),
    "review_decision": ("Approve or reject a submitted patch. A rejection sends the task back to the queue.",
                        {**TASK_ID, "approved": {"type": "boolean"}, "reasons": {"type": "array", "items": {
                            "type": "object", "properties": {"code": {"type": "string", "description":
                            "OUT_OF_FOOTPRINT, TESTS_FAIL, REGRESSION or TESTS_MODIFIED"},
                            "detail": {"type": "string"}}, "required": ["code", "detail"]}}},
                        ["task_id", "approved"]),
    "review_patch": ("Check a submitted patch: applies it to a clean copy, runs the whole test suite, and reports "
                     "files touched, files outside the footprint, regressions and the patch text.", TASK_ID, ["task_id"]),
    "record_outcome": ("Save a lesson as filled-in fields. Fill ONLY the fields of the kind you choose. "
                       "episodic (always): where, symptom, cause, fix. "
                       "semantic (only if the patch proves a lasting rule): where, rule, because. "
                       "procedural (only if there is a reusable way of working): when, check, then_step.",
                       {**TASK_ID, "kind": {"type": "string", "enum": ["episodic", "semantic", "procedural"]},
                        "where": {"type": "string", "description": "exact Class.function or file"},
                        "symptom": {"type": "string", "description": "what the user saw go wrong"},
                        "cause": {"type": "string", "description": "what the code did wrong"},
                        "fix": {"type": "string", "description": "what changed"},
                        "rule": {"type": "string", "description": "Class.function() must ..."},
                        "because": {"type": "string", "description": "the reason for the rule"},
                        "when": {"type": "string", "description": "the kind of symptom"},
                        "check": {"type": "string", "description": "where to look first"},
                        "then_step": {"type": "string", "description": "the next step"}},
                       ["task_id", "kind"]),
    "retire_fact": ("Mark a saved lesson as outdated, optionally pointing to the lesson that replaces it.",
                    {"memory_id": {"type": "string"}, "reason": {"type": "string"}, "superseded_by": {"type": "string"}},
                    ["memory_id", "reason"]),
    "rate_memory": ("Say whether a recalled lesson helped (true) or not (false).",
                    {"memory_id": {"type": "string"}, "worked": {"type": "boolean"}}, ["memory_id", "worked"]),
    "list_files": ("List a folder in your worktree.", {"path": {"type": "string", "description": "Default '.'"}}, []),
    "read_file": ("Read a file with line numbers. Use start and end to read a range.",
                  {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["path"]),
    "read_function": ("Read one whole function (or method) by its id or name, for example tinydb.table.Table.update or "
                      "Table.update. Uses the code graph to find the file. Shows line numbers as <number>|<line>.",
                      {"function": {"type": "string", "description": "A function id from the footprint, or a name."}},
                      ["function"]),
    "search_code": ("Find lines containing some text in your worktree.", {"text": {"type": "string"},
                    "glob": {"type": "string", "description": "File pattern, default *.py"}}, ["text"]),
    "edit_file": ("Replace one exact piece of text in a file with new text. The old text must appear exactly once.",
                  {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
                  ["path", "old", "new"]),
    "write_file": ("Write a whole file (use edit_file for small changes).", {"path": {"type": "string"},
                   "content": {"type": "string"}}, ["path", "content"]),
    "run_tests": ("Run tests in your worktree. targets are test files or test ids; leave empty for the whole suite.",
                  {"targets": {"type": "array", "items": {"type": "string"}}}, []),
    "apply_handoff_patch": ("Apply the unfinished changes another worker left for this task, if any.", TASK_ID,
                            ["task_id"]),
}


def tool_spec(name):
    desc, props, required = SPECS[name]
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


def short(obj, limit=6000):
    text = json.dumps(obj, default=str)
    return text if len(text) <= limit else text[:limit] + '... (cut off)"}'


class ToolBox:
    def __init__(self, api, role, agent_id, wt=None, worktree=None, review_worktree=None, patches_dir=None,
                 baseline=None, worktree_of=None, roles=None, naive_claim=False, naive_gap=0.05):
        self.roles = roles or ROLE_TOOLS
        self.naive_claim, self.naive_gap = naive_claim, naive_gap   # evaluation only: the no-coordination baseline
        if role not in self.roles:
            raise ValueError(f"unknown role {role}")
        self.api, self.role, self.agent = api, role, agent_id
        self.wt, self.worktree, self.review_worktree = wt, worktree, review_worktree
        self.patches = patches_dir or (os.path.join(wt.root, "patches") if wt else "patches")
        self.baseline = baseline or {}                # test id -> 'passed'/'failed' on the starting tree
        self.worktree_of = worktree_of or (lambda agent: agent)
        self.allowed = set(self.roles[role])
        self.refused = []                             # attempts to use tools outside the role
        self.facts = {}                               # reviewer: task id -> what review_patch found
        self.decisions = {}                           # reviewer: task id -> (approved, reasons) it last submitted
        self.recalled = set()                         # worker: memory ids that were shown for the current work
        self.log = []                                 # (tool, ok) for every call
        self.current_task = None                      # worker: the ticket being worked on
        self._fp_cache = {}                           # task id -> footprint files
        self._full_run_ok = {}                        # task id -> the full suite was run with nothing newly broken

    def specs(self, only=None):
        return [tool_spec(n) for n in self.roles[self.role] if only is None or n in only]

    def names(self):
        return list(self.roles[self.role])

    def call(self, name, args=None):
        args = args or {}
        if name not in self.allowed:
            self.refused.append(name)
            self.log.append((name, False))
            return {"ok": False, "error": "TOOL_NOT_ALLOWED",
                    "detail": f"the {self.role} role cannot use {name}. Your tools: {', '.join(self.names())}"}
        try:
            result = getattr(self, "_t_" + name)(**args)
        except TypeError as exc:
            result = {"ok": False, "error": "BAD_ARGUMENTS", "detail": str(exc)[:200]}
        except WorkTreeError as exc:
            result = {"ok": False, "error": "FILE_ERROR", "detail": str(exc)}
        self.log.append((name, bool(result.get("ok"))))
        self._remember_step(name, args, result)
        return result

    def _remember_step(self, name, args, result):
        """A one-line trace of what this agent did, used for handoff notes (the model's own text is often empty)."""
        ok = result.get("ok")
        if name == "read_file":
            line = f"read {args.get('path')} {args.get('start', 1)}-{args.get('end', 'end')}"
        elif name == "read_function":
            line = f"read function {args.get('function')}"
        elif name == "search_code":
            line = f"searched for '{args.get('text')}'"
        elif name == "edit_file":
            line = f"edited {args.get('path')}" if ok else f"edit of {args.get('path')} failed: {str(result.get('detail'))[:60]}"
        elif name == "run_tests":
            line = f"ran tests: {result.get('passed')} passed, {result.get('failed')} failed" if ok else "ran tests (error)"
        elif name in ("write_file", "submit_result"):
            line = f"{name} {args.get('path', '')}".strip()
        else:
            return
        self.trace = (getattr(self, "trace", []) + [line])[-40:]

    # ---- plain pass-throughs to the database ----------------------------------------------------------
    def _t_create_task(self, ticket): return self.api.create_task(ticket)
    def _t_get_footprint(self, task_id): return self.api.get_footprint(task_id)
    def _t_find_conflicts(self, task_id): return self.api.find_conflicts(task_id)
    def _t_set_task_order(self, ordered_task_ids): return self.api.set_task_order(ordered_task_ids)
    def _t_expire_stale_claims(self): return self.api.expire_stale_claims()
    def _t_list_open_tasks(self): return self.api.list_open_tasks()
    def _t_get_task(self, task_id): return self.api.get_task(task_id)
    def _t_get_result(self, task_id): return self.api.get_result(task_id)
    def _t_heartbeat(self, task_id): return self.api.heartbeat(task_id, self.agent)
    def _t_get_handoff(self, task_id): return self.api.get_handoff(task_id)
    def _t_recall_similar(self, task_id):
        res = self.api.recall_similar(task_id)
        self.recalled.update(m["memory_id"] for m in res.get("memories", []))
        return res
    def _t_retire_fact(self, memory_id, reason, superseded_by=None):
        return self.api.retire_fact(memory_id, reason, superseded_by)
    def _t_rate_memory(self, memory_id, worked): return self.api.rate_memory(memory_id, bool(worked))

    # ---- worker ----------------------------------------------------------------------------------------
    def _t_claim_task(self, task_id):
        if self.naive_claim:
            res = self.api.naive_claim(task_id, self.agent, self.naive_gap)
        else:
            res = self.api.claim_task(task_id, self.agent)
        if res.get("ok") and res.get("reason") == "claimed" and self.wt:
            self.wt.reset(self.worktree)              # a fresh start on every new claim
        return res

    def _t_release_claim(self, task_id):
        res = self.api.release_claim(task_id, self.agent)
        if res.get("ok") and self.wt:
            self.wt.reset(self.worktree)
        return res

    def _patch_path(self, prefix, task_id):
        os.makedirs(self.patches, exist_ok=True)
        return os.path.join(self.patches, f"{prefix}__{task_id}.patch")

    def _t_write_handoff(self, task_id, done, remaining, tried):
        files = self.wt.changed_files(self.worktree) if self.wt else []
        if self.wt and files:                         # keep the unfinished work so a taker-over can start from it
            with open(self._patch_path("handoff", task_id), "w") as f:
                f.write(self.wt.diff(self.worktree))
        return self.api.write_handoff(task_id, self.agent, {"done": done, "remaining": remaining, "tried": tried,
                                                            "files_touched": files})

    def _t_apply_handoff_patch(self, task_id):
        path = self._patch_path("handoff", task_id)
        if not os.path.exists(path):
            return {"ok": False, "error": "NO_HANDOFF_PATCH"}
        self.wt.apply_patch(self.worktree, open(path).read())
        return {"ok": True, "files": self.wt.changed_files(self.worktree)}

    def _t_submit_result(self, task_id, summary):
        task = self.api.get_task(task_id)
        if not task.get("ok"):
            return task
        files = self.wt.changed_files(self.worktree)
        allowed = self._footprint_files(task_id)
        reverted = [f for f in files if allowed and (f not in allowed or self._is_test_path(f))]
        if reverted:                                  # stray files and leftovers from other work never reach the reviewer
            self.wt.revert_paths(self.worktree, reverted)
            files = self.wt.changed_files(self.worktree)
        if not files:
            return {"ok": False, "error": "NO_CHANGES", "detail": "you have not changed any file"
                    + (f" (files outside the footprint were put back: {', '.join(reverted)})" if reverted else "")}
        run = self.wt.run_tests(self.worktree, task["failing_tests"])
        failed = sorted(t for t, s in run["results"].items() if s != "passed")
        missing = [t for t in task["failing_tests"] if t not in run["results"]]
        report = {"passed": not failed and not missing and not run["timed_out"], "ran": len(run["results"]),
                  "failed": failed[:10], "missing": missing[:10]}
        patch_ref = self._patch_path(self.agent, task_id)
        patch = self.wt.diff(self.worktree)
        for path in (patch_ref, self._patch_path("handoff", task_id)):     # the latest work is the base for any retry
            with open(path, "w") as f:
                f.write(patch)
        res = self.api.submit_result(task_id, self.agent, summary, report, files, patch_ref)
        out = {**res, "tests_passed": report["passed"], "test_report": report, "files_touched": files}
        if reverted:
            out["put_back"] = f"Files outside the footprint were put back to the original: {', '.join(reverted)}"
        return out

    def _t_list_files(self, path="."): return {"ok": True, "listing": self.wt.list_dir(self.worktree, path)}

    def _t_read_file(self, path, start=1, end=None):
        return {"ok": True, "text": self.wt.read(self.worktree, path, start or 1, end)}

    def _t_read_function(self, function):
        found = self.api.find_function(function)["functions"]
        if not found:
            return {"ok": False, "error": "NO_SUCH_FUNCTION", "detail": f"no function matches '{function}'. Try search_code."}
        if len(found) > 1 and not any(f["id"] == function for f in found):
            return {"ok": False, "error": "AMBIGUOUS", "detail": "several functions match; use one of these ids",
                    "matches": [f["id"] for f in found]}
        f = next((x for x in found if x["id"] == function), found[0])
        # the graph knows which file and roughly where; the worker's copy may have shifted, so find it again in the file
        import ast
        try:
            text = self.wt.read(self.worktree, f["file"], 1, None, max_chars=10 ** 7)
            src = "\n".join(line.split("|", 1)[1] for line in text.split("\n"))
            best = None
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == f["name"]:
                    if best is None or abs(node.lineno - f["start_line"]) < abs(best.lineno - f["start_line"]):
                        best = node
            start, end = (best.lineno, best.end_lineno) if best else (f["start_line"], f["end_line"])
        except (SyntaxError, ValueError):
            start, end = f["start_line"], f["end_line"]
        body = self.wt.read(self.worktree, f["file"], start, end, max_chars=9000)
        return {"ok": True, "function": f["id"], "file": f["file"], "start": start, "end": end, "text": body}

    def _t_search_code(self, text, glob="*.py"):
        return {"ok": True, "hits": self.wt.search(self.worktree, text, glob or "*.py")}

    @staticmethod
    def _is_test_path(path):
        p = os.path.normpath(path).replace("\\", "/")
        return p.startswith("tests/") or p.startswith("test_") or "/tests/" in p or os.path.basename(p).startswith("test_")

    def _footprint_files(self, task_id):
        if task_id not in self._fp_cache:
            fp = self.api.get_footprint(task_id)
            self._fp_cache[task_id] = list(fp.get("files", [])) if fp.get("ok") else None
        return self._fp_cache[task_id]

    def _outside_footprint(self, path):
        """A worker may only change files in the ticket's footprint. Returns an error dict, or None if the path is fine."""
        files = self._footprint_files(self.current_task) if self.current_task else None
        if not files:
            return None
        p = os.path.normpath(path).replace("\\", "/")
        if p in files:
            return None
        return {"ok": False, "error": "OUT_OF_FOOTPRINT",
                "detail": f"you may only change these files for this ticket: {', '.join(files)}. Do not create new files."}

    def _t_edit_file(self, path, old, new):
        if self._is_test_path(path):
            return {"ok": False, "error": "TESTS_READ_ONLY", "detail": "test files are read-only; fix the code instead"}
        refused = self._outside_footprint(path)
        if refused:
            return refused
        return {"ok": True, "message": self.wt.replace(self.worktree, path, old, new)}

    def _t_write_file(self, path, content):
        if self._is_test_path(path):
            return {"ok": False, "error": "TESTS_READ_ONLY", "detail": "test files are read-only; fix the code instead"}
        refused = self._outside_footprint(path)
        if refused:
            return refused
        return {"ok": True, "message": self.wt.write(self.worktree, path, content)}

    def _t_run_tests(self, targets=None):
        run = self.wt.run_tests(self.worktree, targets or None)
        res = run["results"]
        failing = sorted(run["failures"].items())
        out = {"ok": True, "passed": run["passed"], "failed": run["failed"], "timed_out": run["timed_out"]}
        task = self.api.get_task(self.current_task) if self.current_task else {}
        ticket = list(task.get("failing_tests") or [])
        if not self.baseline or not ticket:            # no starting picture: report plainly
            out["failures"] = [{"test": t, "reason": r} for t, r in failing[:15]]
            return out
        ticket_left = [t for t in ticket if res.get(t) != "passed"] if any(t in res for t in ticket) else None
        newly_broken = sorted(t for t, s in self.baseline.items() if s == "passed" and t in res and res[t] != "passed")
        old_failures = sum(1 for t, s in res.items() if s != "passed" and self.baseline.get(t) != "passed" and t not in ticket)
        shown = [(t, r) for t, r in failing if t in newly_broken or t in ticket]
        out["failures"] = [{"test": t, "reason": r} for t, r in shown[:15]]
        out["newly_broken_by_you"] = newly_broken[:10]
        if ticket_left is not None:
            out["ticket_tests_still_failing"] = ticket_left
        if old_failures and not targets:
            out["other_failures_ignore"] = (f"{old_failures} other tests were already failing before you started. They belong "
                                            "to other tickets. Ignore them; only your ticket's tests and anything newly "
                                            "broken matter.")
        full = not targets
        if ticket_left == [] and not newly_broken:
            if full:
                self._full_run_ok[self.current_task] = True
                out["next_step"] = "Your ticket's tests pass and you broke nothing. Call submit_result now."
            else:
                out["next_step"] = ("Your ticket's tests pass. Run the full test suite once (run_tests with no targets) to "
                                    "check nothing else broke, then call submit_result.")
        elif newly_broken:
            out["next_step"] = "Your change broke tests that passed before. Fix that before you submit."
        return out

    # ---- reviewer --------------------------------------------------------------------------------------
    def _t_review_decision(self, task_id, approved, reasons=None):
        facts = self.facts.get(task_id)
        if facts is None:
            return {"ok": False, "error": "NOT_CHECKED", "detail": "call review_patch for this task first"}
        if approved:                                  # the rules the reviewer may not break
            problems = []
            if facts["ticket_tests_failing"]:
                problems.append("TESTS_FAIL: the ticket's tests still fail")
            if facts["regression_count"]:
                problems.append("REGRESSION: other tests that passed before now fail")
            if facts["out_of_footprint"]:
                problems.append("OUT_OF_FOOTPRINT: files changed outside the footprint")
            if facts["tests_modified"]:
                problems.append("TESTS_MODIFIED: test files were changed")
            if problems:
                return {"ok": False, "error": "CANNOT_APPROVE", "detail": "; ".join(problems) + ". Reject it instead."}
        res = self.api.review_decision(task_id, self.agent, bool(approved), reasons or [])
        if res.get("ok"):
            self.decisions[task_id] = (bool(approved), list(reasons or []))
        return res

    def _t_review_patch(self, task_id):
        res = self.api.get_result(task_id)
        if not res.get("ok"):
            return res
        task = self.api.get_task(task_id)
        self.wt.reset(self.review_worktree)
        patch = open(res["patch_ref"]).read() if res.get("patch_ref") and os.path.exists(res["patch_ref"]) else ""
        self.wt.apply_patch(self.review_worktree, patch)
        run = self.wt.run_tests(self.review_worktree)
        ticket_failed = [t for t in task["failing_tests"] if run["results"].get(t) != "passed"]
        regressions = sorted(t for t, s in self.baseline.items() if s == "passed" and run["results"].get(t) != "passed")
        out = {"ok": True, "files_touched": res["files_touched"], "out_of_footprint": res["out_of_footprint"],
               "tests_modified": [f for f in res["files_touched"] if self._is_test_path(f)],
               "ticket_tests_failing": ticket_failed, "regressions": regressions[:10],
               "regression_count": len(regressions), "tests_run": len(run["results"]), "patch": patch[:4000]}
        self.facts[task_id] = out
        return out

    # ---- historian -------------------------------------------------------------------------------------
    LESSON_FIELDS = {"episodic": [("where", "WHERE"), ("symptom", "SYMPTOM"), ("cause", "CAUSE"), ("fix", "FIX")],
                     "semantic": [("where", "WHERE"), ("rule", "RULE"), ("because", "BECAUSE")],
                     "procedural": [("when", "WHEN"), ("check", "CHECK"), ("then_step", "THEN")]}

    def _t_record_outcome(self, task_id, kind, where=None, symptom=None, cause=None, fix=None, rule=None,
                          because=None, when=None, check=None, then_step=None):
        """Lessons have a fixed shape: every field of the kind must be filled in, or the lesson is refused."""
        if kind not in self.LESSON_FIELDS:
            return {"ok": False, "error": "BAD_KIND", "detail": "kind must be episodic, semantic or procedural"}
        given = {"where": where, "symptom": symptom, "cause": cause, "fix": fix, "rule": rule, "because": because,
                 "when": when, "check": check, "then_step": then_step}
        fields = [(key, label, str(given[key] or "").strip()) for key, label in self.LESSON_FIELDS[kind]]
        missing = [key for key, _, v in fields if len(v) < 3]
        if missing:
            return {"ok": False, "error": "MISSING_FIELDS",
                    "detail": f"a {kind} lesson needs these fields filled in: "
                              f"{', '.join(k for k, _, _ in fields)}. Missing or empty: {', '.join(missing)}"}
        summary = " | ".join(f"{label}: {v}" for _, label, v in fields)
        res = self.api.get_result(task_id)
        details = {"fields": {key: v for key, _, v in fields}}
        if res.get("ok"):
            files = res["files_touched"]
            details["fix_files"] = files
            details["evidence_path"] = res.get("patch_ref")
            mods = {re.sub(r"\.py$", "", f).replace("/", ".") for f in files}
            fp = self.api.get_footprint(task_id)
            funcs = [x["id"] for x in fp.get("functions", []) if any(x["id"].startswith(m + ".") for m in mods)]
            if funcs:
                details["functions"] = funcs[:8]
        return self.api.record_outcome(task_id, summary, kind, details)
