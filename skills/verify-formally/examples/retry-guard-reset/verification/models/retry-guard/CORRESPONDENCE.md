# Correspondence: retry-guard

- **Code**: this repo, `retry.py`, at commit `7cc0dcad4bd2f8a9bdd48121237218153bf847c2` (`verification/findings.json`'s `commit` field carries the exact value for the run being validated; `run_example.sh`'s `check-run` step re-stamps a fresh commit in its own temp copy so `scripts/check_run.py` can validate against a real git repository)
- **Model**: `RetryGuard.tla` (+ `RetryGuard.cfg`, `RetryGuardLiveness.cfg`, `RetryGuardFixed.cfg`, `RetryGuardFixedSmallBudget.cfg`, `Plan001.cfg`, `sanity/ReachesGaveUp.cfg`, `sanity/ReachesGaveUpFixed.cfg`, `sanity/ReachesGaveUpFixedSmall.cfg`, `mutants/RetryGuardFixedMutant.cfg`, `mutants/RetryGuardTerminationMutant.cfg`), `verification/lean/RetryGuard/Model.lean`
- **Property in plain words**: `BoundedAttempts` - the total number of calls to `op()` never exceeds `max_errors + max_timeouts`. `Termination` - `call_with_retry` always eventually returns or raises, no matter what `op()` does.

## State

