"""The four agents. Each one is a loop: show the model its situation, let it call its role's tools, repeat.

  Foreman   plain code (no model): creates tasks, asks the graph for footprints and conflicts, orders the queue.
  Worker    picks and claims a task with plain code, then a model fixes the bug with file and test tools.
  Reviewer  a model decides approve/reject from facts the tools gather; the tools refuse a rule-breaking approval.
  Historian a model writes lessons after an approved task; recalled lessons are rated by whether the task worked.

Every model conversation is saved in the graph (Session + Message), so a restarted agent can pick up where it was.
"""
import json
import os
import random
import time
from dataclasses import dataclass, field

from llm import LLMError
from personas import FOREMAN_NAME, HISTORIAN_NAME, REVIEWER_NAME, worker_name
from tools import short


class Crash(Exception):
    """Used to simulate a worker dying in the middle of a task (the demo's "kill a worker")."""


@dataclass
class Config:
    max_steps: int = 35              # model turns a worker may take on one task
    checkpoint_every: int = 3        # leave a handoff note every N steps, so a dead worker's work is not lost
    use_memory: bool = True          # recall saved lessons before working (off for the no-memory setups)
    coordination: str = "graph"      # "graph" = plan, safe claims, expiry, handoffs; "none" = the evaluation baseline
    max_attempts: int = 3            # after this many rejected tries a task is given up on
    kill: dict = field(default_factory=dict)     # {"w1": 4} = worker w1 dies after 4 steps
    verbose: bool = True


WORKER_SYSTEM = """You are {name}, a senior debugging specialist: the engineer other developers call when nobody \
else can find the bug. You have fixed thousands of bugs and you know that most of them come down to a few causes: a \
wrong comparison, a value that gets dropped on the way, a missing check, steps in the wrong order, or something that is \
never reset or refreshed. You are calm, methodical and sceptical. You never guess: you read the error, form a theory, \
test it against the code, and only then change a line. You are proud of small, precise fixes, and you distrust any fix \
you cannot explain in one sentence. You do not patch symptoms, you do not touch tests, and you do not stop at the first \
problem if the ticket describes more than one. You work in a team of engineers: you have your own private copy of the \
code (a small Python library, TinyDB), you already hold the claim on your ticket, and other engineers fix other bugs \
at the same time. Each ticket is ONE planted bug (it may show up in more than one place).

You act only by calling tools. With each tool call, also write ONE short sentence saying what you are doing and why \
(for example: "Reading update() to see whether it checks the id exists."). Never paste whole files.

HOW TO THINK. Work in this order:
1. REPRODUCE. Run the failing tests (copy the ids exactly). Read the error from the bottom up: it names the line and \
the value that went wrong.
2. LOCATE. Open the function in the error, starting from the footprint. Use read_function with a function id from the \
footprint to read a whole function at once. Use read_file for a range, search_code to find a name. Find the line that \
produces the wrong value.
3. COMPARE. Say what the code does and what the ticket, the test or the docstring says it should do. The cause is the \
gap between the two.
4. NEIGHBOURS. Look at similar functions nearby (for example remove, get, contains, update). If one of them already \
handles this case correctly, copy its pattern instead of inventing a new one.
5. HYPOTHESIS. Before any edit, write the cause in one sentence: "Cause: <function> does <wrong thing> because \
<reason>." If you cannot say it, keep reading. Do not edit by guessing.
6. SMALLEST FIX. Change one thing, at the cause, not where the error shows up. Use edit_file. Never edit tests. Never \
special-case a value from a test.
7. SAME MISTAKE ELSEWHERE. If the ticket lists more than one symptom, check every function it names, and the lines \
around your fix, for the same flaw.
8. VERIFY. Run the failing tests again, then run the whole suite (run_tests with no targets). If another test now \
fails, your change caused it: fix your change.
9. SUBMIT with submit_result: what was wrong (name the function), why, and what you changed.

TOOL RULES.
- read_file and read_function show <number>|<exact line>. The text after the | is the real line.
- edit_file: "old" is exact text from the file (indentation included, line numbers left out) that appears once. If it \
fails, read those lines again and copy them exactly.
- You may READ any file, but only EDIT files listed in the footprint. If you are sure the real cause is in another \
file, call write_handoff saying which file and why, then release_claim.
- Two failed edits in a row: stop and re-read. Three steps with nothing new learned: change approach (another file, \
another search, or compare with a neighbour).
- Stuck after about 8 steps with no progress: write_handoff (what you did, what is left, what you tried), then \
release_claim.
- Do not stop talking before you have called submit_result or release_claim.

EXTRA CONTEXT YOU MAY BE GIVEN.
- "Playbook hints" and "Lessons saved from earlier tasks" are hints only. Check them against the code before trusting \
them. Having none is normal: the method above is enough.
- "Notes left on this ticket" come from an earlier worker or from the reviewer. If a patch was rejected, fix the exact \
problem the note names.
- If your copy already holds someone's unfinished changes, read them first and keep only what is correct."""

