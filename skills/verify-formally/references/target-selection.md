# Target Selection

Formal modeling is expensive per target, so choosing where to spend it is most of the skill.
A good target has three things: a real chance of a defect, a property you can state in one sentence, and a boundary small enough to model faithfully in an hour.

---

## Where bugs hide that tests miss

Tests check the paths someone thought of.
Model checkers enumerate the paths nobody thought of.
So look for code whose behavior depends on **combinations and orderings** of events rather than on single inputs:

- **Exception and interruption paths.** `try`/`except`/`finally` ladders, re-raising, a `raise` inside `finally` (it replaces the in-flight exception), `KeyboardInterrupt`/`SIGINT` arriving at any bytecode boundary, `asyncio.CancelledError` at any `await`, `AbortSignal` in JS.
  Look for state that is appended, printed, or persisted in one step and paired with other state in a later step: an interrupt between the two leaves them unpaired.
  "Log, then commit" and "notify, then record" are classic windows.
- **Loops with guards and budgets.** Iteration limits, spend limits, deadlines, retry counts, error caps.
  Check how counters are reset (a reset on one path can cancel a budget on another), whether comparisons are `<` or `<=` consistently, whether the limit is checked before or after the action it limits, and whether every exit path is reachable.
- **State machines.** Status or phase enums, mode flags, `is_running`/`done` booleans.
  Look for transitions that skip cleanup, flags not cleared on the error path, states that can be entered twice, and "impossible" combinations the types allow.
- **Protocols between components.** Request/response pairing (RPC ids, a result for every request), stream termination signals, handshakes, sentinel lines in output, ordering constraints an external API enforces (every request needs its matching response before the next one).
- **Concurrency.** Shared counters or files touched from a thread pool, check-then-act, locks held around some accesses but not others, reads outside the lock, callbacks that can run twice, futures completed from two places.
- **Subprocess and timeout handling.** What a timeout or cancellation actually stops, what keeps running, what keeps resources open, and whether cleanup runs on every exit path.
- **Parsers and validators** with crisp pre/post conditions: "every accepted input round-trips", "every input is either parsed completely or rejected with an error".
  These suit Lean.

## Risk signals (how to find candidates fast)

- `git log --oneline -40` and `git log --format='%h %s' -- <file>`: fix commits cluster in fragile code.
  A file with several recent "fix(...)" commits in its exception paths is a top candidate, because the remaining paths were written by the same reasoning.
- **Is each fix still there?** For the recent fix commits in candidate files, read the commit's diff (`git show <sha> -- <file>`) and check the current code still contains the fix.
  A fix that a later refactor, revert, or merge resolution dropped is a regression waiting to be confirmed, and the fix commit's message and test tell you the property and its source.
- **Merges hide changes.** `git log -p` shows merge commits without a diff; use `git log --first-parent -p`, `git show --cc <merge>`, or `git diff <merge>^1 <merge> -- <file>` to see what a merge changed, including edits made while resolving conflicts.
- Tests that were removed (`git log --diff-filter=D --stat -- tests/`, or a test named in a fix commit that no longer exists) often mark behavior that is no longer protected.
- Grep for `while True`, `finally:`, `except KeyboardInterrupt`, `except Exception`, `BaseException`, `threading.Lock`, `ThreadPoolExecutor`, `asyncio`, `await`, `timeout`, `retry`, `signal.`, `subprocess`, `Popen`, `.kill(`, `limit`, `max_`, `status =`, `state =`, `mode =`.
  In TypeScript: `Promise.race`, `AbortController`, `setTimeout`, `for await`, `finally`, `.catch(`, `EventEmitter`, `queueMicrotask`, `Mutex`.
- Comments that argue about correctness ("can't happen", "should never", "must be called before", "keep in sync with") mark invariants someone was worried about.
- Docs that describe a control flow (a "control flow" or "lifecycle" page) give you the intended property for free, and any gap between the doc and the code is itself a lead.
- Code that subclasses and overrides one step of a base class's loop is where the base class's invariants silently stop holding.

