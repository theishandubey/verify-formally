# retry-guard-reset

A worked example of the `verify-formally` skill: a retry loop where two failure counters reset each other, so neither limit is ever reached under an alternating failure pattern, producing an infinite retry.
This directory is laid out as **a repository after a completed run**: `retry.py` and `fix.patch` are the code and its fix, and `verification/` and `plans/` are exactly what the skill would have written for this one target.
`run_example.sh` rebuilds every checker output from scratch and then validates the whole thing with `scripts/check_run.py`, the same validator a real run must pass.

## The code

`retry.py` wraps a call to `op()` in a retry loop with two independent limits: `max_errors` consecutive `TransientError`s, and `max_timeouts` consecutive `TimeoutError`s.
Each `except` branch increments its own counter and resets the *other* one, on the reasoning that "a different kind of failure means the previous streak is over" (retry.py:16-17 and retry.py:19-20).
That reasoning is exactly the bug.

## Why it is buggy

If `op()` fails with `TransientError`, then `TimeoutError`, then `TransientError`, then `TimeoutError`, forever, then on every single call one counter is incremented to 1 and the other is reset to 0.
Neither counter ever reaches its limit (retry.py:21), so `GiveUp` is never raised and `call_with_retry` retries forever.
There is no total-attempts budget, only two counters that repeatedly cancel each other out.

## The TLA+ model

`verification/models/retry-guard/RetryGuard.tla` models one call to `op()` as one of three nondeterministic actions: `SuccessStep`, `TransientStep`, `TimeoutStep` (see `verification/models/retry-guard/CORRESPONDENCE.md` for the exact line-by-line mapping to `retry.py`).
State: `errors`, `timeouts`, `attempts` (the abstract call counter - there is no such variable in the buggy code; it is what the fix adds), and `status \in {"running", "gave_up", "succeeded"}`.
`GiveUpCond` mirrors retry.py:21 exactly, plus a third disjunct `Fixed /\ AttemptsGuard(a)` that only exists when the `Fixed` constant is `TRUE` (i.e. `fix.patch` applied); `AttemptsGuard(a)` is `a >= MaxAttempts`, where `MaxAttempts == MaxErrors + MaxTimeouts` is a **derived operator, not a `CONSTANT`** - it is computed the same way `fix.patch` computes `max_attempts` (`max_errors + max_timeouts`, whenever the caller does not pass an explicit `max_attempts`), so the model automatically tracks the fix at *any* `MaxErrors`/`MaxTimeouts` the cfg chooses, not just one hand-picked combination.
`Done` is a self-loop once `status # "running"`, purely so TLC has a successor state (Python has simply returned or raised by that point).
Two further constants exist only to drive the vacuity mutants below (`MutantNoIncrement`, `MutantAttemptsStrict`); both are `FALSE` in every cfg that reports a real result.

Constants used throughout: `MaxErrors = 3, MaxTimeouts = 3` (so `MaxAttempts = 6`) for the primary bound set, and `MaxErrors = 2, MaxTimeouts = 2` (so `MaxAttempts = 4`) for a second, smaller bound set that shows the fix is not tuned to one caller's limits.

### The invariant and its bound

`BoundedAttempts == attempts <= MaxAttempts` (i.e. total attempts should never exceed the sum of both per-kind limits - the bound a caller actually cares about, and, since `MaxAttempts` is derived exactly that way, also the tightest bound the fix can promise).
The fixed model hits this bound with equality: the very step that pushes `attempts` to `MaxAttempts` also sets `status' = "gave_up"` (or `"succeeded"`), and `attempts` is frozen from then on, so `attempts <= MaxAttempts` is exact, not slack, at every `MaxErrors`/`MaxTimeouts` combination checked.

### The cfg files

