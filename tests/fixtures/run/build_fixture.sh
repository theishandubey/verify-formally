#!/usr/bin/env bash
# Usage: build_fixture.sh <dest-dir> <venv-python>
# Builds a tiny, self-contained fake verify-formally skill run in <dest-dir>: a git repo with a
# real lost-release bug, a TLA+ model checked with run_tlc.sh, a pytest repro, and the
# findings.json / README / rerun.sh / plan that check_run.py validates. Fresh build only:
# <dest-dir> must not already exist (this script does not support re-running against one).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scripts_dir="$(cd "$here/../../../skills/verify-formally/scripts" && pwd)"
dest="$1"
venv_python="$2"

mkdir -p "$dest"
cd "$dest"

git init -q
git config user.email "test@example.com"
git config user.name "Test"

mkdir -p demo
cat > demo/worker.py <<'EOF'
class Queue:
    def __init__(self, job_ids):
        self._pending = list(job_ids)
        self.leased = set()

    def has_pending(self):
        return len(self._pending) > 0

    def lease(self):
        job_id = self._pending.pop(0)
        self.leased.add(job_id)
        return job_id

    def release(self, job_id):
        self.leased.discard(job_id)


def drain(queue, stop_after):
    leased_here = []
    while queue.has_pending():
        job_id = queue.lease()
        leased_here.append(job_id)
        if len(leased_here) == stop_after:
            for early_id in leased_here[:-1]:
                queue.release(early_id)
            return leased_here
    for job_id in leased_here:
        queue.release(job_id)
    return leased_here
EOF

printf '.venv\n' > .gitignore
git add demo/worker.py .gitignore
git commit -q -m "add job drain loop"
commit_sha="$(git rev-parse HEAD)"
ln -s "$(dirname "$(dirname "$venv_python")")" .venv

mkdir -p verification/models/job-drain/mutants verification/models/job-drain/sanity \
  verification/models/job-drain/results verification/repro verification/lean plans

cat > verification/models/job-drain/JobDrain.tla <<'EOF'
---- MODULE JobDrain ----
EXTENDS Naturals

CONSTANTS StopAfter, Fixed, MutantNoRelease

VARIABLES leasedCount, outstanding, draining

TypeOK == leasedCount \in 0..StopAfter /\ outstanding \in 0..StopAfter /\ draining \in BOOLEAN

Init == leasedCount = 0 /\ outstanding = 0 /\ draining = TRUE

ReleasedOnStop ==
  IF MutantNoRelease THEN 0
  ELSE IF Fixed THEN outstanding
  ELSE IF outstanding > 0 THEN outstanding - 1 ELSE 0

Lease ==
  /\ draining
  /\ leasedCount < StopAfter
  /\ leasedCount' = leasedCount + 1
  /\ outstanding' = outstanding + 1
  /\ UNCHANGED draining

Stop ==
  /\ draining
  /\ leasedCount = StopAfter
  /\ outstanding' = outstanding - ReleasedOnStop
  /\ draining' = FALSE
  /\ UNCHANGED leasedCount

Done ==
  /\ ~draining
  /\ UNCHANGED <<leasedCount, outstanding, draining>>

Next == Lease \/ Stop \/ Done

Spec == Init /\ [][Next]_<<leasedCount, outstanding, draining>>

LeasesReleased == ~draining => outstanding = 0

SanityAlwaysDraining == draining
====
EOF

cat > verification/models/job-drain/Spec.cfg <<'EOF'
SPECIFICATION Spec
INVARIANT LeasesReleased
CONSTANTS
  StopAfter = 3
  Fixed = FALSE
  MutantNoRelease = FALSE
EOF

cat > verification/models/job-drain/SpecFixed.cfg <<'EOF'
SPECIFICATION Spec
INVARIANT LeasesReleased
CONSTANTS
  StopAfter = 3
  Fixed = TRUE
  MutantNoRelease = FALSE
EOF

cat > verification/models/job-drain/mutants/MutantFixedNoRelease.cfg <<'EOF'
SPECIFICATION Spec
INVARIANT LeasesReleased
CONSTANTS
  StopAfter = 3
  Fixed = TRUE
  MutantNoRelease = TRUE
EOF

