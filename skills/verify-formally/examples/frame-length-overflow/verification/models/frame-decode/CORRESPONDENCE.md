# Correspondence: frame-decode

- **Code**: this repo, `src/frame.c` and `src/frame.h`, at commit `0000000000000000000000000000000000000000` (`run_example.sh` re-stamps this line and `findings.json` with the commit of its scratch repository, so `check_run.py` validates against a real git history)
- **Harness**: `harness_frame_decode.c`, a CBMC harness compiled with the unmodified `src/frame.c`; real-code mutant `mutants/drop-cap-check.patch`
- **Properties in plain words**: `FrameReadsInBounds` - `frame_decode` never reads outside `in[0..in_len)`. `NoLengthTruncation` - the header-plus-payload length is computed without truncation. `FrameWritesInBounds` - `frame_decode` never writes outside `out[0..cap)`. All three come from the contract in `src/frame.h:6-10`.

## Harness

| Harness part | Code | Notes |
|---|---|---|
| `frame_decode(in, in_len, out, cap)` | `src/frame.c:4-16`, compiled unmodified (`cbmc <harness> src/frame.c -I src`) | the function under verification; nothing in it is stubbed |
| `in = malloc(in_len)` with nondeterministic contents | the `in`, `in_len` arguments (`src/frame.h:10`) | every byte value, every length `0..MAX_IN` |
| `out = malloc(cap)` | the `out`, `cap` arguments | every capacity `0..MAX_OUT` |
| `MAX_IN = 8` | input length bound | assumption: the read bug needs `n + 1 > in_len`, which every length up to 8 already shows; longer inputs only scale the same path |
| `MAX_OUT = 256` | output capacity bound | covers every value of the 1-byte length `n` (0..255), so `n > cap` and `n <= cap` are both reachable for every `n` |
| `__CPROVER_assume(in != NULL && out != NULL)` | callers pass valid buffers (`src/frame.h:6-10` says nothing about NULL) | assumption: NULL buffers are out of scope |
| `VF_SANITY` | none | sanity: a must-fail assertion at the end; it fails, so the assumptions are satisfiable and the call returns |

## Checks and what they cover

| Property | CBMC checks (explicit flags) | Run | CBMC check id |
|---|---|---|---|
| `FrameReadsInBounds` | `--bounds-check --pointer-check` | `results/Harness.json` | `frame_decode.precondition_instance.2` (`memcpy source region readable`, `src/frame.c:14`) |
| `NoLengthTruncation` | `--conversion-check --signed-overflow-check` | `results/Harness.json` | `frame_decode.overflow.2` (`(unsigned char)((signed int)n + 1)`, `src/frame.c:9`) |
| `FrameWritesInBounds` | `--bounds-check --pointer-check`, restricted with `--property frame_decode.precondition_instance.3` | `results/Writes.json` | `memcpy destination region writeable` (`src/frame.c:14`); isolated because the read and conversion checks fail on this code |

The harness has no loops, so no unwinding bound applies; CBMC's `memcpy` model copies the whole region in one step.

## Mutant

| Patch | Code change | Expected |
|---|---|---|
| `mutants/drop-cap-check.patch` | removes `if (n > cap) return -1;` (`src/frame.c:12-13`) | `FrameWritesInBounds` fails (`memcpy destination region writeable`): the write check really guards that line |

## Not covered

- Callers of `frame_decode`: none in this example.
- Inputs longer than 8 bytes, and NULL buffers (see the assumptions above).
- Concurrency: `frame_decode` has no shared state.
