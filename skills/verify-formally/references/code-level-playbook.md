# Code-Level Verification Playbook

TLA+ and Lean check a model you wrote.
A code-level verifier checks the real implementation: it compiles or loads the code under verification, lets a harness choose its inputs nondeterministically, and explores every path within a bound.
That makes it the right lane for properties that live in the code's own semantics (memory safety, overflow, panics, a contract on a real function), where a hand model would have to copy the very detail the bug hides in.

---

## 1. When the code-level lane fits

Use it when the question is "does this function behave for every input", not "can these events happen in a bad order":

- **Memory safety** in C, C++, unsafe Rust: out-of-bounds read or write, null or dangling dereference, use after free, double free, leaks on an error path.
- **Undefined behavior and arithmetic**: signed overflow, unsigned wraparound the code does not expect, shift by too much, division by zero, lossy conversions, pointer arithmetic past an object.
- **Panics and crashes** that callers do not expect: Rust `unwrap`/index panics, Python exceptions out of a function documented as total.
- **Data-structure invariants** over every operation sequence up to a size: a ring buffer's head never passes its capacity, a free list stays acyclic, a sorted container stays sorted.
- **Contracts on real functions**: pre/postconditions a docstring, type, or test states ("returns an index in `0..n`", "round-trips every accepted input").

Prefer TLA+ when the risk is interleaving, interruption, or liveness across components; prefer Lean when you need a guarantee at every size and the step function is small and pure.
Use both lanes on one target when a concurrency shell wraps a memory-unsafe core: TLA+ for the shell, a code-level harness for the core.

## 2. Choose the verifier by language

| Language | Verifier (`--tool`) | Kind | What it checks by default |
|---|---|---|---|
| C, C++ | `cbmc` | bounded model check | array bounds, pointer validity, and assertions; add `--signed-overflow-check --unsigned-overflow-check --pointer-overflow-check --conversion-check --div-by-zero-check --memory-leak-check` as the property needs |
| C, C++ | `esbmc` | bounded model check | bounds, pointers, overflow (`--overflow-check`), memory leaks (`--memory-leak-check`); pass `--unwinding-assertions` with `--unwind` |
| Rust | `kani` | bounded model check | panics, arithmetic overflow, out-of-bounds, UB in `unsafe`, `assert!` |
| Java bytecode | `jbmc` | bounded model check | exceptions, array bounds, assertions |
| Python | `crosshair` | symbolic execution | contracts (PEP 316 docstrings, `assert`-based, icontract, deal) on real functions |
| Python | `hypothesis` | generated inputs | `@given` property tests run by pytest; a test, never a proof |
| Go, TypeScript, others | none supported | | model the logic in TLA+ or Lean instead, and record the code-level gap in `coverage_gaps` |

**Claim checks explicitly.**
A `memory_safety`, `overflow`, or `undefined_behavior` property checked by CBMC, ESBMC, Kani, or JBMC lists the checks it claims in `checks`, and every cited run (including the mutant and sanity runs) passes each claimed check's flag explicitly, even when the verifier's version enables it by default; defaults differ between versions, and `check_run.py` (C17) refuses a claim the command line does not back.

| `checks` entry | CBMC / JBMC flag | ESBMC flag | Kani |
|---|---|---|---|
| `bounds` | `--bounds-check` | on unless disabled | on unless disabled |
| `pointer` | `--pointer-check` | on unless disabled | on unless disabled |
| `signed_overflow` | `--signed-overflow-check` | `--overflow-check` | on unless disabled |
| `unsigned_overflow` | `--unsigned-overflow-check` | `--unsigned-overflow-check` | on unless disabled |
| `pointer_overflow` | `--pointer-overflow-check` | | |
| `conversion` | `--conversion-check` | | |
| `div_by_zero` | `--div-by-zero-check` | on unless disabled | on unless disabled |
| `undefined_shift` | `--undefined-shift-check` | | on unless disabled |
| `memory_leak` | `--memory-leak-check` | `--memory-leak-check` | |

(JBMC claims only `bounds` and `div_by_zero`.)
"On unless disabled" needs no flag: `run_code_check.py` rejects every `--no-*` flag.

Prefer a verifier the repo's developers can install; say why you chose it in the README.
`scripts/check_toolchain.sh --code` reports which code-level verifiers are installed and how to install the rest.
Never install one without the user's say-so when the user is present; if none is available, record the gap and fall back to a model.

**Exhaustive or not.**
A bounded model checker (CBMC, ESBMC, Kani, JBMC) that passes with every unwinding assertion holding covers every path of the harness within its input domain: report `no_violation_within_bounds` with the bounds.
CrossHair covers every path only when it says so for every condition ("Confirmed over all paths"); otherwise, like Hypothesis, it is a search that found nothing: report `no_counterexample_found`, never "verified".
Expect no confirmation for string-heavy code, parsing (`json.loads`, regexes), or deep library calls: CrossHair explores those paths but cannot exhaust them, so budget for `no_counterexample_found` and use its runtime (minutes per run) on contracts where a counterexample is plausible.
`run_code_check.py` sets `exhaustive` accordingly, and `check_run.py` refuses `no_violation_within_bounds` for a non-exhaustive run.