| cfg | `Fixed` | `MaxErrors, MaxTimeouts` | Checks | TLC result (observed) |
|---|---|---|---|---|
| `RetryGuard.cfg` | `FALSE` | `3, 3` | `TypeOK`, `BoundedAttempts` | **invariant_violation** of `BoundedAttempts` |
| `RetryGuardLiveness.cfg` | `FALSE` | `3, 3` | `Termination == <>(status # "running")` under `WF_Vars(Step)` | **property_violation**, a 2-state back-loop |
| `RetryGuardFixed.cfg` | `TRUE` | `3, 3` | `TypeOK`, `BoundedAttempts`, `Termination` | **pass** |
| `RetryGuardFixedSmallBudget.cfg` | `TRUE` | `2, 2` | `TypeOK`, `BoundedAttempts`, `Termination` | **pass** (second bound set: the same fix, a different caller's limits) |
| `Plan001.cfg` | `TRUE` | `3, 3` | same as `RetryGuardFixed.cfg` | **pass** (the per-plan cfg `plans/001-add-attempt-budget.md` names as its done criterion) |
| `sanity/ReachesGaveUp.cfg` | `FALSE` | `3, 3` | `SanityReachesGaveUp == status # "gave_up"` | **invariant_violation** (vacuity: the state is reachable at the buggy run's constants) |
| `sanity/ReachesGaveUpFixed.cfg` | `TRUE` | `3, 3` | `SanityReachesGaveUp` | **invariant_violation** (vacuity: reachable at `RetryGuardFixed.cfg`'s constants too) |
| `sanity/ReachesGaveUpFixedSmall.cfg` | `TRUE` | `2, 2` | `SanityReachesGaveUp` | **invariant_violation** (vacuity: reachable at `RetryGuardFixedSmallBudget.cfg`'s constants too) |
| `mutants/RetryGuardFixedMutant.cfg` | `TRUE`, `MutantAttemptsStrict = TRUE` | `2, 2` | `TypeOK`, `BoundedAttempts` | **invariant_violation** again (vacuity for `BoundedAttempts` - the fix's own guard, off by one) |
| `mutants/RetryGuardTerminationMutant.cfg` | `TRUE`, `MutantNoIncrement = TRUE` | `3, 3` | `Termination` | **property_violation** again (vacuity for `Termination` - the fix's own increment is skipped) |

Exact state counts and depths are in `verification/models/retry-guard/results/*.json` (regenerated by `verification/rerun.sh`, and reproduced from a scratch copy by `run_example.sh`'s self-check, so they always match the spec on disk) and are copied verbatim into `verification/findings.json`'s `runs[]`.

Run any of these with `run_tlc.sh`, e.g.:

```
scripts/run_tlc.sh examples/retry-guard-reset/verification/models/retry-guard/RetryGuard.tla examples/retry-guard-reset/verification/models/retry-guard/RetryGuardLiveness.cfg
```

### The liveness check needs care

The environment can choose failures forever, so `<>(status # "running")` cannot mean "the environment is eventually nice to us" - it has to mean "the loop terminates no matter what the environment does," which is exactly what `WF_Vars(Step)` plus the give-up logic is supposed to guarantee.
`attempts` grows without bound along the buggy infinite-retry behavior, and since it is part of the state, no two states along that behavior are ever identical - which means TLC's liveness checker (which needs to find a literal cycle, a "lasso," in the state graph) can never find one, and the check would time out searching a state space that is infinite exactly where it needs a repeat.
The fix used here is TLC's `VIEW` config option (`RetryGuardLiveness.cfg` sets `VIEW ViewNoAttempts`, where `ViewNoAttempts == <<errors, timeouts, status>>`): TLC uses the view, not the full state, to decide whether two states are "the same" for the purposes of the model-checking search, so the ever-growing `attempts` component is invisible to cycle detection.
Dropping `attempts` from the view is sound, not just convenient: with `Fixed = FALSE` the third disjunct of `GiveUpCond` never applies, so `attempts` does not affect which actions are enabled or what any successor state's `errors`/`timeouts`/`status` are - two states that agree on `<<errors, timeouts, status>>` are indistinguishable in every way the property or the transition relation can observe, i.e. `ViewNoAttempts` is a bisimulation for this spec.
That also means the "lasso" TLC reports is a *view-level* cycle, not a literal repeated full state: the printed counterexample's last state reports `Back to state: 2`, a distinct full state (`attempts` differs) that the view-abstracted search treats as closing the loop, because it is `<<errors, timeouts, status>>` equality among the states reachable from there, not full-state equality, that TLC is testing.
For the safety check (`RetryGuard.cfg`) this problem does not arise the same way - TLC halts at the very first invariant violation, which happens at a shallow depth regardless of how large the reachable state space might eventually get - but a `CONSTRAINT StateConstraint == attempts <= Bound` (`Bound = 10`) is still included defensively, in case the cfg is ever reused with the invariant removed.

Worth noting: `[][Next]_Vars` alone permits stuttering forever, even in states where `Step` is enabled, so termination is never "unconditional" under plain `[][Next]_Vars` - some fairness hypothesis is always needed to rule out the model simply refusing to ever call `op()` again.
What `Fixed = TRUE` buys is not the absence of a fairness requirement but a weaker one: `attempts` strictly increases on every step while `status = "running"`, so the "running" region of the state graph is acyclic (a DAG), and weak fairness (`WF_Vars(Step)`) is enough to guarantee termination - there is no cycle for the search to get stuck looping around, so it does not need the stronger guarantee strong fairness would provide.
`WF_Vars(Step)` is also what the *buggy* model needs to rule out that same stuttering-forever behavior, leaving the two-state alternation as the only genuine liveness counterexample.
`RetryGuardFixed.cfg` and `RetryGuardFixedSmallBudget.cfg` need no `VIEW` at all: once `Fixed = TRUE`, `attempts` is bounded by `MaxAttempts` (the guard freezes it), so the full state space is already finite and exhaustive search terminates on its own.

**`mutants/RetryGuardTerminationMutant.cfg` also needs the view, and needs it to still be sound.**
This mutant sets `Fixed = TRUE` *and* `MutantNoIncrement = TRUE`, so `NextAttempts` never actually increments `attempts` - the guard's third disjunct is syntactically present but can never fire, because `attempts` is stuck at `0` forever.
Naively, dropping `attempts` from the view is *unsound* whenever `Fixed = TRUE`, because the guard reads `attempts` (see the note above) - but here `attempts` is not merely bounded, it is *constant*, so it trivially cannot distinguish any two states or affect any transition, and the view is sound again by a different, degenerate argument.
This is why `Termination`'s vacuity mutant has to be its own spec-level constant (`MutantNoIncrement`) rather than reusing `MutantAttemptsStrict`: `MutantAttemptsStrict` still lets `attempts` grow and still lets the guard fire eventually (just one step later than it should), so it does not break termination at all - it is the right mutant for `BoundedAttempts`'s off-by-one and the wrong mutant for `Termination`'s "does the loop ever stop" question, which needs the increment itself disabled.

### Vacuity checks

Every property gets its own falsifiability mutant and its own reachability sanity check, at the same constants as the run it is vacuity-checked against (see [references/pitfalls.md](../../references/pitfalls.md) section 1); the fixed model is checked at two bound sets, and each passing run has a sanity cfg at its own constants.

1. **`BoundedAttempts`** (falsifiability): `mutants/RetryGuardFixedMutant.cfg` is the fixed model at `MaxErrors=2, MaxTimeouts=2` (a different bound set than the buggy run's `3, 3`) with `MutantAttemptsStrict = TRUE`, so `AttemptsGuard(a)` reads `a > MaxAttempts` instead of `a >= MaxAttempts`.
   Structurally this is still "the fixed model" - the third disjunct of `GiveUpCond` is syntactically present and still uses the derived `MaxAttempts` - but the guard is one step too permissive, so the state where `attempts = MaxAttempts` is allowed to continue for one more call, `attempts` reaches `MaxAttempts + 1`, and `BoundedAttempts` fails.
   This demonstrates that `RetryGuardFixed.cfg`'s (and `RetryGuardFixedSmallBudget.cfg`'s) pass is not vacuous: it depends on the guard's exact `>=` direction, not merely on `Fixed` being `TRUE` or on `MaxAttempts` being present.
2. **`Termination`** (falsifiability): `mutants/RetryGuardTerminationMutant.cfg` is `RetryGuardFixed.cfg`'s constants with `MutantNoIncrement = TRUE` added - the fix's own `attempts` increment is skipped, a different mechanism than turning `Fixed` back off, and `Termination` fails again for the reason explained above.
3. **Both properties, both bound sets** (reachability): `sanity/ReachesGaveUp.cfg` checks `INVARIANT SanityReachesGaveUp == status # "gave_up"` at the buggy run's constants; `sanity/ReachesGaveUpFixed.cfg` and `sanity/ReachesGaveUpFixedSmall.cfg` check the same invariant at `RetryGuardFixed.cfg`'s and `RetryGuardFixedSmallBudget.cfg`'s constants respectively.
   All three are violated, confirming the `"gave_up"` state both `BoundedAttempts` and `Termination` care about is actually reachable at every bound set a passing run is reported at, not just the buggy one.

## The Lean model

`verification/lean/RetryGuard/Model.lean` mirrors the TLA+ model: `Outcome` (success/transient/timeout), `Status` (running/gaveUp/succeeded), `State` (errors, timeouts, attempts, status), a `Cfg` structure (`maxErrors`, `maxTimeouts`, `fixed : Bool`) selecting the buggy/fixed variant exactly like TLA+'s `Fixed` constant does, with `Cfg.maxAttempts` **derived** as `cfg.maxErrors + cfg.maxTimeouts` (a `def`, not a stored field - the same derivation `fix.patch` performs, and the same reason `MaxAttempts` is a derived operator rather than a `CONSTANT` in the TLA+ spec), `step cfg s o`, and `run cfg s os` (folds `step` over a `List Outcome`, a no-op once `s.done`).

Two families of theorems are proved, all fully quantified (no `sorry`, no extra axioms beyond the three permitted ones):

- **`fixed_terminates`** and **`fixed_terminates_small`**: for every `os : List Outcome` with `os.length >= cfg.maxAttempts`, `run cfg State.init os` is done, for `cfg = defaultCfg` (`maxErrors=3, maxTimeouts=3`, so `maxAttempts=6`) and `cfg = smallCfg` (`maxErrors=2, maxTimeouts=2`, so `maxAttempts=4`) respectively - i.e. *for every possible sequence of environment outcomes*, `maxAttempts` calls are always enough, at both bound sets, mirroring the TLA+ model's two passing cfgs.
  Both are proved via one general helper, `bounded_termination_from`, quantified over *any* `cfg` with `cfg.fixed = true` and its **derived** `cfg.maxAttempts`, not just the two concrete constants used here - the same argument used for the TLA+ model (attempts strictly increases each step while running, so the "running" region terminates) is what the induction formalizes, for any `maxErrors`/`maxTimeouts` a caller picks.
- **`buggy_not_bounded`**: the same bounded-termination statement is **false** for `buggyCfg` (`fixed := false`) - proved by exhibiting `altList`, the length-6 alternating `[transient, timeout, transient, timeout, transient, timeout]` list (length `6 = buggyCfg.maxAttempts`), and checking by `decide` that `run buggyCfg State.init altList` is *not* done.
  This is the Lean vacuity check: it proves the counterexample itself is a genuine counterexample, not just an assumption.

`verification/lean/results.json` (regenerated by `verification/rerun.sh` and by `run_example.sh`'s self-check, both via `scripts/lean_audit.sh verification/lean --out verification/lean/results.json`) reports `"result": "proved"` with `"forbidden_hits": []`; every theorem-like declaration passes the kernel re-check, within `{propext, Classical.choice, Quot.sound}`.

## Correspondence

See `verification/models/retry-guard/CORRESPONDENCE.md` for the full variable/action -> `retry.py:line` table, the fix-flag table (what `Fixed` models, and the exact code change `plans/001-add-attempt-budget.md` prescribes), and the list of abstractions.

## The repro test

`verification/repro/repro_retry_guard_reset.py` has three tests:

- `test_alternating_failures_cause_runaway_retry`: drives `call_with_retry` with an `op` that alternates `TransientError`/`TimeoutError` forever.
  `op` itself asserts the violated property on every call - `calls <= max_errors + max_timeouts` (`BoundedAttempts`, stated directly on the observable call count) - with a failure message naming `BoundedAttempts`; this doubles as the safety cap, since it fails fast (by the 7th call on the buggy code) instead of looping to some arbitrary large number.
  On the buggy `retry.py` this test **fails** with that `AssertionError`, for exactly the reason the TLA+/Lean counterexamples predict.
  On the fixed `retry.py` it passes (gives up at exactly `max_errors + max_timeouts = 6` calls).
  This is `verification/findings/001-alternating-failures-runaway-retry.md`'s repro test.
- `test_gives_up_after_max_errors_consecutive_errors`: the finding's **guard test** - drives only `TransientStep` repeatedly (never alternating) and passes on both the buggy and fixed code, showing the harness can pass and the failing test above is about the code, not the harness.
- `test_success_after_one_error`: a third ordinary test that also passes on both versions.

The file lives under `verification/repro/`, named so pytest's default discovery (`test_*.py` / `*_test.py`) never collects it; `run_example.sh`'s `collect_command` check confirms this.

## The fix

`fix.patch` (apply with `patch -p1 < fix.patch` or `git apply fix.patch` from this directory) adds a `max_attempts=None` parameter and, when the caller does not pass one, derives it as `max_errors + max_timeouts`; it also adds an `attempts` counter that is incremented every call and never reset, and adds `attempts >= max_attempts` to the give-up condition.
Deriving the budget, rather than hard-coding a default like `max_attempts=5`, is what makes `BoundedAttempts` hold for *every* `max_errors`/`max_timeouts` a caller passes, not just the ones a cfg happens to check: with a hard-coded `max_attempts=5` and, say, `max_errors=max_timeouts=2` (`MaxErrors + MaxTimeouts = 4`), TLC finds `attempts` reaching `5 > 4` before either per-kind limit fires - `BoundedAttempts` would be false at that bound set even though the fix "looks" applied.
The derived version is checked at exactly that second bound set (`RetryGuardFixedSmallBudget.cfg`, `MaxErrors=2, MaxTimeouts=2`, and Lean's `fixed_terminates_small`) and passes, and was also confirmed by hand: applying `fix.patch` and calling `call_with_retry(op, max_errors=2, max_timeouts=2)` against an alternating `op` gives up after exactly 4 calls, never more.
The vacuity/mutant check (`mutants/RetryGuardFixedMutant.cfg`, the fix's own guard weakened from `>=` to `>`) confirms the guard's direction is load-bearing, not just its presence.
`plans/001-add-attempt-budget.md` is the fix plan an executor with no other context could follow to land exactly this change; its done criteria include `Plan001.cfg` passing.

## Output layout

This directory follows the skill's own [output layout](../../SKILL.md#output-layout):

```
retry-guard-reset/
  README.md, retry.py, fix.patch
  regenerate.sh              maintainer tool: rebuilds every committed result deterministically
  verification/
    README.md, findings.json, rerun.sh
    models/retry-guard/          RetryGuard.tla, the cfgs above, sanity/, mutants/, results/, CORRESPONDENCE.md
    lean/                        the lake project audited above
    findings/001-alternating-failures-runaway-retry.md
    repro/repro_retry_guard_reset.py
  plans/
    README.md, 001-add-attempt-budget.md
  run_example.sh
```

## Running everything

```
examples/retry-guard-reset/run_example.sh
```

runs the full chain: toolchain check, every TLC cfg (buggy safety, buggy liveness, fixed at two bound sets, the per-plan cfg, both vacuity mutants, all three sanity checks), the repro test on both buggy and fixed code, `lean_audit.sh`, and `rerun.sh` (regenerating every result JSON, and the Lean audit, from the specs on disk) - before finally copying the *committed* example into a fresh temp git repository and running `scripts/check_run.py` against it - the same validator a real run must satisfy - and printing a summary table.
It exits 0 only if every row passes.

`run_example.sh` never writes into this directory: the `rerun.sh` self-check and the Lean audit each run inside a scratch copy under `${TMPDIR:-/tmp}` (via `cp -Rp`, never `touch`), and the `check-run` step validates the results already committed here, not a freshly regenerated set.
Running `run_example.sh` twice in a row leaves every file under this directory byte-identical.
The one script that *does* write here is `regenerate.sh` - a separate, maintainer-only tool (run it after editing any `.tla`, `.cfg`, or `.lean` file, before committing) that rebuilds `verification/models/*/results/*.json`, the matching `.log` files, and `verification/lean/results.json` deterministically (`--workers 1`), the same way, in the same kind of scratch copy, then rewrites each result's `spec`/`cfg`/`raw_log`/`command` fields (and each `.log`'s embedded path) to be relative to this repository instead of naming the machine `regenerate.sh` happened to run on.
