"""Quick health check: can Python talk to FalkorDB, write a graph and read it back?"""
import os
import sys

from falkordb import FalkorDB

HOST = os.environ.get("FALKORDB_HOST", "localhost")
PORT = int(os.environ.get("FALKORDB_PORT", "6379"))

try:
    db = FalkorDB(host=HOST, port=PORT)
    g = db.select_graph("setup_check")
    g.query("CREATE (:Person {name:'Dev'})-[:BUILDS]->(:Project {name:'Foreman'})")
    rows = g.query("MATCH (p:Person)-[:BUILDS]->(x:Project) RETURN p.name, x.name").result_set
    g.delete()
except Exception as exc:  # noqa: BLE001
    print(f"NOT OK: could not use FalkorDB at {HOST}:{PORT}")
    print(f"  reason: {type(exc).__name__}: {exc}")
    print("  Check: is Docker Desktop open and running? Did you run `docker compose up -d`?")
    sys.exit(1)

assert rows == [["Dev", "Foreman"]], rows
print(f"OK: FalkorDB at {HOST}:{PORT} accepted a write and returned it: {rows[0]}")
