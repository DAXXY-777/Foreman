#!/usr/bin/env bash
# Proves data survives a hard crash of the database container.
# Run from this folder, with the database already started (docker compose up -d).
set -euo pipefail
export FALKORDB_HOST="${FALKORDB_HOST:-localhost}" FALKORDB_PORT="${FALKORDB_PORT:-6379}"

wait_for_db () {
  for _ in $(seq 1 40); do
    if python3 -c "from falkordb import FalkorDB; FalkorDB(host='$FALKORDB_HOST', port=$FALKORDB_PORT).connection.ping()" 2>/dev/null; then return 0; fi
    sleep 1
  done
  echo "Database did not come back within 40 seconds"; return 1
}

wait_for_db
python3 - <<'PY'
import os
from falkordb import FalkorDB
g = FalkorDB(host=os.environ["FALKORDB_HOST"], port=int(os.environ["FALKORDB_PORT"])).select_graph("persistence_check")
g.query("MATCH (n) DETACH DELETE n")
g.query("CREATE (:Lesson {text:'this lesson must survive a crash'})")
print("1. wrote a lesson into the graph")
PY

sleep 3                                  # the change log is flushed to disk every second
echo "2. killing the database container (like a crash)..."
docker compose kill falkordb >/dev/null
docker compose up -d falkordb >/dev/null
wait_for_db
echo "3. database is back, reading the lesson..."

python3 - <<'PY'
import os, sys
from falkordb import FalkorDB
g = FalkorDB(host=os.environ["FALKORDB_HOST"], port=int(os.environ["FALKORDB_PORT"])).select_graph("persistence_check")
rows = g.query("MATCH (l:Lesson) RETURN l.text").result_set
g.delete()
if rows == [["this lesson must survive a crash"]]:
    print("OK: memory survived the restart")
else:
    print("NOT OK: the lesson is gone after restart:", rows)
    sys.exit(1)
PY
