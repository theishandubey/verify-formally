#!/usr/bin/env bash
# Usage: run_example.sh
# End-to-end self-check for the retry-guard-reset example; exits 0 only if every check below
# holds. Never writes into this directory: TLC runs here without --out (their JSON is only
# printed, never saved), and the rerun.sh / lean_audit.sh self-checks below each run inside a
# scratch copy under ${TMPDIR:-/tmp}, discarded afterward. To regenerate the committed result
# JSONs themselves, run ./regenerate.sh instead (a separate, maintainer-only tool).
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scripts_dir="$(cd "$here/../../scripts" && pwd)"
models="$here/verification/models/retry-guard"
lean="$here/verification/lean"
repro="$here/verification/repro/repro_retry_guard_reset.py"
venv="$here/.venv"

rows=()
ok=1
tmp_dirs=()

record() {
  rows+=("$1|$2|$3")
  [ "$2" = PASS ] || ok=0
}

cleanup() {
  for d in "${tmp_dirs[@]:-}"; do
    [ -n "$d" ] && rm -rf "$d"
  done
}
trap cleanup EXIT

echo "== toolchain =="
if "$scripts_dir/check_toolchain.sh" > /tmp/rgr_toolchain.$$ 2>&1; then
  record "toolchain" PASS "all required tools present"
else
  record "toolchain" FAIL "see $scripts_dir/check_toolchain.sh output"
  cat /tmp/rgr_toolchain.$$ >&2
fi
rm -f /tmp/rgr_toolchain.$$

echo "== TLC: buggy safety (RetryGuard.cfg) =="
safety_json="$("$scripts_dir/run_tlc.sh" "$models/RetryGuard.tla" "$models/RetryGuard.cfg")"
safety_result="$(printf '%s\n' "$safety_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"])')"
safety_violated="$(printf '%s\n' "$safety_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["violated"])')"
if [ "$safety_result" = "invariant_violation" ] && [ "$safety_violated" = "BoundedAttempts" ]; then
  record "tlc-buggy-safety" PASS "invariant_violation of BoundedAttempts, as expected"
else
  record "tlc-buggy-safety" FAIL "got result=$safety_result violated=$safety_violated"
fi

echo "== TLC: buggy liveness (RetryGuardLiveness.cfg) =="
live_json="$("$scripts_dir/run_tlc.sh" "$models/RetryGuard.tla" "$models/RetryGuardLiveness.cfg")"
live_result="$(printf '%s\n' "$live_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"])')"
if [ "$live_result" = "property_violation" ]; then
  record "tlc-buggy-liveness" PASS "property_violation, as expected"
else
  record "tlc-buggy-liveness" FAIL "got result=$live_result"
fi

if [ ! -x "$venv/bin/python" ] || ! "$venv/bin/python" -c 'import pytest, hypothesis' 2>/dev/null; then
  echo "== bootstrapping venv =="
  uv="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"
  [ -x "$venv/bin/python" ] || "$uv" venv "$venv" > /dev/null 2>&1
  "$uv" pip install --python "$venv/bin/python" pytest hypothesis > /dev/null 2>&1
fi

echo "== pytest: buggy retry.py =="
buggy_out="$(PYTHONDONTWRITEBYTECODE=1 "$venv/bin/python" -m pytest -q -p no:cacheprovider "$repro" 2>&1)"
buggy_exit=$?
if [ "$buggy_exit" -ne 0 ] && printf '%s\n' "$buggy_out" | grep -q "BoundedAttempts violated"; then
  record "pytest-buggy" PASS "alternating-failure test fails on BoundedAttempts, as expected"
else
  record "pytest-buggy" FAIL "expected a BoundedAttempts failure; got exit=$buggy_exit"
  printf '%s\n' "$buggy_out" >&2
fi

echo "== pytest: fixed retry.py (fix.patch applied in a temp copy) =="
fixed_work="$(mktemp -d "${TMPDIR:-/tmp}/retry_guard_reset_fixed.XXXXXX")"
tmp_dirs+=("$fixed_work")
mkdir -p "$fixed_work/verification/repro"
cp "$here/retry.py" "$here/fix.patch" "$fixed_work/"
cp "$repro" "$fixed_work/verification/repro/"
if (cd "$fixed_work" && patch -p1 < fix.patch) > /tmp/rgr_patch.$$ 2>&1; then
  fixed_out="$(PYTHONDONTWRITEBYTECODE=1 "$venv/bin/python" -m pytest -q -p no:cacheprovider "$fixed_work/verification/repro/repro_retry_guard_reset.py" 2>&1)"
  fixed_exit=$?
  if [ "$fixed_exit" -eq 0 ]; then
    record "pytest-fixed" PASS "all tests pass after fix.patch"
  else
    record "pytest-fixed" FAIL "expected all tests to pass; exit=$fixed_exit"
    printf '%s\n' "$fixed_out" >&2
  fi
else
  record "pytest-fixed" FAIL "fix.patch did not apply"
  cat /tmp/rgr_patch.$$ >&2
