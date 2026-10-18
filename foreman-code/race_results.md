# Claim race: 50 workers, one ticket, 20 rounds

| Claim | Rounds with exactly one winner | Most workers told 'yours' in one round | p50 ms | p95 ms |
|---|---|---|---|---|
| Atomic claim (the product) | 20 of 20 | 1 | 21.2 | 24.3 |
| Naive look-then-take | 0 of 20 | 50 | 64.1 | 68.1 |
