# 001: Alternating TransientError/TimeoutError resets both retry counters, so call_with_retry never gives up

- **Status**: CONFIRMED
- **Severity**: medium - triggers whenever a dependency alternates between two distinct failure modes (a flaky call that sometimes times out and sometimes errors outright); no crash, but the caller retries forever, holding a thread/coroutine and never surfacing the failure
- **Targets**: retry-guard (`verification/models/retry-guard/`)
- **Tools**: TLA+
- **Properties violated**: `BoundedAttempts` - the total number of calls to `op()` never exceeds `max_errors + max_timeouts` (source: inferred from `retry.py`'s own `max_errors`/`max_timeouts` naming); `Termination` - `call_with_retry` always eventually returns or raises (source: inferred - a retry helper with two declared limits is expected to terminate)
- **Location**: `retry.py:12-22` (the retry loop and its two `except` branches)
- **Commit**: `7cc0dcad4bd2f8a9bdd48121237218153bf847c2`
- **Repro tests**: `verification/repro/repro_retry_guard_reset.py::test_alternating_failures_cause_runaway_retry` (alternating `TransientError`/`TimeoutError` forever)
- **Repro command**: `.venv/bin/python -m pytest -q -p no:cacheprovider verification/repro/repro_retry_guard_reset.py::test_alternating_failures_cause_runaway_retry` (fails on current code)
- **Guard tests**: `verification/repro/repro_retry_guard_reset.py::test_gives_up_after_max_errors_consecutive_errors` (passes)
- **Plan**: `plans/001-add-attempt-budget.md`

## What goes wrong

`call_with_retry` tracks two independent counters, `errors` and `timeouts`, and each `except` branch resets the *other* counter on the reasoning that "a different kind of failure means the previous streak is over" (`retry.py:16-17`, `retry.py:19-20`).
If a dependency fails by alternating between the two kinds of error - transient, timeout, transient, timeout, forever - then on every single call one counter increments to 1 and the other resets to 0.
Neither counter ever reaches its limit, `GiveUp` is never raised, and `call_with_retry` retries forever, holding whatever thread or coroutine called it.

## Counterexample

1. `TransientStep` (`retry.py:15-17`): `op()` raises `TransientError`; `errors = 1, timeouts = 0`.
2. `TimeoutStep` (`retry.py:18-20`): `op()` raises `TimeoutError`; `errors = 0, timeouts = 1` (the transient streak is discarded).
3. `TransientStep` again: `errors = 1, timeouts = 0` (the timeout streak is discarded).
4. ... repeats forever; `errors` and `timeouts` never exceed 1, so `retry.py:21`'s check never fires.

Checker: `run_tlc.sh RetryGuard.tla RetryGuardLiveness.cfg`, constants `MaxErrors=3, MaxTimeouts=3, Fixed=FALSE` (`VIEW ViewNoAttempts`), 12 distinct states, depth 3 (a 2-state back-loop), result `property_violation` of `Termination`.
The same root cause also violates `BoundedAttempts`: `run_tlc.sh RetryGuard.tla RetryGuard.cfg`, same constants, 51 distinct states (search halts at the first violation), depth 8, result `invariant_violation` of `BoundedAttempts`.

## Reproduction

`test_alternating_failures_cause_runaway_retry` drives `call_with_retry` with a fake `op()` that raises `TransientError` on odd call counts and `TimeoutError` on even ones; `op()` itself asserts the violated property on every call - `calls <= max_errors + max_timeouts` - so the test fails on the 7th call instead of looping:

```
AssertionError: BoundedAttempts violated: call_with_retry made 7 calls to op() without giving
up, more than max_errors + max_timeouts = 6; alternating TransientError/TimeoutError resets
both counters on every call, so neither limit is ever reached
```

## Root cause

`retry.py:17` (`timeouts = 0` in the `TransientError` branch) and `retry.py:19` (`errors = 0` in the `TimeoutError` branch) each reset the *other* counter instead of leaving it alone, and there is no counter that survives across failure kinds - only two counters that repeatedly cancel each other out at `retry.py:21`.

## Fix direction

Add a total-attempts counter that is incremented every call and never reset, derive its budget from the two existing limits (`max_attempts = max_errors + max_timeouts` when not given explicitly), and add it as a third, independent condition to the give-up check.
Fix flag `Fixed` (model change: `GiveUpCond`'s third disjunct `Fixed /\ AttemptsGuard(a)`, `AttemptsGuard(a) == a >= MaxAttempts`, `MaxAttempts == MaxErrors + MaxTimeouts`) models exactly this; `Plan001.cfg` (`Fixed = TRUE`, same constants) passes both `BoundedAttempts` and `Termination`, and `RetryGuardFixedSmallBudget.cfg` (`MaxErrors=2, MaxTimeouts=2`) confirms the same holds at a second, smaller bound set - the fix is not tuned to one caller's limits.
Fix check: **observed** - `fix.patch` was applied in a disposable copy and all three tests in `repro_retry_guard_reset.py` pass (see `run_example.sh`'s `pytest-fixed` row); the derived budget was also checked by hand at `max_errors=2, max_timeouts=2` (see the example README's "The fix" section).

## Vacuity

- `BoundedAttempts`: mutant `mutants/RetryGuardFixedMutant.cfg` (fixed model, `MaxErrors=2, MaxTimeouts=2`, `Fixed = TRUE`, but `MutantAttemptsStrict = TRUE` so the fix's own guard reads `a > MaxAttempts` instead of `a >= MaxAttempts`) still violates `BoundedAttempts` - the guard's exact direction, not just its presence, is load-bearing.
- `Termination`: mutant `mutants/RetryGuardTerminationMutant.cfg` (fixed model, `Fixed = TRUE`, but `MutantNoIncrement = TRUE` so `attempts` is never actually incremented - a different mechanism than turning `Fixed` off) still violates `Termination`.
- Both: sanity `sanity/ReachesGaveUp.cfg` (buggy constants), `sanity/ReachesGaveUpFixed.cfg` (fixed, 3/3), and `sanity/ReachesGaveUpFixedSmall.cfg` (fixed, 2/2) - each `INVARIANT SanityReachesGaveUp == status # "gave_up"` - are violated at the same constants as the runs they support, confirming the `"gave_up"` state both properties talk about is actually reachable at every bound set reported, including both passing fixed-model runs.
