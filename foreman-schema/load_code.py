"""Load a repo's code map into the graph: files, functions, who-calls-who, tests, and which tests run what.

    python load_code.py --repo ~/Desktop/Foreman/tinydb --trace trace.json [--repo-name tinydb]

--trace is the file made by trace_tests.py (a ready-made one is included).
The graph is named foreman_<repo-name>. It is deleted and rebuilt each time, because it is derived data.
"""
import argparse
import ast
import json
import os

import schema
from schema import q

ap = argparse.ArgumentParser()
ap.add_argument("--repo", required=True)
ap.add_argument("--trace", default="trace.json")
ap.add_argument("--repo-name", default="tinydb")
args = ap.parse_args()
repo = os.path.realpath(os.path.expanduser(args.repo))
pkg = os.path.join(repo, "tinydb")
if not os.path.isdir(pkg):
    raise SystemExit(f"NOT OK: no tinydb/ folder inside {repo}")


def module_name(path):
    rel = os.path.relpath(path, repo)[:-3].replace(os.sep, ".")
    return rel[: -len(".__init__")] if rel.endswith(".__init__") else rel


# ---- 1. read the source: files, functions (with line numbers), imports --------------------------
files, funcs, imports = [], {}, set()
for fn in sorted(os.listdir(pkg)):
    if not fn.endswith(".py"):
        continue
    path = os.path.join(pkg, fn)
    rel = os.path.relpath(path, repo)
    mod = module_name(path)
    files.append({"path": rel, "module": mod})
    tree = ast.parse(open(path).read())

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs[f"{mod}.{prefix}{node.name}"] = {
                    "id": f"{mod}.{prefix}{node.name}", "name": node.name, "file": rel,
                    "start": node.lineno, "end": node.end_lineno}
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}.")
    visit(tree.body, "")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:      # from .table import X
            base = mod.split(".")
            if not path.endswith("__init__.py"):
                base = base[:-1]
            base = base[: len(base) - (node.level - 1)]
            target = ".".join(base + ([node.module] if node.module else []))
            imports.add((rel, target))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tinydb"):
            imports.add((rel, node.module))
mod_to_file = {f["module"]: f["path"] for f in files}
imports = sorted({(a, mod_to_file[m]) for a, m in imports if m in mod_to_file and mod_to_file[m] != a})

# ---- 2. read the test trace ---------------------------------------------------------------------
trace = json.load(open(args.trace))
unknown = {f for t in trace.values() for f in t["functions"] if f not in funcs}
if unknown:
    print(f"NOTE: {len(unknown)} traced functions are not in the source (is the repo the same version?): "
          f"{sorted(unknown)[:3]}")
calls = {}
covers = []
for tid, t in trace.items():
    for f, n in t["functions"].items():
        if f in funcs:
            covers.append({"t": tid, "f": f, "n": n})
    for a, b, n in t["edges"]:
        if a in funcs and b in funcs:
            calls[(a, b)] = calls.get((a, b), 0) + n

# ---- 3. build the graph -------------------------------------------------------------------------
db = schema.connect()
name = schema.graph_name(args.repo_name)
try:
    db.select_graph(name).delete()
except Exception:  # noqa: BLE001
    pass
g = db.select_graph(name)
schema.create_schema(g)


def batch(query, rows, size=400):
    for i in range(0, len(rows), size):
        q(g, query, {"rows": rows[i:i + size]})


batch("UNWIND $rows AS r CREATE (:File {path:r.path, module:r.module})", files)
batch("""UNWIND $rows AS r
         CREATE (:Function {id:r.id, name:r.name, file:r.file, start_line:r.start, end_line:r.end, test_count:0})""",
      list(funcs.values()))
batch("UNWIND $rows AS r MATCH (f:Function {id:r.id}), (x:File {path:r.file}) CREATE (f)-[:DEFINED_IN]->(x)",
      list(funcs.values()))
batch("UNWIND $rows AS r MATCH (a:File {path:r.a}), (b:File {path:r.b}) CREATE (a)-[:IMPORTS]->(b)",
      [{"a": a, "b": b} for a, b in imports])
batch("UNWIND $rows AS r MATCH (a:Function {id:r.a}), (b:Function {id:r.b}) CREATE (a)-[:CALLS {n:r.n}]->(b)",
      [{"a": a, "b": b, "n": n} for (a, b), n in calls.items()])
batch("UNWIND $rows AS r CREATE (:Test {id:r.id, status:r.status, file:r.file})",
      [{"id": tid, "status": t["status"], "file": tid.split("::")[0]} for tid, t in trace.items()])
batch("UNWIND $rows AS r MATCH (x:Test {id:r.t}), (f:Function {id:r.f}) CREATE (x)-[:COVERS {n:r.n}]->(f)", covers)
q(g, schema.SET_TEST_COUNTS)

for label in ("File", "Function", "Test"):
    print(f"{label:9s}", q(g, f"MATCH (n:{label}) RETURN count(n)")[0][0])
for rel in ("DEFINED_IN", "IMPORTS", "CALLS", "COVERS"):
    print(f"{rel:10s}", q(g, f"MATCH ()-[r:{rel}]->() RETURN count(r)")[0][0])
print(f"OK: code loaded into graph '{name}'")