cat > verification/models/job-drain/sanity/SanityAlwaysDraining.cfg <<'EOF'
SPECIFICATION Spec
INVARIANT SanityAlwaysDraining
CONSTANTS
  StopAfter = 3
  Fixed = FALSE
  MutantNoRelease = FALSE
EOF

cat > verification/models/job-drain/sanity/SanityAlwaysDrainingFixed.cfg <<'EOF'
SPECIFICATION Spec
INVARIANT SanityAlwaysDraining
CONSTANTS
  StopAfter = 3
  Fixed = TRUE
  MutantNoRelease = FALSE
EOF

mkdir -p verification/models/retry-budget/results verification/models/shutdown-drain/results
sed 's/MODULE Syntax/MODULE RetryBudget/' "$here/../tlc/Syntax.tla" \
  > verification/models/retry-budget/RetryBudget.tla
cp "$here/../tlc/Syntax.cfg" verification/models/retry-budget/RetryBudget.cfg
sed 's/MODULE Pass/MODULE ShutdownDrain/' "$here/../tlc/Pass.tla" \
  > verification/models/shutdown-drain/ShutdownDrain.tla
cp "$here/../tlc/Pass.cfg" verification/models/shutdown-drain/ShutdownDrain.cfg

run_tlc="$scripts_dir/run_tlc.sh"
model_dir="verification/models/job-drain"
set +e
"$run_tlc" "$model_dir/JobDrain.tla" "$model_dir/Spec.cfg" \
  --out "$model_dir/results/Spec.json" --quiet
"$run_tlc" "$model_dir/JobDrain.tla" "$model_dir/SpecFixed.cfg" \
  --out "$model_dir/results/SpecFixed.json" --quiet
"$run_tlc" "$model_dir/JobDrain.tla" "$model_dir/mutants/MutantFixedNoRelease.cfg" \
  --out "$model_dir/results/MutantFixedNoRelease.json" --quiet
"$run_tlc" "$model_dir/JobDrain.tla" "$model_dir/sanity/SanityAlwaysDraining.cfg" \
  --out "$model_dir/results/SanityAlwaysDraining.json" --quiet
"$run_tlc" "$model_dir/JobDrain.tla" "$model_dir/sanity/SanityAlwaysDrainingFixed.cfg" \
  --out "$model_dir/results/SanityAlwaysDrainingFixed.json" --quiet
"$run_tlc" verification/models/retry-budget/RetryBudget.tla \
  verification/models/retry-budget/RetryBudget.cfg \
  --out verification/models/retry-budget/results/RetryBudget.json --quiet
"$run_tlc" verification/models/shutdown-drain/ShutdownDrain.tla \
  verification/models/shutdown-drain/ShutdownDrain.cfg \
  --out verification/models/shutdown-drain/results/ShutdownDrain.json --quiet
set -e

cat > verification/models/job-drain/CORRESPONDENCE.md <<'EOF'
Correspondence between JobDrain.tla and demo/worker.py.

## Variables

| Model | `worker.py` | Notes |
|---|---|---|
| `leasedCount` | `len(leased_here)` (demo/worker.py:19, updated at demo/worker.py:22) | Number of jobs leased so far in this `drain` call. |
| `outstanding` | `len(queue.leased)` (demo/worker.py:4, updated at demo/worker.py:11 and demo/worker.py:15) | Jobs the queue still considers leased (not yet released). |
| `draining` | still inside the `while queue.has_pending():` loop (demo/worker.py:20) | `draining = FALSE` once `drain` returns. |

## Actions

| Model action | `worker.py` | Notes |
|---|---|---|
| `Lease` | one pass of the loop body, `queue.lease()` (demo/worker.py:21) | Fires while `draining` and `leasedCount < StopAfter`. |
| `Stop` | `len(leased_here) == stop_after` becomes true and `drain` returns early (demo/worker.py:23-26) | Models the early stop; `leasedCount` no longer changes. |
| `ReleasedOnStop` (buggy, `Fixed = FALSE`) | `for early_id in leased_here[:-1]: queue.release(early_id)` (demo/worker.py:24-25) | The bug: releases every leased job except the last one. |
| `ReleasedOnStop` (fixed, `Fixed = TRUE`) | releasing every job in `leased_here`, the intended fix | Models replacing `leased_here[:-1]` with `leased_here` at demo/worker.py:24. |

## Constants

