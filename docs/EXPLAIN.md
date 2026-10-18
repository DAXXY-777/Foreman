# How to explain Foreman (read this before the video and before answering any judge)

The hackathon rules say you must be able to explain your code. This page is the short version, in plain words. Say it in your own words. Open the file named in each section and read the function once. If you can find it and read it aloud, you know it.

## 1. The idea in 30 seconds

- Ten bugs are planted in a copy of TinyDB (a small open-source database library).
- A crew of AI agents fixes them: three Workers, one Reviewer, one Historian, and a Foreman.
- The agents never talk to each other directly. They all read and write **one FalkorDB graph**. The graph is the shared memory and the referee.
- The graph decides who may take which bug, stops two bugs that touch the same code from running together, takes a stalled worker's bug back and hands the next worker the old notes, and stores lessons so the next bug on the same code starts smarter.

## 2. The parts and where they live

| Part | File | One line |
|---|---|---|
| Graph rules and queries | `foreman-schema/schema.py` | Every important Cypher query, each one a single statement |
| Graph functions the agents call | `foreman-schema/graph_api.py` | `claim_task`, `find_conflicts`, `expire_stale_claims`, `write_handoff`, `recall_similar`, `explain`, and so on |
| Load the code into the graph | `foreman-schema/load_code.py` | Files, functions, tests, who calls whom, which test runs which function |
| The agents | `foreman-code/agents.py` | Foreman, Worker, Reviewer, Historian, each a loop |
| Each role's tools | `foreman-code/tools.py` | A role can only call its own tools; the code enforces the rules |
| Model client | `foreman-code/llm.py` | Rate limit, retries, backup model, call log |
| One git folder per worker | `foreman-code/worktrees.py` | Workers cannot overwrite each other's files |
| Run everything | `foreman-code/run_crew.py` | Starts the crew and scores the run |
| Compare three setups | `foreman-code/eval_runner.py` | None, graph only, graph plus memory |
| Screen | `foreman-code/show.py`, `foreman-code/ui/index.html` | Turns a run into moments the page plays |

## 3. One ticket, start to finish

1. **Foreman (plain code, no model)** creates all 10 tickets in the graph, links each to its failing tests, and works out each ticket's **footprint**: the functions its failing tests run.
2. It stores a `CONFLICTS_WITH` link between two tickets whose footprints overlap. It sorts tickets into waves of tickets that do not conflict, and puts the most conflicted first (they form the longest chain).
3. A **Worker** (plain code) lists open tickets and tries to claim one. The claim is one Cypher statement: it only succeeds if the ticket is open **and** no conflicting ticket is claimed. If it fails, the worker simply tries another.
4. The Worker's model is told the ticket, the failing tests, the footprint, and the **lessons recalled from the graph** (if memory is on). It edits code in its own git worktree and runs the tests with tools.
5. Every step it **heartbeats** (extends its claim). Every 3 steps it writes a **handoff note** into the graph (what it did, what it tried). If it gives up, it writes a final note and releases the claim.
6. If a worker dies, its claim runs out. The Foreman's `tick` calls expiry: the ticket goes back to open, a Decision records why, and the next worker reads the note.
7. When the worker submits, the **Reviewer** gets the patch, applies it in a clean folder, runs the full test suite, and decides. The tools **refuse an approval** if the ticket's tests fail, if other tests broke, if files outside the footprint changed, or if test files were edited.
8. After an approval the **Historian** writes a lesson (episodic, semantic or procedural). A lesson with a missing field is refused. Lessons are linked to the functions they are about.
9. The next ticket that touches the same functions **recalls** those lessons. When a recalled lesson helps, its success count goes up, so good lessons rank higher.

## 4. The rules and where each is enforced (not by the AI)

| Rule | Where | Why it is trustworthy |
|---|---|---|
| Only one worker gets a ticket | `CLAIM` in `schema.py` | One Cypher statement; the database runs it atomically |
| Clashing tickets never run together | `CLAIM` checks `CONFLICTS_WITH` | The check is inside the same statement as the claim |
| A dead worker's ticket comes back | `EXPIRE_STALE` / `expire_stale_claims` | Plain time comparison on `claim_expires_at` |
| The next worker sees old notes | `write_handoff` / `get_handoff` | Notes are nodes on the ticket |
| The reviewer cannot rubber-stamp | `_t_review_decision` in `tools.py` | Returns `CANNOT_APPROVE` with the reasons |
| Scores are real | `score_run` in `run_crew.py` | Merges approved patches into a fresh copy and runs all tests |
| Every choice can be explained | `explain` in `graph_api.py` | Follows a `Decision` to the agent, ticket, code and conflicts |

## 5. Questions judges may ask, with plain answers

