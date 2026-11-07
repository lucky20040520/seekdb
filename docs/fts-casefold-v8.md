# FTS normalization fast path (v8)

Base: seekdb-query-optimization at d7ec83b2ba07bf0c636107d9538ab954d1614b4d. The original branch is untouched. Whether this exact commit produced the reported 48.528/50 score remains unconfirmed. This candidate includes v7 iterator erasure and error propagation, plus normalization changes. Full contest marks remain a goal, not a verified result.

## Evidence and implementation

Exploratory GDB sampling of the base binary using the official IK text found five stacks containing TOKENIZE paths at depth eight. Two included ObAddWord::process_word, ObCharset::tolower and ob_casedn_utf8mb4; dictionary matching and arbitration also appeared. This small sample establishes that the path executes, not its CPU percentage.

Previously every word was copied, decoded as UTF8, case-mapped, and encoded again. For UTF8MB4_BIN, UTF8MB4_GENERAL_CI and UTF8MB4_UNICODE_CI only, words containing exclusively ASCII and U+4E00..U+9FFF now use validated byte sequences and direct ASCII lowercasing. Basic CJK bytes remain unchanged. Every other code point, collation, malformed or truncated sequence uses the original ObCharset::tolower. Output always belongs to the caller allocator; no input or parser-buffer borrowing. There are no input/token length restrictions or changes to token rules, stopwords, frequency or MATCH execution.

## Correctness

Release build and all 70 native tests passed. Native regression compares all BMP code points in all three collations and all 65,536 two-byte combinations against ObCharset::tolower, including NUL, malformed UTF8 and surrogate encodings. It overwrites source strings to check independent output lifetime. Allocation failure is checked for the fast and fallback paths. Existing FTS and v7 erasure/reuse tests run together; final native output is included in the evidence directory.

Complete SQL output matches the base, with empty stderr: SMART/MAX_WORD/BENG TOKENIZE, long inputs, shared parsers, FTS builds, actual MATCH scores and consumers, projections/filters/sorting/aggregation/joins, LIMIT/OFFSET, string and composite primary keys, prepared statements, DML, rollback and rebuilding. Hits 8001/11000/7332/20 are auxiliary checks only. ASan and full repository tests were not run.

## Full benchmark

Same Ubuntu VM, 4 CPUs/4GB, Release flags and server parameters. Four successful full runs alternate base/candidate/candidate/base on one new dedicated runtime. The previous stalled runtime is excluded. Official parameters: 20000 rows, batch 500, tokenize 3000 rounds, query 200 rounds, samples 3, warmup 30, SKIP_LOAD=0. Official benchmark scripts, data, SQL, timing, scoring, and baseline files are unchanged.

| Category | Pair A time reduction | Pair B time reduction |
| --- | ---: | ---: |
| Build mean | 3.191% | 1.278% |
| Tokenize mean | 6.930% | 1.649% |
| Query mean | -0.843% | 1.295% |
| Equal-category overall | **3.093%** | **1.407%** |

IK tokenize: 0.2111 to 0.1969ms and 0.2037 to 0.1979ms (6.727% and 2.847%). BENG tokenize: 7.134% and 0.451% reduction. SQL round trips and VM noise remain significant; changes in unrelated metrics cannot all be attributed to this optimization. Evidence includes every full report and sample, including regressions, in fts-evidence/2026-11-07-v8.

Both overall comparisons improve, making this a candidate for official evaluation. Local relative improvements cannot be added directly to the reported 48.528% fixed-baseline improvement and do not guarantee 50/50 marks. The original high-scoring branch and v7 remain preserved.
