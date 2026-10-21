# 001: A length byte of 255 wraps the 8-bit frame total to 0, so frame_decode copies 255 bytes from a shorter input

- **Status**: CONFIRMED
- **Severity**: high - any caller that decodes a frame from untrusted bytes reads up to 254 bytes past the input buffer when the first byte is 255; the bytes read are copied into `out` and returned to the caller (an information leak, or a crash on an unmapped page)
- **Targets**: frame-decode (`verification/models/frame-decode/`)
- **Tools**: code-level (CBMC 6.4.1)
- **Properties violated**: `FrameReadsInBounds` - `frame_decode` never reads outside `in[0..in_len)` (source: `src/frame.h:6-10`); `NoLengthTruncation` - the frame's total length `n + 1` is computed without truncation (source: `src/frame.h:6-8`)
- **Location**: `src/frame.c:9-11` (the 8-bit `total` and the truncation check that trusts it)
- **Commit**: `0000000000000000000000000000000000000000`
- **Repro tests**: `verification/repro/repro_frame_decode.c::test_length_255_reads_past_input` (a 3-byte frame whose length byte is 255)
- **Repro command**: `bash verification/repro/run_c_repro.sh verification/repro/repro_frame_decode.c test_length_255_reads_past_input` (fails on current code)
- **Guard tests**: `verification/repro/repro_frame_decode.c::test_well_formed_frame_decodes` (passes)
- **Plan**: `plans/001-widen-frame-total.md`

## What goes wrong

`frame_decode` computes the frame's total length, header byte plus payload, in an `unsigned char`.
For a length byte of 255 the sum 256 truncates to 0, so the truncation check `total > in_len` passes for any input, and `memcpy` copies 255 bytes starting at `in + 1` from an input that may hold only a few bytes.
The caller gets back 255 bytes of whatever follows its buffer in memory.

## Counterexample

CBMC (`results/Harness.json`), in the code's terms:

1. `src/frame.c:8`: `n = in[0] = 255`, with `in_len` anywhere from 1 to 8.
2. `src/frame.c:9`: `n + 1 = 256` converts to `unsigned char` `total = 0` (check `frame_decode.overflow.2`: arithmetic overflow on signed to unsigned type conversion).
3. `src/frame.c:10`: `0 > in_len` is false, so the frame is not rejected as truncated.
4. `src/frame.c:12`: with `cap >= 255` the frame fits.
5. `src/frame.c:14`: `memcpy(out, in + 1, 255)` reads past `in` (check `frame_decode.precondition_instance.2`: memcpy source region readable).

Checker: `run_code_check.py --tool cbmc -- cbmc verification/models/frame-decode/harness_frame_decode.c src/frame.c -I src --function harness --bounds-check --pointer-check --conversion-check --signed-overflow-check`; 43 checks, 2 failed.
The state diagram (`verification/state-diagram.html#t-frame-decode`) highlights the path entry, header read, copied.

## Reproduction

`test_length_255_reads_past_input` builds a 3-byte frame `{255, 'a', 'b'}` and an output buffer of 255 bytes, calls `frame_decode`, and asserts that it returns -1 (truncated).
Under AddressSanitizer the call never returns: the `memcpy` read is reported, and `run_c_repro.sh` reports it as the property the test checks:

```
FAIL test_length_255_reads_past_input: FrameReadsInBounds violated: SUMMARY: AddressSanitizer: heap-buffer-overflow ... in memcpy
```

## Root cause

`src/frame.c:9` declares `total` as `unsigned char`, the type of the length byte, but `total` holds the length byte plus one, which needs nine bits.
The check at `src/frame.c:10` then compares the truncated value.

## Fix direction

Compute `total` in `size_t` (`size_t total = (size_t)n + 1;`), as in `fix.patch`.
Observed: with the fix applied in a scratch copy, the repro test passes and the same CBMC command passes all 41 checks.

## Vacuity

`FrameReadsInBounds` and `NoLengthTruncation` are violated on the unmodified code, so they are not vacuous.
`FrameWritesInBounds` (the property that holds) is caught failing on the mutant `mutants/drop-cap-check.patch`, and its sanity run fails on the sanity assertion.
