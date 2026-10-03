# Plan 001: Add a total-attempt budget to call_with_retry

> **Executor instructions**: Follow this plan step by step.
> Run every verification command and confirm the expected result before moving to the next step.
> If anything in "STOP conditions" occurs, stop and report; do not improvise.
> When done, update this plan's row in `plans/README.md`, unless a reviewer dispatched you and said they maintain the index.
>
> **Drift check (run first)**: `git diff --stat 7cc0dcad4bd2f8a9bdd48121237218153bf847c2..HEAD -- retry.py`
> If `retry.py` changed since this plan was written, compare the "Current state" excerpt below with the live code; on a mismatch, treat it as a STOP condition.

## Status

- **Findings closed**: `verification/findings/001-alternating-failures-runaway-retry.md` (CONFIRMED, severity medium)
- **Fix check**: observed (prototyped in a disposable copy: all three tests in `repro_retry_guard_reset.py` pass after `fix.patch`)
- **Priority**: P2
- **Effort**: S
- **Risk**: LOW - additive parameter with a safe default, no change to existing call sites
- **Depends on**: none
- **Planned at**: commit `7cc0dcad4bd2f8a9bdd48121237218153bf847c2`, 2026-09-25

## Why this matters

`call_with_retry` has no bound on the total number of calls to `op()`: its two counters (`errors`, `timeouts`) each reset the other on every failure of the opposite kind, so a dependency that alternates between the two failure modes is retried forever.
This is a real, observable hang - not a crash, so nothing surfaces it - and it holds whatever thread or coroutine called `call_with_retry` for as long as the dependency keeps alternating.

## The proof that it is broken

- Repro test: `verification/repro/repro_retry_guard_reset.py::test_alternating_failures_cause_runaway_retry`, run with `.venv/bin/python -m pytest -q -p no:cacheprovider verification/repro/repro_retry_guard_reset.py::test_alternating_failures_cause_runaway_retry`.
  Today it fails with:
  ```
  AssertionError: BoundedAttempts violated: call_with_retry made 7 calls to op() without
  giving up, more than max_errors + max_timeouts = 6; alternating TransientError/TimeoutError
  resets both counters on every call, so neither limit is ever reached
  ```
- Model: `verification/models/retry-guard/RetryGuard.tla`; `RetryGuard.cfg` (buggy) reports `invariant_violation` of `BoundedAttempts` and `RetryGuardLiveness.cfg` reports `property_violation` of `Termination`; `Plan001.cfg` (`Fixed = TRUE`, the only fix flag this model has) passes both within `MaxErrors=3, MaxTimeouts=3` (`MaxAttempts` is derived in the model as `MaxErrors + MaxTimeouts = 6`, matching the fix below exactly); `RetryGuardFixedSmallBudget.cfg` shows the same passes at `MaxErrors=2, MaxTimeouts=2`.
  The fix flag models exactly the change in the steps below; if you change the approach, the model no longer vouches for it (STOP condition).
- Counterexample in code terms: `TransientStep` (`retry.py:15-17`) then `TimeoutStep` (`retry.py:18-20`) then `TransientStep` again, repeating forever; `errors` and `timeouts` never exceed 1, so `retry.py:21`'s check never fires.

## Current state

- `retry.py` - the only file in scope; a single free function, `call_with_retry`.

```python
def call_with_retry(op, max_errors=3, max_timeouts=3):
    errors = 0
    timeouts = 0
    while True:
        try:
            return op()
        except TransientError:
            errors += 1
            timeouts = 0
        except TimeoutError:
            timeouts += 1
            errors = 0
        if errors >= max_errors or timeouts >= max_timeouts:
            raise GiveUp(f"gave up after {errors} errors, {timeouts} timeouts")
```

- No existing tests for `retry.py` in this example; the repro/guard tests below are the first.
- Conventions to match: plain functions, no framework; keyword arguments with defaults for every limit (`max_errors=3, max_timeouts=3`), matching the style to extend with `max_attempts=None`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Repro test | `.venv/bin/python -m pytest -q -p no:cacheprovider verification/repro/repro_retry_guard_reset.py::test_alternating_failures_cause_runaway_retry` | 1 passed (after fix); fails today with `AssertionError: BoundedAttempts violated` |
| Full tests | `.venv/bin/python -m pytest -q -p no:cacheprovider verification/repro/repro_retry_guard_reset.py` | 3 passed |
| Model check (fixed) | `<skill>/scripts/run_tlc.sh verification/models/retry-guard/RetryGuard.tla verification/models/retry-guard/Plan001.cfg` | `"result": "pass"` |