| Model constant | `worker.py` default | Notes |
|---|---|---|
| `StopAfter` | `stop_after` argument | Bound checked at `Spec.cfg`: `StopAfter = 3`. |
| `Fixed` | n/a | `FALSE` = code as shipped; `TRUE` = the one-line fix. |
| `MutantNoRelease` | n/a | Vacuity mutant only: `TRUE` drops every release so the invariant must fail even in the fixed model. |

## Abstractions

- The queue running out of jobs before `stop_after` is reached (the `while` loop exiting normally, demo/worker.py:27-29) is not modeled: that path already releases every leased job, so it cannot exhibit the bug; only the early-stop path (demo/worker.py:23-26) can strand a job.
- `outstanding` is bounded in `TypeOK` by `StopAfter` because a `drain` call leases at most `StopAfter` jobs before the early stop fires.
- The property `LeasesReleased == ~draining => outstanding = 0` states the intended contract: "after drain returns, no job is left leased."
EOF

cat > verification/repro/repro_demo.py <<PYEOF
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "demo"))

from worker import Queue, drain


def test_drain_releases_all_jobs_on_early_stop():
    queue = Queue(["j1", "j2", "j3"])
    drain(queue, stop_after=2)
    assert queue.leased == set(), (
        "LeasesReleased violated: jobs still leased after drain: %r" % queue.leased
    )


def test_drain_without_early_stop_releases_all_jobs():
    queue = Queue(["j1", "j2"])
    drain(queue, stop_after=5)
    assert queue.leased == set()
PYEOF

cat > verification/lean/results.json <<'EOF'
{
  "result": "proved",
  "theorems": [
    {"name": "JobDrain.leases_released_after_drain", "status": "proved"}
  ]
}
EOF

cat > verification/rerun.sh <<'EOF'
#!/usr/bin/env bash
echo "rerun placeholder for the job-drain fixture"
EOF
chmod +x verification/rerun.sh

venv_bin="$(dirname "$venv_python")"
test_command="PATH=$venv_bin:\$PATH PYTHONDONTWRITEBYTECODE=1 $venv_python -m pytest -q -p no:cacheprovider"
repro_runner="PATH=$venv_bin:\$PATH PYTHONDONTWRITEBYTECODE=1 $venv_python -m pytest -q -p no:cacheprovider"
collect_command="PATH=$venv_bin:\$PATH $venv_python -m pytest --collect-only -q"

cat > verification/README.md <<EOF
# Verification

