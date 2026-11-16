# Python exact baseline diagnosis

Baseline: 8ec857e60bd63f345c9341038ffd37018a5f163e.
Dockerfile entry: python -m minivector --data-dir /data.
Declared image runtime: Python 3.12.8 and NumPy 1.26.4.

## Executed

- Local unittest: 60 passed.
- Local public-fixture replay (core API): 20 cases, 95 SQL passed.
- Standard JSONL workload, seeds 19017 and 29017, 100 batches of 100, 1000 TopK requests: write 4.7025714 s, query 3.0218961 s. All 1000 independent oracle comparisons and restart record verification passed.
- Separate core API cProfile probe, 10000 rows, 128 dimensions, 1000 queries, half tagged, SQL array literals: 5.770 s profiled; TopK 2.653 s cumulative; exact candidate generator 0.668 s cumulative; NumPy einsum 0.358 s; SQL parser constructor 1.105 s cumulative. Inclusive times overlap. This probe excludes JSONL output and is distinct from the standard workload.
- Over-budget diagnostic: 12 queries, 3.260583 s, zero vector-cache filter calls.

Commands: python -m unittest discover -s tests -q; python -m tools.run_public; python -m tools.filter_diagnostic over-budget on. Standard workload replay uses tools.run_filter_local.phase with performance_checker.workload and the independent oracle.

## Pending

Baseline checkpoint. Subsequent block trials and the tie counterexample are recorded in conversation.json. Linux paired rounds and resource measurements remain pending. No platform submission was triggered.
