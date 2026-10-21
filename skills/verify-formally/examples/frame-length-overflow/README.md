# frame-length-overflow

A worked example of the `verify-formally` skill's code-level lane on C: a frame decoder whose length arithmetic truncates, so a single crafted byte makes it read past its input.
This directory is laid out as **a repository after a completed run** (`/verify-formally code src/frame.c`): `src/` and `fix.patch` are the code and its fix, and `verification/` and `plans/` are what the skill writes.
`run_example.sh` re-runs every check from scratch and validates the result with `scripts/check_run.py`; it needs CBMC and a C compiler with AddressSanitizer, and prints `SKIP` without them.

## The code

`frame_decode` (`src/frame.c`) decodes a frame made of a 1-byte payload length `n` followed by the payload, and copies the payload into a caller's buffer of `cap` bytes.
Its contract (`src/frame.h:6-10`) says it rejects a truncated frame or one that does not fit, and never reads or writes outside the two buffers.

## The bug

The frame's total length, header byte plus payload, is computed in the same 8-bit type as the length byte:

```c
unsigned char n = in[0];
unsigned char total = n + 1; /* header byte plus payload */
if (total > in_len)
    return -1; /* truncated */
```

For `n = 255` the sum 256 truncates to 0, the truncation check passes for any input, and `memcpy(out, in + 1, 255)` reads up to 254 bytes past an input that may hold only a few.
Nothing crashes in ordinary tests: the extra bytes are simply copied to the caller.

## The check

`verification/models/frame-decode/harness_frame_decode.c` is a CBMC harness compiled with the unmodified `src/frame.c`.
It gives `frame_decode` an input of any length from 0 to 8 bytes with any contents, and an output buffer of any capacity from 0 to 256, both as heap objects of exactly that size, so CBMC's pointer checks flag any access outside them.
Every run goes through `scripts/run_code_check.py --tool cbmc` (see `verification/rerun.sh`).

| Property | Kind, claimed checks | Result |
|---|---|---|
| `FrameReadsInBounds` | memory_safety: `bounds`, `pointer` | **violated**: `memcpy source region readable` at `src/frame.c:14` |
| `NoLengthTruncation` | overflow: `conversion`, `signed_overflow` | **violated**: `(unsigned char)((signed int)n + 1)` at `src/frame.c:9` |
| `FrameWritesInBounds` | memory_safety: `bounds`, `pointer` | **no violation within bounds**, exhaustive (the harness has no loops) |

Three details make these results trustworthy, and `check_run.py` checks each one:

- **Claimed checks are on the command line.** Each property lists the checks it claims, and every cited run passes their flags (`--bounds-check --pointer-check --conversion-check --signed-overflow-check`) explicitly, whatever the CBMC version's defaults.
- **The holding property is isolated.** On this code the read and conversion checks fail, so `FrameWritesInBounds` is checked alone with `--property frame_decode.precondition_instance.3` (the `memcpy` destination check); otherwise a failure elsewhere in the harness would hide whether the writes hold.
- **The pass can fail.** `mutants/drop-cap-check.patch` removes the `n > cap` check from the real `src/frame.c`; `run_code_check.py --mutant` applies it to a scratch copy, and the same check fails (`memcpy destination region writeable`). The sanity run (`-DVF_SANITY --property harness.assertion.1`) fails on its own `sanity:` assertion, so the harness's assumptions are satisfiable and the call returns.

## The repro test

`verification/repro/repro_frame_decode.c::test_length_255_reads_past_input` passes the 3-byte frame `{255, 'a', 'b'}` and asserts that `frame_decode` rejects it.
`verification/repro/run_c_repro.sh` builds the test with `src/frame.c` under AddressSanitizer and UndefinedBehaviorSanitizer; the read aborts the test inside `memcpy`, and the runner reports it as the property the test checks:

```
FAIL test_length_255_reads_past_input: FrameReadsInBounds violated: SUMMARY: AddressSanitizer: heap-buffer-overflow ... in memcpy
```

`test_well_formed_frame_decodes` is the guard test; it passes.
`findings.json` records the runner as a placeholder template (`bash verification/repro/run_c_repro.sh {file} {test}`), which `check_run.py` runs per test id.

## The state diagram

`verification/state-diagram.html` draws `frame_decode`'s paths by hand (there is no TLA+ model to derive them from), each state and transition citing its line, with finding 001's path, entry to header read to copy, highlighted.

## The fix

`fix.patch` computes the total in `size_t`.
With it applied, the repro test passes and the same CBMC command passes all 41 checks exhaustively; `plans/001-widen-frame-total.md` makes that command a done criterion.

## Running everything

```
examples/frame-length-overflow/run_example.sh
```

checks the repro on the buggy and fixed code, the guard test, CBMC on the fixed code, re-runs `verification/rerun.sh` in a scratch git repository and compares every result, and finally runs `check_run.py` (which re-runs every cited CBMC result) against a fresh copy of the committed files.
`regenerate.sh` is the maintainer tool that rebuilds the committed results.
