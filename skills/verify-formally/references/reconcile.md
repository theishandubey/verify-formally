# Reconcile: Keeping the Verification True to the Code

A check proves a property of what it describes.
When the code changes and the verification does not, the check can keep passing while it describes code that no longer exists.
That pass is the dangerous case, because it looks like a proof.
`/verify-formally reconcile` is the upkeep loop for an existing `verification/`: it reads every change to covered code since the verification was last confirmed, brings each model, harness, and property back in line with the code as it is now, re-runs, and says for each failure whether it is drift in the verification or a bug in the code.

---

## 1. Outcomes

Give each target one outcome in `targets[].reconcile.outcome`, and say which in the README:

- **`in_sync`**: no change since the sync commit alters what the verification describes, and every check gives the same result.
- **`updated`**: the code changed, the verification now describes the new code, and every check that should pass passes.
- **`bug`**: the verification describes the current code, and the code breaks the property. This is a finding about the code, not drift.
- **`blocked`**: the verification could not be brought back in line, or could not run. Say exactly why in `blocked_reason`.

A target can be both `updated` and `bug` (`["updated", "bug"]`): the update makes the verification describe the code again, and then the property fails on it.

## 2. Edit scope

Edit only the verification: models, cfgs, harnesses, mutants, `CORRESPONDENCE.md`, `diagram.json`, findings, plans, and the README.
Never edit product code, even to confirm a fix.
When a property fails on the current code, do not weaken the property, the model, the harness's assumptions, or the bounds to make it pass; report the failure.

## 3. Pass

0. **Back up first.**
   `verification/` and `plans/` are usually untracked, so git cannot undo a bad edit: `cp -a verification plans /tmp/reconcile-backup-<date>/` before touching anything.
   Always copy with `cp -a` (or `cp -Rp`): a copy that resets modification times makes every result look older than its spec, and the validator reports them stale.

1. **Find the checks and the sync commit.**
   Read `verification/README.md`, `verification/findings.json`, and every target's `CORRESPONDENCE.md`.
   For each target collect its properties, the code it covers (`files`, and every `file:line` in `CORRESPONDENCE.md`; the validator counts both), its bounds and assumptions, the commands in `verification/rerun.sh`, and the sync commit (the SHA on the `- **Code**: ... at commit <SHA>` line; the path on that line may name another checkout, and only the SHA matters).
   If no `verification/` exists, stop and run a full `/verify-formally` instead.
   If a target records no sync commit, use the last commit that changed its verification files (`git log -1 --format=%H -- verification/models/<id>/`) and say so in `defaults_taken`.

2. **Run every check as it is, before changing anything.**
   Re-run each cfg and harness exactly as recorded, writing to `verification/models/<id>/results/before/` (`run_tlc.sh ... --out results/before/<stem>.json`, `run_code_check.py ... --out results/before/<stem>.json`), and record the paths in `reconcile.pre_update_runs`.
   The simplest way: run `verification/rerun.sh` in a `cp -a` copy of the repository, then move each target's `results/*.json` and `*.log` into `results/before/` of the real one.
   Every cfg and harness that existed at the sync commit gets a before run; the validator warns when fewer than half do.
   A pass here does not tell you the code is correct; it tells you the old description still holds.
   A failure here can be drift or a bug; do not decide which until step 5.