## 3. Write the harness

The harness lives in `verification/models/<slug>/` and is the only new code; the code under verification is compiled, imported, or linked **unmodified**.

- **Call the real code.** `#include` or link the real source file, `use` the real crate, import the real module.
  Never paste a copy of the function into the harness: a copy is a model, and it drifts.
- **Inputs are nondeterministic, assumptions are explicit.**
  CBMC/ESBMC: `int nondet_int(void);` and `__CPROVER_assume(len <= 8);`.
  Kani: `let n: u8 = kani::any(); kani::assume(n <= 8);`.
  CrossHair: preconditions (`pre: n > 0`) on a wrapper function in the harness module that calls the real function and states the postcondition.
  Hypothesis: strategies (`st.integers(0, 8)`), never `assume()` calls that filter out most inputs.
  Every assumption is a scope limit; list each one in `CORRESPONDENCE.md` with its reason (the API documents it, callers guarantee it, or it is a size bound).
- **Drive operation sequences for invariants.**
  For a data structure, a loop of nondeterministic operations (`push`, `pop`, `clear`, chosen by a nondeterministic selector) followed by the invariant, so the checker explores every sequence up to the loop bound.
- **Stub only the environment.**
  Replace I/O, clocks, allocator failure, or system calls with nondeterministic stubs (an allocator that may return NULL is how leak and null bugs show).
  Never stub a function of the code under verification; if you must, say so as an under-approximation.
- **Bounds cover the inputs.**
  `--unwind N` (CBMC/ESBMC) or `#[kani::unwind(N)]` must exceed the longest loop the harness drives; unwinding assertions stay on, and a run where one fails is `incomplete`, not a pass.
  CrossHair: `--per_condition_timeout` and `--per_path_timeout` set the budget; record them.
  Hypothesis: `@settings(max_examples=...)`; record it.
- **Make the target importable or compilable from the harness directory** without editing the repo: an include path (`-I src`), a path dependency, or `sys.path` set inside the harness module.
  For Python, import from the repository's own tree (`sys.path.insert(0, <repo>/src)` in the harness), not from whatever the virtualenv has installed: `run_code_check.py --mutant` puts the patched copy first on `PYTHONPATH`, but a harness that imports an installed copy by another route would test the unpatched code (the mutant then "passes", and vacuity fails).
- **Name harness files so the project's normal tests do not collect them**: `harness_<slug>.c`, `contracts_<slug>.py`, `prop_<slug>.py` (pytest's default discovery skips files not named `test_*.py` or `*_test.py`).
- **Rust harnesses live in their own crate** under `verification/models/<slug>/kani/`, depending on the real crate by path (`[dependencies] target = { path = "../../../.." }`), because a `#[kani::proof]` inside the real crate would be an edit to its `src/`.
  Such a harness reaches only the crate's public API; a private function is checked through its public callers, or recorded as a coverage gap.

## 4. Vacuity for code-level properties

Every pass claim (`no_violation_within_bounds` or `no_counterexample_found`) needs both of these, recorded in the property's `vacuity` block; `check_run.py` verifies each one.

1. **A sanity run that must fail** (`vacuity.sanity`): the same harness with a must-fail check after the call to the code under verification, and that check must be the one that fails: name it `sanity` (its message, test name, or harness name), and `check_run.py` requires a failing check that names it.
   On buggy code the harness fails anyway, so restrict the sanity run to the sanity check (CBMC `--property harness.assertion.1`, Kani `--harness <name>_sanity`).
   It shows the assumptions are satisfiable and the harness reaches the end; an over-constrained harness (contradictory `assume`, a precondition nothing satisfies, a strategy that generates nothing) passes every property vacuously.
   - CBMC/ESBMC: `#ifdef VF_SANITY __CPROVER_assert(0, "sanity: harness reaches the end"); #endif`, run with `-DVF_SANITY --property harness.assertion.1`.
   - Kani: a sibling `#[kani::proof] fn <name>_sanity()` with the same body ending in `assert!(false, "sanity: ...")`, run with `--harness <name>_sanity`.
   - CrossHair: a sibling wrapper with the same preconditions and `post: False`.
   - Hypothesis: a sibling test with the same strategies ending in `assert False, "sanity: ..."`.
2. **A real-code mutant that must fail** (`vacuity.mutants`): a patch that breaks the code the property guards, run with `run_code_check.py --mutant mutants/<name>.patch`.
   Remove the bounds check, turn `<` into `<=`, drop the `free`/`NULL` check or the overflow guard, skip the reset.
   The checker must report a violation on the mutant; if it does not, the harness does not exercise that code.
   Write the patch from a scratch copy, never by editing the repo:
   ```bash
   cp src/ring.c /tmp/ring_mutant.c && $EDITOR /tmp/ring_mutant.c
   diff -u --label a/src/ring.c --label b/src/ring.c src/ring.c /tmp/ring_mutant.c \
     > verification/models/ring-buffer/mutants/drop-wrap-check.patch
   ```
   `run_code_check.py` applies the patch to a scratch copy of the repository and refuses a patch that touches `verification/` or `plans/`.