fi
rm -f /tmp/rgr_patch.$$

echo "== TLC: fixed, MaxErrors=3,MaxTimeouts=3 (RetryGuardFixed.cfg) =="
fixed_json="$("$scripts_dir/run_tlc.sh" "$models/RetryGuard.tla" "$models/RetryGuardFixed.cfg")"
fixed_result="$(printf '%s\n' "$fixed_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"])')"
if [ "$fixed_result" = "pass" ]; then
  record "tlc-fixed" PASS "safety + liveness both pass"
else
  record "tlc-fixed" FAIL "got result=$fixed_result"
fi

echo "== TLC: fixed, MaxErrors=2,MaxTimeouts=2 (RetryGuardFixedSmallBudget.cfg) =="
fixed2_json="$("$scripts_dir/run_tlc.sh" "$models/RetryGuard.tla" "$models/RetryGuardFixedSmallBudget.cfg")"
fixed2_result="$(printf '%s\n' "$fixed2_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"])')"
if [ "$fixed2_result" = "pass" ]; then
  record "tlc-fixed-small-budget" PASS "second bound set: safety + liveness both pass"
else
  record "tlc-fixed-small-budget" FAIL "got result=$fixed2_result"
fi

echo "== TLC: mutant vacuity check, BoundedAttempts (mutants/RetryGuardFixedMutant.cfg) =="
mutant_json="$("$scripts_dir/run_tlc.sh" "$models/RetryGuard.tla" "$models/mutants/RetryGuardFixedMutant.cfg")"
mutant_result="$(printf '%s\n' "$mutant_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"])')"
if [ "$mutant_result" != "pass" ]; then
  record "tlc-mutant-attempts" PASS "mutant (guard weakened to >) fails again, result=$mutant_result"
else
  record "tlc-mutant-attempts" FAIL "mutant unexpectedly passed; BoundedAttempts may be vacuous"
fi

echo "== TLC: mutant vacuity check, Termination (mutants/RetryGuardTerminationMutant.cfg) =="
term_mutant_json="$("$scripts_dir/run_tlc.sh" "$models/RetryGuard.tla" "$models/mutants/RetryGuardTerminationMutant.cfg")"
term_mutant_result="$(printf '%s\n' "$term_mutant_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"])')"
if [ "$term_mutant_result" != "pass" ]; then
  record "tlc-mutant-termination" PASS "mutant (increment skipped) fails again, result=$term_mutant_result"
else
  record "tlc-mutant-termination" FAIL "mutant unexpectedly passed; Termination may be vacuous"
fi

echo "== TLC: sanity checks (sanity/*.cfg) =="
sanity_ok=1
sanity_detail=""
for cfg in ReachesGaveUp ReachesGaveUpFixed ReachesGaveUpFixedSmall; do
  sanity_json="$("$scripts_dir/run_tlc.sh" "$models/RetryGuard.tla" "$models/sanity/$cfg.cfg")"
  sanity_result="$(printf '%s\n' "$sanity_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"])')"
  if [ "$sanity_result" != "invariant_violation" ]; then
    sanity_ok=0
    sanity_detail="$sanity_detail $cfg=$sanity_result"
  fi
done
if [ "$sanity_ok" = 1 ]; then
  record "tlc-sanity" PASS "all three SanityReachesGaveUp cfgs violated, as expected (the state is reachable at every reported bound set)"
else
  record "tlc-sanity" FAIL "unexpected result(s):$sanity_detail"
fi

echo "== TLC: per-plan check (Plan001.cfg) =="
plan_json="$("$scripts_dir/run_tlc.sh" "$models/RetryGuard.tla" "$models/Plan001.cfg")"
plan_result="$(printf '%s\n' "$plan_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"])')"
if [ "$plan_result" = "pass" ]; then
  record "tlc-plan001" PASS "Plan001.cfg passes, matching plans/001-add-attempt-budget.md's done criteria"
else
  record "tlc-plan001" FAIL "got result=$plan_result"
fi

echo "== lean_audit.sh (in a scratch copy) =="
lean_check_work="$(mktemp -d "${TMPDIR:-/tmp}/retry_guard_reset_lean.XXXXXX")"
tmp_dirs+=("$lean_check_work")
cp -Rp "$lean" "$lean_check_work/lean"
lean_json="$("$scripts_dir/lean_audit.sh" "$lean_check_work/lean" --out "$lean_check_work/lean/results.json")"
lean_exit=$?
lean_result="$(printf '%s\n' "$lean_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"])')"
if [ "$lean_exit" -eq 0 ] && [ "$lean_result" = "proved" ]; then
  record "lean-audit" PASS "all theorems proved, no forbidden hits (scratch copy only; verification/lean on disk is untouched)"
else
  record "lean-audit" FAIL "got result=$lean_result exit=$lean_exit"
fi

