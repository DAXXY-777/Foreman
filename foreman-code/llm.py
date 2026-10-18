"""LLM client for NVIDIA's OpenAI-compatible endpoint.

    llm = LLMClient()                              # reads .env
    reply = llm.chat(messages, tools=[...])        # reply.text, reply.tool_calls, reply.model_used

What it does for you:
  - Rate limit: at most `rpm` requests a minute across all threads (one shared limiter).
  - Retry: 429, 5xx, timeouts and dropped connections are retried with growing waits.
  - Fallback: if the main model still fails (or keeps sending broken tool calls), ONE call goes to the backup
    model. A 400 caused by our own bad request is never sent to the backup (it would fail the same way).
  - Cache (optional, `cache=True`): same request + same model = same answer, saved on disk. Off by default so
    evaluation runs are real runs. The cache key includes the model, so a backup answer is never served as a main one.
  - Log: every call is appended to llm_calls.jsonl with the model used, whether fallback fired, and why.
"""
import hashlib
import json
import os
import random
import re
import sqlite3
import threading
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=None):
    """Tiny .env reader (no extra package). Values in the .env file win over anything already in the shell."""
    for p in [path] if path else [os.path.join(HERE, ".env"), os.path.join(HERE, "..", ".env")]:
        if p and os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")     # the .env file wins


class LLMError(Exception):
    pass


class LLMBadRequest(LLMError):
    """The server said our request is wrong (HTTP 400/401/403/404). Retrying or falling back will not help."""


class LLMReply:
    def __init__(self, text, tool_calls, message, model_used, fallback_used, usage, cached=False, latency=0.0,
                 fallback_reason=None):
        self.text = text
        self.tool_calls = tool_calls            # [{"id","name","arguments": dict}]
        self.message = message                  # assistant message, ready to append to the history
        self.model_used = model_used
        self.fallback_used = fallback_used
        self.fallback_reason = fallback_reason
        self.usage = usage or {}
        self.cached = cached
        self.latency = latency

    def __repr__(self):
        return f"<LLMReply {self.model_used} tools={[c['name'] for c in self.tool_calls]} text={self.text[:40]!r}>"


class _Limiter:
    """Hands out request slots at most `rpm` per minute, shared by every thread."""

    def __init__(self, rpm):
        self.gap = 60.0 / rpm
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.gap
        if start > now:
            time.sleep(start - now)

    def slow_down(self, seconds):
        with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + seconds)


class _Cache:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS c (k TEXT PRIMARY KEY, v TEXT)")
        self.db.commit()

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT v FROM c WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO c VALUES (?,?)", (key, json.dumps(value)))
            self.db.commit()


THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class LLMClient:
    def __init__(self, api_key=None, base_url=None, primary=None, backup=None, rpm=None,
                 cache_path=None, log_path=None, timeout=120, max_retries=4, sleep=time.sleep):
        load_env()
        self.key = api_key or os.environ.get("NVIDIA_API_KEY", "")
        self.base = (base_url or os.environ.get("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")).rstrip("/")
        self.primary = primary or os.environ.get("LLM_PRIMARY_MODEL", "nvidia/nemotron-3-super-120b-a12b")
        self.backup = backup or os.environ.get("LLM_BACKUP_MODEL", "openai/gpt-oss-20b")
        self.limiter = _Limiter(rpm or int(os.environ.get("LLM_RPM", "40")))
        self.timeout, self.max_retries, self.sleep = timeout, max_retries, sleep
        self.cache = _Cache(cache_path or os.path.join(HERE, ".llm_cache.sqlite"))
        self.log_path = log_path or os.path.join(HERE, "llm_calls.jsonl")
        self.log_lock = threading.Lock()
        self.stats = {"calls": 0, "cached": 0, "retries": 0, "fallbacks": 0}
        self.session = requests.Session()

    # ---- one raw HTTP call, with retries -------------------------------------------------------------
    def _post(self, model, body):
        """Returns the response JSON. Raises LLMBadRequest for our mistakes, LLMError when retries run out."""
        last = None
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                r = self.session.post(self.base + "/chat/completions", json={**body, "model": model},
                                      headers={"Authorization": f"Bearer {self.key}"}, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = f"network: {type(exc).__name__}"
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (400, 401, 403, 404, 422):
                    raise LLMBadRequest(f"HTTP {r.status_code}: {r.text[:300]}")
                last = f"HTTP {r.status_code}"
                if r.status_code == 429:
                    wait = float(r.headers.get("Retry-After", 0) or 0)
                    if wait:
                        self.limiter.slow_down(wait)
            if attempt < self.max_retries:
                self.stats["retries"] += 1
                self.sleep(min(30, 2 ** attempt + random.random()))
        raise LLMError(f"gave up on {model} after {self.max_retries + 1} tries ({last})")

    # ---- reading the answer -------------------------------------------------------------------------
    @staticmethod
    def _parse(data):
        """-> (text, tool_calls, message, usage, problem). `problem` is set when a tool call is unreadable."""
        msg = data["choices"][0]["message"]
        text = THINK_RE.sub("", msg.get("content") or "").strip()      # never treat thinking text as the answer
        calls, problem = [], None
        for c in msg.get("tool_calls") or []:
            raw = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                if not isinstance(args, dict):
                    raise ValueError("arguments are not an object")
            except ValueError as exc:
                problem = f"tool call '{c['function'].get('name')}' has unreadable arguments ({exc})"
                args = {}
            calls.append({"id": c.get("id") or f"call-{len(calls)}", "name": c["function"]["name"], "arguments": args})
        clean = {"role": "assistant", "content": text or None}
        if calls:
            clean["tool_calls"] = [{"id": c["id"], "type": "function",
                                    "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                   for c in calls]
        return text, calls, clean, data.get("usage") or {}, problem

    def _one_model(self, model, messages, tools, params, repairs=2):
        """Call one model; re-ask up to `repairs` times if a tool call comes back unreadable."""
        convo = list(messages)
        for attempt in range(repairs + 1):
            body = {"messages": convo, **params}
            if tools:
                body["tools"] = tools
            data = self._post(model, body)
            text, calls, msg, usage, problem = self._parse(data)
            if not problem:
                return text, calls, msg, usage
            convo = convo + [{"role": "assistant", "content": text or "(tool call)"},
                             {"role": "user", "content": f"Your last tool call could not be read: {problem}. "
                                                         "Call the tool again with valid JSON arguments."}]
        raise LLMError(f"{model} kept sending unreadable tool calls")

    # ---- the one call agents use ----------------------------------------------------------------------
    def chat(self, messages, tools=None, temperature=0.2, max_tokens=4096, cache=False, allow_fallback=True,
             extra=None):
        params = {"temperature": temperature, "max_tokens": max_tokens, **(extra or {})}
        t0 = time.perf_counter()
        self.stats["calls"] += 1

        def key_for(model):
            blob = json.dumps({"m": model, "msgs": messages, "tools": tools, "p": params}, sort_keys=True)
            return hashlib.sha256(blob.encode()).hexdigest()

        if cache:
            hit = self.cache.get(key_for(self.primary))
            if hit:
                self.stats["cached"] += 1
                reply = LLMReply(hit["text"], hit["calls"], hit["msg"], self.primary, False, hit["usage"],
                                 cached=True, latency=time.perf_counter() - t0)
                self._log(reply, "cache")
                return reply

        reason = None
        try:
            text, calls, msg, usage = self._one_model(self.primary, messages, tools, params)
            model, used_backup = self.primary, False
        except LLMBadRequest:
            raise
        except LLMError as exc:
            if not allow_fallback:
                raise
            reason = str(exc)
            self.stats["fallbacks"] += 1
            text, calls, msg, usage = self._one_model(self.backup, messages, tools, params)
            model, used_backup = self.backup, True

        if cache:
            self.cache.put(key_for(model), {"text": text, "calls": calls, "msg": msg, "usage": usage})
        reply = LLMReply(text, calls, msg, model, used_backup, usage, latency=time.perf_counter() - t0,
                         fallback_reason=reason)
        self._log(reply, "ok")
        return reply

    def _log(self, reply, status):
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": status, "model_used": reply.model_used,
               "fallback_used": reply.fallback_used, "fallback_reason": reply.fallback_reason,
               "cached": reply.cached, "latency_s": round(reply.latency, 2), "usage": reply.usage,
               "tools": [c["name"] for c in reply.tool_calls]}
        with self.log_lock:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(row) + "\n")


class ScriptedLLM:
    """A pretend model for tests. Give it a list of replies; each chat() call returns the next one.
    A reply is a string (plain text) or a dict {"text":..., "calls":[{"name":..., "arguments":{...}}]}
    or a function(messages, tools) that returns one of those."""

    primary = backup = "scripted"

    def __init__(self, replies):
        self.replies, self.i, self.seen = list(replies), 0, []
        self.stats = {"calls": 0, "cached": 0, "retries": 0, "fallbacks": 0}

    def chat(self, messages, tools=None, **_):
        self.seen.append(messages)
        self.stats["calls"] += 1
        r = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        if callable(r):
            r = r(messages, tools)
        if isinstance(r, str):
            r = {"text": r, "calls": []}
        calls = [{"id": f"s{self.i}-{n}", "name": c["name"], "arguments": c.get("arguments", {})}
                 for n, c in enumerate(r.get("calls", []))]
        msg = {"role": "assistant", "content": r.get("text") or None}
        if calls:
            msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                 for c in calls]
        return LLMReply(r.get("text", ""), calls, msg, "scripted", False, {})
