# Foreman agents (workers, reviewer, historian, foreman)

| File | What it is |
|---|---|
| `llm.py` | Model client: rate limit, retry, backup model, optional cache, call log |
| `worktrees.py` | One git worktree per worker, from a neutral one-commit copy of the seeded repo |
| `tools.py` | Each role's tools (a role can only call its own), plus the worker's safe file and test tools |
| `show.py`, `results.py`, `export_replay.py`, `test_show.py` | Turn a run in the graph into the moments the screen plays; the numbers panel; save a run as a replay file; test |
| `ui/` | The screen (`index.html`), with the earlier graph-only board in `ui/classic/` |
| `agents.py` | The four agents and the model loop |
| `run_crew.py` | Runs the whole crew on the tickets and writes `run_report.json` |
| `eval_runner.py`, `race_test.py` | The three-setup comparison and the claim race (see `EVAL.md`) |
| `llm_check.py` | One-minute check that both models can call tools on NVIDIA's endpoint |
| `test_llm.py`, `test_tools.py`, `test_crew.py`, `test_new.py`, `test_eval.py` | Tests that need no key (a pretend model makes the tool calls) |

## How to run

See the main `README.md` (section 8, "Run it"). The short version, from this folder:

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run_all.sh --screen          # just watch the recorded run, no database and no key
./run_all.sh                   # the whole crew (needs Docker, and your own NVIDIA key in .env)
QUICK=1 ./run_all.sh           # one worker, one ticket, about 3 minutes
```

`cp .env.example .env` and put your own NVIDIA key in `.env`. Never commit it, and never paste it in a chat.

Some `test_*.py` files need the answer-key kit (`--kit ../a2-eval-kit`). That kit is not in this repository on purpose, so the agents can never see the answers.
