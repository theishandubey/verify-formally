# Plan 001: Compute the frame total in size_t

> **Executor instructions**: Follow this plan step by step.
> Run every verification command and confirm the expected result before moving to the next step.
> If anything in "STOP conditions" occurs, stop and report; do not improvise.
> When done, update this plan's row in `plans/README.md`, unless a reviewer dispatched you and said they maintain the index.
>
> **Drift check (run first)**: `git diff --stat 0000000000000000000000000000000000000000..HEAD -- src/frame.c src/frame.h`
> If either file changed since this plan was written, compare the "Current state" excerpt below with the live code; on a mismatch, treat it as a STOP condition.

## Status

- **Findings closed**: `verification/findings/001-length-wrap-reads-past-input.md` (CONFIRMED, severity high)
- **Fix check**: observed (prototyped in a disposable copy: the repro test passes and the CBMC harness passes all 41 checks after `fix.patch`)
- **Priority**: P1
- **Effort**: S
- **Risk**: LOW - one declaration changes type; no caller-visible behavior changes for well-formed frames
- **Depends on**: none
- **Planned at**: commit `0000000000000000000000000000000000000000`, 2026-10-21

## Why this matters

A frame whose first byte is 255 makes `frame_decode` copy 255 bytes from an input that may be only a few bytes long, and hand those bytes to the caller.
On untrusted input that is an out-of-bounds read: a leak of adjacent memory, or a crash.

## The proof that it is broken

- Repro test: `bash verification/repro/run_c_repro.sh verification/repro/repro_frame_decode.c test_length_255_reads_past_input`.
  Today it fails with:
  ```
  FAIL test_length_255_reads_past_input: FrameReadsInBounds violated: SUMMARY: AddressSanitizer: heap-buffer-overflow ... in memcpy
  ```
- Verifier: the CBMC run in `verification/models/frame-decode/results/Harness.json` reports `frame_decode.overflow.2` (the truncating conversion at `src/frame.c:9`) and `frame_decode.precondition_instance.2` (the read at `src/frame.c:14`).

## Current state

- `src/frame.c` - the only file in scope:

```c
    unsigned char n = in[0];
    unsigned char total = n + 1; /* header byte plus payload */
    if (total > in_len)
        return -1; /* truncated */
```

- No existing tests for `frame_decode`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Repro test | `bash verification/repro/run_c_repro.sh verification/repro/repro_frame_decode.c test_length_255_reads_past_input` | `PASS test_length_255_reads_past_input` (after fix) |
| Guard test | `bash verification/repro/run_c_repro.sh verification/repro/repro_frame_decode.c test_well_formed_frame_decodes` | `PASS test_well_formed_frame_decodes` |
| Verifier | `<skill>/scripts/run_code_check.py --tool cbmc --out /tmp/fixed.json --quiet -- cbmc verification/models/frame-decode/harness_frame_decode.c src/frame.c -I src --function harness --bounds-check --pointer-check --conversion-check --signed-overflow-check` | `pass - checks=41 exhaustive=true` |

## Scope

**In scope** (the only files you should modify):
- `src/frame.c`
- `tests/test_frame.c` (new; the repro and guard tests move here, see step 3)

**Out of scope**:
- `verification/**` - the harness and results are evidence; do not edit them to make them pass.

## Git workflow

- Branch: `verify/001-widen-frame-total`.
- Commit message style: short, imperative (e.g. `compute frame total in size_t`).
- Do not push or open a PR unless the operator instructed it.

## Steps

### Step 1: Confirm the failure

Run the repro command.
**Verify**: it fails with the `FrameReadsInBounds violated` line quoted above.
If it passes or fails differently, STOP.

### Step 2: Widen the total

In `src/frame.c`, change `unsigned char total = n + 1;` to `size_t total = (size_t)n + 1;`.
This is exactly `fix.patch`.
**Verify**: the repro command prints `PASS test_length_255_reads_past_input`, and the verifier command passes with `exhaustive=true`.

### Step 3: Move the tests into the real suite

Copy `repro_frame_decode.c` to `tests/test_frame.c` and build it the same way `run_c_repro.sh` does (with the sanitizers) in the project's test target.
**Verify**: both tests pass.

## Test plan

- `test_length_255_reads_past_input`: the finding's repro test.
- `test_well_formed_frame_decodes`: the guard test.
- Worth adding: a frame whose length byte equals `in_len - 1` exactly (the boundary that must be accepted).

## Done criteria

All must hold:

- [ ] Repro command passes.
- [ ] The guard test passes.
- [ ] The verifier command (`run_code_check.py --tool cbmc` with the same harness, flags, and bounds) reports `pass` with `exhaustive: true` on the fixed code.
- [ ] No files outside the in-scope list are modified (`git status`).
- [ ] `plans/README.md` row updated.

## STOP conditions

Stop and report back if:
- The repro test does not fail before the fix, or fails with a different message.
- The code at `src/frame.c` does not match the "Current state" excerpt above.
- The verifier still reports a failing check after the fix.
- The fix appears to need changes outside `src/frame.c` and the new test file.

## Maintenance notes

- Any arithmetic on the length byte (a header grown to two bytes, a trailer or checksum added to `total`) must be computed in `size_t`; re-run the CBMC harness after such a change.
