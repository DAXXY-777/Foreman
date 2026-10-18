# The three-setup comparison

Same bugs, same model, same tools, same crash. Only the coordination changes.

| Setup | What it has |
|---|---|
| **No coordination** (`none`) | No plan, no safe claims, no conflict checks, no claim expiry, no handoffs, no memory. Workers pick any open bug and take it with a simple "look, then take" claim. |
| **Graph only** (`graph`) | The graph plans the work, grants each claim safely, blocks bugs that touch the same code, reopens a dead worker's bug and passes its note on. No memory. |
| **Full system** (`full`) | Everything in Graph only, plus memory: the Historian writes lessons, workers recall them. |

Kept the same in all three, so the comparison isolates coordination: the model, the tickets, the footprint hint
(which files a bug probably touches), the tools, the Reviewer (it approves only fixes that pass), the step budget and
the crash (`w1` dies after 6 steps, on whatever bug it holds).

## How a run is scored

After every run, all approved fixes are merged, in approval order, into one fresh copy of the buggy code and the
whole test suite is run. **Bugs fixed in the merged code** is the number that counts: it is what really works, not
what the agents said. The other columns:

- **Double claims**: two workers holding one bug (from the graph's own record).
- **Clashing bugs worked on together**: two bugs that touch the same code, worked at the same time.
- **Duplicate work**: two workers working the same bug at the same time.
- **Wasted worker sessions**: worker sessions beyond one per approved bug (retries, lost claims, give-ups).
- **Crashed bug recovered**: whether the bug held by the killed worker was eventually approved.

## Commands (database running)

Once, to give the comparison its own graph (your real run's data is left alone):

    python ../foreman-schema/load_code.py --repo ../tinydb --repo-name tinydb_eval

The claim race (no model needed, about 1 minute):

    python race_test.py

The comparison (about 3 hours for 2 repeats; stop and restart any time, finished runs are skipped):

    caffeinate -i python eval_runner.py --seeded ../tinydb --repeats 2

Results: `eval_results.md` (tables) and `eval_results.json`; every run's full report is in `eval_runs/`.

## Honest limits (put these in the README)

- A handful of runs per setup is a small sample. Report the averages and the per-run numbers, and say how many runs.
- "No coordination" is a reasonable simple baseline, not the worst possible one.
- The bugs were planted by us, and each ticket has its own tests, so most patches do not overlap. Expect the
  biggest differences in crash recovery, duplicate work and speed, and a smaller one in the final merged code.
- Everything runs on NVIDIA's free tier, so timing depends on how busy the service is.
