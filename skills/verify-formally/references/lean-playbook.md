# Lean 4 Playbook

How to model a piece of code as a Lean 4 step function, state the property as a theorem, prove it (or prove it fails), and audit the proof.
Pinned toolchain: Lean `leanprover/lean4:v4.34.0`, core only (no Mathlib), so builds take seconds and nothing downloads.
A property counts as proved only when `scripts/lean_audit.sh` reports it `proved`; `unproved` and `kernel_unverified` (the kernel replay rejected the module) are both not proved.

---

## 1. When Lean fits

- A pure or nearly pure function whose correctness is a statement over all inputs: a parser, a validator, a counter or budget update, a message-list transformation, a padding function.
- A small state machine where you want an **unbounded** guarantee (all list lengths, all limits), not just "no violation within bounds".
- The data part of a target whose concurrency shell is modeled in TLA+.

Lean does not fit timing, interleavings, or anything where the question is "which order can these happen in"; use TLA+ there.

## 2. Project layout

One lake project for all Lean models, at `verification/lean/`:

```
verification/lean/
  lean-toolchain            leanprover/lean4:v4.34.0
  lakefile.toml
  Verification.lean         imports every model module
  Verification/<Target>.lean
```

`lakefile.toml`:

```toml
name = "verification"
defaultTargets = ["Verification"]

[[lean_lib]]
name = "Verification"
```

Build with `lake build` inside `verification/lean/` (use the `lake` path `check_toolchain.sh` printed; `~/.elan/bin` is often not on `PATH`), audit with `scripts/lean_audit.sh verification/lean --out verification/lean/results.json --quiet`.
The audit's `theorems` list includes compiler-generated lemmas; `user_theorems` lists the ones declared with `theorem`/`lemma` in your source, which is what the report names.

## 3. Modeling code as types and step functions

Mirror the code's data and control flow, one-to-one where possible, so `CORRESPONDENCE.md` is easy to write:

```lean
namespace Verification.RetryGuard

inductive Outcome where
  | success | transient | timeout
deriving DecidableEq, Repr

structure State where
  errors : Nat
  timeouts : Nat
  attempts : Nat
  done : Bool
deriving DecidableEq, Repr

structure Cfg where
  maxErrors : Nat
  maxTimeouts : Nat
  fixed : Bool

def step (cfg : Cfg) (s : State) (o : Outcome) : State := ...

def run (cfg : Cfg) : State → List Outcome → State
  | s, [] => s
  | s, o :: os => run cfg (step cfg s o) os

end Verification.RetryGuard
```

- Environment behavior is an input list (`List Outcome`); quantifying over all lists is quantifying over all environments.
- Exceptions become sum types (`Except E α`) or an explicit status field.
- Mutable fields become structure fields; `{ s with f := v }` is assignment.
- Keep the same `fixed : Bool` flag idea as in TLA+: buggy and fixed behavior in one function, selected by the config.
- Prefer `Nat` and `List`; avoid `Float`, `IO`, and `partial def` (a `partial def` cannot be reasoned about).
  If the real loop is unbounded, model it with fuel (`run : Nat → State → State`) or with the environment list as above.

## 4. Stating theorems

State the property the way the code's users care about, quantified over everything the environment controls:

```lean
theorem fixed_terminates (os : List Outcome) (h : os.length ≥ cfg.maxAttempts) :
    (run cfg State.init os).done = true := ...
```

For a suspected bug, prove the **negation** for the buggy configuration with a concrete witness.
This turns the counterexample itself into a checked artifact and is the Lean vacuity check:

```lean
theorem buggy_not_bounded :
    ¬ (∀ os : List Outcome, os.length ≥ 5 → (run buggyCfg State.init os).done = true) := by
  intro h
  have := h [.transient, .timeout, .transient, .timeout, .transient] (by decide)
  revert this
  decide
```

Hypothesis sanity: for every theorem with hypotheses, add an `example` that instantiates them with concrete values (`example : ∃ os, os.length ≥ 5 := ⟨[...], by decide⟩`).
A theorem with unsatisfiable hypotheses is vacuously true.

