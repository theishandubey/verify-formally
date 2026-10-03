# Model-Code Correspondence

A model is evidence about the code only through its correspondence to the code.
`CORRESPONDENCE.md` is where that link is made explicit, and it is what a reviewer reads to decide whether a result means anything.
Write it while modeling, not after.

---

## 1. What `CORRESPONDENCE.md` contains

```markdown
# Correspondence: <target>

- **Code**: `<repo>` at commit `<full SHA>`
- **Model**: `Spec.tla` (+ cfgs), `Verification/<Target>.lean`
- **Property in plain words**: <one sentence per property>

## State

| Model | Code | Notes |
|---|---|---|
| `acks` | `Worker.acked` (`jobrunner/worker.py:37`) | only job ids and outcome kind kept |
| `exc` | exception in flight between `_run_batch()` and `loop()`'s handlers | values = exception classes in `jobrunner/errors.py:1-40` |
| `ran[i]` | ground truth: whether job i's command actually ran | not stored by the code; needed for truthfulness |

## Actions

| Model action | Code | Environment choices |
|---|---|---|
| `CheckBudget` | `jobrunner/worker.py:88-97` | none |
| `LeaseBatch` | `jobrunner/worker.py:98-104`, `jobrunner/queue.py:40-61` | lease 1..MaxBatch jobs, empty queue, lease lost, SIGTERM |

## Guards and constants

| Model | Code | Checked equal? |
|---|---|---|
| `attempts >= MaxAttempts` | `if self.max_attempts and self.attempts >= self.max_attempts:` (`worker.py:90`) | yes, same `>=` direction; `max_attempts = 0` (unlimited) not modeled |

## Abstractions

| Abstraction | Why | Errs toward |
|---|---|---|
| job payload dropped | acking depends only on ids and outcomes | neutral |
| lease expiry merged with explicit release | both raise `LeaseLost` at the same point with the same handler | over-approximation (more exits) |

## Fix flags

| Flag | Planned code change | Model change | New interference points the change creates |
|---|---|---|---|
| `FixAckBeforeLog` | in `_finish_batch`'s finally, record the acks, then log them (`worker.py:141-147`) | `FinishBatch` appends before the log action | SIGTERM between the new append and the log (modeled; harmless) |

## Not modeled

- Dry-run mode (`worker.py:60-70`): jobs are never acked, so the property does not apply.

## Trace to test

How each action is driven from a test: which fake, mock, or patch point realizes each environment choice.
```

## 2. Fidelity checklist

Go through this before trusting any result, and again before reproducing a counterexample:

1. **Every model action maps to a line range**, and every code path in the target's scope maps to some action or is listed under "Not modeled" with a reason.
2. **Guards are copied exactly.**
   `<` versus `<=`, `and` versus `or`, the order of checks, the value of a default (`0` meaning "no limit").
   Off-by-one bugs live here; a paraphrased guard hides them.
3. **Initial state matches the constructor and the entry point**, including values set in `run()` that reset state set in `__init__`.
4. **Atomicity matches interference points.**
   Every point where an exception, signal, other thread, `await`, or callback can intervene is an action boundary.
   Merging across one of these hides every bug in the gap.
5. **Environment covers reality.**
   List every behavior the environment can produce (model replies, user inputs, errors, return codes, interrupts) and check each is a disjunct somewhere.
   If one is missing, say so under "Not modeled".
6. **Exception semantics match the language.**
   Which handler catches which class, whether `finally` runs, what a raise inside `finally` does, that `KeyboardInterrupt` is a `BaseException` in Python, that a rejected promise without a handler does not stop the caller in JS.
7. **Overrides are modeled at the subclass level.**
   If a subclass overrides one step of the loop (`process_batch`, `handle_result`), the model must follow the subclass, and the base class becomes a sibling configuration.
8. **Constants map to real config values.**
   Record the real defaults and which ones the bounds cover (bound 3 covers a default `max_retries = 3`).
9. **Ground truth is labeled as such**, so nobody thinks the code stores it, and it is assigned where the real-world event happens, independently of where the code records it.
10. **Fix flags match the planned change.**
    Each flag's model change is the code change the plan will prescribe, at the model's granularity, including new interference points; an idealized fix is an under-approximation of the fixed code and makes the fixed model's pass meaningless.

## 3. Abstraction rules

- **Over-approximation is the safe direction.**
  A model that allows more behaviors than the code can produce spurious counterexamples, which Phase E catches as MODEL-ONLY.
  A model that allows fewer behaviors can pass while the code is broken, and nothing catches it.
  When unsure, allow the behavior.
- **Drop data the property does not read**, keep everything that affects control flow.
  A job's payload is irrelevant to acking; its id and outcome are not.
- **Merge values that the code treats identically** (two exception classes caught by the same handler with the same effect), and say so.
- **Bound, do not delete, unbounded structures.**
  A list becomes a list of bounded length; a counter gets a constant bound; say what the bound covers.
- **Never abstract the thing the property is about.**
  If the property is about ordering of appends, appends must be separate actions.

## 4. Drift and reconcile

The SHA in `CORRESPONDENCE.md` is the model's validity stamp.
On `/verify-formally reconcile`, run `git diff --stat <SHA>..HEAD -- <files in the table>`.
If a mapped file changed: re-read each mapped line range, update line numbers, update the model if behavior changed, re-run the checker and the repro tests, then update the SHA.
A model whose code changed and was not re-checked is stale and is reported as such.

## 5. When correspondence fails

If you cannot say which code a model action stands for, or the code's behavior depends on something you cannot model (unbounded data, real timing, opaque library internals), stop.
Report the target as a coverage gap with the specific obstacle ("behavior depends on litellm's internal retry of streaming responses, not visible from this repo").
A model that does not correspond to the code produces confident nonsense.
