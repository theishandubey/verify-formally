# Finding Format

Every finding, every non-finding classification, and every checked property is recorded in two places: human-readable Markdown and `verification/findings.json`.
Subagents return results in the shape of section 5; the advisor vets them and writes the files.

---

## 1. Granularity

- **One finding per root cause**: one code defect that one fix removes.
  A root cause that violates several properties, or shows through several triggers, is still one finding; list all its properties, triggers, and repro tests.
- **Two defects under one property are two findings** when each needs its own fix (the fixed model with only one flag on still fails).
- **One plan per fix**: a plan may close several findings (two findings whose fixes must land together, or one fix that removes both).
- Findings are numbered in discovery order (monotonic across runs); plans are numbered in execution order; the two numberings are independent and cross-reference each other.

## 2. Finding file: `verification/findings/NNN-<slug>.md`

One file per CONFIRMED finding.

```markdown
# NNN: <Short statement of the defect, as a fact>

- **Status**: CONFIRMED
- **Severity**: high | medium | low - <one-sentence realistic trigger; name any other bound that limits the damage>
- **Targets**: <slug> (`verification/models/<slug>/`)<, <slug2>>
- **Tools**: TLA+ | Lean | both
- **Properties violated**: `<Name>` - <plain words> (source: <doc/test/comment/commit location>)<; `<Name2>` ...>
- **Location**: `path/file.py:NN-MM` (the code that causes it), plus `path/other.py:NN` for each contributing site
- **Commit**: `<full SHA>`
- **Repro tests**: `verification/repro/repro_<slug_with_underscores>.py::test_<name>` (<trigger>)<, ...>
- **Repro command**: `<exact command>` (fails on current code)
- **Guard tests**: `verification/repro/repro_<slug_with_underscores>.py::test_<guard>` (passes)
- **Plan**: `plans/NNN-<slug>.md` (or "not planned: <reason>")

## What goes wrong

2-5 sentences: the observable misbehavior, who hits it, and the consequence (with the downstream effect if it matters, such as an API rejecting the next request).

## Counterexample

The model trace, condensed to the steps that matter, each with its code location:

1. `LeaseBatch` (`worker.py:98`): the worker leases 1 job.
2. `LogLeased` (`worker.py:104`): SIGTERM arrives while the lease is being logged.
3. ...

Checker: `<command from the result JSON>`, constants `MaxBatch=2, MaxSignals=2`, about 180 distinct states, depth 9, result `invariant_violation` of `LeasesSettled`.

## Reproduction

What the test does, step by step, mapped to the trace steps, and the exact failure output (trimmed):

```
AssertionError: LeasesSettled violated: job 'j1' is still leased after the worker exited
```

## Root cause

The code-level reason in 2-4 sentences, citing lines.
Include the history if it explains the defect (the commit that introduced it, a fix that a later merge dropped).

## Fix direction

1-3 sentences, enough to judge effort; the plan holds the details.
Name the fix flag(s) and the per-plan cfg that passes (`Plan001.cfg`), and whether the fix was prototyped (observed) or only modeled (predicted).

## Vacuity

The mutants that make each property fail, and the sanity cfgs that confirmed reachability at the reported constants.
```

## 3. `verification/findings.json`