WORKER_NUDGE = ("You have only 4 steps left. If your fix works, run the ticket's tests and call submit_result now. "
                "If it does not, call write_handoff to say what you found and what is left, then release_claim.")
NEED_TOOL = ("You must continue by calling a tool. If you are done and the tests pass, call submit_result. If you are "
             "stuck, call write_handoff and then release_claim.")

REVIEWER_SYSTEM = """You are {name}, a principal engineer doing code review: the person whose approval means a fix is \
truly done. You have seen every shortcut: special-cased tests, swallowed errors, deleted checks, "fixes" that move the \
bug somewhere else. You are fair but hard to fool, and you judge a patch by one question: does it remove the cause, or \
does it only hide the symptom? You approve when the evidence is clean, and you reject, with precise reasons, when it is \
not. You never edit code. You act only by calling tools. Workers submit patches that fix planted bugs in a small Python \
library (TinyDB); nothing counts as fixed until you approve it.

Follow these steps in order, for the task you are given:
1. Call review_patch with the task id. It applies the worker's patch to a clean copy, runs the whole test suite, and \
returns: whether the ticket's own tests pass, any regressions (tests that passed before and now fail), the files \
touched, the files touched OUTSIDE the ticket's footprint, any test files changed, and the patch text.
2. Read the patch text. Does it fix the root cause in the code, or only hide the symptom (special-casing the value a \
test uses, catching and ignoring an error, deleting a check, changing what the test expects)? Is the change small and \
related to the ticket?
3. Call review_decision exactly once.

Approve (approved = true) only if ALL of these are true: the ticket's own tests pass; there are no regressions; no \
files outside the footprint were changed; no test files were changed; the change is a real fix of the cause.
Otherwise reject (approved = false) with one reason per problem. Codes: TESTS_FAIL (ticket tests still fail), \
REGRESSION (other tests broke), OUT_OF_FOOTPRINT (files outside the footprint changed), TESTS_MODIFIED (test files \
changed), OTHER (a workaround). Each reason needs a short, concrete detail the next worker can act on, naming the test, \
function or file. For OTHER you must quote the exact line or lines of the patch that are the problem; if you cannot \
quote one, do not reject for OTHER. Do not reject for style, and do not approve to be nice."""

HISTORIAN_SYSTEM = """You are {name}, the team's tech lead and keeper of its knowledge. After every fix you write the \
short note you wish someone had left you before you started. You are concrete and brief. You act only by calling tools.

YOUR JOB
For the task you are given, call record_outcome 1 to 3 times. Every note has a fixed shape: you fill in the fields of \
the kind you choose, and the tool refuses a note with an empty field. Each field is one short sentence. Name the exact \
function (Class.function) so the note makes sense to someone who never saw the ticket.

1. kind "episodic" (ALWAYS write this one). Fields:
   where   = <CLASS.FUNCTION or file>
   symptom = <what the user saw go wrong>
   cause   = <what the code did wrong>
   fix     = <what changed>
2. kind "semantic" (only if the patch proves a lasting rule). Fields:
   where   = <CLASS.FUNCTION>
   rule    = <CLASS.FUNCTION>() must <do this>
   because = <the reason>
3. kind "procedural" (only if there is a real, reusable way of working). Fields:
   when      = <the kind of symptom>
   check     = <where to look first>
   then_step = <the next step>

FILLED-IN EXAMPLE (a different ticket, for style only; never copy its words)
  Ticket: "remove() on a missing key crashes"
  record_outcome(kind="episodic", where="Store.remove", symptom="remove() raised KeyError on a missing key",
                 cause="it indexed the dict directly without checking the key", fix="skip keys that are not in the dict")
  record_outcome(kind="semantic", where="Store.remove", rule="Store.remove() must tolerate missing keys",
                 because="callers pass lists that may include keys already deleted")
  (procedural skipped: nothing reusable beyond this fix)

NEVER WRITE
  - vague advice such as "be careful with keys" or "fixed the bug"
  - a copy of the ticket text
  - a rule the patch does not show

RETIRING OLD NOTES
If a lesson that was shown to the worker turned out to be wrong or outdated, call retire_fact with its memory id and a \
reason (and the id of the lesson that replaces it, if you just wrote one).

When you are done, reply with one short sentence and no tool call."""


