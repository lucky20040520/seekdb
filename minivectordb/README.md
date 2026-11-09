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
uses random hyperplane buckets. Its default `ann` path examines every bucket and
reranks exactly; it therefore prioritizes recall rather than speed. The Python
API accepts an experimental `candidate_budget` to stop early, with no recall
guarantee. A small 1,000-row, 16-dimensional experiment observed 0.707 mean
Recall@10 with a budget of 320 candidates; it is not a competition benchmark.

Run locally with `python -m minivectordb --data ./data`. Input consists of SQL
statements terminated by semicolons. The **local, provisional** output format
starts with `READY`; successful statements use `OK`, while queries use
`COLUMNS`, `ROW`, and `END` lines. This format and the `--search-mode ann`
switch are not the official contest protocol. Results are emitted row by row.
Build with `docker build -t minivectordb .` and run with
`docker run --rm -i -v minivectordb-data:/data minivectordb` when Docker is
available. The base image tag is specified but its digest is not yet pinned.

This is not yet a contest-ready database. Sort runs and transaction changes use
disk, but SQL parsing materializes the input statement, and the compatibility
API materializes results unless `stream=True` is requested. The B+ tree uses
tombstones for deletion; data and indexes are rebuilt into temporary files at
startup. The official stdin/stdout protocol, error mapping, dependencies and
complete syntax require the competition package. The current storage format is
experimental and crash/resource testing is not exhaustive.

Run the local tests with `python -m unittest discover -s tests -v`.
The public examples under `mysqltest/` come from OceanBase commit
`a3dba559c1e7ebdc48bc990ffb1a4c75d1c1075c`. Run them alone with
`python -m unittest discover -s tests -p test_public_cases.py -v`. The harness
tests both the in-process API and the provisional local process protocol; the
public examples do not define the official process protocol.

The v4 public examples from commit
`9f6df2ba5acf871376e22b64a3d0724e4b575044` are retained separately under
`mysqltest_v4/`. Run them with
`python -m unittest discover -s tests -p test_public_v4.py -v`. The local
driver handles normal restarts and sends `--mode ann` as a local control line;
`MODE` and the local semantic error codes are provisional and do not define
the official transport protocol or its error mapping.
