# Evaluation results

| Setup | Runs | Bugs fixed in the merged code (of 10) | Double claims | Clashing bugs worked on together | Duplicate work | Wasted worker sessions | Sent back by reviewer | Crashed bug recovered | Time (min) | Model calls | Tokens (million) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| No coordination | 2 | 9 | 0 | 1 | 0 | 1.5 | 0 | 0 of 2 | 32.7 | 240.5 | 1.8 |
| Graph only | 2 | 9 | 0 | 0 | 1.5 | 8 | 0 | 2 of 2 | 35.4 | 266.5 | 1.5 |
| Full system (graph + memory) | 2 | 9.5 | 0 | 0 | 1 | 4.5 | 1 | 2 of 2 | 42.3 | 320.5 | 2.0 |

Every number is an average over the runs of that setup. Per-run numbers:

| Run | Fixed in merged code | Approved | Regressions | Double claims | Duplicate work | Stalled | Time (min) |
|---|---|---|---|---|---|---|---|
| eval-full-1 | 10 | 10 | 0 | 0 | 2 | False | 34.4 |
| eval-full-2 | 9 | 9 | 0 | 0 | 0 | False | 50.2 |
| eval-graph-1 | 10 | 10 | 0 | 0 | 0 | False | 20.6 |
| eval-graph-2 | 8 | 8 | 0 | 0 | 3 | False | 50.2 |
| eval-none-1 | 9 | 9 | 0 | 0 | 0 | True | 20.7 |
| eval-none-2 | 9 | 9 | 0 | 0 | 0 | True | 44.7 |
