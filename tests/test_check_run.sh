#!/usr/bin/env bash
# Usage: test_check_run.sh
# Builds the fixture in tests/fixtures/run/, runs check_run.py against the good run and
# against a set of single mutations, and asserts each gives the exit code and check id we want.
# Exits 0 iff every case passes.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scripts_dir="$(cd "$here/../skills/verify-formally/scripts" && pwd)"
check_run="$scripts_dir/check_run.py"
build_fixture="$here/fixtures/run/build_fixture.sh"

python3_bin="$(command -v python3 || true)"
if [ -z "$python3_bin" ]; then
  python3_bin="/usr/local/bin/python3"
fi

venv_python="$here/../skills/verify-formally/examples/retry-guard-reset/.venv/bin/python"
if [ ! -x "$venv_python" ]; then
  echo "FAIL: fixture venv python not found or not executable: $venv_python"
  echo "run skills/verify-formally/examples/retry-guard-reset/run_example.sh once to create it"
  exit 1
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/check_run_test.XXXXXX")"
trap 'rm -rf "$work"' EXIT

good="$work/good"
bash "$build_fixture" "$good" "$venv_python" > "$work/build.log" 2>&1
if [ ! -f "$good/verification/findings.json" ]; then
  echo "FAIL: fixture build did not produce findings.json; see $work/build.log"
  cat "$work/build.log"
  exit 1
fi

pass_count=0
fail_count=0

pass() { echo "PASS: $1"; pass_count=$((pass_count + 1)); }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

make_copy() {
  local name="$1"
  local dst="$work/$name"
  # -p preserves mtimes: the result JSONs must stay newer than the spec/cfg files they were
  # produced from, or every case would trip the staleness check regardless of what it mutates.
  cp -Rp "$good" "$dst"
  echo "$dst"
}

run_checker() {
  local dir="$1"
  shift
  "$python3_bin" "$check_run" "$dir" "$@"
}

check_case() {
  local name="$1" dir="$2" expected_exit="$3" expected_check="$4" expected_substring="${5:-}"
  local out ec
  out="$(run_checker "$dir" 2>&1)"
  ec=$?
  if [ "$ec" != "$expected_exit" ]; then
    fail "$name (expected exit=$expected_exit, got exit=$ec)"
    echo "$out" | sed 's/^/    /'
    return
  fi
  if [ -n "$expected_check" ]; then
    local line
    line="$(printf '%s\n' "$out" | grep "\[ERROR\] $expected_check:" | head -1)"
    if [ -z "$line" ]; then
      fail "$name (expected an [ERROR] $expected_check line, none found)"
      echo "$out" | sed 's/^/    /'
      return
    fi
    if [ -n "$expected_substring" ] && ! printf '%s' "$line" | grep -qF "$expected_substring"; then
      fail "$name (expected [ERROR] $expected_check line to contain '$expected_substring')"
      echo "$line" | sed 's/^/    /'
      return
    fi
  fi
  pass "$name"
}

mutate() {
  local dir="$1" script="$2"
  shift 2
  "$python3_bin" - "$dir" "$@" <<PYEOF
$script
PYEOF
}

echo "== good run =="
check_case "good-run-exits-0" "$good" "0" ""

echo "== --json produces valid JSON =="
json_out="$("$python3_bin" "$check_run" "$good" --json)"
if printf '%s' "$json_out" | "$python3_bin" -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
  pass "json-output-parses"
else
  fail "json-output-parses"
  echo "$json_out" | sed 's/^/    /'
fi

echo "== usage errors =="
"$python3_bin" "$check_run" "$work/does-not-exist" >/dev/null 2>&1
ec=$?
if [ "$ec" = "2" ]; then pass "missing-repo-root-exits-2"; else fail "missing-repo-root-exits-2 (got exit=$ec)"; fi

echo "== --no-exec warns instead of skipping silently =="
noexec_out="$("$python3_bin" "$check_run" "$good" --no-exec 2>&1)"
noexec_ec=$?
if [ "$noexec_ec" = "0" ] && printf '%s' "$noexec_out" | grep -q "\[WARNING\] C10: --no-exec"; then
  pass "no-exec-warns"