def say(cfg, who, text):
    if cfg.verbose:
        print(f"[{time.strftime('%H:%M:%S')}] {who:<9} {text}", flush=True)


def run_loop(llm, box, system, user, api, task_id, only=None, max_steps=25, after_step=None, stop_when=None,
             recap_limit=6, nudge=None, needs_tool=False):
    """One model conversation about one task. Returns {status, steps, tokens, text}."""
    sess = api.start_session(task_id, box.agent)
    sid = sess.get("session_id")
    messages = [{"role": "system", "content": system}]
    prior = api.get_history(sid)["messages"] if sid and sess.get("resumed") else []
    if prior:                                       # a restarted agent: remind it what it did before
        recap = "\n".join(f"- {m['role']}: {m['content'][:200]}" for m in prior[-recap_limit:])
        user += f"\n\nYou already started this task earlier. The last things that happened:\n{recap}"
    messages.append({"role": "user", "content": user})
    if sid:
        api.log_message(sid, "user", user[:4000])
    tokens, text, status = 0, "", "max_steps"
    seen_calls, text_only = {}, 0
    for step in range(1, max_steps + 1):
        if nudge and max_steps > 10 and step == max_steps - 4:      # running out of steps: say so while it can still act
            messages.append({"role": "user", "content": nudge})
        try:
            reply = llm.chat(messages, tools=box.specs(only))
        except LLMError as exc:
            return {"status": "llm_error", "steps": step - 1, "tokens": tokens, "text": str(exc)[:200]}
        tokens += (reply.usage or {}).get("total_tokens", 0)
        messages.append(reply.message)
        text = reply.text or text
        if sid:
            api.log_message(sid, "agent", json.dumps({"text": reply.text, "calls": [
                {"name": c["name"], "arguments": c["arguments"]} for c in reply.tool_calls]})[:4000])
        if not reply.tool_calls:
            if needs_tool and text_only < 2 and not (stop_when and stop_when()):   # it talked instead of acting
                text_only += 1
                messages.append({"role": "user", "content": NEED_TOOL})
                if sid:
                    api.log_message(sid, "user", NEED_TOOL)
                continue
            status = "finished"
            break
        for c in reply.tool_calls:
            result = box.call(c["name"], c["arguments"])
            key = c["name"] + json.dumps(c["arguments"], sort_keys=True, default=str)
            seen_calls[key] = seen_calls.get(key, 0) + 1
            content = short(result)
            if seen_calls[key] >= 3:                  # a model stuck in a loop: tell it plainly
                content += (f"\nNOTE: you have made this exact call {seen_calls[key]} times and the answer will not "
                            f"change. Do something different now: use what you already know and make the edit, or search "
                            f"somewhere else.")
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": content})
            if sid:
                api.log_message(sid, "tool", short({"name": c["name"], "result": result}, 1500))
        if after_step:
            after_step(step, reply)
        if stop_when and stop_when():
            status = "stopped"
            break
    return {"status": status, "steps": step, "tokens": tokens, "text": text}


# ---------------------------------------------------------------------------------------------------------
class Foreman:
    """Plain code. All its choices go through the role-scoped tools, so each one is recorded in the graph."""

    def __init__(self, box, cfg):
        self.box, self.cfg = box, cfg

    def setup(self, tickets, plan=True):
        ids = []
        for t in tickets:
            r = self.box.call("create_task", {"ticket": t})
            if not r["ok"]:
                raise RuntimeError(f"could not create {t['ticket_id']}: {r}")
            ids.append(t["ticket_id"])
        size, conflicts = {}, {i: set() for i in ids}
        for i in ids:
            size[i] = len(self.box.call("get_footprint", {"task_id": i})["functions"])
        for i in ids:
            for c in self.box.call("find_conflicts", {"task_id": i})["conflicts"]:
                if c["other_task_id"] in conflicts:
                    conflicts[i].add(c["other_task_id"])
                    conflicts[c["other_task_id"]].add(i)
        # Waves: each wave is a set of tickets that do not conflict with each other, so they can run together.
        waves, left = [], set(ids)
        while left:
            wave = []
            for i in sorted(left, key=lambda x: (len(conflicts[x] & left), size[x], x)):
                if not (conflicts[i] & set(wave)):
                    wave.append(i)
            waves.append(wave)
            left -= set(wave)
        order = [i for w in waves for i in w]
        d = self.box.call("set_task_order", {"ordered_task_ids": order}) if plan else {}    # "none" keeps the plan only to measure clashes
        say(self.cfg, "foreman", f"{len(ids)} tasks, {sum(len(v) for v in conflicts.values()) // 2} conflicting pairs, "
                                 f"{len(waves)} waves, order {order}")
        return {"order": order, "waves": waves, "conflicts": {k: sorted(v) for k, v in conflicts.items()},
                "decision_id": d.get("decision_id")}

    def tick(self):
        r = self.box.call("expire_stale_claims")
        if r.get("reopened"):
            say(self.cfg, "foreman", f"claims expired, reopened {r['reopened']}")
        return r.get("reopened", [])


