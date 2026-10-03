# Fix Plan Template

Adapted from the improve skill's handoff template.
Every plan is written for an executor with **zero context**: it has not seen the verification session, the model, or the other plans, and it may be a smaller model.
It follows explicit instructions well and fills gaps badly.

What is different from a generic plan: the bug is already proven by a failing test and a model trace, so the plan's acceptance is mechanical.
The repro test must pass, the full suite must pass, and the model with the fix flag on must pass its property within the same bounds.

File naming: `plans/NNN-<slug>.md`, numbered in recommended execution order, monotonic across runs.
When the run uses the `verification-plans/` fallback (because `plans/` belongs to something else), every `plans/` path in this template and in the findings becomes `verification-plans/`.
Plan numbers are independent of finding numbers; a plan lists the findings it closes, and each finding names its plan.
One plan per fix: findings whose fixes must land together, or that one change removes, share a plan.

---

## Template

```markdown
# Plan NNN: <Imperative title - what will be true after this plan>

> **Executor instructions**: Follow this plan step by step.
> Run every verification command and confirm the expected result before moving to the next step.
> If anything in "STOP conditions" occurs, stop and report; do not improvise.
> When done, update this plan's row in `plans/README.md`, unless a reviewer dispatched you and said they maintain the index.
>
> **Drift check (run first)**: `git diff --stat <SHA>..HEAD -- <in-scope paths>`
> If any in-scope file changed since this plan was written, compare the "Current state" excerpts with the live code; on a mismatch, treat it as a STOP condition.

## Status

- **Findings closed**: `verification/findings/NNN-<slug>.md` (CONFIRMED, severity <level>)<, ...>
- **Fix check**: observed (prototyped in a disposable copy: <what passed>) | predicted (fixed model only)
- **Priority**: P1 | P2 | P3
- **Effort**: S | M | L
- **Risk**: LOW | MED | HIGH - <one line>
- **Depends on**: plans/NNN-*.md or "none"
- **Planned at**: commit `<short SHA>`, <YYYY-MM-DD>

## Why this matters

2-5 sentences: the misbehavior, its trigger, its consequence, and what improves.
Plain language; the executor needs intent to make a correct judgment call when a detail is off.

## The proof that it is broken

- Repro test: `verification/repro/repro_<slug_with_underscores>.py::test_<name>`, run with `<exact command>`.
  Today it fails with:
  ```
  <exact failure line>
  ```
- Model: `verification/models/<target>/<Spec>.tla`; `<Spec>.cfg` (buggy) reports `invariant_violation` of `<Property>`; `Plan<NNN>.cfg` (only this plan's fix flags on: `<FixFlag> = TRUE`) passes within `<constants>`.
  The fix flag models exactly the change in the steps below; if you change the approach, the model no longer vouches for it (STOP condition).
- Counterexample in code terms (condensed, 3-8 steps with `file:line`).

## Current state

- The files involved, one line each on their role.
- Excerpts of the code as it is today, with `file:line` markers, enough to confirm the executor is looking at the right thing.
- The conventions to match, with an exemplar (error handling, message construction, test style: "tests use the `FakeQueue` fixture from `tests/conftest.py`; see `tests/test_worker.py:30-60`").

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Repro test | `<command>` | 1 passed (after fix); fails today |
| Full tests | `<command>` | all pass, counts >= baseline |
| Lint | `<command>` | exit 0 |
| Model check (fixed) | `<skill>/scripts/run_tlc.sh <Spec>.tla Plan<NNN>.cfg` | `"result": "pass"` |

(If the verify-formally skill's scripts are not installed where the executor runs, give the raw fallback too: `java -cp <tla2tools.jar> tlc2.TLC -workers auto -config <Spec>Fixed.cfg <Spec>.tla` run from the model directory, expected output contains `Model checking completed. No error has been found.` and no `Warning`.)

## Scope

**In scope** (the only files you should modify):
- `src/...`
- `tests/...` (the repro test moves here, see step N)

**Out of scope**:
- `verification/models/**` - the models are evidence; do not edit them to make them pass.
- <related-looking files and why they must not change>

## Git workflow

- Branch: `verify/NNN-<slug>` (or the repo's convention).
- Commit message style: <match repo, example from `git log`>.
- Do not push or open a PR unless the operator instructed it.

## Steps

### Step 1: Confirm the failure

Run the repro command.
**Verify**: it fails with the failure line quoted above.
If it passes or fails differently, STOP (the code has drifted or the environment differs).

### Step 2: <the fix, precisely>

Exact files and symbols; the target code shape where it is load-bearing.
**Verify**: repro command -> passes.

### Step 3: Move the repro into the real suite

Copy the repro test (and its guard test) into `tests/<matching path>/test_<name>.py`, adapted to the project's test layout and imports; keep the assertions unchanged.
Name the helpers the target test module already defines that the repro duplicates (small process or file helpers, fixtures), and say to reuse them rather than paste a second copy.
**Verify**: `<full test command>` -> all pass, including the new tests.

### Step N: ...

## Test plan

- The moved repro and guard tests (named above).
- Any additional regression cases the finding's root cause suggests (list them concretely).

## Done criteria

All must hold:

- [ ] Repro command passes.
- [ ] `<full test command>` exits 0 with counts >= baseline plus the new tests.
- [ ] `run_tlc.sh <Spec>.tla Plan<NNN>.cfg` reports `pass` (unchanged model; confirms the fix matches the modeled fix).
- [ ] Lint/format commands exit 0.
- [ ] No files outside the in-scope list are modified (`git status`).
- [ ] `plans/README.md` row updated.

## STOP conditions

Stop and report back if:
- The repro test does not fail before the fix, or fails with a different message.
- The code at the "Current state" locations does not match the excerpts.
- The fix appears to need changes outside the in-scope list.
- The fixed code passes the repro test but breaks other tests in a way that suggests the old behavior was relied on (it may be by-design after all).
- The change you are about to make differs from the one described in the steps (for example, recording every event before logging it instead of only the acks); the model only vouches for the described change.
- <plan-specific risks>

## Maintenance notes

- What future changes interact with this (for example, "any new exception path between the lease and the ack must still release the lease").
- What a reviewer should scrutinize.
- Follow-ups deliberately left out.
```

---

## Index: `plans/README.md`

```markdown
# Fix Plans

Generated by the verify-formally skill on <date> at commit `<SHA>`.
Each plan fixes one CONFIRMED finding from `verification/findings/`.

| Plan | Title | Findings | Priority | Effort | Depends on | Status |
|------|-------|----------|----------|--------|------------|--------|
| 001 | ... | 001, 003 | P1 | S | - | TODO |

Status values: TODO | IN PROGRESS | DONE | BLOCKED (reason) | REJECTED (reason)
```

## Quality bar

- Could a model that has never seen this repo execute the plan with only the plan file and the repo?
- Is every verification a command with an expected result?
- Does every step name exact files and symbols?
- Does the Scope list include every file any step tells the executor to edit (steps that say "X or Y" must have both in scope)?
- Did you prescribe test mechanics (where a barrier goes, which call raises) only when you ran them in a prototype? Otherwise state the test's intent and the assertion, and leave the mechanics to the executor; a wrong mechanic (a barrier the losing thread never reaches) hangs the test.
- Is every item in "Test plan" also a numbered step (or marked optional)? Executors follow the steps and done criteria; a test mentioned only in the test plan does not get written.
- Do the expected counts in "Commands you will need" come from the baseline you recorded, with environmental failures named, so an executor can tell a regression from a known flake?
- Did you search the test suite for every test that exercises the changed code path, including recorded fixtures and golden files (trajectories, snapshots, `test_data/`), and either confirm they still pass with the fix (prototype) or put the fixture update in scope with the exact expected change?
  A plan that changes observable output without listing the fixtures that record it will stop at the full-suite gate.
- Is the model-check done criterion present with the same bounds as the finding, using a per-plan cfg that turns on only this plan's fix flags?
- Does the fix flag's action match the steps line for line, including where the fix adds a new interruption point?
- Are the STOP conditions specific to this fix's risks?
- No secret values anywhere.