# A scratch git repository laid out like the project this example stands for (retry.py at its
# root), so rerun.sh's code-level checks can run, including run_code_check.py --mutant.
make_scratch_repo() {
  local dir="$1"
  git init -q "$dir"
  git -C "$dir" config user.email "verify-example@example.com"
  git -C "$dir" config user.name "verify-formally example"
  printf '.venv\n' > "$dir/.gitignore"
  cp "$here/retry.py" "$here/fix.patch" "$dir/"
  git -C "$dir" add retry.py fix.patch .gitignore
  git -C "$dir" commit -q -m "add call_with_retry retry loop" > /dev/null
}

echo "== rerun.sh (regenerates every result in a scratch git repository; committed files are untouched) =="
rerun_work="$(mktemp -d "${TMPDIR:-/tmp}/retry_guard_reset_rerun.XXXXXX")"
tmp_dirs+=("$rerun_work")
make_scratch_repo "$rerun_work"
cp -Rp "$here/verification" "$rerun_work/verification"
ln -s "$venv" "$rerun_work/.venv"
rm -f "$rerun_work"/verification/models/*/results/*.json
VERIFY_SKILL_DIR="$(cd "$scripts_dir/.." && pwd)" "$rerun_work/verification/rerun.sh" > /tmp/rgr_rerun.$$ 2>&1
rerun_status=$?
if [ "$rerun_status" -eq 0 ]; then
  record "rerun-sh" FAIL "every check passed in the scratch copy, but the buggy/mutant/sanity checks are supposed to fail"
elif grep -rq '"result": "error"\|"result": "timeout"' "$rerun_work/verification/models"/*/results/*.json "$rerun_work/verification/lean/results.json" 2>/dev/null \
    || [ ! -f "$rerun_work/verification/state-diagram.html" ]; then
  record "rerun-sh" FAIL "rerun reported an error/timeout result or wrote no state diagram; see output"
  cat /tmp/rgr_rerun.$$ >&2
else
  record "rerun-sh" PASS "buggy/mutant/sanity checks failed as expected, fixed/Plan001 cfgs passed, diagram rendered (scratch copy only; verification/ on disk is untouched)"
fi
rm -f /tmp/rgr_rerun.$$

echo "== code-level checks (results from the rerun.sh scratch copy) =="
code_detail="$(python3 - "$rerun_work/verification/models/retry-guard/results" <<'PYEOF'
import json, os, sys
want = {"CallsBounded": "violation", "StreakGivesUp": "pass",
        "sanity-StreakGivesUp": "violation", "mutant-error-guard-off-by-one": "violation"}
bad = []
for stem, expected in want.items():
    try:
        got = json.load(open(os.path.join(sys.argv[1], stem + ".json"))).get("result")
    except (OSError, ValueError) as e:
        got = "unreadable"
    if got != expected:
        bad.append("%s=%s (want %s)" % (stem, got, expected))
print("; ".join(bad))
sys.exit(1 if bad else 0)
PYEOF
)"
if [ $? -eq 0 ]; then
  record "code-level" PASS "CallsBounded violated on the real code; StreakGivesUp passes, its sanity test fails, and the real-code mutant is caught"
else
  record "code-level" FAIL "$code_detail"
fi

echo "== check_run.py against a fresh git copy of the committed results =="
check_work="$(mktemp -d "${TMPDIR:-/tmp}/retry_guard_reset_checkrun.XXXXXX")"
tmp_dirs+=("$check_work")
make_scratch_repo "$check_work"
check_commit="$(git -C "$check_work" rev-parse HEAD)"

cp -Rp "$here/verification" "$check_work/verification"
cp -Rp "$here/plans" "$check_work/plans"
# git checkout gives files arbitrary mtimes, so committed results can look older than their specs/cfgs.
find "$check_work/verification/models" -path '*/results/*' -type f -exec touch {} +
ln -s "$venv" "$check_work/.venv"

python3 - "$check_work" "$check_commit" <<'PYEOF'
import json, sys
work, sha = sys.argv[1], sys.argv[2]
path = work + "/verification/findings.json"
with open(path) as f:
    data = json.load(f)
data["commit"] = sha
with open(path, "w") as f:
    json.dump(data, f, indent=2)
PYEOF

# The page stamps the commit it was rendered at; the scratch repository's commit is new.
python3 "$scripts_dir/render_diagram.py" "$check_work" > /dev/null

check_out="$(python3 "$scripts_dir/check_run.py" "$check_work" 2>&1)"
check_exit=$?
if [ "$check_exit" -eq 0 ]; then
  record "check-run" PASS "check_run.py exited 0 against the committed results, re-stamped at $check_commit"
else
  record "check-run" FAIL "check_run.py exited $check_exit"
  printf '%s\n' "$check_out" >&2
fi

echo
printf '%-24s %-6s %s\n' "CHECK" "RESULT" "DETAIL"
for row in "${rows[@]}"; do
  IFS='|' read -r name status detail <<<"$row"
  printf '%-24s %-6s %s\n' "$name" "$status" "$detail"
done

[ "$ok" = 1 ] && exit 0 || exit 1