# ---------------------------------------------------------------------------------------------------------
class Worker:
    def __init__(self, llm, box, cfg, name):
        self.llm, self.box, self.cfg, self.name = llm, box, cfg, name
        self.api = box.api
        self.history = []                           # one dict per task attempt, for the run report

    def claim_next(self, skip=()):
        tasks = self.box.call("list_open_tasks")["tasks"]
        if self.cfg.coordination == "none":                             # no plan: take any open ticket
            random.shuffle(tasks)
        else:
            tasks.sort(key=lambda t: len(t["conflicts_with_active"]))  # try the ones least likely to be refused first
        for t in tasks:
            if t["task_id"] in skip:
                continue
            info = self.box.call("get_task", {"task_id": t["task_id"]})
            if info.get("attempt_count", 0) >= self.cfg.max_attempts:
                continue
            res = self.box.call("claim_task", {"task_id": t["task_id"]})
            if res.get("ok"):
                say(self.cfg, self.name, f"claimed {t['task_id']} ({res['reason']})")
                return t["task_id"]
        return None

    def context(self, task_id):
        b = self.box
        t = b.call("get_task", {"task_id": task_id})
        fp = b.call("get_footprint", {"task_id": task_id})
        parts = [f"Ticket {task_id}: {t['title']}", t["description"] or "", "",
                 "Failing tests that must pass:", *[f"  {x}" for x in t["failing_tests"]], "",
                 "Footprint (likely files): " + ", ".join(fp["files"]),
                 "Most suspicious functions: " + ", ".join(f["id"] for f in fp["functions"][:8])]
        if self.cfg.use_memory:
            hints = self.api.get_playbook(f"{t['title']} {t['description'] or ''}", k=2).get("hints", [])
            if hints:
                parts += ["", "Playbook hints (general know-how, check them against the code):"]
                parts += [f"  - {h['summary']}" for h in hints]
            mem = b.call("recall_similar", {"task_id": task_id}).get("memories", [])
            if mem:
                parts += ["", "Lessons saved from earlier tasks (hints, verify them):"]
                parts += [f"  [{m['kind']}] {m['summary']} (files: {', '.join(m['fix_files'])})" for m in mem]
        h = b.call("get_handoff", {"task_id": task_id}).get("note") if self.cfg.coordination == "graph" else None
        if h:
            parts += ["", "Notes left on this ticket by an earlier worker or reviewer:",
                      f"  done: {h['done']}", f"  remaining: {h['remaining']}", f"  tried: {h['tried']}"]
            if self.box.wt and b.call("apply_handoff_patch", {"task_id": task_id}).get("ok"):
                parts.append("  Their unfinished changes have been applied to your copy of the code.")
        return "\n".join(parts), t

    def work(self, task_id):
        """Work one claimed task until submitted, stuck, or out of steps."""
        t0 = time.time()
        self.box.recalled.clear()
        self.box.current_task = task_id
        ctx, task = self.context(task_id)
        steps = {"n": 0, "tests": ""}

        def after(step, reply):
            graph = self.cfg.coordination == "graph"
            if graph:
                self.box.call("heartbeat", {"task_id": task_id})
            steps["n"] = step
            if self.cfg.kill.get(self.name) == step:
                raise Crash(f"{self.name} stopped after {step} steps")
            if graph and step % self.cfg.checkpoint_every == 0:
                self._checkpoint(task_id, reply.text)

        def submitted():
            return self.api.get_task(task_id)["status"] != "claimed"

        only = [n for n in self.box.names() if n not in ("claim_task", "list_open_tasks", "get_task", "heartbeat",
                                                          "get_handoff", "apply_handoff_patch")]
        if not self.cfg.use_memory:
            only = [n for n in only if n != "recall_similar"]
        res = run_loop(self.llm, self.box, WORKER_SYSTEM.replace("{name}", worker_name(self.name)), ctx, self.api, task_id, only=only,
                       max_steps=self.cfg.max_steps, after_step=after, stop_when=submitted, nudge=WORKER_NUDGE, needs_tool=True)
        status = self.api.get_task(task_id)["status"]
        self.box.current_task = None
        if status == "claimed":                      # stuck or out of steps: leave a note and give the task back
            if self.cfg.coordination == "graph":
                self._checkpoint(task_id, res["text"], final=True)
            self.box.call("release_claim", {"task_id": task_id})
            say(self.cfg, self.name, f"gave up {task_id} for now ({res['status']})")
        rec = {"task": task_id, "worker": self.name, "steps": res["steps"], "tokens": res["tokens"],
               "t_start": round(t0, 2), "t_end": round(time.time(), 2), "seconds": round(time.time() - t0, 1), "loop": res["status"], "task_status": status,
               "used_memory": sorted(self.box.recalled)}
        self.history.append(rec)
        return rec

    def _checkpoint(self, task_id, text, final=False):
        files = self.box.wt.changed_files(self.box.worktree) if self.box.wt else []
        trace = getattr(self.box, "trace", [])
        recent = "; ".join(trace[-8:]) or "nothing yet"
        self.box.call("write_handoff", {
            "task_id": task_id,
            "done": ((text or "").strip() + " Recent steps: " + recent).strip()[:400],
            "remaining": "gave up for now; see tried" if final else "not finished (worker may have stopped)",
            "tried": ("changed " + ", ".join(files)) if files else "no files changed yet"})


