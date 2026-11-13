Pinned Python/C++ boundary comparison (2026-11-13)

Python: f1b9f4e204f08f5043b315ba1ebec500ad7af255
C++:    8c2bbcac83953c360a0f55e601da8538e5b18326

This folder contains only test tools and evidence. Database sources, official
test assets and the checked-out commit are unchanged. The remote branch has
later commits; these tests deliberately target the two revisions above.

The organizer publishes 20 examples, while formal validation contains 29 cases.
Its hidden inputs are not included here. MySQL documentation inspired boundary
probes, but full MySQL syntax/collations are not imposed on this SQL subset.

Results are in results/20261113/summary.json. Detailed files retain SQL and
responses, independent expected results, failures, image IDs and source hashes.
Long generated SQL/strings are summarized by length and SHA256; run.py contains
their deterministic generators. Random-state seed: 20261113. Float comparison
uses the public 1e-5 absolute/relative tolerances; BIGINT and strings are exact.

Results:
- Both images: 20/20 official public cases, using the original Linux checker
  against default image entrypoints, a read-only root, 2 CPUs and 2 GiB.
- 748 additional paired steps: 727 with explicit expected results and 21
  compatibility probes. C++ passes all 727 assertions; Python passes 726.
- Python rejects a 5000-digit request ID and returns id:null; it does not exit
  in this reproduction. C++ echoes it correctly and succeeds.
- 10 responses differ among compatibility probes. Root differences are Unicode
  identifier lowercasing, non-ASCII whitespace and non-ASCII numeric tokens.
  One response difference follows an earlier rejected INSERT; it is not an
  additional independent bug. No hidden-test causation has been established.
- C++ existing regression: 16/16 groups, 729 JSONL requests.
- Both images: 222 asserted SQL requests over 20000 rows containing 156.25 MiB
  of string payload in a 128 MiB container, including sorting, vector indexing,
  updates, rollback, committed/uncommitted SIGKILL and restart checks. No OOM.
  Cgroup memory.peak is 128 MiB for both and includes file cache, not just RSS.
  This reduced-memory run is not the official full-scale 2 GiB workload.

unicode_case.sql is a minimal reproduction: Python returns rows:[[7]] with
columns:["x"]; C++ returns an unknown-column error. Compare cpp/sql.cpp:16
(ASCII-only lower) with minivector/sql.py:48 (Unicode str.lower). The published
competition package does not freeze every identifier case-folding rule, so this
is a demonstrated migration difference, not a proven official failure cause.

Error-code spelling is recorded, not treated as a semantic failure: the public
config currently has error_codes:{} and both pinned implementations use broad
uppercase codes. A later remote error-code change is not mixed into this test.

Reproduction (PowerShell from the repository root, Docker must be running):

  python boundary_check/prepare.py --output .artifacts/boundary-new
  docker build --pull=false -t minivector-boundary:python-f1b9f4e .artifacts/boundary-new/python
  docker build --pull=false -t minivector-boundary:cpp-8c2bbcac8 .artifacts/boundary-new/cpp
  python boundary_check/run.py --output protocol-results/boundary-new
  python boundary_check/public.py --output protocol-results/boundary-public-new
  python tools/test_cpp.py --docker-image minivector-boundary:cpp-8c2bbcac8 --output protocol-results/boundary-cpp-new.json

run.py exits nonzero for failed expected results (including the known Python
giant-ID failure), transport failures or cleanup errors. Compatibility-only
differences are recorded and do not independently set failure status. Each
output directory must be new. Lifecycle requests are excluded from its step
count. Fresh test volumes are removed after each group.

public.py and over_memory.py use the existing Linux driver image
minivectordb-protocol-check:driver, which contains Python and the Docker CLI.
It is separate from the tested database images. To reproduce over_memory.py,
mount the repository at /work (read-only), a fresh host output parent at /out,
and /var/run/docker.sock into that driver, then run:

  python /work/boundary_check/over_memory.py --output /out/memory-new

The Docker socket is used only to create and remove dedicated test containers
and volumes through the unchanged public transport. Database containers have
no network, no added capabilities and no writable root filesystem.

Limits: no organizer hidden data, no full-scale >2 GiB run, no exhaustive
fsync/rename fault injection or verified kill during an active recovery write.
Individual request timings in these reports are diagnostics from one run, not
stable performance estimates or official scores. No database fix or remote
submission is performed by these tools.
