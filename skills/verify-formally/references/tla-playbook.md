# TLA+ Playbook

How to turn a piece of control logic into a TLA+ spec that TLC can check, and how to read the result.
Pinned toolchain: TLA+ tools v1.7.4 (`tla2tools.jar`), Java 17+.
Always run TLC through `scripts/run_tlc.sh`; it rejects non-exhaustive modes and classifies results so a broken run never reads as a pass.

---

## 1. Shape of a spec

```tla
---- MODULE WorkerLoop ----
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS MaxBatch, MaxSignals, FixRelease     \* bounds first, then one flag per suspected defect

VARIABLES pc, exc, leased, nb                    \* program counter, in-flight exception, observable state, counters
Vars == <<pc, exc, leased, nb>>

TypeOK == ...
Init == ...
ActionA == /\ pc = "a" /\ ... /\ UNCHANGED <<...>>
Next == ActionA \/ ActionB \/ ... \/ Done
Spec == Init /\ [][Next]_Vars /\ WF_Vars(Step)   \* fairness only on the program's own steps

Prop1 == ...                                     \* safety invariants
Termination == <>(pc \in {"done", "crashed"})    \* liveness, checked under the fairness above
====
```

Rules that prevent most first-run errors:
- Every action is `guard /\ primed assignments /\ UNCHANGED <<everything else>>`.
  In every disjunct of an action, assign exactly the same set of variables; TLC reports "successor state is not completely specified" otherwise.
- Keep `Vars` complete; a variable missing from `Vars` makes fairness and stuttering behave strangely.
- Put a `\* file.py:NN-MM` comment on every action.
  This is one of the few comments that earns its place: it is the correspondence, inline.
- Comments in TLA+ are `\*` (line) and `(* *)` (block).
  Strings use double quotes.
  `#` is "not equal", `=>` is implication, `\in`, `\A`, `\E`, `EXCEPT`, `IF THEN ELSE`, `LET IN`.
- `Done == pc \in {"done", "crashed"} /\ UNCHANGED Vars` avoids a deadlock report at terminal states; alternatively run with deadlock checking off, but then a real deadlock is missed, so prefer the explicit `Done`.
- A target that only needs safety needs no fairness and no `Termination`: `Spec == Init /\ [][Next]_Vars`.
  Add fairness and liveness properties only when the target has a "must eventually" requirement.

## 2. Encoding program control flow

**Program counter per program point.**
Name `pc` values after real program points (`"lease_batch"`, `"finish_finally"`, `"loop_catch"`), each mapped to `file:line` in `CORRESPONDENCE.md`.
The counterexample trace then reads as a debugging script, and the test's injection points are already decided.

**Granularity: one action per point where the environment can interfere.**
An action is atomic in TLA+.
Split the code into actions exactly at the points where something outside the function can happen: an exception can be raised, a signal can arrive, another thread can run, an `await` yields, a callback fires.
Code between two such points can be one action.
If you merge two steps that the environment can separate, you will miss every bug in the gap.
Two interference points may share one action only when every environment choice at either point leads to the same successor states; say so in `CORRESPONDENCE.md`.