When the property holds for the real code (no bug flag to flip), the vacuity check needs a mutant: write a mutated copy of the step function with one realistic change (`<` for `≤`, a dropped reset) and prove the property false for it with a concrete witness.
Keep mutants in their own module (`Verification/<Target>Mutants.lean`) so the real theorems stay readable.

## 5. Proof toolkit (Lean 4.34 core, verified)

Available and useful:
- `simp`, `simp only [...]`, `simp_all`, `simp [f]` to unfold a definition.
- `omega` for linear arithmetic over `Nat`/`Int` (the workhorse for counters and limits).
- `decide` for closed decidable goals (concrete lists and small numbers); `decide +kernel` when elaboration-time evaluation is slow.
- `grind` for goals mixing equalities, arithmetic, and case splits; try it before writing a long manual proof.
- `induction xs generalizing s with | nil => ... | cons x xs ih => ...`, and `fun_induction f x` to induct following a function's own recursion.
- `cases h : e with ...`, `split` (on `if`/`match` in the goal), `by_cases h : p`.
- `obtain ⟨a, b⟩ := h`, `rcases`, `constructor`, `exact`, `refine`, `intro`, `revert`, `unfold f`, `rw [h]`, `show`, `have`.

Not available without Mathlib or Batteries: `by_contra` (use `Classical.byContradiction fun h => ...` or restate), `split_ifs` (use `split` or `by_cases`), `set`, `linarith`/`nlinarith` (use `omega` for linear goals), `norm_num`, `ring`, `positivity`, `aesop`.

Gotchas seen in practice (Lean 4.34):
- `decide` fails with "failed to synthesize Decidable" when a proposition is a `def P : Prop`; make it an `abbrev` (or add a `Decidable` instance) so instance search can unfold it.
- `omega` treats a structure projection such as `{ f := x }.f` as an opaque atom; `simp only` it away first.
- `split` handles `if a && b then ...` on `Bool` poorly; `by_cases h : a = true` (and on `b`) first, or `cases a <;> cases b`.
- `if_true`/`if_false` in simp sets give deprecation warnings; use `ite_true`/`ite_false`, but check the goal after `simp only [h, ite_true]`: `simp` may also rewrite the hypothesis `h` into other terms.
  When it does, `split` or `by_cases` on the condition is more predictable.

Proof patterns:
- Termination-within-N-steps: prove an invariant "if not done, then attempts < max" for one step, then induct over the list generalizing the state, with a measure hypothesis `max ≤ s.attempts + os.length`.
- Sticky terminal state: prove `run cfg s os = s` when `s.done`, by induction; nearly every termination proof needs it.
- When `simp` loops or stalls, switch to `simp only [specific lemmas]`.
- When a proof gets long, extract the per-step fact as its own theorem; the audit checks all of them.

## 6. Forbidden and trust-escalating constructs

The audit fails the project if any theorem depends on `sorryAx` or a non-standard axiom, or if the source contains `sorry`, `admit`, `axiom`, `native_decide` or `decide +native`, `@[implemented_by]`, `@[extern]`, or `unsafe`.
Allowed axioms: `propext`, `Classical.choice`, `Quot.sound`.
`native_decide` trusts the compiler; it is allowed only with `--allow-native-decide`, and the report must say so.
If a proof will not close, leave it as an honest failure: report the theorem as unproved with the remaining goal, not a weaker theorem under the same name.

## 7. Reading failures

- `unsolved goals` shows the remaining goal; add `trace_state` or split the proof with `have` to see which step fails.
- `declaration uses 'sorry'` is only a warning to `lake build`; the audit catches it.
- `maximum recursion depth` in `decide`: the concrete instance is too large; use smaller constants or a structural proof.
- A theorem you believe true that will not prove may be false: evaluate `#eval run cfg State.init [...]` on candidate counterexamples before spending more time.
  `#eval` on small inputs is also the fastest way to validate the model against the real code's behavior on the same inputs.
