# AI function enhancement regressions

The implementation follows the [AI function enhancement requirements](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/psu9fks8dexldoum).
Selected implementation and test cases were adapted from
[Mocairo/seekdb, ai-function-enhancement](https://github.com/Mocairo/seekdb/tree/9206efebc7f94a600f08034961a7bd45b0efdcda).
The current repository's HTTP restrictions, credential redaction, WebAssembly
fallback and existing Rerank helpers are preserved.

## Acceptance checkpoint (2026-10-28)

Validated on Linux x86_64 in Ubuntu 22.04 / WSL:

```sh
python3 build.py test -j 6 --config=O1 --copt=-g0 --linkopt=-Wl,--strip-debug \
  //src/observer:seekdb //unittest/sql/engine/expr:test_ai_function_utils \
  --test_output=all --nocache_test_results
```

The seekdb executable built successfully and the C++ utility test passed.
All modified C++ implementations and the new C++ test sources also passed
standalone syntax compilation against the actual generated headers.
The original 106 SQL source lines in `ai_prompt.test` were retained.

The 15 Python SQL integration cases and native mysqltest result comparison
are provided but have not yet been executed at this checkpoint. The contest
organizer's original Mock AI Service and official test files are unavailable.
Linux runtime testing requires the system `libaio.so.1` library.

## C++ tests

Prepare dependencies and run the repository's Bazel test target on Linux:

```sh
python3 build.py init
python3 build.py test -j 2 //unittest/sql/engine/expr:test_ai_function_utils --test_output=errors
```

The tests call the production C++ Prompt and Provider utilities. They cover
JSON arrays/objects, nested prompts, repeated placeholders, Unicode/escaping,
invalid and overflowing indices, embedding result ordering and dimensions,
invalid vector elements, Ollama headers and non-streaming requests, and
malformed Dashscope responses alongside valid existing response formats.

## SQL integration tests

Run against a disposable instance of the database built from this branch.
The suite creates and drops its own database and AI model resources. It uses
PyMySQL and starts its own HTTP fixture on an available loopback port:

```sh
python3 -m pip install pymysql
python3 unittest/sql/engine/expr/test_ai_function_enhancement.py
```

Connection settings are `SEEKDB_TEST_PORT` (default 2883), `SEEKDB_TEST_USER`
(default root), and `SEEKDB_TEST_PASSWORD` (default empty). The server must be
on the same host as the test process so it can reach the loopback fixture.

Coverage includes all six requirements and ordinary SQL joins, grouping,
filtering and LIMIT. The fixture records actual request bodies. Tests verify
that embedding requests contain multiple inputs, that each vector belongs to
the original row, and that different models/dimensions and NULLs remain
correct across 1,153 rows. Error cases include malformed or truncated JSON,
HTTP/network failures, duplicate/missing/out-of-range response indices,
invalid dimensions and vector elements, and failure of the second batch.
Rerank cases cross the 20-document batch boundary, check stable ties, and
retain the original IDs and object fields.

The suite also executes the existing `ai_prompt.test` and the new
`mysqltest/t/ai_function_enhancement.test`, honoring expected-error directives.
This adapter executes SQL through PyMySQL; it does not replace native
mysqltest result-file comparison. Existing SQL statements are retained in
`ai_prompt.test`; only the obsolete JSON/TEXT unsupported expectations are
updated to match the enhancement.

New test-only files are kept under `unittest/`. The mysqltest pair in
`mysqltest/` can be used as a test suite with a native mysqltest runner.
For the SQL suite's fixed URL, start the fixture separately:

```sh
python3 unittest/sql/engine/expr/ai_mock_service.py --dimension 768
```

It listens on `0.0.0.0:8000`. The integrated Python suite sets dimension 768
automatically for those statements. The default dimension used by the other
local regressions is 8.

This deterministic fixture is local test support. The contest organizer's
original Mock AI Service and official test files were not provided.