else
  fail "no-exec-warns (exit=$noexec_ec)"
  echo "$noexec_out" | sed 's/^/    /'
fi

echo "== H2: fixture moved to a different directory after generation =="
moveme="$work/moveme"
bash "$build_fixture" "$moveme" "$venv_python" > "$work/moveme-build.log" 2>&1
moved="$work/moved-elsewhere"
mv "$moveme" "$moved"
check_case "moved-fixture-still-validates" "$moved" "0" ""

echo "== mutations =="

# inverted repro: asserts the buggy value, so it passes on current code
dir="$(make_copy inverted-repro)"
mutate "$dir" '
import sys
path = sys.argv[1] + "/verification/repro/repro_demo.py"
text = open(path).read()
text = text.replace(
    "assert queue.leased == set(), (",
    "assert queue.leased == {\"j2\"}, (",
)
open(path, "w").write(text)
'
check_case "inverted-repro-caught" "$dir" "1" "C10"

# repro file named test_*.py, which the project's default discovery would collect
dir="$(make_copy repro-default-discovery-name)"
mv "$dir/verification/repro/repro_demo.py" "$dir/verification/repro/test_demo.py"
mutate "$dir" '
import sys
path = sys.argv[1] + "/verification/findings.json"
text = open(path).read()
text = text.replace("repro_demo.py", "test_demo.py")
open(path, "w").write(text)
'
check_case "repro-default-discovery-name-caught" "$dir" "1" "C09"

# only 1 target in a full run, with fewer_targets_reason cleared
dir="$(make_copy no-fewer-targets-reason)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["fewer_targets_reason"] = ""
json.dump(data, open(path, "w"), indent=2)
'
check_case "too-few-targets-without-reason-caught" "$dir" "1" "C04"

# vacuity mutant with the same cfg as the buggy run (re-running the buggy config)
dir="$(make_copy mutant-equals-buggy-config)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
prop = data["targets"][0]["properties"][0]
prop["vacuity"]["mutants"] = [
    "verification/models/job-drain/results/Spec.json: invariant_violation"
]
json.dump(data, open(path, "w"), indent=2)
'
check_case "mutant-equal-to-buggy-config-caught" "$dir" "1" "C07" "same cfg as the buggy run"

# vacuity sanity list emptied
dir="$(make_copy sanity-missing)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][0]["properties"][0]["vacuity"]["sanity"] = []
json.dump(data, open(path, "w"), indent=2)
'
check_case "sanity-missing-caught" "$dir" "1" "C07" "vacuity.sanity is empty"

# stale result: touch the spec after the results were produced
dir="$(make_copy stale-result)"
sleep 1
touch "$dir/verification/models/job-drain/JobDrain.tla"
check_case "stale-result-caught" "$dir" "1" "C06" "stale result"

# source file modified outside verification/ and plans/
dir="$(make_copy source-modified)"
printf '\n# comment\n' >> "$dir/demo/worker.py"
check_case "source-file-modified-caught" "$dir" "1" "C03"

# a required README section is missing
dir="$(make_copy readme-missing-section)"
mutate "$dir" '
import sys
path = sys.argv[1] + "/verification/README.md"
text = open(path).read()
lines = text.splitlines(keepends=True)
out = []
skip = False
for line in lines:
    if line.startswith("## Not modeled"):
        skip = True
        continue
    if skip and line.startswith("## "):
        skip = False
    if not skip:
        out.append(line)
open(path, "w").write("".join(out))
'
check_case "readme-missing-section-caught" "$dir" "1" "C12"

# Lean claimed proved without results.json
dir="$(make_copy lean-no-results)"
rm -f "$dir/verification/lean/results.json"
check_case "lean-proved-without-results-caught" "$dir" "1" "C08"

echo "== C1: statuses outside the closed set no longer skip finding checks =="
dir="$(make_copy c1-bad-finding-status)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
fi = data["findings"][0]
fi["status"] = "CONFIRMED-FIXED"
fi["repro_tests"] = []
fi["guard_tests"] = []
fi["evidence"] = ""
json.dump(data, open(path, "w"), indent=2)
'
check_case "c1-bad-finding-status-caught" "$dir" "1" "C01" "not one of"