# ---------------------------------------------------------------------------------------------------------
class Reviewer:
    def __init__(self, llm, box, cfg):
        self.llm, self.box, self.cfg = llm, box, cfg
        self.api = box.api

    def review(self, task_id):
        t0 = time.time()
        res = self.api.get_result(task_id)
        user = (f"Review the submitted patch for task {task_id}.\nWorker's summary: {res.get('summary')}\n"
                f"Worker's own test report: {json.dumps(res.get('test_report'))}")
        out = run_loop(self.llm, self.box, REVIEWER_SYSTEM.replace("{name}", REVIEWER_NAME), user, self.api, task_id,
                       max_steps=8, needs_tool=True,
                       stop_when=lambda: self.api.get_task(task_id)["status"] != "submitted")
        status = self.api.get_task(task_id)["status"]
        approved, reasons = self.box.decisions.get(task_id, (None, []))
        say(self.cfg, "reviewer", f"{task_id}: {'APPROVED' if status == 'approved' else 'rejected' if approved is False else 'no decision'}"
            + ("" if status == "approved" else f" {[r.get('code') for r in reasons]}"))
        if approved is False and self.cfg.coordination == "graph":   # pass the reasons on to whoever takes the task next
            self.api.write_handoff(task_id, self.box.agent, {
                "done": "A patch was submitted and rejected by the reviewer.",
                "remaining": "Fix: " + "; ".join(f"{r.get('code')}: {r.get('detail')}" for r in reasons),
                "tried": (res.get("summary") or "")[:300], "files_touched": res.get("files_touched", [])})
            stale = self.box._patch_path("handoff", task_id)      # a rejected patch is not a base to build on
            if os.path.exists(stale):
                os.remove(stale)
        return {"task": task_id, "status": status, "steps": out["steps"], "tokens": out["tokens"],
                "seconds": round(time.time() - t0, 1), "reasons": reasons}


class Historian:
    def __init__(self, llm, box, cfg):
        self.llm, self.box, self.cfg = llm, box, cfg
        self.api = box.api

    def remember(self, task_id, recalled):
        t0 = time.time()
        res = self.api.get_result(task_id)
        task = self.api.get_task(task_id)
        mem = self.api.recall_similar(task_id).get("memories", [])
        user = (f"Task {task_id} was approved.\nTicket: {task['title']}\n{task['description']}\n"
                f"Worker's summary: {res.get('summary')}\nFiles changed: {res.get('files_touched')}\n"
                f"Patch reference: {res.get('patch_ref')}\n")
        if mem:
            user += "Lessons the worker was shown:\n" + "\n".join(f"  {m['memory_id']}: {m['summary']}" for m in mem)
        out = run_loop(self.llm, self.box, HISTORIAN_SYSTEM.replace("{name}", HISTORIAN_NAME), user, self.api, task_id, max_steps=8)
        for mid in recalled:                          # the lessons the worker was shown did their job
            self.box.call("rate_memory", {"memory_id": mid, "worked": True})
        say(self.cfg, "historian", f"{task_id}: lessons saved ({out['steps']} steps)")
        return {"task": task_id, "steps": out["steps"], "tokens": out["tokens"], "seconds": round(time.time() - t0, 1)}