A `violated` result needs neither: a counterexample on the real code is non-vacuous by construction, and Phase E's failing repro test is its evidence.

**One harness, several properties.**
When a harness shows one property violated and another holding (a read past the input, while every write stays in bounds), run the holding property on its own: CBMC `--property <check id>` restricts the run to that check, so a failure elsewhere in the harness does not hide the pass.
Take the id from a full run's results (`frame_decode.precondition_instance.3`), confirm the mutant run fails on the same id, and say in `bounds` that the run covers that check only.
`examples/frame-length-overflow/` does exactly this.

## 5. Run the checks

Run every code-level check through `scripts/run_code_check.py`, in the foreground, one at a time; never hand-write or edit its JSON or log.

```bash
S=<skill>/scripts
T=verification/models/ring-buffer
# C, memory safety and overflow, every operation sequence up to 6 operations
# (checks claimed: bounds, pointer, signed_overflow, unsigned_overflow, pointer_overflow)
$S/run_code_check.py --tool cbmc --out $T/results/Harness.json --quiet --timeout 600 -- \
  cbmc $T/harness_ring_buffer.c src/ring.c -I src --function harness --unwind 7 \
  --bounds-check --pointer-check \
  --signed-overflow-check --unsigned-overflow-check --pointer-overflow-check --trace
$S/run_code_check.py --tool cbmc --out $T/results/sanity-Harness.json --quiet -- \
  cbmc $T/harness_ring_buffer.c src/ring.c -I src --function harness --unwind 7 \
  --bounds-check --pointer-check \
  --signed-overflow-check --unsigned-overflow-check --pointer-overflow-check \
  -DVF_SANITY --property harness.assertion.1
$S/run_code_check.py --tool cbmc --out $T/results/mutant-drop-wrap-check.json --quiet \
  --mutant $T/mutants/drop-wrap-check.patch -- \
  cbmc $T/harness_ring_buffer.c src/ring.c -I src --function harness --unwind 7 \
  --bounds-check --pointer-check \
  --signed-overflow-check --unsigned-overflow-check --pointer-overflow-check

# Rust (from the verification crate's directory)
$S/run_code_check.py --tool kani --cwd $T/kani --out $T/results/Harness.json --quiet -- \
  cargo kani --harness push_pop_in_bounds

# Python contracts and property tests
$S/run_code_check.py --tool crosshair --out $T/results/Contracts.json --quiet -- \
  .venv/bin/crosshair check $T/contracts_parser.py --report_all --per_condition_timeout 20
$S/run_code_check.py --tool hypothesis --out $T/results/Props.json --quiet -- \
  .venv/bin/python -m pytest -q -p no:cacheprovider $T/prop_parser.py
```

Read the JSON `result` field: `pass`, `violation`, `incomplete` (raise the bound), `vacuous` (nothing was checked: no harness found, no contract, no test collected), `error`, `timeout`.
`violated` lists the failing checks; `counterexample` holds the trace excerpt; `exhaustive` says whether a pass covers every path within the bounds.
Flags that switch checks off (`--no-*`), stop at partial loops, or change the output format are rejected; `crosshair check` must pass `--report_all`.
Add every command to `verification/rerun.sh`.

## 6. From counterexample to repro test

The counterexample gives concrete inputs; Phase E turns them into a failing test on the real code, as for a model trace:

- CBMC/ESBMC: read the input values from the trace (`--trace`), write a C test that calls the real function with them, and build it with `-fsanitize=address,undefined` (`examples/frame-length-overflow/verification/repro/` has a test file and a runner to copy).
  Run it through a placeholder `repro_runner` (a shell script under `verification/repro/` that builds, runs, and on a sanitizer report prints `<Property> violated: <first sanitizer line>` and exits 1), so the failure names the property.
- Kani: `--concrete-playback=print` prints a unit test with the failing input; adapt it into a test in `verification/repro/` that catches the panic (`std::panic::catch_unwind`) and asserts with a message naming the property, so it fails by assertion, not by crash.
- CrossHair and Hypothesis: the failing call is printed (`when calling f(12345)`, `Falsifying example: test_x(i=1, n=1)`); write a pytest repro that makes exactly that call and asserts the property.

The repro test is still required for every CONFIRMED finding; the verifier's counterexample alone is a lead.

## 7. Correspondence and diagram for a code-level target

`CORRESPONDENCE.md` for a code-level target maps the harness instead of a model:

- the real functions called and the files compiled or imported, with `file:line`;
- every assumption on the inputs, with its source, and which direction it errs (an assumption the API does not document hides real bugs);
- every stub, and what real behavior it stands for;
- the bounds (unwind depth, sizes, timeouts, example counts) and what they cover;
- what the harness does not reach (concurrency, I/O, other callers);
- the sync commit (`- **Code**: ... at commit <full SHA>`).

Its `diagram.json` is `"kind": "manual"`: the states of the data structure or the function's phases that the harness drives (empty, partial, full, wrapped; parsing header, body, done), each citing `file:line`, with the counterexample's path highlighted.
See `references/state-diagram.md`.
