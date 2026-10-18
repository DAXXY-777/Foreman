# Foreman agents (workers, reviewer, historian, foreman)

| File | What it is |
|---|---|
| `llm.py` | Model client: rate limit, retry, backup model, optional cache, call log |
| `worktrees.py` | One git worktree per worker, from a neutral one-commit copy of the seeded repo |
| `tools.py` | Each role's tools (a role can only call its own), plus the worker's safe file and test tools |
| `agents.py` | The four agents and the model loop |
| `run_crew.py` | Runs the whole crew on the tickets and writes `run_report.json` |
| `eval_runner.py`, `race_test.py` | The three-setup comparison and the claim race (see `EVAL.md`) |
| `llm_check.py` | One-minute check that both models can call tools on NVIDIA's endpoint |
| `test_llm.py`, `test_tools.py`, `test_crew.py`, `test_new.py`, `test_eval.py` | Tests that need no key (a pretend model makes the tool calls) |

## First run (database already running and TinyDB code loaded)

1. Put this folder next to `foreman-schema` and `foreman-db-setup`.
2. `source ../foreman-db-setup/.venv/bin/activate` then `pip install requests pytest pyyaml`
3. `cp .env.example .env` and put your NVIDIA key in `.env` (never paste it in a chat).
4. `python llm_check.py` (both models should PASS the tool call and round trip).
5. One worker, one ticket: `python run_crew.py --seeded ../tinydb --workers 1 --max-tasks 1`
6. The full crew: `python run_crew.py --seeded ../tinydb --workers 3`
   - Kill a worker to see the takeover: add `--kill w1:4 --claim-ttl 60`
   - Memory off (for the comparison): add `--no-memory`

Tests without a key (database must be running):
`FALKORDB_PORT=6379 python test_crew.py --seeded ../tinydb --kit ../a2-eval-kit`