## Scoring

Score each candidate on two 1-5 scales and multiply:

**Risk** (chance of a real, user-visible defect):
- 5: many interacting paths, recent fixes nearby, external contract that punishes mistakes (API 400s, data loss, runaway cost, hangs).
- 3: several paths, some interaction, failure would be visible but recoverable.
- 1: straight-line code, well tested, recently rewritten with tests.

**Modelability** (chance you can model it faithfully in about an hour):
- 5: small finite control state, clear program points, nondeterminism limited to a few environment choices, test seams exist (injectable model, env, clock, prompt).
- 3: needs abstraction of data or of a library's behavior you must read to understand.
- 1: behavior depends on unbounded data, floating point, real timing, or large third-party internals; no test seam to drive the trace.

Tiebreakers, in order:
1. A property you can check both ways (safety now, liveness later) floats up.
2. A target whose repro is cheap (deterministic seams already exist in the tests) floats up, because a finding needs a failing test.
3. A target with an external contract (an API that rejects malformed sequences, a limit users rely on for cost) floats above an internal-only invariant.
4. Prefer depth over breadth: related budgets in one loop (step, cost, error caps) are one target, and three well-checked targets beat five shallow ones.

## Stating the property

Write the property before any model, in the form the code's users would care about, and record its **source**:

| Source | Example | Strength |
|---|---|---|
| doc | `docs/operations.md:31`: "max_attempts: a job runs at most this many times" | strong |
| API contract | the queue service rejects an ack for a lease it no longer holds | strong |
| test | `tests/test_worker.py::test_retry_budget` asserts `attempts == 3` | strong |
| code comment or docstring | `worker.py:52`: "give up after this many consecutive failures" | medium |
| commit message | `fix: release leases on shutdown (#212)` | medium |
| inferred | you derived it from how the code seems meant to work | weak: a violation is NEEDS-DECISION unless the consequence is wrong under any intent |

When sources disagree (a docstring says "consecutive failures", a commit says "any retry resets the counter"), record both; that disagreement is itself worth reporting.

- Safety: "Never X" or "Whenever P, Q holds" - "every leased job is acked or released before the worker exits", "attempts never exceed max_errors + max_timeouts", "an ack says 'done' only if the job's command ran".
- Liveness: "Eventually X" under stated fairness - "the worker loop terminates for every environment behavior", "every submitted job is eventually reported".
- Functional (Lean): "for all inputs, f(x) satisfies P" - "parse returns exactly one record or raises", "the counter update never decreases the total budget".

If the property needs ground truth the code does not store (did the command actually run?), keep that ground truth in the model; truthfulness properties depend on it.

## Poor targets

- UI layout, string formatting, logging content, templating output.
- Thin glue over a library where all the logic is in the library.
- Anything whose only spec is "looks right" or "matches the snapshot".
- Numerical code where the risk is precision (use property-based tests instead).
- Code you cannot drive from a test without editing it (no seam for the environment choices); note it as a coverage gap with the missing seam.

## The ranked table

| # | Target | Files | Risk signal | Property to check (source) | Tool | Risk | Model | Score |
|---|--------|-------|-------------|----------------------------|------|------|-------|-------|
| 1 | Lease settlement across shutdown signals | jobrunner/worker.py:88-150, jobrunner/queue.py:40-90 | 3 fixes in 40 commits in these paths; queue rejects stale acks | every leased job acked or released before exit (docs); acks truthful (inferred) | TLA+ | 5 | 5 | 25 |

Record the whole table (including rejected candidates and one line on why) in `verification/README.md` so the next run does not redo the survey.

Give each selected target a **slug**: lowercase kebab-case of what is modeled, 2-4 words (`lease-settlement`, `retry-budget`, `shutdown-drain`).
The slug names `verification/models/<slug>/`, the repro file `verification/repro/repro_<slug_with_underscores>.py`, and the target `id` (and each finding's `targets`) in `findings.json`.
On a collision with an existing slug from a previous run, reuse it if it is the same target, otherwise append `-2`.
