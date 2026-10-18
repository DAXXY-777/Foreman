# Load test results

Run on 2026-10-15 13:56 UTC, Linux-6.18.44-fc-v114-x86_64-with-glibc2.39, Python 3.13.16.
Numbers include the Python client, so they are end to end. Problems found: none.

## Safe claiming
- 100 workers claiming one ticket at the same instant, 10 rounds: exactly one winner in 10 of 10.
- Claim latency at that load: p50 93.2 ms, p95 131.1 ms, p99 145.1 ms.

## Full lifecycle
- 50 workers took 100 tickets from open to approved in 0.9 s (114.5 tickets/s, 1276 database calls/s), errors 0.

## Mixed reads and writes
- 50 threads, 6755 calls in 4.0 s (1682 calls/s, 5370 reads, 1385 writes), errors 0.

## Conflict rule under load
- 6 conflicting pairs among the 10 real tickets, so at most 7 can run at once. Most held at once: 7. Times two conflicting tickets were held together: 0 in 51 checks.

## Many tenants
- 1 tenant: 266.0 tickets/s. 5 tenants at once: 266.3 tickets/s in total. Every tenant saw only its own tickets: True.

## Per-call latency (lifecycle run)

| Call | Count | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|
| `claim_task` | 137 | 40.3 | 117.7 | 175.8 |
| `list_open_tasks` | 277 | 3.7 | 83.6 | 128.3 |
| `log_message` | 300 | 26.1 | 42.5 | 52.7 |
| `review_decision` | 100 | 47.2 | 70.6 | 81.9 |
| `start_session` | 100 | 64.0 | 125.0 | 140.6 |
| `submit_result` | 100 | 45.5 | 78.0 | 81.4 |
| `write_handoff` | 100 | 46.5 | 68.2 | 74.4 |
