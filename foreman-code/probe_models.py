"""Tries a one-word question on several NVIDIA models and shows which ones answer, and how fast.

    python probe_models.py          (reads NVIDIA_API_KEY from .env; each model gets 25 seconds)
"""
import threading
import time

import requests

from llm import LLMClient

MODELS = ["deepseek-ai/deepseek-v4.1-flash", "z-ai/glm-5.3-flash", "openai/gpt-oss-20b",
          "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-nano-3-30b-a3b", "nvidia/nemotron-3.5-lightning-30b-a3b",
          "moonshotai/kimi-k2.6", "mistralai/mistral-large-2-instruct", "google/gemma-4-31b-it",
          "nvidia/llama-3.1-nemotron-70b-instruct"]
TOOL = [{"type": "function", "function": {"name": "get_footprint", "description": "Files a task touches.",
         "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}}]
llm = LLMClient()
rows = {}


def probe(model):
    t0 = time.time()
    try:
        r = requests.post(llm.base + "/chat/completions", headers={"Authorization": f"Bearer {llm.key}"}, timeout=25,
                          json={"model": model, "max_tokens": 400, "tools": TOOL, "tool_choice": "auto",
                                "messages": [{"role": "user", "content": "What files will task T-07 touch? Use the tool."}]})
        note = ""
        if r.status_code == 200:
            m = r.json()["choices"][0]["message"]
            calls = m.get("tool_calls") or []
            note = "TOOL CALL OK" if calls and calls[0]["function"]["name"] == "get_footprint" else "answered, no tool call"
        else:
            note = r.text[:80].replace("\n", " ")
        rows[model] = f"HTTP {r.status_code} in {time.time() - t0:4.1f}s  {note}"
    except Exception as exc:                                    # noqa: BLE001
        rows[model] = f"no answer in {time.time() - t0:4.1f}s  ({type(exc).__name__})"


ts = [threading.Thread(target=probe, args=(m,)) for m in MODELS]
[t.start() for t in ts]
[t.join() for t in ts]
for m in MODELS:
    print(f"{m:<45} {rows[m]}")