## Scope

**In scope** (the only files you should modify):
- `retry.py`
- `tests/test_retry.py` (new; the repro test moves here, see step 3)

**Out of scope**:
- `verification/models/**` - the models are evidence; do not edit them to make them pass.
- `verification/repro/repro_retry_guard_reset.py` - copy it into `tests/`, do not edit the original in place.

## Git workflow

- Branch: `verify/001-add-attempt-budget`.
- Commit message style: short, imperative, lower case first word (e.g. `add max_attempts budget to call_with_retry`).
- Do not push or open a PR unless the operator instructed it.

## Steps

### Step 1: Confirm the failure

Run the repro command.
**Verify**: it fails with the `AssertionError: BoundedAttempts violated` failure quoted above.
If it passes or fails differently, STOP (the code has drifted or the environment differs).

### Step 2: Add the attempt budget

In `retry.py`:
- Add a `max_attempts=None` parameter to `call_with_retry`.
- At the top of the function, if `max_attempts is None`, set it to `max_errors + max_timeouts` - the budget is derived from the two limits the caller already gave, not an independently chosen number, so it stays correct for any `max_errors`/`max_timeouts` the caller passes.
- Add `attempts = 0` next to the existing `errors = 0` / `timeouts = 0` initialization.
- Increment `attempts += 1` once per loop iteration, before the `try:` block (so it counts every call to `op()`, including the one that eventually succeeds).
- Extend the give-up condition to `if errors >= max_errors or timeouts >= max_timeouts or attempts >= max_attempts:` and include `attempts` in the `GiveUp` message.

This is exactly `fix.patch` in this directory; apply it verbatim or reproduce the same diff by hand.
**Verify**: repro command -> passes.

### Step 3: Move the repro into the real suite

Copy `test_alternating_failures_cause_runaway_retry` and `test_gives_up_after_max_errors_consecutive_errors` (the guard test) from `verification/repro/repro_retry_guard_reset.py` into `tests/test_retry.py`, adjusting the import to `from retry import ...` (no `sys.path` manipulation needed once the test lives next to `retry.py`'s normal test location); keep the assertions unchanged.
**Verify**: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_retry.py` -> all pass, including the new tests.

## Test plan

- `test_alternating_failures_cause_runaway_retry` (moved to `tests/test_retry.py`): the finding's repro test.
- `test_gives_up_after_max_errors_consecutive_errors` (moved to `tests/test_retry.py`): the guard test.
- `test_success_after_one_error` (already in `verification/repro/repro_retry_guard_reset.py`): an additional regression case worth moving alongside the other two, since it exercises the loop's success path with the new `attempts` counter in play.

## Done criteria

All must hold:

- [ ] Repro command passes.
- [ ] `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_retry.py` exits 0 with 3 passed.
- [ ] `run_tlc.sh verification/models/retry-guard/RetryGuard.tla verification/models/retry-guard/Plan001.cfg` reports `pass` (unchanged model; confirms the fix matches the modeled fix).
- [ ] No files outside the in-scope list are modified (`git status`).
- [ ] `plans/README.md` row updated.

## STOP conditions

Stop and report back if:
- The repro test does not fail before the fix, or fails with a different message.
- The code at `retry.py` does not match the "Current state" excerpt above.
- The fix appears to need changes outside `retry.py` and `tests/test_retry.py`.
- The fixed code passes the repro test but breaks another test in a way that suggests the old unbounded-retry behavior was relied on (unlikely here, but would mean this is BY-DESIGN after all).
- The change you are about to make differs from the one described in Step 2 (for example, incrementing `attempts` only inside one of the `except` branches instead of once per loop iteration) - the model only vouches for the described change.

## Maintenance notes

- Any new failure kind added to `call_with_retry` (a third `except` branch) must also count toward `attempts`, or it reopens the same class of bug with three counters instead of two.
- A reviewer should check that `attempts` increments before the `try:` block, not after - incrementing after would undercount the call that raised the exception.
- A caller that passes an explicit `max_attempts` opts out of the derived default; that is intentional (it lets a caller set a tighter or looser total budget than `max_errors + max_timeouts`), but it also means `BoundedAttempts` (as modeled) only holds for the derived default - an explicit `max_attempts` smaller than `max_errors + max_timeouts` is outside this model's scope.