echo "== C2: property tool/target status outside the closed set are caught (via C1) =="
dir="$(make_copy c2-bad-tool)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
p = data["targets"][0]["properties"][0]
p["tool"] = "TLA+"
p["runs"] = []
p.pop("vacuity")
json.dump(data, open(path, "w"), indent=2)
'
check_case "c2-bad-tool-caught" "$dir" "1" "C01" "not one of"

echo "== C3: non-clean run results (vacuous, pass_with_warnings) are no longer accepted =="
dir="$(make_copy c3-non-clean-result)"
mutate "$dir" '
import json, sys
d = sys.argv[1]
path = d + "/verification/findings.json"
data = json.load(open(path))
data["targets"][0]["properties"][0]["runs"][1]["result"] = "pass_with_warnings"
json.dump(data, open(path, "w"), indent=2)
sp = d + "/verification/models/job-drain/results/SpecFixed.json"
s = json.load(open(sp))
s["result"] = "pass_with_warnings"
json.dump(s, open(sp, "w"))
'
check_case "c3-non-clean-result-caught" "$dir" "1" "C06" "not a clean pass or violation"

echo "== C4: vacuity mutant/sanity content is checked against the property, not just its result =="
dir="$(make_copy c4-mutant-wrong-invariant)"
mutate "$dir" '
import json, sys
d = sys.argv[1]
mp = d + "/verification/models/job-drain/results/MutantFixedNoRelease.json"
m = json.load(open(mp))
m["violated"] = "TypeOK"
json.dump(m, open(mp, "w"))
'
check_case "c4-mutant-wrong-invariant-caught" "$dir" "1" "C07" "not the property"

dir="$(make_copy c4-sanity-is-buggy-run)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][0]["properties"][0]["vacuity"]["sanity"] = ["results/Spec.json"]
json.dump(data, open(path, "w"), indent=2)
'
check_case "c4-sanity-is-buggy-run-caught" "$dir" "1" "C07" "violate a separate reachability invariant"

echo "== C5: C10 requires an actual assertion failure, not any nonzero exit =="
dir="$(make_copy c5-repro-nameerror)"
mutate "$dir" '
import sys
path = sys.argv[1] + "/verification/repro/repro_demo.py"
text = open(path).read()
text = text.replace(
    "    queue = Queue([\"j1\", \"j2\", \"j3\"])",
    "    queue = Queueue([\"j1\", \"j2\", \"j3\"])",
)
open(path, "w").write(text)
'
check_case "c5-repro-nameerror-caught" "$dir" "1" "C10" "not an assertion failure"

echo "== H1: runs[] entries without json are an ERROR, and cfg/json cannot be swapped =="
dir="$(make_copy h1-runs-missing-json)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
for r in data["targets"][0]["properties"][0]["runs"]:
    r.pop("json")
    r["result"] = "pass"
json.dump(data, open(path, "w"), indent=2)
'
check_case "h1-runs-missing-json-caught" "$dir" "1" "C06" "has no json field"

dir="$(make_copy h1-runs-json-swapped)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
runs = data["targets"][0]["properties"][0]["runs"]
runs[1]["json"] = runs[0]["json"]
runs[1]["result"] = "invariant_violation"
json.dump(data, open(path, "w"), indent=2)
'
check_case "h1-runs-json-swapped-caught" "$dir" "1" "C06" "does not match the result JSON"

echo "== H2: moved-fixture case above; also confirm cfg mismatch after a move is still caught =="
dir="$(make_copy h2-cfg-does-not-exist)"
mutate "$dir" '
import json, sys
d = sys.argv[1]
path = d + "/verification/findings.json"
data = json.load(open(path))
sp = d + "/verification/models/job-drain/results/Spec.json"
s = json.load(open(sp))
s["cfg"] = "/nonexistent/path/Spec.cfg"
json.dump(s, open(sp, "w"))
'
check_case "h2-cfg-does-not-exist-caught" "$dir" "1" "C06" "does not resolve to a real file"

echo "== H3: baseline.dirty_files is honored by C03 =="
dir="$(make_copy h3-dirty-tree-honored)"
printf '\n# pre-existing uncommitted edit\n' >> "$dir/demo/worker.py"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["baseline"]["dirty_files"] = ["demo/worker.py"]
json.dump(data, open(path, "w"), indent=2)
'
check_case "h3-dirty-tree-honored" "$dir" "0" ""