3. **Read every change to covered code.**
   `git log --format='%H %s' <sync>..HEAD -- <covered files>`, then `git show <commit> -- <file>` for each, reading each commit message; merges with `git show --cc` or `git diff <merge>^1 <merge>`.
   Also look for code that moved: `git log --follow`, `git diff -M <sync>..HEAD --stat`, and a grep for each covered function's name; a covered function that was renamed, split, or moved to another file is still covered code (list its old path in `reconcile.files_before`).
   Also look for **new code that touches the state a check guards**: grep for writes to the same fields, new callers of the covered functions, new threads or handlers on the same objects.
   List every commit in `reconcile.commits_read`, and classify each one in `reconcile.changes` (`{"commit", "effect", "note"}`):
   - **`no_effect`**: the change does not alter anything the verification describes (formatting, comments, code paths outside the model's scope). Check this against the model or harness, not against the commit message.
   - **`shape_changed`**: the verification no longer describes the code: a step that was one atomic action is now two, a branch was added or removed, a check moved, a new path reaches the guarded state, a value now has a different range, a harness's call no longer compiles. The model or harness must change.
   - **`promise_changed`**: the change alters what the code promises, and evidence in the change says it is intended: the doc comment, the tests, the changelog, the commit message. The property must change; cite the evidence in `evidence` and record `property_change: {"old": "...", "new": "..."}`.
   - **`promise_maybe_broken`**: the code changed, and no evidence says the promise changed. A failure that follows is a bug candidate.
   - **`coverage_gap`**: the change touches behavior that no property describes (typically an upstream fix of a bug the old verification could not see). Say what in `note`. If the behavior matters, add a property for it now, with its full vacuity check (and a mutant that reverts the change, so the property is shown to see the old bug), and record in the README that the old verification missed it; a bug that is already fixed upstream is not a finding.
   One commit can need two entries (a commit that reshapes a modeled step and also fixes something no property covered).

4. **Update from the code, not from the result.**
   Change the model or harness so each changed part again maps to code a reviewer can open, and keep one atomic step in the code as one step in the model (`references/correspondence.md` section 2).
   Write the update from the code before re-running; do not shape it by what makes the check pass.
   Update `CORRESPONDENCE.md`'s line numbers and tables for every change, not only the ones that changed behavior.
   Change a property only for a `promise_changed` change; add one for a `coverage_gap`.
   Keep the bounds unless the change makes them wrong; if you must reduce one, say so in the README.

5. **Re-run and judge each result.**
   - **Pass after an update**: the check can now pass on a description that has lost the ability to fail.
     Break the new code the way the property guards against, and show the check catches it: a TLA+ mutant cfg that weakens the changed action, or `run_code_check.py --mutant` with a patch that removes the new check or reorders the new step.
     Record the violating result in `reconcile.post_update_break`.
     The property's ordinary vacuity block (mutant and sanity results at the reported bounds) must be current too.
   - **Fail**: replay the counterexample against the real code, step by step, through `CORRESPONDENCE.md`.
     If a step cannot happen in the code, the verification is wrong: fix it and say what was wrong.
     If every step can happen, it is a bug: write the failing repro test (Phase E), classify it, find the commit that introduced it (the first commit after the sync commit at which the repro fails; `git bisect run` against the repro in a scratch worktree, never in the user's tree), record it in the finding's `introduced_by`, and state the harm in one sentence.
   - **A pass before and a failure after the update** means the old pass was on a stale description; say so in the README.
   - Re-run every CONFIRMED finding's repro test: a repro that now passes on current code means the bug is fixed, and the finding becomes `FIXED` (never deleted).

6. **Move the sync commit and record the outcome.**
   For every target whose outcome is not `blocked`, set the sync commit in `CORRESPONDENCE.md` to HEAD and `reconcile.to_commit` to HEAD, only after you have confirmed the verification describes the code at HEAD.
   For a `blocked` target leave both at the old sync commit (`to_commit` equals `from_commit`).
   Refresh what refers to line numbers or the old commit: findings' `locations` and Markdown, plans' "Current state" excerpts and drift-check SHAs, and `plans/README.md`.
   Leave a SHA stamp in a spec's header comment alone when the spec did not otherwise change (editing it makes every result of that spec stale for no gain).
   Set `findings.json` `commit` to HEAD and `invocation` to `/verify-formally reconcile`; run the project's suite once if covered code changed, and refresh `baseline` from it (otherwise keep the old baseline and say so).
   Re-render `verification/state-diagram.html`, run `verification/rerun.sh`, and then `scripts/check_run.py`; C19 checks the reconcile record.

## 4. The reconcile record

```json
"reconcile": {
  "outcome": ["updated", "bug"],
  "from_commit": "<old sync SHA>",
  "to_commit": "<HEAD SHA>",
  "files_before": ["jobrunner/worker_old.py"],
  "commits_read": ["<sha>", "<sha>"],
  "changes": [
    {"commit": "<sha>", "effect": "shape_changed", "note": "LeaseBatch now logs before leasing (worker.py:98-110); split into LogIntent and LeaseBatch"},
    {"commit": "<sha>", "effect": "promise_changed", "note": "cancelled jobs no longer count against max_attempts", "evidence": "docs/operations.md:33 and tests/test_worker.py::test_cancel_not_counted (same commit)", "property_change": {"old": "a job runs at most max_attempts times", "new": "a job runs at most max_attempts times, not counting runs cancelled by the user"}},
    {"commit": "<sha>", "effect": "no_effect", "note": "log message wording only"},
    {"commit": "<sha>", "effect": "coverage_gap", "note": "fix makes failed parses count toward cost; no property covered cost accounting; added CostTracksBilled (mutant reverting the fix violates it)"}
  ],
  "pre_update_runs": ["verification/models/lease-settlement/results/before/Spec.json"],
  "post_update_break": ["verification/models/lease-settlement/results/mutant-SplitLeaseNoRelease.json"],
  "blocked_reason": ""
}
```

`check_run.py` (C19) refuses a reconcile run where a commit touching the covered files (`files`, `files_before`, and every file `CORRESPONDENCE.md` cites) is not in `commits_read`, a SHA in it is shorter than 7 hex digits, a commit read is not classified, a `promise_changed` change has no evidence or no old and new wording, a `coverage_gap` has no note, an `updated` target has no violating `post_update_break` newer than the updated model (a result under `results/before/` does not count), a non-blocked target lacks `pre_update_runs` or a sync commit at HEAD, a `blocked` target moved its sync commit, or a `bug` target has no CONFIRMED finding naming the commit that introduced it (`introduced_by`; findings from earlier runs need none).
It cannot check that a property changed only for a promise change; the README's per-change reasons are what a reviewer reads for that.

## 5. Report

`verification/README.md` gets a `## Reconcile` section, one entry per target:

- the outcome, the sync commits (from, to), and the commits read;
- what changed in the verification and why, one line per change: the code changed shape, or the promise changed on purpose (with the evidence and the old and new wording);
- the before and after results, and the break used to show each updated check can still fail;
- each bug in plain words: the counterexample as steps in the code's words, the harm, and the commit that introduced it;
- which failures were drift in the verification and which were bugs in the code, and every check whose old pass was on a stale description;
- what the verification still does not cover, including new code it should cover and does not.

Keep verification updates in a change of their own, separate from any product fix.
