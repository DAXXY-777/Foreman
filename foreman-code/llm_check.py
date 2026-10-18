"""Quick check that both models work on NVIDIA's endpoint, including tool calls. About 1 minute.

    python llm_check.py

Prints a small table and writes llm_compat.json (it goes into the README as the compatibility table).
"""
import json
import statistics
import time

from llm import LLMClient, LLMError

TOOL = {"type": "function", "function": {
    "name": "get_footprint", "description": "Files and functions a task is likely to touch.",
    "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}}


def check_model(model):
    llm = LLMClient(primary=model, backup=model, cache_path=":memory:", log_path="llm_check_calls.jsonl",
                    timeout=45, max_retries=1)
    out, notes = {}, {}

    def attempt(name, fn):
        print(f"  running {name} ...", flush=True)
        try:
            ok, note = fn()
        except (LLMError, Exception) as exc:                  # noqa: BLE001
            ok, note = False, f"{type(exc).__name__}: {str(exc)[:160]}"
        out[name] = ok
        notes[name] = note
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  {note}", flush=True)

    def smoke():
        r = llm.chat([{"role": "user", "content": "Reply with just the word OK."}], max_tokens=300, allow_fallback=False)
        return bool(r.text), repr(r.text[:40])

    def tool_call():
        r = llm.chat([{"role": "user", "content": "What files will task T-07 touch? Use the tool to find out."}],
                     tools=[TOOL], max_tokens=1500, allow_fallback=False)
        ok = bool(r.tool_calls) and r.tool_calls[0]["name"] == "get_footprint" and r.tool_calls[0]["arguments"].get("task_id") == "T-07"
        return ok, f"tool_calls={[(c['name'], c['arguments']) for c in r.tool_calls]}"

    def round_trip():
        first = llm.chat([{"role": "user", "content": "What files will task T-07 touch? Use the tool to find out."}],
                         tools=[TOOL], max_tokens=1500, allow_fallback=False)
        if not first.tool_calls:
            return False, "no tool call to answer"
        c = first.tool_calls[0]
        msgs = [{"role": "user", "content": "What files will task T-07 touch? Use the tool to find out."}, first.message,
                {"role": "tool", "tool_call_id": c["id"], "content": json.dumps({"files": ["tinydb/table.py"]})}]
        r = llm.chat(msgs, tools=[TOOL], max_tokens=1500, allow_fallback=False)
        return bool(r.text) and not r.tool_calls and "table" in r.text.lower(), repr(r.text[:60])

    def latency():
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            llm.chat([{"role": "user", "content": "What files will task T-07 touch? Use the tool."}], tools=[TOOL],
                     max_tokens=1500, allow_fallback=False)
            times.append(time.perf_counter() - t0)
        out["median_s"], out["max_s"] = round(statistics.median(times), 1), round(max(times), 1)
        return True, f"median {out['median_s']} s, slowest {out['max_s']} s over 5 tool calls"

    print(model, flush=True)
    for name, fn in (("smoke call", smoke), ("tool call", tool_call), ("tool round trip", round_trip), ("latency", latency)):
        attempt(name, fn)
    return {"results": out, "notes": notes}


if __name__ == "__main__":
    base = LLMClient()
    results = {m: check_model(m) for m in (base.primary, base.backup)}
    json.dump(results, open("llm_compat.json", "w"), indent=2)
    print("\nSaved llm_compat.json")
