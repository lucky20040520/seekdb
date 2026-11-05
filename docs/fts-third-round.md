# FTS perf, round 3: measured ceilings and one-pass character decoding

Round 1 (`75a4f9f0f`) and round 2 (`090a9506a`) took the reported improvement from
19.926% to 21.558% by attacking row fetching (DAAT merge), the dictionary cache
staleness fix, the ASCII normalizer fast path, `set_or_update` grouping in
`ObAddWord`, and a lock-free private hash table for the IK arbitrator. This round
starts from measurement instead of another guess: two new GDB samplings describe
where the 13.1 s of index build and the 0.05 ms of tokenization actually go. They
show that the remaining distance to a 2x win is not in the code these rounds
touched.

## Method

`profile4.py` samples only the three `ALTER TABLE ... ADD FULLTEXT INDEX`
statements (the build stage), interrupting every 10 ms with `SIGINT`, dumping
`thread apply all bt 10`, and classifying each thread. `profile3.py` covers the
tokenize stage and the two build stages separately. Idle frames (`epoll_wait`,
`clock_nanosleep`, `fsync`, `futex`) are counted as idle, so "busy sample" means
the thread was executing FTS code, not waiting.

Sample counts: `build_ik_all` 794 samples / 67 busy, `build_ik_content` 673 /
fewer, tokenize stages 90 each with ~44 busy each. GDB slows the server down
roughly 3x, so these runs give *shares*, not absolute times.

## Where index build time goes (`build_ik_all`, busy leaf frames)

| leaf | samples |
| --- | --- |
| `fsync` | 12 |
| `__memset_avx2_unaligned_erms` (DAT dictionary block init) | 4 |
| `ObFTDATBuilder<void>::build_from_trie` | 4 |
| `__libc_pwrite64` | 3 |
| `ObArrayHashMap::find` | 3 |
| `ObNullSafeDatumStrCmp` / `ob_strnncollsp_utf8mb4` | 6 |
| `ObSortOpImpl::Compare::operator()` | 2 |
| tokenizer frames (`first_valid_char_fast`, `classify_utf8mb4_fast`, `do_process`, `process_arabic_letter`) | 5 |

Two conclusions:

1. **The build is storage bound, not tokenizer bound.** Roughly one third of the
   busy samples are block-file I/O (`fsync` + `pwrite`), another large share is
   DAT dictionary construction (trie traversal, `ObArrayHashMap::find`, the
   `array_size = node_num * 6` memset) and the sort/comparison operator that
   orders the generated rows by rowkey. Tokenizer frames are about 3% of the
   build. Optimising the tokenizer further can no longer move `build_total_sec`
   appreciably, which is why the build category stays near 13 s.
2. **The build is effectively single threaded** (~8% busy across the whole
   process). The only change that could halve it would be parallel index
   writers, which is a structural change to the DDL index build path, not a
   hot-path tweak.

## Where tokenization time goes

Reported `tokenize_ik_avg_ms` is 0.0529 ms for a 74-character sentence that
produces ~40 tokens; the kernel part of one call is ~27 µs, i.e. ~250 ns per
character. The remaining ~25 µs per call is client round-trip and result
serialization, which the benchmark measures as part of the statement. Even a
perfect tokenizer would therefore reduce this metric by well under half, and
both tokenize metrics (2 of 9) are already close to the transport floor.

## The change shipped this round

`ObFTCharUtil::decode_and_classify()` resolves the length and the class of one
character in a single UTF-8 walk. Before, every character of every document was
decoded twice: `first_valid_char_fast()` walked the prefix to find the length, and
`classify_first_char()` walked the same prefix again to classify it. The fused helper
handles the plain scalar cases inline (ASCII letter/digit, 2-byte lead `>= 0xc2`,
3-byte with the `U+4E00..U+9FFF` shortcut, 4-byte with the `F0`/`F4` bounds) and
falls back to the original two helpers for anything else, including malformed
sequences, so error codes and semantics are unchanged.

Two call sites use it: `TokenizeContext::prepare_next_char` (the per-character
walker that feeds the arbitrator) and `ObIKArbitrator::output_result`'s main
loop, which used to decode every character of the document a second time just to
walk positions. The `keep_single` inner loop inside `output_result` still calls
`first_valid_char_fast` alone: it never needs the class (it reuses the class of
the enclosing chain token), so there is nothing to fuse there. Soundness rests on
the 1,112,064-scalar enumeration test, which compares the fused helper against the
charset library for every scalar value, on the invalid/truncated-input test, and
on the differential SQL golden file.

## Verification

Native GoogleTest: 23 tests, all passing, including
`utf8_fast_path_matches_charset_for_all_scalar_values` (every Unicode scalar),
`utf8_fast_path_preserves_invalid_input_results` and the arbitrator/grouping/DAT
regressions. Differential SQL golden file: output SHA256
`bfbd2901fc3c4d0ede7875ec7f902184a83c160971b4dacaaa469b5d6437b6ab`, byte
identical to the recorded original output.

## Measured effect: inside the noise of this machine

Two runs of the fused helper and one run of the round-2 binary, same VM, same
benchmark parameters, sequential so ordering effects cancel between `v3b` and the
round-2 sample:

| metric | round 2 | v3 run 1 | v3 run 2 | v3 mean vs round 2 |
| --- | --- | --- | --- | --- |
| select1 (loop baseline) | 0.1317 ms | 0.1380 | 0.1319 | +2% |
| build_ik_all | 12.470 s | 13.645 | 13.640 | +9.4% |
| build_ik_content | 10.420 s | 10.245 | 9.703 | -4.3% |
| build_beng_en | 5.197 s | 5.490 | 5.386 | +4.6% |
| build_total | 28.099 s | 29.394 | 28.742 | +3.4% |
| tokenize_ik | 0.2065 ms | 0.2132 | 0.2055 | +1.4% |
| query_cn (8001) | 4.0815 ms | 4.1607 | 3.9747 | -0.3% |
| query_beng (11000) | 5.8063 ms | 5.9825 | 5.7397 | -1.0% |
| query_mixed (7332) | 4.3032 ms | 4.3310 | 4.2432 | -0.4% |
| query_limit (20) | 5.5491 ms | 6.1895 | 6.3872 | +13% |

The two build statements move in opposite directions (`ik_all` +9%,
`ik_content` -4%) and `query_limit` swings by 13% with a 0.45 ms stdev, which is
the scale of run-to-run noise on this 4 vCPU VM. Tokenization — the only stage
this change can touch — is flat. So the fused decoder removes redundant byte
checks in a loop that was never a bottleneck: sound, but not a score lever.

## What would actually move the number

Both remaining categories are already close to a floor that this round can
document but not lower:

* Query returns 8001 / 11000 / 7332 rows per statement, i.e. 141 ns per row on
  the reference machine. That is fetch + serialization; there is no 2x left.
* Tokenize is ~50% client round-trip per call; the fused decoder shaves tens of
  nanoseconds from a 27 µs kernel.
* Build is I/O + DAT dictionary + rowkey sort bound, and runs single threaded at
  ~8% CPU.

A 50% overall improvement (the full 50 points) would require halving all three
categories, which needs a structural change (parallel index writers and a batched
memtable path in the DDL build), not further hot-path work.