**Exceptions as `(exc, pc')`.**
Keep a variable `exc` for the exception in flight.
A raise sets `exc' = "Kind"` and `pc'` to the handler the language would unwind to (the enclosing `finally`, the `except` that matches, or `"crashed"` if nothing catches it).
A `finally` block is an ordinary action that runs with `exc` still set, then continues to the next handler if `exc # "none"`.
A raise inside `finally` overwrites `exc`, which matches Python and JavaScript semantics.
Model the exception class hierarchy explicitly where it matters (a subclass is caught by its parent's handler, so a handler for the parent that re-prompts or retries also catches the subclass; `KeyboardInterrupt` is not an `Exception` in Python, so `except Exception` does not catch it).

**Asynchronous interrupts.**
For `KeyboardInterrupt`, `CancelledError`, abort signals, or timeouts that can fire at any point, add an interrupt disjunct to every action that corresponds to an interruptible point, guarded by a budget (`nint < MaxInterrupts`) so the state space stays finite.
Pay special attention to the points "after the append, before the return" and "after the print, before the append": that is where paired state gets separated.

**Environment choices as disjunctions.**
Model replies, user input, subprocess outcomes, network failures: each is a disjunct (or `\E x \in S:`) inside the action where the code receives it.
Cover every behavior the real environment can produce (over-approximate); a spurious trace is cheap to rule out later, a missing behavior hides bugs silently.

**Data abstraction.**
Keep only what the property needs: kinds instead of contents, counts instead of lists, a boolean instead of a string.
Keep **ground truth** even when the code does not store it (did the command actually run?), because truthfulness properties need it.
Ground truth must be assigned by the action where the real-world event happens (the command runs), and the recorded value by the action where the code records it (the result is appended or padded).
If one assignment sets both, the truthfulness property holds by construction and proves nothing.

**Threads.**
Use a set of process ids and a function `pc[p]`: `\E p \in Procs: Step(p)`.
Split each thread's code at every shared-state access (read and write are separate actions unless a lock makes them atomic).
Model a lock as a variable holding the owner or `NoOwner`, with acquire enabled only when free.
For `asyncio` or JS event loops, a single thread with interleaving only at `await` points: one action per segment between awaits.

**Loops and counters.**
Unbounded counters make the state space infinite.
Options: bound them with the constants (`nb \in 0..MaxBatch`), use a `CONSTRAINT` to cut exploration for safety checks (never for liveness; see below), drop a monotonic counter from state identity with a `VIEW` when it does not affect enabledness or successors, or saturate it (`n' = IF n < Limit THEN n + 1 ELSE Limit`), which is exact when the only reader is a guard of the form `Limit <= n`.

**One flag per suspected defect.**
`CONSTANT FixReleaseOnSignal` selects the corrected behavior in the one action it affects.
The buggy model, the fixed model, and the vacuity mutants are then the same spec with different cfgs, and the fixed model doubles as the fix plan's acceptance check.
The flag must model the exact code change the plan will prescribe, at the same granularity as the rest of the model.
If the change moves an append before a print, the fixed branch has an interrupt point between the new append and the print, just like the code will; an idealized fix ("results are never lost") passes and proves nothing.
When several fixes are plausible, give each its own flag and let TLC show which ones actually close every window.

**Sibling configurations.**
If the code has a simpler sibling (a base class the property should hold for), model it in the same spec behind a constant.
Its passing run is cheap evidence that the invariant is satisfiable and not just too strict.

## 3. The cfg file

```
SPECIFICATION Spec
INVARIANT TypeOK
INVARIANT LeasesSettled
PROPERTY Termination
CONSTANTS
  MaxBatch = 2
  MaxSignals = 2
  FixRelease = FALSE
```

- Use one cfg per question: `Spec.cfg` (buggy, safety), `SpecLiveness.cfg`, `SpecFixed.cfg` (all fix flags, all properties, must pass), `Plan001.cfg` (only that plan's fix flags, must pass the properties it claims to fix), `mutants/*.cfg` (must fail), `sanity/*.cfg` (must be violated).
- A cfg in `mutants/` or `sanity/` is run against the parent spec: `run_tlc.sh Spec.tla sanity/ReachesExec.cfg --out results/sanity-ReachesExec.json`.
  Copy the spec into `mutants/` only when a constant cannot express the mutation, and then change exactly one thing.
- Put each sanity invariant in its own cfg, with the same constants as the property it supports.
  When a property is reported at several bound sets that only enlarge the same constants, the sanity cfgs are needed at the smallest one: anything reachable there stays reachable at larger bounds.
  TLC stops at the first violation, so a sanity invariant sharing a cfg with real invariants hides whether the others were checked.
- When the buggy model has several independent defects, the all-buggy cfg always stops at the shortest trace; to see each defect's own trace, turn on the fix flags of all the others.
- Check one `PROPERTY` per run when you need to know which one failed; TLC's temporal-violation message does not name the property.
- `CONSTRAINT` limits exploration for safety checks only.
- `VIEW` changes state identity; see the soundness condition below.
- `SYMMETRY` is incompatible with sound liveness checking; avoid it unless the state space forces it, and then only for safety.

`verification/rerun.sh` re-runs everything; generate it from the cfgs on disk instead of writing each line by hand:

```bash
#!/usr/bin/env bash
set -uo pipefail
skill=<absolute path to the skill>
cd "$(dirname "$0")/.."
status=0
for spec in verification/models/*/*.tla; do
  dir=$(dirname "$spec")
  for cfg in "$dir"/*.cfg "$dir"/sanity/*.cfg "$dir"/mutants/*.cfg; do
    [ -f "$cfg" ] || continue
    stem=$(basename "$cfg" .cfg); sub=$(basename "$(dirname "$cfg")")
    case "$sub" in sanity) out="$dir/results/sanity-$stem.json" ;; mutants) out="$dir/results/mutant-$stem.json" ;; *) out="$dir/results/$stem.json" ;; esac
    "$skill/scripts/run_tlc.sh" "$spec" "$cfg" --out "$out" --workers 1 --quiet || status=1
  done
done
[ -d verification/lean ] && { "$skill/scripts/lean_audit.sh" verification/lean --out verification/lean/results.json --quiet || status=1; }
exit $status
```

A nonzero exit is expected (buggy cfgs, mutants, and sanity cfgs are supposed to fail); compare each result with the expectation recorded in `findings.json`.
A mutant `.tla` in `mutants/` is a separate spec; give it its own directory or adapt the loop.

## 4. Bounds

- Start small (2 processes, 2-3 iterations, budgets of 2-3) to debug the spec quickly, then grow until the state count is in the hundreds of thousands to low millions or the run takes a few minutes.
- Pick bounds that exceed the "interesting" depth: if the property involves a limit of 3, bounds of 3 and 4 both matter (off-by-one lives at the boundary).
- Report bounds and distinct-state counts with every result.
  In `deep` mode, run a second, larger bound set and report both; a result that holds at two bound sets is more credible than one.
- If TLC runs for more than 10 minutes, the model is too detailed; abstract data rather than lowering the control bounds below the interesting depth.

## 5. Liveness

- State fairness explicitly: `WF_Vars(Step)` means "if the program can keep taking a step, it eventually does".
  Put fairness on the program's actions, not on the environment's choices; the environment is adversarial.
- Use `SF` only when an action is enabled intermittently and the real system guarantees it will run (rare in the code you model).
- Liveness with a `CONSTRAINT` is unsound (TLC warns with message 2284 and `run_tlc.sh` reports `pass_with_warnings`).
  Bound the state space with constants or a `VIEW` instead.
- **VIEW soundness condition:** dropping a variable from the view is sound only when that variable never influences which actions are enabled or what their successors are (the view is a bisimulation).
  A pure step counter that no guard reads qualifies; a counter compared against a limit does not.
- A liveness counterexample is a lasso: a prefix plus a loop (`loop_to` in the JSON).
  With a `VIEW`, the loop is a cycle in the view, not in the full state.
  When the behavior instead ends by stuttering forever, the JSON has `loop_to: null` and a final trace entry whose `action` is `Stuttering`; read it with the blocking-on-input rule below.
- Termination properties usually want `<>(pc \in Terminal)`; progress properties want `[](P => <>Q)` (`P ~> Q`).
- Fairness at a branch point where the environment chooses: write the program step and the environment's options as separate actions, and put `WF` on the program step only.
  `WF_Vars(Next)` also makes optional environment behavior (an injected fault, a Ctrl-C) happen eventually, which is wrong.
  If termination needs the environment to cooperate (the LM eventually replies), put that fairness on its own action and state the assumption in the property's description.
- A program blocked waiting for input has no enabled program action, so TLC reports the violation as the behavior stuttering there forever; that is a real hang, not a missing-fairness artifact, when the waiting state is reachable and no program action is enabled in it.
  If a program action is enabled in the stuttering state, the violation is a missing-fairness artifact.
- With counters bounded as guards (`nb < MaxBatch`), non-termination often appears as a state at the bound with no successor, reported as `deadlock`, rather than as a lasso.
  For a liveness check, run with `-- -deadlock` only after confirming the terminal states have an explicit `Done` action; otherwise the deadlock is the finding.
  Write liveness mutants so the cycle changes some variable (for example, reset a counter), so they show up as a lasso rather than a deadlock.

## 6. Running and reading results

```
scripts/run_tlc.sh verification/models/<t>/Spec.tla verification/models/<t>/Spec.cfg --out verification/models/<t>/results/Spec.json --quiet
```

Name each result after its cfg (`results/<cfg-stem>.json`, with a `sanity-` or `mutant-` prefix for those directories); the JSON records the spec, the cfg, and the re-run `command`.
With `--workers 2` or more, a run that stops at a violation can report a different state count and even a different trace each time; use `--workers 1` for every run whose trace or count you cite (and in `rerun.sh`), and keep more workers for large passing runs.

| `result` | Meaning | What to do |
|---|---|---|
| `pass` | Exhaustive, no violation, no warnings | Report "no violation within <constants>, N distinct states" after the vacuity check |
| `invariant_violation` | A safety invariant failed; `violated` names it; `trace` holds the states | Read the trace, then reproduce |
| `property_violation` | A temporal property failed; `loop_to` marks the lasso | Check fairness first, then reproduce |
| `deadlock` | A reachable state has no successor | Usually a missing `Done` action or a guard that blocks; sometimes a real hang |
| `vacuous` | Zero distinct states (Init is empty), or the cfg checks nothing (no `INVARIANT`, no `PROPERTY`, and deadlock checking off; see `check_deadlock` in the JSON) | Fix the spec or the cfg |
| `pass_with_warnings` | TLC finished but warned (liveness with constraints, symmetry) | Not a pass; remove the cause |
| `error` | Parse, semantic, or evaluation error | Read `error_detail`; the trace is kept when evaluation failed mid-run |
| `timeout` | Killed after `--timeout` seconds | Reduce the model; report as unchecked |

Reading traces:
- TLC explores breadth-first, so the first trace is a shortest one, but not necessarily the most interesting variant.
  Read it, then decide which variant the repro should follow (a variant where the program keeps running after the bad state is usually more convincing than one that ends immediately).
- To see a specific variant, add an invariant that is violated only on that variant, or strengthen a guard to exclude the one you already have.
- Each trace state lists every variable; diff consecutive states to see what each step changed, and read the `action` field to see which action fired.

## 7. Common spec errors

- `Attempted to compare integer with string` or similar: mixed types in one variable; use separate variables or records.
- `successor state is not completely specified`: a disjunct forgot to assign or `UNCHANGED` a variable.
- `The variable x was changed while it is specified as UNCHANGED`: the same variable is both assigned and in `UNCHANGED` within one disjunct.
- Records: `[kind |-> "job", n |-> 1]`; access `r.kind`; update `[r EXCEPT !.n = 2]`.
- Sequences: `<<>>`, `Append(s, x)`, `Len(s)`, `s[i]` (1-indexed), `SubSeq(s, m, n)`; `Last(s) == s[Len(s)]` needs a nonempty guard.
- Functions: `[j \in 1..N |-> 0]`, update `[f EXCEPT ![j] = 1]`.
- A `LET` in an action cannot define primed values to be used by `UNCHANGED`; assign directly.
- **Precedence trap:** `x' = a \/ b` parses as `(x' = a) \/ b`, which leaves `x'` unconstrained whenever `b` holds; the same happens with `/\`.
  Write `x' = (a \/ b)` or use bulleted `/\`/`\/` lists with one conjunct per line.
  Symptoms: spurious deadlocks, mutants that should fail but pass.
