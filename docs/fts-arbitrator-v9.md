# IK output decoding experiment (v9)

Base: v8 at 1c3ee4f8b0d150ff9aa2904df574aed4bf586154. User reports the latest submission scored 49.312/50 (mean improvement 0.49312471815493775); exact submitted commit has not been independently checked.

IK output_result previously validated character length and then classified it, decoding UTF8 twice. Like the existing scan stage, it now decodes once for valid UTF8 scalars and classifies the code point directly; all failures and other charsets retain the original validation/classification sequence. The inner gap loop and token selection rules are unchanged.

Release build and 71 native tests pass. The new test compares complete single-character output offsets/lengths/types against the original functions for every Unicode scalar, all individual byte values, truncated and illegal encodings, and GBK fallback. Complete SQL outputs match the original high-score base with empty stderr, including real MATCH scores and downstream expressions.

Official full-parameter ABBA benchmark against v8 finished four runs. Equal-category changes were +2.207% and +15.802%, but these are NOT credible estimates of this change's benefit: SELECT 1 latency roughly doubled from the preceding session, the unchanged BENG path shifted substantially, and the second candidate's query timings fell about 30% without a query implementation change. Preserve all reports rather than attribute those changes to decoding.

Supplementary diagnostic only: same CPU affinity, same runtime/parameters, three before/after pairs, user-variable TOKENIZE input, 1000 warmup then five samples of 100000 server-side iterations per parser. IK medians improve 1.711%, 1.827%, 0.706%; BENG control changes -0.958%, +0.420%, -0.691%. Shorter preliminary loops had large outliers and are retained too. This supports a small IK compute-path reduction, not a contest score prediction or a replacement for full benchmarks.

Original v8 high-score branch is untouched. Full marks remain unverified. Evidence: fts-evidence/2026-11-07-v9.