dir="$(make_copy h3-dirty-tree-not-listed)"
printf '\n# pre-existing uncommitted edit\n' >> "$dir/demo/worker.py"
check_case "h3-dirty-tree-not-listed-caught" "$dir" "1" "C03"

echo "== M1: C11 fails on a broken collect_command (exit 127) =="
dir="$(make_copy m1-collect-command-broken)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["baseline"]["collect_command"] = "/no/such/interpreter -m pytest --collect-only -q"
json.dump(data, open(path, "w"), indent=2)
'
check_case "m1-collect-command-broken-caught" "$dir" "1" "C11" "expected 0, or 5"

echo "== M2: staleness applies to mutant/sanity result JSONs too =="
dir="$(make_copy m2-mutant-result-stale)"
sleep 1
touch "$dir/verification/models/job-drain/JobDrain.tla"
mutate "$dir" '
import os, sys
d = sys.argv[1]
os.utime(d + "/verification/models/job-drain/results/Spec.json", None)
os.utime(d + "/verification/models/job-drain/results/SpecFixed.json", None)
'
check_case "m2-mutant-result-stale-caught" "$dir" "1" "C07" "stale result"

echo "== M3: a passing run needs a sanity result at the same constants =="
dir="$(make_copy m3-sanity-missing-for-pass-run)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][0]["properties"][0]["vacuity"]["sanity"] = [
    "sanity/SanityAlwaysDraining.cfg: invariant_violation"
]
json.dump(data, open(path, "w"), indent=2)
'
check_case "m3-sanity-missing-for-pass-run-caught" "$dir" "1" "C07" "no sanity result shares constants"

echo "== M5: C03 checks both sides of a rename, and gates plans/ by name/tracked-state =="
dir="$(make_copy m5-git-mv-source-into-verification)"
(cd "$dir" && git mv demo/worker.py verification/worker.py >/dev/null 2>&1)
check_case "m5-git-mv-source-into-verification-caught" "$dir" "1" "C03"

dir="$(make_copy m5-tracked-plans-file-modified)"
mkdir -p "$dir/plans"
printf 'a\n' > "$dir/plans/roadmap.md"
(cd "$dir" && git add plans/roadmap.md >/dev/null 2>&1 && git commit -q -m "add roadmap")
mutate "$dir" '
import json, subprocess, sys
d = sys.argv[1]
head = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
path = d + "/verification/findings.json"
data = json.load(open(path))
data["commit"] = head
json.dump(data, open(path, "w"), indent=2)
'
printf 'CHANGED\n' > "$dir/plans/roadmap.md"
check_case "m5-tracked-plans-file-modified-caught" "$dir" "1" "C03"

echo "== review round 2, H1: --tb=short in repro_runner must not hide a non-assertion exception =="
dir="$(make_copy r2h1-tbshort-nameerror)"
mutate "$dir" '
import sys
path = sys.argv[1] + "/verification/repro/repro_demo.py"
text = open(path).read()
text = text.replace(
    "    queue = Queue([\"j1\", \"j2\", \"j3\"])",
    "    queue = Queueue([\"j1\", \"j2\", \"j3\"])",
)
open(path, "w").write(text)
'
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["baseline"]["repro_runner"] += " --tb=short"
json.dump(data, open(path, "w"), indent=2)
'
check_case "r2h1-tbshort-nameerror-caught" "$dir" "1" "C10" "not an assertion failure"

echo "== review round 3, R1: a skipped guard test is not a passing guard =="
dir="$(make_copy r2-guard-skipped-caught-pytest)"
mutate "$dir" '
import sys
path = sys.argv[1] + "/verification/repro/repro_demo.py"
text = open(path).read()
old = "def test_drain_without_early_stop_releases_all_jobs():\n"
assert old in text
text = text.replace(old, old + "    import pytest\n    pytest.skip(\"later\")\n")
open(path, "w").write(text)
'
check_case "r2-guard-skipped-caught-pytest" "$dir" "1" "C10" "did not pass (skipped"

