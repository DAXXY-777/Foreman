#!/usr/bin/env bash
# One command from an empty database to the screen.
#
#   ./run_all.sh                 full run: load code, run the crew, save the replay, open the screen
#   ./run_all.sh run-6           same, with your own run name (default: run-<time>)
#   QUICK=1 ./run_all.sh         one worker, one ticket (about 3 minutes): checks that everything is connected
#   ./run_all.sh --view run-5    skip the run: save that run as a replay and open the screen
#   ./run_all.sh --screen        just open the screen (uses saved replays, no database needed)
#
# Run it from this folder (foreman-code). Needs: Docker running, .env with your key, ../tinydb, ../foreman-schema.
set -u
cd "$(dirname "$0")"
ulimit -n 4096 2>/dev/null || true
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mSTOP: %s\033[0m\n' "$*"; exit 1; }

MODE=run; RUN=""
case "${1:-}" in
  --view)   MODE=view; RUN="${2:-}"; [ -n "$RUN" ] || die "say which run, e.g. ./run_all.sh --view run-5" ;;
  --screen) MODE=screen ;;
  "")       RUN="run-$(date +%d%H%M)" ;;
  *)        RUN="$1" ;;
esac

[ -d .venv ] && source .venv/bin/activate
PORT="${PORT:-8000}"

open_screen() {
  say "Opening the screen at http://localhost:$PORT   (Ctrl+C stops it)"
  ( sleep 2; (open "http://localhost:$PORT" || xdg-open "http://localhost:$PORT") >/dev/null 2>&1 ) &
  exec python -m uvicorn server:app --port "$PORT"
}
[ "$MODE" = screen ] && open_screen

say "1/6  Database"
if [ -z "${SKIP_DOCKER:-}" ] && ! docker ps --format '{{.Names}}' | grep -q '^foreman-falkordb$'; then
  docker start foreman-falkordb >/dev/null 2>&1 || (cd ../foreman-db-setup && docker compose up -d) || die "could not start Docker. Open Docker Desktop and try again."
fi
for i in $(seq 1 20); do
  python - <<'PY' && break
import sys, _paths
from graph_api import GraphAPI
try:
    GraphAPI("tinydb", "ping").g.query("RETURN 1"); sys.exit(0)
except Exception as e:
    sys.exit(1)
PY
  sleep 1; [ "$i" = 20 ] && die "the database did not answer on port 6379"
done
echo "database is answering"

if [ "$MODE" = run ]; then
  say "2/6  Key check"
  python llm_check.py || die "the model check failed. Fix .env (key) and try again."

  say "3/6  Load the TinyDB code map into the graph"
  (cd ../foreman-schema && python load_code.py --repo ../tinydb --trace trace.json) || die "load_code failed"

  say "4/6  Run the crew ($RUN)"
  if [ -n "${QUICK:-}" ]; then
    python run_crew.py --seeded ../tinydb --workers 1 --max-tasks 1 --run-id "$RUN" || die "the crew run failed"
  else
    python run_crew.py --seeded ../tinydb --workers 3 --run-id "$RUN" --claim-ttl 60 || die "the crew run failed"
  fi
fi

say "5/6  Save the run as a replay"
python export_replay.py --repo tinydb --run "$RUN" || die "could not save the replay"
[ -f eval_results.json ] && echo "found eval_results.json: the Numbers page will show the three-setup comparison"

say "6/6  Screen"
open_screen
