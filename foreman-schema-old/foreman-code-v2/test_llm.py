"""Quick check of the LLM client against a tiny local fake server (no key, no internet)."""
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from llm import LLMBadRequest, LLMClient

SCRIPT = {}          # model -> list of (status, body) answers, used in order


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        status, payload = SCRIPT[body["model"]].pop(0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


def ok(text=None, calls=None):
    m = {"role": "assistant", "content": text}
    if calls:
        m["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": n, "arguments": a}} for n, a in calls]
    return 200, {"choices": [{"message": m}], "usage": {"total_tokens": 5}}


def main():
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d = tempfile.mkdtemp()
    llm = LLMClient(api_key="x", base_url=f"http://127.0.0.1:{srv.server_port}", primary="P", backup="B", rpm=6000,
                    cache_path=os.path.join(d, "c.sqlite"), log_path=os.path.join(d, "log.jsonl"), sleep=lambda s: None)
    msgs = [{"role": "user", "content": "hi"}]
    bad = []

    def check(cond, name):
        print(("PASS  " if cond else "FAIL  ") + name)
        if not cond:
            bad.append(name)

    SCRIPT["P"] = [(429, {}), (503, {}), ok("<think>hmm</think>hello")]
    r = llm.chat(msgs)
    check(r.text == "hello" and r.model_used == "P" and not r.fallback_used, "retries 429 and 503, hides thinking text")

    SCRIPT["P"] = [ok(calls=[("get_footprint", "{bad json")]), ok(calls=[("get_footprint", '{"task_id":"T-01"}')])]
    r = llm.chat(msgs, tools=[{"type": "function", "function": {"name": "get_footprint"}}])
    check(r.tool_calls[0]["arguments"] == {"task_id": "T-01"}, "re-asks after an unreadable tool call")

    SCRIPT["P"] = [(500, {})] * 5
    SCRIPT["B"] = [ok("from backup")]
    r = llm.chat(msgs)
    check(r.fallback_used and r.model_used == "B" and r.text == "from backup", "falls back when the main model keeps failing")

    SCRIPT["P"] = [(400, {"error": "bad"})]
    try:
        llm.chat(msgs)
        check(False, "400 does not go to the backup")
    except LLMBadRequest:
        check(not SCRIPT["B"], "400 does not go to the backup")

    SCRIPT["P"] = [ok("cached answer")]
    a = llm.chat(msgs, cache=True)
    b = llm.chat(msgs, cache=True)
    check(a.text == b.text == "cached answer" and b.cached and not a.cached, "same request comes from the cache")

    rows = [json.loads(x) for x in open(os.path.join(d, "log.jsonl"))]
    check(any(x["fallback_used"] for x in rows) and any(x["cached"] for x in rows), "every call is logged")
    print(f"\n{6 - len(bad)} of 6 passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