**Why a graph and not a normal database?**
The question the crew keeps asking is "which tickets touch the same code?" That is a path question: ticket to failing test to function to another ticket. In a graph it is one short query. Tickets, code, lessons and decisions are all things linked to other things, so one graph holds all of them.

**What does FalkorDB do that matters here?**
Four jobs: it grants claims atomically, it stores the code map and runs the conflict query, it holds the shared handoffs and conversation history, and it stores memory that is recalled by code overlap. Without it the crew would have no shared state.

**What stops two workers taking the same bug?**
The claim query. It checks "open and no claimed conflict" and sets the owner in one statement. We tested 50 workers racing for one ticket, and 100 in the load test; a naive "look, then take" version gave several workers the same ticket, the atomic one gave exactly one. (Numbers in the README.)

**How do you know two bugs conflict?**
From the code, not a guess. Each ticket has failing tests. The graph knows which functions each test runs. A function gets a weight: high if the ticket's failing tests run it and few passing tests do, and lower if almost every test runs it. Two tickets conflict if their weighted footprints overlap by 0.10 or more.

**What is the memory?**
Three kinds. Episodic: what happened on a ticket (where, symptom, cause, fix). Semantic: a rule that holds in this code. Procedural: a habit ("when X, check Y, then Z"). Each is linked to the functions it is about. Recall finds lessons about the same functions the new ticket touches and ranks them by how targeted and how often they helped. Old ones can be retired, not deleted.

**Did memory help?**
Honestly: not in our numbers. Graph plus memory took the longest and used the most model calls, and its bugs-fixed average (9.5) is within the run-to-run spread. What the data shows clearly is that **graph coordination** mattered: both runs without it stalled and the crashed worker's bug was never recovered; all 4 graph runs recovered it and none stalled. Bugs fixed was about the same (9, 9 and 9.5 on average). We did not claim more than the data shows.

**What happens if a worker crashes?**
Its claim expires, the ticket reopens, a decision is recorded, and the next worker starts from the dead worker's handoff note. In all 4 graph runs (2 graph only, 2 with memory), the killed worker's bug was recovered; in the 2 runs without coordination it was not.

**What data survives a restart?**
Everything in the graph. The database writes a change log to disk (`appendonly yes` in `docker-compose.yml`). `foreman-db-setup/check_persistence.sh` writes a lesson, kills the container hard, restarts it and checks the lesson is still there.

**Do the AI models make the important decisions?**
No. The models write code, review patches and write lessons. Which ticket may be claimed, which tickets clash, when a claim expires, and whether an approval is allowed are all plain code and database queries. That is why the numbers are trustworthy.

**What are the models?**
`nvidia/nemotron-3-super-120b-a12b` for Workers, Reviewer and Historian; `openai/gpt-oss-20b` as a backup if the main model fails. Both passed the tool-call check. The Foreman is plain code, no model.

**What did AI help you build?**
Claude helped write first drafts of code, queries, tests and the screen, and helped read run results. You chose the idea and design, ran every test yourself, and reviewed the code. See `AI-USAGE.md`.

**What is on the public page, and is it live?**
A replay of one real run (run-5: 10 of 10 approved in about 29.5 minutes, 0 double claims) recorded from FalkorDB. It is labelled "Replay". Live runs need your own key and database, and the README shows how.

**What are the weak points?**
- The comparison is a small sample (two runs per setup), on a free-tier model service whose speed varies (the same setup took 20 to 50 minutes).
- The bugs are planted by us, and each has its own tests, so most patches do not overlap.
- Memory's benefit is not proven by our numbers.
- Graph setups used more worker sessions that ended without an approved fix (8 and 4.5 against 1.5). We have not broken that down.
- The model sometimes gives up or repeats a mistake; the crew recovers by handing the ticket on.

**How is data kept separate between repos?**
One graph per repo (`foreman_<repo>`). Claims, tickets and lessons for one repo cannot be seen from another.

**Why did run-4 get 6 of 10 and run-5 get 10 of 10?**
Between them we fixed two problems the logs showed: agents giving up too early, and a poor order of work (clashing tickets were left for the end). Run-5 and the comparison runs are after those fixes.

## 6. Three things to show live (never skip these)

1. **Atomic claim**: run `python race_test.py` and show the table: atomic gives exactly one winner every round, the naive claim does not.
2. **Connected-code conflict**: on the Graph page, jump to "Clashing ticket held back" and show the dashed arc and the lock on the Board.
3. **Restart persistence**: run `foreman-db-setup/check_persistence.sh` and show it ends with the lesson surviving.

Then show the three-setup table on the Numbers page.

## 7. If you get stuck

- Say what the code does at the level of the table above. It is fine to say "I would check `schema.py` for the exact line" and then open it.
- Do not claim more than the numbers show. Reading the honest limits aloud is a strength.
