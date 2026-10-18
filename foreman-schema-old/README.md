# Foreman graph schema v1.1 and the real TinyDB code

Files:
- `graph-schema-v1.pdf` / `.docx` : the plain-language design. Read this first.
- `schema.py` : creates the indexes and uniqueness rules, and holds the core queries.
- `demo_graph.py` : builds a small example graph and checks every Track 2 focus area on it.
- `trace_tests.py` : runs a repo's tests once and records what each test ran (a ready-made `trace.json` is included).
- `load_code.py` : loads the real TinyDB code, tests and call links into the graph `foreman_tinydb`.
- `tune_conflicts.py` : creates the 10 tickets, scores how much they overlap, and marks conflicts.
- `graph_api.py` : the 26 functions of contract v1.2 as one Python class.
- `contract_tests.py` : 14 tests for those functions.
- `load_test.py` : many workers and tenants at once; checks the rules hold and measures speed.
- `tickets.json` : the 10 symptom-only tickets that workers see.

## Run it (database must be running: `docker compose up -d` in the foreman-db-setup folder)

1. Put this folder next to `foreman-db-setup`, then in Terminal:
   `cd ~/Desktop/Foreman/foreman-schema`
2. Use the Python space you already made:
   `source ../foreman-db-setup/.venv/bin/activate`
3. Run:
   `python demo_graph.py`
4. You want to see `ALL CHECKS PASSED` at the end.
5. To look at it: open http://localhost:3000, choose the graph `foreman_demo_repo_a`, and run
   `MATCH (n)-[r]->(m) RETURN n, r, m`
6. To delete the demo graphs afterwards: `python demo_graph.py --clean`

## Load the real TinyDB and measure conflicts (after the demo passes)

1. `python load_code.py --repo ../tinydb`
   - You want: File 10, Function 117, Test 228, and `OK: code loaded into graph 'foreman_tinydb'`.
2. `python tune_conflicts.py`
   - You want: `6 of 19 ticket pairs marked as conflicting`.
3. In the browser (http://localhost:3000) choose graph `foreman_tinydb` and run:
   `MATCH (a:Function)-[c:CALLS]->(b:Function) RETURN a, c, b LIMIT 150`

## Run the contract tests (after load_code.py)

`python contract_tests.py --real`
- You want: `14 of 14 passed`. It takes under a minute.
- Without `--real` it runs 13 tests on a tiny made-up code graph and does not need load_code.py.

## Run the load test (after load_code.py)

`python load_test.py`
- You want: `ALL RULES HELD UNDER LOAD` at the end. It takes under a minute.
- It runs five things: 100 workers racing for one ticket, 50 workers finishing 300 tickets, 80% reads / 20% writes mixed, the conflict rule with the 10 real tickets, and 5 repos at once.
- Numbers are saved in `load_test_results.md` and `load_test_results.json`.
- `python load_test.py --quick` is a shorter run (about 10 seconds).
