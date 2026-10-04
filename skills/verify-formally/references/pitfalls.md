# Pitfalls and the Vacuity Check

Formal methods fail quietly.
A wrong model does not crash; it passes.
This file lists how results become meaningless and the mandatory checks that catch it.

---

## 1. The vacuity check (mandatory for every property)

A property that cannot fail proves nothing.
Before reporting any result for a property, do all three:

1. **Falsifiability (mutant must fail).**
   Make the smallest change to the model that a real bug of this kind would make, and confirm the checker reports a violation of this property:
   - remove or weaken the guard the property depends on (`<=` to `<`, drop a clause of a condition);
   - drop a reset, an append, a cleanup, or an unlock;
   - flip the fix flag off (if the fixed model passes, the buggy model must fail);
   - add a transition the code does not have (skip the finally block, return early).
   Put mutants in `mutants/` (a cfg that flips a constant, run against the parent spec; or a copied spec with exactly one change when a constant cannot express it) and record each mutant and its result.
   For a TLA+ property that already fails on the buggy model, the buggy run is the falsifiability evidence; the fixed model then needs its own mutant, and that mutant must break the property by a **different mechanism** than turning the fix flag off: weaken one part of the fix (`>` instead of `>=` in the fix's guard, its new append skipped on one path, its check split so it is no longer atomic).
   Re-enabling the known bug only shows what the buggy run already showed.
   For a property that only exists in the fixed code (it talks about behavior the fix adds), the mutant breaks one element of the fix itself (the new record is written with the wrong value, the new check is skipped on one path).
2. **Reachability (the model does what you think).**
   Every action fires at least once, and the states the property talks about are reachable.
   Check with sanity invariants that must be violated: `SanityReachesFinish == pc # "finish_finally"`, `SanityTwoJobs == ~(\E b \in 1..Len(batches): batches[b].n = 2)`.
   A property over states that never occur is trivially true.
   Run each sanity invariant in its own cfg under `sanity/` (TLC stops at the first violation, so sharing a cfg hides the others), with the same constants as the property it supports; reachability at larger bounds says nothing about the bounds you report.
3. **Non-trivial state space.**
   `distinct_states` greater than the number of `pc` values, and the depth greater than one pass through the loop.
   `run_tlc.sh` reports `vacuous` for zero states and for a cfg that checks nothing (no invariant, no property, deadlock checking off).
   Under a `VIEW`, count view states instead; a handful is normal for a small loop, so rely on the mutant and sanity checks there.
4. **Independent ground truth.**
   A truthfulness property compares what the code records with what really happened.
   If the same assignment sets both, the property cannot fail no matter how the code behaves, and a hand-written mutant that breaks the recording step may still "fail" it for the wrong reason.
   Check in the spec that ground truth is set where the event happens and the recorded value where the code records it.

For Lean, the equivalent is: prove the property fails for the buggy configuration or a concrete input (`¬ ∀ ...` with a witness), and instantiate every theorem's hypotheses with a concrete example.

Record the vacuity result per property and per bound set: `passed` (mutant failed, sanity invariants violated) or `failed` (the property is vacuous; fix it before reporting anything).
A property can be vacuous at one bound set and not at another (with one worker a race cannot occur); claim results only at the bound sets where vacuity passed.

## 2. Modeling pitfalls

- **Too-small bounds.**
  Bugs at a limit need bounds at and beyond the limit.
  A budget of 3 checked with a bound of 2 never reaches the interesting edge.
  Two workers find races that one worker cannot; three workers are rarely needed for a first pass.
- **Missing or wrong fairness.**
  Liveness without `WF` fails trivially by stuttering (a violation that is not a bug).
  Fairness on environment choices (for example, "the model eventually returns a valid reply") hides real non-termination; only the program's own steps get fairness unless the real system guarantees otherwise, and then say so.
- **Liveness with CONSTRAINT or SYMMETRY.**
  Both make liveness checking unsound; TLC warns (message 2284) and `run_tlc.sh` returns `pass_with_warnings`.
- **Stuttering mutants.**
  A liveness mutant that loops through an `UNCHANGED` self-loop is a stuttering step; fairness steps over it and the mutant passes.
  The mutant cycle must change some variable.
- **Over-abstraction.**
  Merging program points into one action hides interleavings between them.
  Symptoms: the model passes, but a hand-traced path through the code breaks the property.
  Fix: split actions at every interference point (see the TLA+ playbook).
- **Under-approximated environment.**
  Forgetting a behavior (a malformed reply, an empty queue, a timeout, a second signal) removes every trace through it.
  List the environment's behaviors in `CORRESPONDENCE.md` and check each has a disjunct.
- **Modeling the intended code instead of the actual code.**
  Read the guard; do not remember it.
  Paraphrase is where off-by-ones disappear.
- **Wrong exception semantics.**
  `except Exception` does not catch `KeyboardInterrupt`; a raise in `finally` replaces the in-flight exception; `return` in `finally` swallows it.
- **Type invariant masking.**
  A too-strict `TypeOK` can make TLC report a type violation before the interesting property, or an over-constrained `Init` can exclude the bad states entirely.
- **VIEW misuse.**
  Dropping a variable that a guard reads merges states that behave differently and can hide or fabricate cycles.
- **Property states what the code does, not what it should do.**
  A property written by reading the code confirms the code.
  Derive properties from docs, API contracts, and user expectations first.

## 3. Checker and toolchain pitfalls

- A TLC run is a result only when `run_tlc.sh` says `pass`; `pass_with_warnings`, `timeout`, `error`, and `vacuous` are not results.
- A result JSON you wrote or edited is not a result; `check_run.py` re-runs every cited pass or violation cfg and replays every other result against the TLC log beside it, which must carry TLC's banner and the spec name, while the result's `command` must name the same spec and cfg as its `spec` and `cfg` fields.
- A cfg that cannot re-check within min(1800s, max(120s, 4 x its original elapsed time x workers)) fails the run; keep models small enough.
  A command without a numeric `--workers` (the default, auto) counts as the machine's core count.
- A target is modeled only when a TLA+ property has a `violated` or `no_violation_within_bounds` result, or a Lean property is `proved` (or `violated` by a proved negation theorem listed in `verification/lean/results.json`); a spec that never produced a checked property is an attempt, so list it as `not_modeled`.
- TLC's temporal-violation message does not name the property: check one `PROPERTY` per run when attribution matters.
- Do not pass `-continue`, `-simulate`, or `-generate`; `run_tlc.sh` rejects them because they make runs non-exhaustive or keep going past violations.
- `lake build` succeeds with `sorry`; only `lean_audit.sh` decides "proved".
- A Lean theorem under the same name but a weaker statement is not the same result; never weaken silently.

## 4. Reproduction pitfalls

- **Counting red instead of reading it.**
  A test can fail for a reason unrelated to the trace (import error, wrong fixture, harness bug).
  Read the assertion and the traceback; CONFIRMED means "fails for the predicted reason".
- **Test edits the code under test.**
  Patching the function whose behavior is in question, instead of the environment around it, proves nothing.
  Patch only environment seams: external service replies, user input, clock, subprocess, network, logging and display.
  Never patch a method of the class under test, even to inject an interrupt "at that point"; find the environment call that runs at that point instead.
- **Directory runs collect nothing.**
  `pytest verification/repro/` collects zero tests (the files are named outside default discovery) and reports "no tests ran", which is easy to misread as green.
  Always pass explicit file paths.
- **Injected faults that repeat.**
  An interrupt or error injected on every call re-triggers on later steps and produces a different failure; make injections one-shot and state which call they hit.
- **Repro collected by the real suite.**
  A failing test under a default-discovered name turns the user's suite red; use the naming convention and confirm with the collector.
- **No guard test.**
  Without a passing test on a non-violating trace, a failing test may just mean the harness cannot pass.

## 5. Reporting pitfalls

- "Verified" without bounds.
  Always "no violation within <constants> (<N> distinct states)".
- Reporting a MODEL-ONLY trace as a bug.
- Reporting a property whose vacuity check failed or was skipped.
- Reporting results from an older version of a spec.
  After any spec edit (a new constant, a new action), re-run every cfg with `verification/rerun.sh`; a cfg that does not assign a new constant fails, and one that does may now mean something different.
- Approving a fix plan on a fixed model that models an idealized fix rather than the planned code change.
- Counting an `inferred` property's violation as a bug; it is NEEDS-DECISION until intent is established.
- Claiming a Lean theorem proved without the audit's `proved`.
- Hiding what was not modeled.
  The coverage-gap list is part of the result.