```json
{
  "schema": "verify-formally-findings/2",
  "commit": "<full SHA>",
  "tree": "clean",
  "generated_at": "YYYY-MM-DD",
  "invocation": "/verify-formally quick",
  "defaults_taken": ["non-interactive: modeled the top 3 targets", "planned all CONFIRMED >= medium"],
  "baseline": {"dirty_files": [], "test_command": "...", "repro_runner": "<pytest command prefix, or a runner template with {file}/{test}/{dir} placeholders>", "collect_command": "<command listing the normal suite's tests>", "passed": 568, "failed": 0, "skipped": 51, "lint_command": "...", "lint": "clean", "environmental_failures": []},
  "fewer_targets_reason": "<only when a full run models fewer than 3 targets>",
  "targets": [
    {
      "id": "lease-settlement",
      "title": "Leases settled across shutdown signals",
      "files": ["jobrunner/worker.py", "jobrunner/queue.py"],
      "tools": ["tla"],
      "status": "modeled",
      "properties": [
        {
          "name": "LeasesSettled",
          "kind": "invariant",
          "statement": "every job the worker leases is acked or released before the worker exits",
          "source": "docs/operations.md:22: 'a stopped worker never strands a job'",
          "tool": "tla",
          "result": "violated",
          "runs": [
            {"cfg": "verification/models/lease-settlement/Spec.cfg", "result": "invariant_violation", "constants": {"MaxBatch": "2", "MaxSignals": "2"}, "distinct_states": 180, "exact_states": false, "json": "verification/models/lease-settlement/results/Spec.json"},
            {"cfg": "verification/models/lease-settlement/SpecFixed.cfg", "result": "pass", "constants": {"...": "..."}, "distinct_states": 4210, "exact_states": true, "json": "..."}
          ],
          "vacuity": {"status": "passed", "bound_sets": ["MaxBatch=2,MaxSignals=2"], "mutants": ["mutants/NoRelease.cfg: invariant_violation"], "sanity": ["sanity/ReachesFinish.cfg: violated"]}
        },
        {
          "name": "retry_budget",
          "kind": "theorem",
          "statement": "for every max_attempts > 0 and every outcome list, a job runs at most max_attempts times",
          "source": "docs/operations.md:31",
          "tool": "lean",
          "result": "proved",
          "theorems": [{"name": "Verification.Retry.retry_budget", "status": "proved"}, {"name": "Verification.Retry.retry_budget_mutant_false", "status": "proved"}],
          "scope": "unbounded, over the counter/guard abstraction in CORRESPONDENCE.md",
          "vacuity": {"status": "passed", "mutants": ["retry_budget_mutant_false"], "sanity": ["example witnesses for all hypotheses"]}
        }
      ]
    },
    {
      "id": "shutdown-drain",
      "title": "Queue drain on shutdown",
      "files": ["jobrunner/drain.py"],
      "tools": ["tla"],
      "status": "not_modeled",
      "reason": "TLC timed out at the smallest useful bounds; spec and result kept under verification/models/shutdown-drain/",
      "properties": []
    }
  ],
  "findings": [
    {
      "id": "001",
      "slug": "sigterm-strands-leases",
      "title": "SIGTERM while a lease is being logged leaves the job leased forever",
      "status": "CONFIRMED",
      "severity": "medium",
      "targets": ["lease-settlement"],
      "tools": ["tla"],
      "properties": ["LeasesSettled"],
      "locations": [{"file": "jobrunner/worker.py", "line_start": 98, "line_end": 110}],
      "repro_tests": ["verification/repro/repro_lease_settlement.py::test_sigterm_while_logging_lease"],
      "repro_command": "...",
      "guard_tests": ["verification/repro/repro_lease_settlement.py::test_clean_shutdown_releases_all"],
      "evidence": "AssertionError: LeasesSettled violated: job 'j1' is still leased after the worker exited",
      "plan": "001",
      "fix_check": "predicted",
      "file": "verification/findings/001-sigterm-strands-leases.md"
    }
  ],
  "needs_decision": [
    {"id": "D1", "summary": "...", "properties": ["..."], "repro_tests": ["..."], "reading_a": "... (evidence)", "reading_b": "... (evidence)", "question": "Should a job that a user cancels count against its retry budget?"}
  ],
  "model_only": [
    {"target": "...", "property": "...", "trace_summary": "...", "why_not_reproducible": "...", "next_step": "refine model | modeling gap"}
  ],
  "rejections": [
    {"target": "...", "summary": "...", "status": "BY-DESIGN", "evidence": "docs/operations.md:40 says cancelled jobs are never retried"},
    {"target": "...", "summary": "...", "status": "OUT-OF-SCOPE", "assumption": "the queue service eventually responds"}
  ],
  "refuted_claims": [
    {"claim": "comment at queue.py:88 says a lost lease 'can never be re-leased twice'", "result": "the model shows a double lease when two workers race the expiry", "evidence": "verification/models/lease-settlement/results/Spec.json"}
  ],
  "plans": [
    {"id": "001", "file": "plans/001-release-leases-on-shutdown.md", "findings": ["001", "003"]}
  ],
  "coverage_gaps": [
    {"area": "...", "reason": "..."}
  ]
}
```

Field rules:
- `targets[]` lists the selected targets: every target the run committed to model (top 3 by default).
  A `modeled` target has at least one TLA+ property whose `result` is `violated` or `no_violation_within_bounds`, or a Lean property that is `proved` or `violated` by a proved negation theorem listed in `verification/lean/results.json`; a property that is only `unproved`, `vacuous`, or `not_checked` does not make a target modeled, and a Lean property never uses `no_violation_within_bounds`.
  A Lean property may cite only theorems of its own target: the theorem name or its module in `verification/lean/results.json` must mention the target id (ignoring case and punctuation).
  A `not_modeled` entry is an attempt: it has a non-empty `reason` and keeps its `.tla` and at least one `run_tlc.sh` `results/*.json` on disk, or for a Lean-only target a `.lean` file under `verification/lean/` that names the target (in its path or contents) and a parseable `verification/lean/results.json` with a `result` field.
  Every run except `reconcile` needs at least one modeled target, a `reconcile` run lists at least one target, and a scoped run's named path or symbol must be covered by a modeled target's `files`.
  Ranked candidates that were not selected go in README "Targets considered" and `coverage_gaps`, not in `targets[]`.
- `invocation` is the command exactly as given; annotations go in `defaults_taken`.
  A scoped run names its target by an existing file path or a dotted symbol (`/verify-formally src/worker.py`, `/verify-formally Worker.drain`), and a modeled target's `files` must cover it (if a symbol is not found as a definition in the target's files, name the file path instead); any other free text is read as a full run, which lists at least 3 selected targets.
