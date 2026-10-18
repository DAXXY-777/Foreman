# AI usage in Foreman

This file says exactly where AI was used, as the hackathon rules ask.
There are two kinds of AI use: AI that **helped build** the project, and AI that **runs inside** the project.

## 1. AI that helped build the project

- **Claude (Anthropic)**, used through Claude Code / claude.ai, was the main coding and writing assistant.
- What Claude helped with:
  - First drafts of the Python code: the model client, the graph API, the five agents, the tools, the test runner, the evaluation runner, the replay server.
  - First drafts of the Cypher queries and the graph layout.
  - The test files (they use a pretend model, so they need no key).
  - The screen (the office room with the six animated agents, the graph view, the ticket board, the "ask the graph" chat and the numbers page) and this documentation.
  - Reading run results and finding bugs in earlier runs (for example: agents giving up too early, and the order of work).
- What the human (Dev) did:
  - Chose the idea, the track (Track 02: Agent Memory and Coordination) and the scope.
  - Decided the design: one shared graph, the five roles, role-limited tools, the three-setup comparison.
  - Ran every real test and run on his own machine and with his own keys, and read the results.
  - Approved the prompts and the UI before they were final.
  - Reviewed the code and can explain it (see `docs/EXPLAIN.md`).
- The AI did not run anything on the judges' behalf and was not given any secret keys. Keys live only in a local `.env` file that is never committed.

## 2. AI that runs inside the project

Foreman's agents are language models. All of them are called through NVIDIA's hosted endpoint.

| Use | Model | When |
|---|---|---|
| Main model for the Workers, the Reviewer and the Historian | `nvidia/nemotron-3-super-120b-a12b` | Always, first choice |
| Backup model | `openai/gpt-oss-20b` | Only if the main model fails after retries |

- The Foreman "Rex" is plain code and uses no model: it creates the tickets, asks the graph for conflicts and sets the order.
- The model-driven roles are: Workers "Mina", "Jo", "Kai" (fix bugs), Reviewer "Vera" (checks fixes), Historian "Lore" (writes lessons). Choosing which bug to claim is also plain code.
- Every model call is counted. The run report lists calls, retries, fallbacks and tokens.
- The model never writes to the graph directly. It can only use a small list of tools, and each role gets only its own tools. The code, not the model, enforces the rules (one owner per bug, no two clashing bugs at once, no approving a bad fix).

## 2b. Third-party art and code in the screen

- The six animated characters on the screen are rendered by the **bible-strong-avatar-lab** engine
  (https://github.com/smontlouis/bible-strong-avatar-lab, AGPL-3.0, © Stéphane Montlouis-Calixte). We used its
  Freddy, Nova, Strobi, Sunee, Cubee and Cloudee characters, recolored per agent. Because the engine is AGPL-3.0, this
  repository is public and the credit is shown on the page.
- The office scene behind them (room, windows, table, whiteboard) is drawn in SVG for this page. No generated image assets are used.

## 3. What is NOT AI

- Conflict detection is plain graph logic: two bugs clash if they touch the same function. No model decides that.
- Claiming a bug is one atomic database query, so two workers can never get the same bug.
- Test results come from really running the test suite on a real copy of the code.
- Memory ranking (which past lesson to show) is a formula over graph data: relevance, times helped, times used.

## 4. Things judges may want to know

- The 10 bugs are deliberately planted in a fork of TinyDB (an open-source project). The bug list and the original code are in the repo.
- The "generic playbook" in the prompts is a short, hand-written list of coding habits (read the test first, make a small change, run the tests). It is not copied from a model output.
- The recorded run shown on the public site is a **replay** of one real run stored in FalkorDB. It is labelled as a replay. Live mode runs only on your own machine with your own key.
- Numbers in the README come from real run reports (`run_report.json` and `eval_results.md`). None are estimated, except where marked "estimate".
