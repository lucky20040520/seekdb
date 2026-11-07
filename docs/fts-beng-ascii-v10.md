# FTS candidate v10: IK output decode and BENG ASCII normalization

## Preserved base and score

Base for complete comparisons is v8 at 1c3ee4f8b0d150ff9aa2904df574aed4bf586154. User supplied the latest contest result of 49.312/50, mean improvement 0.49312471815493775. The exact submitted commit has not been independently verified. The supplied report is saved verbatim as metrics in fts-evidence/2026-11-07-v10/user-reported-49.312.json. Original high-score branches and v8 remain untouched. Full marks remain unverified.

## Changes and bottleneck evidence

v9 (included): valid UTF8 in IK output arbitration is decoded once to obtain character length and classification. Previously output_result called first_valid_char and classify_first_char separately; the scan stage already had the single-decode approach. Invalid sequences and other charsets retain the original sequence. Token selection and ordering are unchanged. See fts-arbitrator-v9.md for detailed evidence and noisy preliminary measurements.

v10: BENG's ObBasicEnglishNormalizer copied each trimmed word then used the generic UTF8 decoder/case mapper/encoder even for ASCII. For UTF8MB4_BIN, UTF8MB4_GENERAL_CI, and UTF8MB4_UNICODE_CI, it now validates that the entire token is ASCII, allocates the same caller-owned output, and copies with ASCII uppercase conversion. Other bytes/collations use the original ObCharset::tolower. Trimming, token frequency, grouping, token rules, input size and output lifetime are unchanged. Both the direct BENG parser and shared English analysis pipeline use this normalizer.

No official benchmark data, SQL, timing, scoring, baseline or CI entry files were changed. No input-dependent caching of results or benchmark-specific shortcuts were introduced.

## Correctness

Release compilation and all 72 production-linked native tests pass. Independent BENG reference uses the original whitespace tokenizer, original alnum classification, reference trimming, and ObCharset::tolower; every pair of byte values is checked in each of the four supported collations, including binary's uppercase-preserving behavior. Latin1 and GBK have no backing charset in the preexisting high-score snapshot; their original rejection is checked, not silently skipped or newly restricted. Existing freeing allocator tests verify retained tokens after reset/destruction, reuse, and every output-allocation failure followed by retry.

Included IK test compares output offsets, lengths, types and error returns against the original decode/classification functions for all Unicode scalar values, every single byte, malformed/truncated encodings, and unsupported-charset errors. Earlier casefold tests cover every BMP code point in three UTF8 collations and source-buffer mutation.

Full SQL output matches the original high-score base, with empty stderr: TOKENIZE modes, long inputs, shared parsers, index builds, actual MATCH scores and consumers (projection, filtering, sorting, grouping, joins), LIMIT/OFFSET, prepared statements, string/composite primary keys, DML, rollback and rebuilding. Hits 8001/11000/7332/20 are auxiliary checks. ASan and full repository regression were not run.

## Complete official-parameter measurements

Same Ubuntu VM (4 CPUs/4GB), same Release flags, server configuration and dedicated runtime. Initial order base/candidate/candidate/base; a third base/candidate pair confirms the small effects. Each run uses 20000 rows, batch 500, tokenize rounds 3000, query rounds 200, samples 3, warmup 30, SKIP_LOAD=0. All six runs complete successfully.

| Category time reduction | Pair A | Pair B | Pair C |
| --- | ---: | ---: | ---: |
| Build mean | 0.997% | 2.472% | 0.932% |
| Tokenize mean | -0.655% | -3.684% | -0.400% |
| Query mean | -0.312% | -3.104% | -0.376% |
| Equal-category overall | 0.010% | -1.439% | 0.052% |

These full measurements do NOT establish an overall performance improvement on this VM. BENG index construction improves consistently: 3.765 to 3.653s, 3.759 to 3.648s, 3.748 to 3.656s (2.975%, 2.953%, 2.455%). Full SQL-loop tokenizer and query timings include substantial round-trip noise/overhead. Negative metrics and all sample groups are preserved in the reports; they are not clipped or omitted.

## Supplementary compute-path measurements (not scored)

Three sequential before/after pairs on the same single CPU affinity (CPU 2), same runtime and server parameters. For each parser: user-variable input containing the unchanged official text, 1000 warmup server-side BENCHMARK iterations, then five samples of 100000 iterations; compare medians. These diagnostics run separately after the full benchmark, not concurrently. All results are zero (BENCHMARK return), and raw times scale with loop count. This is not a replacement for the official SQL-loop timing.

| Median compute time reduction | Pair 1 | Pair 2 | Pair 3 |
| --- | ---: | ---: | ---: |
| IK | 4.041% | 2.510% | 2.211% |
| BENG | 14.778% | 14.062% | 12.964% |

This supports retaining the genuine compute-path optimizations, while the complete local benchmark still leaves the scoring impact uncertain. A dedicated candidate submission is needed to determine whether they close the approximately 0.688-mark gap. Do not add these percentages directly to the fixed-baseline improvement or claim a full score in advance.

Evidence: fts-evidence/2026-11-07-v10 (including all six full reports, full samples, correctness checks, final native log, and raw diagnostic samples). All owned test servers were stopped; no OOM occurred. Only obsolete self-created intermediate binaries were removed to free disk; high-score source branches and v8 binary remain preserved.
