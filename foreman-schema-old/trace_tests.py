"""Run a repo's test suite once and record, for every test: pass/fail, which functions it ran,
and which function called which. Output is a JSON file that load_code.py puts into the graph.

    python trace_tests.py --repo ~/Desktop/Foreman/tinydb --out trace.json

Run it from any folder; it starts pytest inside the repo. Needs pytest and pyyaml installed.
"""
import argparse
import json
import os
import subprocess
import sys

PLUGIN = '''
import json, os, sys, collections
import pytest

ROOT = os.path.realpath(os.getcwd())
PKG = ROOT + os.sep + "tinydb" + os.sep
OUT = os.environ["TRACE_OUT"]
tests = {}
cache = {}


def fid(code):
    """Function id like tinydb.table.Table.update. Inner functions count as their outer function."""
    key = code
    if key in cache:
        return cache[key]
    fn = os.path.realpath(code.co_filename)
    if not fn.startswith(PKG):
        cache[key] = None
        return None
    mod = os.path.relpath(fn, ROOT)[:-3].replace(os.sep, ".")
    parts = []
    for p in code.co_qualname.split("."):
        if p.startswith("<"):
            break
        parts.append(p)
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    cache[key] = mod + "." + ".".join(parts) if parts else None
    return cache[key]


def from_pytest(frame):
    """True if this tinydb call was started by pytest itself (for example, printing a failed
    assertion calls __repr__). Those calls only happen in failing tests, so they would look
    like clues about the bug. We skip them."""
    f = frame.f_back
    while f is not None and fid(f.f_code) is not None:
        f = f.f_back
    if f is None:
        return False
    name = f.f_code.co_filename
    return "_pytest" in name or "pluggy" in name


def make(funcs, edges):
    def prof(frame, event, arg):
        if event != "call":
            return
        callee = fid(frame.f_code)
        if callee is None or from_pytest(frame):
            return
        funcs[callee] += 1
        back = frame.f_back
        if back is not None:
            caller = fid(back.f_code)
            if caller is not None and caller != callee:
                edges[(caller, callee)] += 1
    return prof


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    funcs, edges = collections.Counter(), collections.Counter()
    sys.setprofile(make(funcs, edges))
    try:
        return (yield)
    finally:
        sys.setprofile(None)
        t = tests.setdefault(item.nodeid, {"status": "pass"})
        t["functions"] = dict(funcs)
        t["edges"] = [[a, b, n] for (a, b), n in edges.items()]


def pytest_runtest_logreport(report):
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        t = tests.setdefault(report.nodeid, {"status": "pass"})
        t["status"] = {"passed": "pass", "failed": "fail", "skipped": "skip"}[report.outcome]
        t.setdefault("functions", {})
        t.setdefault("edges", [])


def pytest_sessionfinish(session):
    json.dump(tests, open(OUT, "w"))
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", default="trace.json")
    args = ap.parse_args()
    repo = os.path.realpath(os.path.expanduser(args.repo))
    out = os.path.realpath(args.out)
    plug_dir = os.path.join(os.path.dirname(out), ".trace_plugin")
    os.makedirs(plug_dir, exist_ok=True)
    with open(os.path.join(plug_dir, "foreman_trace.py"), "w") as fh:
        fh.write(PLUGIN)
    env = dict(os.environ, TRACE_OUT=out, PYTHONHASHSEED="0",
               PYTHONPATH=os.pathsep.join([plug_dir, repo]))
    cmd = [sys.executable, "-m", "pytest", "-p", "foreman_trace", "-p", "no:cacheprovider",
           "-o", "addopts=", "-q", "--tb=no", "tests"]
    subprocess.run(cmd, cwd=repo, env=env)
    data = json.load(open(out))
    n_fail = sum(t["status"] == "fail" for t in data.values())
    print(f"traced {len(data)} tests ({n_fail} failing) -> {out}")


main()