- Result JSONs under `results/` are written only by `run_tlc.sh --out`, which also writes `<stem>.log` beside them, and the log must carry TLC's banner and the spec name, while the result's `command` names the same spec and cfg as its `spec` and `cfg` fields.
  `check_run.py` re-runs every cited `pass` or violation cfg and replays every other result (and every attempt) against its log, so an edited or hand-written result fails the run.
  A cited cfg that cannot re-check within min(1800s, max(120s, 4 x its original elapsed time x workers)) fails, so keep the models small enough.
  A command without a numeric `--workers` (the default, auto) counts as the machine's core count.
- `baseline.repro_runner` is run from the repo root by `check_run.py`, per test id (`file::test`), in one of two forms:
  - pytest: a command prefix such as `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider`; `check_run.py` appends the `file::test` id plus `--junitxml` and `--tb=long`.
  - Any other runner: a command containing `{file}`, `{test}` and/or `{dir}` placeholders, which `check_run.py` substitutes (shell-quoted) from the test id: the file path, the test name, and the file's directory.
    Example: `go test -v -tags verify_repro ./{dir} -run '^{test}$'`; keep Go's `-run` anchored as shown.
  - Never put placeholders in a pytest runner: it fails closed with a confusing "no parseable junit" error.
  - A runner that is neither pytest nor contains a placeholder is rejected (C10 ERROR).
  - For placeholder runners the output must name each test it runs (`go test -v`, `vitest --reporter=verbose`), and a repro's failure output must name the violated property.
- `baseline.collect_command` lists what the normal suite collects (`... -m pytest --collect-only -q`); its output must not mention `verification/repro`.
- Finding `status` is exactly one of `CONFIRMED`, `NEEDS-DECISION`, `MODEL-ONLY`, `BY-DESIGN`, `OUT-OF-SCOPE`, `FIXED` (set only by `reconcile`); property `tool` is exactly `tla` or `lean`; target `status` is exactly `modeled` or `not_modeled`.
- `baseline.dirty_files` lists the paths `git status --porcelain` showed before the run started (empty for a clean tree); `check_run.py` ignores changes to exactly those paths.
- Property `result`: `violated`, `no_violation_within_bounds`, `proved` (Lean, audit `proved`), `unproved`, `vacuous`, `not_checked` (with a reason in `note`).
- Lean `theorems[].status` is copied from the audit: `proved`, `unproved`, or `kernel_unverified` (not proved).
- `runs[].constants`, `distinct_states`, and `cfg` are copied from the result JSON, never typed by hand; `exact_states` is false for violating runs with more than one worker.
- `locations` point at the code that must change for the fix, most important first.
- Every CONFIRMED finding has `repro_tests`, `repro_command`, and `evidence` copied from an actual run, and `fix_check` says whether the fix was `observed` in a prototype or `predicted` by the fixed model.
- A property with `vacuity.status` other than `passed` cannot support any finding or any "no violation" claim.

## 4. `verification/README.md`

When the last `check_run.py` did not exit 0, line 1 of the file is exactly `INCOMPLETE`, followed by the validator output in a fenced block, above `# Verification`; omit both when it exited 0.

```markdown
# Verification

Generated by the verify-formally skill on <date> at commit `<SHA>` (<clean | dirty>).
Invocation: `<invocation>`.
Baseline: `<test command>` -> <counts>; lint: `<command>` -> <result>.

## Defaults taken

- <each default, e.g. "non-interactive: modeled the top 3 targets">

## Findings

| # | Finding | Target | Tool | Status | Severity | Repro test | Evidence |

## Needs a maintainer decision

- D1: <question>, with the evidence for each reading and the repro test that shows the behavior.

## Properties checked

| Target | Property | Source | Tool | Result | Bounds / scope | States | Vacuity |

## Refuted claims

- <claim> (<location>): <what the model shows>.

## Targets considered

The full ranked table from target selection, including rejected candidates with one line each.

## Rejected, model-only, out of scope

- <item>: BY-DESIGN because <evidence>.
- <item>: MODEL-ONLY because <divergence>; next step <...>.
- <item>: OUT-OF-SCOPE because it requires <assumption violated>.

## Not modeled (coverage gaps)

- <area>: <reason>.

## Re-running

`verification/rerun.sh` re-runs every cfg and the Lean audit; repro tests run with `<command>`.
```

## 5. Subagent result: `verification/models/<slug>/RESULT.md`

Subagents write this file before replying, and reply with the same content:

```
TARGET: <slug>
PROPERTIES:
- <name> (<kind>, <tool>, source: <...>): <result>; bounds <constants>; states <n>; vacuity <passed|failed> via <mutant> and <sanity cfgs>
FIX FLAGS:
- <flag>: models <exact code change>; <cfg> -> <result>
COUNTEREXAMPLES:
- <property>: <3-8 line condensed trace with code locations>
  REPRO: <test id> via `<command>` -> <fails|passes>; failure: <one line>
  CLASSIFICATION: CONFIRMED | NEEDS-DECISION | MODEL-ONLY | BY-DESIGN | OUT-OF-SCOPE (<evidence>)
GUARD TESTS: <ids> -> pass
FILES WRITTEN: <list>
OPEN QUESTIONS: <anything the advisor must decide>
```
