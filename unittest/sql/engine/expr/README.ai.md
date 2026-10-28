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

The 16 Python SQL integration cases passed against the actual server. Native
mysqltest result comparison passed for the new enhancement fixture and six
existing cases: `ai_function.ai_prompt`, `ai_function.ai_endpoint_sql_name`,
`ai_function.ai_model_ddl`, `ai_function.ai_model_endpoint_ddl`, `expr.expr_ceil`
and `expr.expr_floor`. The 1,153-row embedding case used 40 requests for 1,048
non-NULL inputs (at most 40 inputs per request in that case). Rerank preserved
global ordering and object fields across 47 documents and three HTTP batches.
Runtime testing exposed and fixed SQL NULL access-key loading for keyless
Ollama endpoints, including creation without a key and subsequent ALTERs.
The contest organizer's original Mock AI Service and official test files are unavailable.
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
python3 unittest/sql/engine/expr/ai_mock_service.py --host 127.0.0.1 --port 18080 --dimension 768
```

The SQL fixture connects to `127.0.0.1:18080` on the server's host.
The integrated Python suite sets dimension 768
automatically for those statements. The default dimension used by the other
local regressions is 8.

For interactive acceptance, execute `ai_manual_acceptance.sql` once through
the MySQL client against the isolated test instance. It creates database
`ai_manual`, five `local_mock_*` models and endpoints, and a three-row table;
these are retained for manual inspection. The standalone Mock must keep running.
The SQL comments describe expected outputs. It covers all six enhancement
requirements at a smoke-test level; the automated suite covers errors and larger batches.

Use a UTF-8 terminal and connect with `mysql --default-character-set=utf8mb4
-h127.0.0.1 -P2883 -uroot -Dai_manual`. On Windows, leave the existing mysql
session and run `chcp 65001` in the terminal before reconnecting. A GBK terminal
can otherwise send invalid UTF-8 bytes even when the server session is utf8mb4;
the Mock then fails to decode the JSON request and curl reports error 52.
The current server does not support `SET NAMES gbk`; use UTF-8 consistently.

With the server on port 2883 and database `ai_native_regression` already created,
run the native enhancement comparison from the repository root:

```sh
mkdir -p /tmp/ai-function-mysqltest
deps/3rd/u01/obclient/bin/mysqltest --host=127.0.0.1 --port=2883 --user=root \
  --database=ai_native_regression --tmpdir=/tmp/ai-function-mysqltest \
  --logdir=/tmp/ai-function-mysqltest --silent \
  --test-file=unittest/sql/engine/expr/mysqltest/t/ai_function_enhancement.test \
  --result-file=unittest/sql/engine/expr/mysqltest/r/mysql/ai_function_enhancement.result
```

The fixed Mock must be running on port 18080 with dimension 768 for this command.

This deterministic fixture is local test support. The contest organizer's
original Mock AI Service and official test files were not provided.