echo "== review round 2, H2: repro_runner {file}/{test}/{dir} placeholders drive non-pytest runners =="
go_runner='go test -v -tags verify_repro ./{dir} -run ^{test}$'
go_test_file="verification/repro/repro_leases_go_test.go"

go_body() {
  printf '//go:build verify_repro\n\npackage repro\n\nimport (\n\t"testing"%s\n)\n\nfunc TestReproLeasesReleased(t *testing.T) {\n%s\n}\n\nfunc TestGuardLeasesReleased(t *testing.T) {\n%s\n}\n' "${3:+$'\n\t'\"$3\"}" "$1" "${2:-}"
}

go_case() {
  local name="$1" body="$2" repro_id="$3" guard_id="$4" expected_exit="$5" expected_check="$6" expected_substring="${7:-}" guard_body="${8:-}" runner="${9:-$go_runner}" extra_import="${10:-}"
  local dir
  dir="$(make_copy "$name")"
  printf 'module r2h2fixture.example\n\ngo 1.22\n' > "$dir/go.mod"
  go_body "$body" "$guard_body" "$extra_import" > "$dir/$go_test_file"
  mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["baseline"]["repro_runner"] = sys.argv[2]
data["baseline"]["collect_command"] = "true"
fi = data["findings"][0]
fi["repro_tests"] = [sys.argv[3] + "::" + sys.argv[4]]
fi["guard_tests"] = [sys.argv[3] + "::" + sys.argv[5]]
json.dump(data, open(path, "w"), indent=2)
' "$runner" "$go_test_file" "$repro_id" "$guard_id"
  (cd "$dir" && git add go.mod "$go_test_file" >/dev/null 2>&1 \
    && git commit -q -m "add go module for the placeholder-runner regression")
  mutate "$dir" '
import json, subprocess, sys
d = sys.argv[1]
head = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
path = d + "/verification/findings.json"
data = json.load(open(path))
data["commit"] = head
json.dump(data, open(path, "w"), indent=2)
'
  check_case "$name" "$dir" "$expected_exit" "$expected_check" "$expected_substring"
}

if command -v go >/dev/null 2>&1; then
  go_fatal='	t.Fatal("LeasesReleased violated: job left leased after drain")'
  go_case "r2h2-go-placeholder-runner-passes" "$go_fatal" \
    "TestReproLeasesReleased" "TestGuardLeasesReleased" "0" ""
  go_case "r2h2-go-placeholder-passing-test-not-reproduced" "$go_fatal" \
    "TestGuardLeasesReleased" "TestGuardLeasesReleased" "1" "C10" "does not reproduce"
  go_case "r2h2-go-placeholder-crash-not-reproduced" \
    '	var m map[string]int
	m["a"] = 1' \
    "TestReproLeasesReleased" "TestGuardLeasesReleased" "1" "C10" "crashed"
  go_case "r2-go-placeholder-abrupt-exit-not-reproduced" \
    '	println("LeasesReleased")
	os.Exit(1)' \
    "TestReproLeasesReleased" "TestGuardLeasesReleased" "1" "C10" "never reported" "" "" "os"
  go_case "r2h2-go-placeholder-missing-guard-caught" "$go_fatal" \
    "TestReproLeasesReleased" "TestGuardTypoDoesNotExist" "1" "C10" "actually ran"
  go_case "r2-guard-skipped-caught-go" "$go_fatal" \
    "TestReproLeasesReleased" "TestGuardLeasesReleased" "1" "C10" "was skipped" \
    '	t.Skip("later")'
  go_case "r2-guard-prefix-match-caught-go" "$go_fatal" \
    "TestReproLeasesReleased" "TestGuard" "1" "C10" "actually ran" "" \
    'go test -v -tags verify_repro ./{dir} -run {test}'
else
  echo "SKIP: go not installed; H2 placeholder-runner regressions not run"
fi

dir="$(make_copy r2h2-non-pytest-runner-without-placeholders)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["baseline"]["repro_runner"] = "go test -tags verify_repro ./..."
json.dump(data, open(path, "w"), indent=2)
'
check_case "r2h2-non-pytest-runner-without-placeholders-caught" "$dir" "1" "C10" "has no {file}/{test}/{dir}"