| Model | Code | Notes |
|---|---|---|
| `errors` | `errors` (`retry.py:10`, updated at `retry.py:16` and reset at `retry.py:20`) | Consecutive-`TransientError` counter. |
| `timeouts` | `timeouts` (`retry.py:11`, updated at `retry.py:19` and reset at `retry.py:17`) | Consecutive-`TimeoutError` counter. |
| `attempts` | the implicit loop-iteration count; one `attempts` step = one call to `op()` at `retry.py:14` | Not a variable in the buggy code at all; it is the abstraction this example adds (`fix.patch` turns it into a real variable, incremented at `fix.patch`'s new line before the `try:`). |
| `status = "running"` | still inside `while True:` (`retry.py:12`) | |
| `status = "gave_up"` | `raise GiveUp(...)` (`retry.py:22`) | |
| `status = "succeeded"` | `return op()` (`retry.py:14`) | |

## Actions

| Model action | Code | Environment choices |
|---|---|---|
| `SuccessStep` | `op()` returns normally, `return op()` (`retry.py:14`) | Ends the loop immediately, matching `status' = "succeeded"`. |
| `TransientStep` | `except TransientError:` (`retry.py:15-17`) | `errors += 1; timeouts = 0` then the shared give-up check (`retry.py:21`). |
| `TimeoutStep` | `except TimeoutError:` (`retry.py:18-20`) | `timeouts += 1; errors = 0` then the shared give-up check (`retry.py:21`). |
| `Done` (self-loop) | the function has already returned or raised | Not represented in Python; added purely so TLC has a successor state once the loop has logically ended. |

## Guards and constants

| Model | Code | Checked equal? |
|---|---|---|
| `GiveUpCond(e, t, a) == e >= MaxErrors \/ t >= MaxTimeouts \/ (Fixed /\ AttemptsGuard(a))`, `AttemptsGuard(a) == a >= MaxAttempts` (`MutantAttemptsStrict = FALSE`) | `if errors >= max_errors or timeouts >= max_timeouts:` (`retry.py:21`, unfixed) / `if errors >= max_errors or timeouts >= max_timeouts or attempts >= max_attempts:` (`fix.patch`) | yes, same `>=` direction on all three disjuncts; the third disjunct only exists when `Fixed = TRUE`, matching `fix.patch` exactly. |
| `MaxErrors` | `max_errors=3` (`retry.py:9`) | yes |
| `MaxTimeouts` | `max_timeouts=3` (`retry.py:9`) | yes |
| `MaxAttempts == MaxErrors + MaxTimeouts` (derived, not a `CONSTANT`) | not present in `retry.py`; `fix.patch`'s `max_attempts=None` default, resolved to `max_errors + max_timeouts` at the top of the function | yes - this is the exact derivation `fix.patch` performs, not an independently chosen number, so it is checked equal for every `MaxErrors`/`MaxTimeouts` combination, not just the one bound set a cfg happens to use |
| `Fixed` | n/a (model-only selector) | `FALSE` = `retry.py` as shipped; `TRUE` = `retry.py` + `fix.patch` |

## Abstractions

| Abstraction | Why | Errs toward |
|---|---|---|
| `op`'s only observable outcomes are success, `TransientError`, `TimeoutError` | any other exception propagates unchanged past the retry loop (`retry.py:14`), so it cannot contribute to the runaway-retry bug | neutral (the excluded behavior does not exercise this property) |
| which way a call to `op()` fails is adversarial, unconstrained nondeterminism | worst case, not the specific pattern in the repro test | over-approximation (covers every possible environment behavior, so a spurious pass here cannot hide a real bug) |
| the `GiveUp`/exception message text is not modeled | the property only concerns whether/when the loop exits, not the message | neutral |
| `errors`/`timeouts` bounded by construction (`TypeOK`) | both reset the instant the other kind of failure fires, and the model transitions out of `"running"` the instant either threshold is hit | neutral (follows directly from the actions, not asserted) |

## Fix flags

| Flag | Planned code change | Model change | New interference points the change creates |
|---|---|---|---|
| `Fixed` | add `max_attempts=None` (resolved to `max_errors + max_timeouts` when not given) and an `attempts` counter incremented every iteration (`fix.patch`, new lines before `retry.py:12`/`retry.py:14`), and add `attempts >= max_attempts` to the give-up check (`fix.patch`, replaces `retry.py:21`) | `GiveUpCond`'s third disjunct `Fixed /\ AttemptsGuard(a)` (`AttemptsGuard(a) == a >= MaxAttempts`, `MaxAttempts == MaxErrors + MaxTimeouts`) becomes live; `NextAttempts` increments `attempts` on every step when `Fixed = TRUE` (the increment itself is unconditional in the real fix, see `MutantNoIncrement` below) | none: the increment and the check are one atomic Python statement/expression each, with no `await`, exception, or signal boundary between them, so no new action split is needed |

## Vacuity-only constants (not part of any fix)

| Constant | Purpose | Real code counterpart |
|---|---|---|
| `MutantNoIncrement` | vacuity mutant for `Termination` only: when `TRUE`, `NextAttempts` never increments `attempts`, so `Fixed`'s guard is syntactically present but can never fire - the fixed model degenerates to the buggy model's give-up condition by a different mechanism than flipping `Fixed` off | none; this models a hypothetical bug where the fix ships with its counter never wired up (the increment silently dropped), not any code that exists |
| `MutantAttemptsStrict` | vacuity mutant for `BoundedAttempts` only: when `TRUE`, `AttemptsGuard(a)` uses `a > MaxAttempts` instead of `a >= MaxAttempts` - the fix's own new guard is present but off by one, a different mechanism than turning `Fixed` off | none; this models a hypothetical bug where `fix.patch`'s `attempts >= max_attempts` shipped as `attempts > max_attempts` instead |

## Not modeled

- `op`'s payload/return value: acking, logging, and any side effect of a successful call are irrelevant to whether `call_with_retry` gives up or how many times it calls `op()`.
- Exceptions other than `TransientError`/`TimeoutError`/success (see Abstractions above): they propagate through `call_with_retry` unchanged, so this target's properties do not apply to them.

## Trace to test

Every action is driven from `verification/repro/repro_retry_guard_reset.py` by controlling what a fake `op()` raises or returns on each call:

- `TransientStep` <-> `op()` raising `TransientError`.
- `TimeoutStep` <-> `op()` raising `TimeoutError` (Python's built-in `TimeoutError`).
- `SuccessStep` <-> `op()` returning a value.
- The counterexample trace (alternating `TransientStep`/`TimeoutStep` forever) is driven by `test_alternating_failures_cause_runaway_retry`'s `op`, which raises `TransientError` on odd call counts and `TimeoutError` on even ones; `op` itself asserts `BoundedAttempts` (`calls <= max_errors + max_timeouts`) on every call, which both states the violated property directly and fails fast instead of looping (TLC's "no bound on `attempts`" becomes "the assertion trips on the 7th call" rather than an unbounded Python loop).
- `test_gives_up_after_max_errors_consecutive_errors` drives only `TransientStep` repeatedly (never alternating), the guard trace that shows the harness passes when the model predicts no violation.