Generated by the verify-formally skill (fixture) at commit \`$commit_sha\` (clean).
Invocation: \`/verify-formally (non-interactive)\`.
Baseline: \`$test_command\` -> 0 passed; lint: not run.

## Defaults taken

- non-interactive: modeled 1 target (job-drain); fewer_targets_reason: fixture keeps the run small on purpose.

## Findings

| # | Finding | Target | Tool | Status | Severity | Repro test | Evidence |
|---|---|---|---|---|---|---|---|
| 001 | jobs-stranded-on-early-stop | job-drain | tla | CONFIRMED | medium | repro_demo.py::test_drain_releases_all_jobs_on_early_stop | LeasesReleased violated |

## Needs a maintainer decision

- none

## Properties checked

| Target | Property | Source | Tool | Result | Bounds / scope | States | Vacuity |
|---|---|---|---|---|---|---|---|
| job-drain | LeasesReleased | demo/worker.py:20 | tla | violated | StopAfter=3 | 5 | passed |

## Refuted claims

- none

## Targets considered

- job-drain: the only target modeled in this fixture.

## Rejected, model-only, out of scope

- none

## Not modeled (coverage gaps)

- retry-budget: first spec draft does not parse; abandoned within the budget.
- shutdown-drain: passing run has no vacuity check and no CORRESPONDENCE.md; supports no claim.
- everything except demo/worker.py: this is a minimal fixture, not a real run.

## Re-running

\`verification/rerun.sh\` re-runs every cfg and the Lean audit; repro tests run with \`$repro_runner\`.
EOF

cat > plans/001-fix-jobs-stranded-on-early-stop.md <<EOF
# Plan 001: Release every leased job when drain() stops early

> **Executor instructions**: Follow this plan step by step.

## Status

- **Findings closed**: \`verification/findings/001-jobs-stranded-on-early-stop.md\` (CONFIRMED, severity medium)
- **Fix check**: predicted (fixed model only)
- **Planned at**: commit \`$commit_sha\`, 2026-09-25

## Why this matters

\`drain\` leaves the most recently leased job stranded when it stops early, because it releases
\`leased_here[:-1]\` instead of \`leased_here\`.

## The proof that it is broken

- Repro test: \`verification/repro/repro_demo.py::test_drain_releases_all_jobs_on_early_stop\`.
- Model: \`verification/models/job-drain/JobDrain.tla\`; \`Spec.cfg\` (buggy) reports
  \`invariant_violation\` of \`LeasesReleased\`; \`SpecFixed.cfg\` (\`Fixed = TRUE\`) passes.

## Done criteria

- \`verification/repro/repro_demo.py::test_drain_releases_all_jobs_on_early_stop\` passes.
- The full test suite passes.
- \`verification/models/job-drain/SpecFixed.cfg\` passes \`LeasesReleased\` at \`StopAfter=3\`.
EOF

python3 - "$commit_sha" "$test_command" "$repro_runner" "$collect_command" <<'PYEOF' > verification/findings.json
import json, sys

commit, test_command, repro_runner, collect_command = sys.argv[1:5]

data = {
    "schema": "verify-formally-findings/2",
    "commit": commit,
    "tree": "clean",
    "generated_at": "2026-09-25",
    "invocation": "/verify-formally (non-interactive)",
    "defaults_taken": ["non-interactive: modeled 1 target"],
    "fewer_targets_reason": "fixture keeps the run small on purpose",
    "baseline": {
        "dirty_files": [],
        "test_command": test_command,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "repro_runner": repro_runner,
        "collect_command": collect_command,
    },
    "targets": [
        {
            "id": "job-drain",
            "title": "Jobs stranded on early stop in drain",
            "files": ["demo/worker.py"],
            "tools": ["tla", "lean"],
            "status": "modeled",
            "properties": [
                {
                    "name": "LeasesReleased",
                    "kind": "invariant",
                    "statement": "after drain returns, no job is left leased",
                    "source": "demo/worker.py:18: drain(queue, stop_after) is documented to leave the queue clean",
                    "tool": "tla",
                    "result": "violated",
                    "runs": [
                        {
                            "cfg": "verification/models/job-drain/Spec.cfg",
                            "result": "invariant_violation",
                            "constants": {"StopAfter": "3", "Fixed": "FALSE", "MutantNoRelease": "FALSE"},
                            "distinct_states": 5,
                            "exact_states": True,
                            "json": "verification/models/job-drain/results/Spec.json",
                        },
                        {
                            "cfg": "verification/models/job-drain/SpecFixed.cfg",
                            "result": "pass",
                            "constants": {"StopAfter": "3", "Fixed": "TRUE", "MutantNoRelease": "FALSE"},
                            "distinct_states": 5,
                            "exact_states": True,
                            "json": "verification/models/job-drain/results/SpecFixed.json",
                        },
                    ],
                    "vacuity": {
                        "status": "passed",
                        "bound_sets": ["StopAfter=3"],
                        "mutants": ["mutants/MutantFixedNoRelease.cfg: invariant_violation"],
                        "sanity": [
                            "sanity/SanityAlwaysDraining.cfg: invariant_violation",
                            "sanity/SanityAlwaysDrainingFixed.cfg: invariant_violation",
                        ],
                    },
                },
                {
                    "name": "job_drain_lean",
                    "kind": "theorem",
                    "statement": "for every non-empty job list, the fixed drain function leaves no job leased",
                    "source": "demo/worker.py:18",
                    "tool": "lean",
                    "result": "proved",
                    "theorems": [{"name": "JobDrain.leases_released_after_drain", "status": "proved"}],
                    "scope": "unbounded (fixture-only, not run through lake)",
                    "vacuity": {
                        "status": "passed",
                        "mutants": ["JobDrain.leases_released_after_drain_mutant_false: proved negation with witness"],
                        "sanity": ["example: exists a job list that reaches an early stop"],
                    },
                },
            ],
        },
        {
            "id": "retry-budget",
            "title": "Retry budget reset",
            "files": ["demo/worker.py"],
            "tools": ["tla"],
            "status": "not_modeled",
            "reason": "first spec draft does not parse; abandoned within the budget",
            "properties": [],
        },
        {
            "id": "shutdown-drain",
            "title": "Shutdown drain ordering",
            "files": ["demo/worker.py"],
            "tools": ["tla"],
            "status": "not_modeled",
            "reason": "passing run has no vacuity check and no CORRESPONDENCE.md; supports no claim",
            "properties": [],
        },
    ],
    "findings": [
        {
            "id": "001",
            "slug": "jobs-stranded-on-early-stop",
            "title": "drain leaves the last leased job stranded on early stop",
            "status": "CONFIRMED",
            "severity": "medium",
            "targets": ["job-drain"],
            "tools": ["tla"],
            "properties": ["LeasesReleased"],
            "locations": [{"file": "demo/worker.py", "line_start": 18, "line_end": 29}],
            "repro_tests": ["verification/repro/repro_demo.py::test_drain_releases_all_jobs_on_early_stop"],
            "repro_command": "%s verification/repro/repro_demo.py::test_drain_releases_all_jobs_on_early_stop" % repro_runner,
            "guard_tests": ["verification/repro/repro_demo.py::test_drain_without_early_stop_releases_all_jobs"],
            "evidence": "AssertionError: LeasesReleased violated: jobs still leased after drain: {'j2'}",
            "plan": "plans/001-fix-jobs-stranded-on-early-stop.md",
            "fix_check": "predicted",
            "file": "verification/findings/001-jobs-stranded-on-early-stop.md",
        }
    ],
    "needs_decision": [],
    "model_only": [],
    "rejections": [],
    "refuted_claims": [],
    "plans": [{"id": "001", "file": "plans/001-fix-jobs-stranded-on-early-stop.md", "findings": ["001"]}],
    "coverage_gaps": [{"area": "everything else", "reason": "fixture, not a real run"}],
}

json.dump(data, sys.stdout, indent=2)
print()
PYEOF

mkdir -p verification/findings
cat > verification/findings/001-jobs-stranded-on-early-stop.md <<EOF
# 001: drain leaves the last leased job stranded on early stop

- **Status**: CONFIRMED
- **Severity**: medium - operator calls drain with any stop_after
- **Targets**: job-drain (\`verification/models/job-drain/\`)
- **Tools**: TLA+
- **Properties violated**: \`LeasesReleased\` - after drain returns, no job is left leased
- **Location**: \`demo/worker.py:18-29\`
- **Commit**: \`$commit_sha\`
- **Repro tests**: \`verification/repro/repro_demo.py::test_drain_releases_all_jobs_on_early_stop\`
- **Repro command**: \`$repro_runner verification/repro/repro_demo.py::test_drain_releases_all_jobs_on_early_stop\`
- **Guard tests**: \`verification/repro/repro_demo.py::test_drain_without_early_stop_releases_all_jobs\`
- **Plan**: \`plans/001-fix-jobs-stranded-on-early-stop.md\`

## What goes wrong

\`drain\` releases \`leased_here[:-1]\` instead of \`leased_here\` when it stops early, so the most
recently leased job is never released.

## Counterexample

1. \`Lease\` (\`demo/worker.py:21\`): leasedCount increments while draining and below StopAfter.
2. At \`leasedCount == StopAfter\` the buggy release drops the last leased job, so one job stays
   outstanding forever.

Checker: \`run_tlc.sh JobDrain.tla Spec.cfg\`, constants \`StopAfter=3\`, 5 distinct states,
depth 5, result \`invariant_violation\` of \`LeasesReleased\`.

## Reproduction

\`\`\`
AssertionError: LeasesReleased violated: jobs still leased after drain: {'j2'}
\`\`\`

## Root cause

The release loop at demo/worker.py:24 slices off the last leased job with \`leased_here[:-1]\`
where the intended contract needs every job released.

## Fix direction

Change \`for early_id in leased_here[:-1]:\` to \`for early_id in leased_here:\` at
demo/worker.py:24. Model: \`SpecFixed.cfg\` (\`Fixed = TRUE\`) passes \`LeasesReleased\` at
\`StopAfter=3\` (predicted).

## Vacuity

Mutant \`mutants/MutantFixedNoRelease.cfg\` (fixed config, release dropped entirely) violates
\`LeasesReleased\`. Sanity \`sanity/SanityAlwaysDraining.cfg\` confirms \`draining\` actually
becomes false at the reported constants.
EOF

echo "$commit_sha"
