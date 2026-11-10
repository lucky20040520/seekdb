# MiniVectorDB prototype

This is an independent implementation of part of the competition task. It does
not use the parent commit's database or vector-index engine. The parent commit's
separation between table operations, sorting and vector distance calculation
informed the design.

`sql.execute(database, statement)` parses one SQL statement and returns a
`Result` with column names, rows, and affected-row count. `Database` stores its
append-only transaction log in the supplied directory. Records have a
transaction commit marker and checksum; a successful commit flushes and syncs
the log. A disk-backed B+ tree with a bounded page cache maps primary keys to
positions in a temporary paged row store. Checkpoints publish immutable binary
row pages and checksummed metadata via an atomic manifest; a fresh WAL records
subsequent commits. Checkpoints run after the WAL reaches 16 MiB, or explicitly
through `Database.checkpoint()`. Indexes and temporary row pages are rebuilt at
startup. Transaction and statement changes are staged in temporary files under
the data directory. Closing an instance rolls back an active transaction.

The prototype supports CREATE TABLE, INSERT, SELECT, UPDATE, DELETE, BEGIN,
COMMIT, ROLLBACK, conjunctions of comparisons, ordering, LIMIT and exact
L2_DISTANCE and CREATE VECTOR INDEX. Vectors may be written as SQL strings
containing JSON arrays or as bracketed arrays. Exact ordering applies filters
before sorting, with primary key as a tie break. The independent vector index
uses eight random hyperplane bits. SQL `ann` searches with an index use an
automatic candidate budget of 93% of indexed rows, at least 256 and at least K;
the Python API also accepts `candidate_budget="auto"` or an explicit count.
Buckets are visited in order of the query's distance from their separating
hyperplanes, then candidates are reranked by exact L2 distance. The budget is soft:
whole buckets are read, and probing continues if fewer than K rows match the
filter. Small datasets, selective filters, transactions and queries without a
usable index can therefore result in a full scan. Recall is not guaranteed;
the default SQL search mode remains `exact`.

For a reproducible synthetic experiment run `python -m tests.benchmark_ann`
if `tests` is importable, or set `PYTHONPATH` to the repository root and run
`python tests/benchmark_ann.py --queries 100 --budget 700`. On one local run,
1,000 uniformly random 16-dimensional vectors and 100 queries per group gave
mean Recall@10 of 0.978 without a filter and 0.953 with a 20% selective filter.
Mean exact/ANN time was 29.4/24.2 ms and 29.8/24.0 ms respectively; ANN read
704 rows on average versus 1,000 for exact search. The index used 8,000 bytes
of key data in 209 open bucket files; traced peak Python allocation during
build was 2.18 MB. These are single-run, warm local measurements, not a
competition benchmark or a general performance or recall guarantee. The
previous 0.707 result used a budget of 320 and is not a controlled comparison.
On another local synthetic run with 10,000 rows, 128 dimensions and 30 queries
per group, a 9,300-candidate budget gave mean recall 0.990 unfiltered and 0.967
with the 20% filter. Mean exact/ANN times were 516/652 ms and 353/496 ms,
respectively, while ANN read 9,326 rows on average. Thus this sample did not
show an ANN speedup; results must not be assumed to meet the official workload.

Run locally with `python -m minivectordb --data ./data`. The process uses the
official JSON Lines protocol: it emits `{"type":"ready"}`, accepts one
`execute` or `shutdown` JSON object per line, and returns one JSON response
per request with its `id`. Specify `search_mode` as `exact` or `ann` on
each execute request. Query rows are encoded incrementally within the single
response line. Shutdown closes the database and rolls back an open transaction.
Build with `docker build -t minivectordb .` and run with
`docker run --rm -i -v minivectordb-data:/data minivectordb` when Docker is
available. The base image tag is specified but its digest is not yet pinned.

This is not yet a contest-ready database. Sort runs and transaction changes use
disk, but SQL parsing materializes the input statement, and the compatibility
API materializes results unless `stream=True` is requested. The B+ tree uses
tombstones for deletion; data and indexes are rebuilt into temporary files at
startup. The official error mapping, dependencies and complete syntax require
the competition package. The current storage format is
experimental and crash/resource testing is not exhaustive.

Run the local tests with `python -m unittest discover -s tests -v`.
The public examples under `mysqltest/` come from OceanBase commit
`a3dba559c1e7ebdc48bc990ffb1a4c75d1c1075c`. Run them alone with
`python -m unittest discover -s tests -p test_public_cases.py -v`. The harness
tests both the in-process API and JSONL process protocol.

The v4 public examples from commit
`9f6df2ba5acf871376e22b64a3d0724e4b575044` are retained separately under
`mysqltest_v4/`. Run them with
`python -m unittest discover -s tests -p test_public_v4.py -v`. The local
driver handles normal restarts and maps `--mode ann` to the search mode on
subsequent requests. Semantic error codes remain subject to the official mapping.