echo "== review round 2, M1: a tla property cannot claim result=proved/unproved =="
dir="$(make_copy r2m1-tla-proved)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][0]["properties"][0]["result"] = "proved"
json.dump(data, open(path, "w"), indent=2)
'
check_case "r2m1-tla-proved-caught" "$dir" "1" "C06" "apply only to lean theorems"

echo "== review round 2, M2: a violated result must be backed by a run that named this property =="
dir="$(make_copy r2m2-violated-run-names-another-invariant)"
mutate "$dir" '
import json, os, sys
d = sys.argv[1]
sp = d + "/verification/models/job-drain/results/Spec.json"
st = os.stat(sp)
s = json.load(open(sp))
s["violated"] = "TypeOK"
s["violated_candidates"] = ["TypeOK"]
json.dump(s, open(sp, "w"))
os.utime(sp, (st.st_atime, st.st_mtime))
'
check_case "r2m2-violated-run-names-another-invariant-caught" "$dir" "1" "C06" "not the property"

dir="$(make_copy r2m2-violated-backed-only-by-deadlock)"
mutate "$dir" '
import json, os, sys
d = sys.argv[1]
sp = d + "/verification/models/job-drain/results/Spec.json"
st = os.stat(sp)
s = json.load(open(sp))
s["result"] = "deadlock"
s["violated"] = None
json.dump(s, open(sp, "w"))
os.utime(sp, (st.st_atime, st.st_mtime))
path = d + "/verification/findings.json"
data = json.load(open(path))
data["targets"][0]["properties"][0]["runs"][0]["result"] = "deadlock"
json.dump(data, open(path, "w"), indent=2)
'
check_case "r2m2-violated-backed-only-by-deadlock-caught" "$dir" "1" "C06" "not the property"

echo "== review round 2, M3: CONFIRMED needs a cited property with result=violated =="
dir="$(make_copy r2m3-confirmed-on-passing-only-property)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
p = data["targets"][0]["properties"][0]
p["runs"] = [p["runs"][1]]
p["result"] = "no_violation_within_bounds"
json.dump(data, open(path, "w"), indent=2)
'
check_case "r2m3-confirmed-on-passing-only-property-caught" "$dir" "1" "C09" "have result="

dir="$(make_copy r2m3-confirmed-lean-only-not-violated)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
p = data["targets"][0]["properties"][1]
data["targets"][0]["properties"] = [p]
data["findings"][0]["properties"] = [p["name"]]
data["invocation"] = "/verify-formally lean demo/worker.py"
json.dump(data, open(path, "w"), indent=2)
'
check_case "r2m3-confirmed-lean-only-not-violated-caught" "$dir" "1" "C09" "have result="

echo "== review round 2, M4: runs[].json cannot resolve to a file outside the target's own directory =="
dir="$(make_copy r2m4-runs-json-outside-target-dir)"
mutate "$dir" '
import json, os, sys
d, good = sys.argv[1], sys.argv[2]
p = d + "/verification/models/job-drain/results/SpecFixed.json"
os.remove(p)
path = d + "/verification/findings.json"
data = json.load(open(path))
data["targets"][0]["properties"][0]["runs"][1]["json"] = good + "/verification/models/job-drain/results/SpecFixed.json"
json.dump(data, open(path, "w"), indent=2)
' "$good"
check_case "r2m4-runs-json-outside-target-dir-caught" "$dir" "1" "C06" "resolves outside this target's own directory"

echo "== review round 2, M5: a repro failure message that omits the property name is an ERROR =="
dir="$(make_copy r2m5-repro-message-omits-property-name)"
mutate "$dir" '
import sys
path = sys.argv[1] + "/verification/repro/repro_demo.py"
text = open(path).read()
text = text.replace(
    "\"LeasesReleased violated: jobs still leased after drain: %r\" % queue.leased",
    "\"jobs still leased after drain: %r\" % queue.leased",
)
open(path, "w").write(text)
'
check_case "r2m5-repro-message-omits-property-name-caught" "$dir" "1" "C10" "does not name any of"

echo ""
echo "$pass_count passed, $fail_count failed"
[ "$fail_count" -eq 0 ]
