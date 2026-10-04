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

if ! "$python3_bin" - "$good" <<'PYEOF'
import json, os, subprocess, sys

good = sys.argv[1]
problems = []
for rel, want in (
    ("verification/models/retry-budget/results/RetryBudget", "error"),
    ("verification/models/shutdown-drain/results/ShutdownDrain", "pass"),
):
    try:
        got = json.load(open(os.path.join(good, rel + ".json"))).get("result")
    except (OSError, ValueError) as e:
        got = "unreadable (%s)" % e
    if got != want:
        problems.append("%s.json result is %r, expected %r" % (rel, got, want))
    if not os.path.isfile(os.path.join(good, rel + ".log")):
        problems.append("%s.log is missing" % rel)
venv_python = os.path.join(good, ".venv", "bin", "python")
rc = subprocess.run([venv_python, "-c", "import pytest"], capture_output=True).returncode
if rc != 0:
    problems.append("%s cannot import pytest (exit %s)" % (venv_python, rc))
for problem in problems:
    print("  " + problem)
sys.exit(1 if problems else 0)
PYEOF
then
  echo "FAIL: fixture guard (F0): the attempt targets or the .venv symlink are not as the cases assume"
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
  shift $(( $# < 5 ? $# : 5 ))
  local out ec
  out="$(run_checker "$dir" "$@" 2>&1)"
  ec=$?
  if [ "$ec" != "$expected_exit" ]; then
    fail "$name (expected exit=$expected_exit, got exit=$ec)"
    echo "$out" | sed 's/^/    /'
    return
  fi
  if [ -n "$expected_check" ]; then
    local lines
    lines="$(printf '%s\n' "$out" | grep "^\[ERROR\] $expected_check: " || true)"
    if [ -z "$lines" ]; then
      fail "$name (expected an [ERROR] $expected_check line, none found)"
      echo "$out" | sed 's/^/    /'
      return
    fi
    if [ -n "$expected_substring" ] && ! grep -qF -- "$expected_substring" <<<"$lines"; then
      fail "$name (expected an [ERROR] $expected_check line to contain '$expected_substring')"
      echo "$lines" | sed 's/^/    /'
      return
    fi
  fi
  pass "$name"
}

check_lines() {
  local name="$1" dir="$2" expected_exit="$3" checker_args="$4"
  shift 4
  local out ec
  out="$(run_checker "$dir" $checker_args 2>&1)"
  ec=$?
  if [ "$ec" != "$expected_exit" ]; then
    fail "$name (expected exit=$expected_exit, got exit=$ec)"
    echo "$out" | sed 's/^/    /'
    return
  fi
  local expect negate rest level check substring lines
  for expect in "$@"; do
    negate=0
    if [ "${expect#!}" != "$expect" ]; then
      negate=1
      expect="${expect#!}"
    fi
    level="${expect%%|*}"
    rest="${expect#*|}"
    check="${rest%%|*}"
    substring="${rest#*|}"
    lines="$(printf '%s\n' "$out" | grep "^\[$level\] $check" || true)"
    if [ -n "$check" ]; then
      lines="$(printf '%s\n' "$lines" | grep "^\[$level\] $check: " || true)"
    fi
    if [ -n "$substring" ] && [ -n "$lines" ]; then
      lines="$(printf '%s\n' "$lines" | grep -F -- "$substring" || true)"
    fi
    if [ "$negate" = "1" ] && [ -n "$lines" ]; then
      fail "$name (expected no [$level] $check line containing '$substring', found one)"
      echo "$lines" | sed 's/^/    /'
      return
    fi
    if [ "$negate" = "0" ] && [ -z "$lines" ]; then
      fail "$name (expected a [$level] $check line containing '$substring', none found)"
      echo "$out" | sed 's/^/    /'
      return
    fi
  done
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
good_out="$(run_checker "$good" 2>&1)"
good_ec=$?
good_errors="$(printf '%s\n' "$good_out" | grep -c '^\[ERROR\] ' || true)"
good_warnings="$(printf '%s\n' "$good_out" | grep '^\[WARNING\] ' || true)"
if [ "$good_ec" = "0" ] && [ "$good_errors" = "0" ] \
  && [ "$(printf '%s\n' "$good_warnings" | grep -c .)" = "1" ] \
  && printf '%s' "$good_warnings" | grep -qF "[WARNING] C04: only 1 modeled target(s)"; then
  pass "good-run-exits-0"
else
  fail "good-run-exits-0 (expected exit 0, no [ERROR], exactly one [WARNING] C04: only 1 modeled target(s); got exit=$good_ec)"
  echo "$good_out" | sed 's/^/    /'
fi

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
if [ "$noexec_ec" = "0" ] && printf '%s' "$noexec_out" | grep -q "\[WARNING\] C10: --no-exec" \
  && printf '%s' "$noexec_out" | grep -qF "[WARNING] C16: --no-exec was given; C16 did not re-run the 5 cited"; then
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
check_case "too-few-targets-without-reason-caught" "$dir" "1" "C04" "fewer_targets_reason is empty"

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
check_case "m1-collect-command-broken-caught" "$dir" "1" "C11" "command not found"

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

echo "== hardening: invocation classification =="
class_out="$("$python3_bin" - "$scripts_dir" "$work/classify-repo" <<'PYEOF'
import os, sys

sys.path.insert(0, sys.argv[1])
import check_run

repo = sys.argv[2]
os.makedirs(os.path.join(repo, "demo"))
os.makedirs(os.path.join(repo, "src"))
with open(os.path.join(repo, "demo", "worker.py"), "w") as f:
    f.write("def drain(queue):\n    return queue\n")
with open(os.path.join(repo, "demo", "reconcile.py"), "w") as f:
    f.write("def noop():\n    return 0\n")
os.makedirs(os.path.join(repo, "ts"))
with open(os.path.join(repo, "ts", "svc.ts"), "w") as f:
    f.write("export const tsConst = () => {}\nconst tsAsync = async () => {}\n")
    f.write("class Svc {\n  async tsMethod() {\n  }\n  tsTyped(): void {\n  }\n}\n")
with open(os.path.join(repo, "ts", "Svc.java"), "w") as f:
    f.write("class Svc {\n  public void javaMethod() {\n  }\n}\n")
with open(os.path.join(repo, "ts", "calls.ts"), "w") as f:
    f.write("callOnly();\nif (callOnly()) {\n}\nif callOnly() {\n}\nwhile callOnly:\n")
    f.write("return callOnly;\nx = callOnly(1)\nlet y = callOnly\n")
with open(os.path.join(repo, "retry.py"), "w") as f:
    f.write("def fn():\n    return 1\n")
with open(os.path.join(repo, "src", "worker.py"), "w") as f:
    f.write("def run():\n    return 2\n")
targets = [{"files": [
    "demo/worker.py", "demo/reconcile.py", "retry.py", "src/worker.py", "ts/svc.ts",
    "ts/Svc.java", "ts/calls.ts",
]}]

rows = [
    ("/verify-formally", True, False),
    ("/verify-formally (default mode, non-interactive)", True, False),
    ("/verify-formally (non-interactive).", True, False),
    ("/verify-formally --non-interactive", True, False),
    ("/verify-formally please check the retry logic", True, False),
    ("/verify-formally run_loop", True, False),
    ("/verify-formally demo/worker.py", False, False),
    ("/verify-formally retry.py", False, False),
    ("/verify-formally lean demo/worker.py", False, False),
    ("/verify-formally Worker.drain", False, False),
    ("/verify-formally pkg::fn", False, False),
    ("/verify-formally src\\worker.py", False, False),
    ("/verify-formally quick", False, False),
    ("/verify-formally reconcile", False, True),
    ("/verify-formally RECONCILE demo/worker.py", False, True),
    ("/verify-formally demo/reconcile.py", False, False),
    ("/verify-formally see README.md", True, False),
    ("/verify-formally on the node.js service", True, False),
    ("/verify-formally full run, e.g. the worker", True, False),
    ("/verify-formally i.e. take the defaults", True, False),
    ("/verify-formally and/or the defaults", True, False),
    ("/verify-formally (non-interactive, the job.queue)", True, False),
    ("/verify-formally svc.tsConst", False, False),
    ("/verify-formally svc.tsAsync", False, False),
    ("/verify-formally Svc.tsMethod", False, False),
    ("/verify-formally Svc.tsTyped", False, False),
    ("/verify-formally Svc.javaMethod", False, False),
    ("/verify-formally svc.callOnly", True, False),
    ("/verify-formally demo/worker.py:12", False, False),
    ("", False, False),
    ("   ", False, False),
    (None, False, False),
    (["/verify-formally"], False, False),
]
bad = []
for inv, want_full, want_reconcile in rows:
    got = (check_run.is_full_run(inv, repo, targets), check_run.is_reconcile_run(inv))
    if got != (want_full, want_reconcile):
        bad.append("%r: got (full, reconcile)=%r, expected %r" % (inv, got, (want_full, want_reconcile)))
for line in bad:
    print(line)
sys.exit(1 if bad else 0)
PYEOF
)"
if [ $? -eq 0 ]; then
  pass "invocation-classification"
else
  fail "invocation-classification"
  echo "$class_out" | sed 's/^/    /'
fi

echo "== hardening: C01 raw structure is checked before any repair =="
dir="$(make_copy p8-schema-id-rejected-and-continues)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["schema"] = "x-verification-1.0"
data["fewer_targets_reason"] = ""
json.dump(data, open(path, "w"), indent=2)
'
check_lines "p8-schema-id-rejected-and-continues" "$dir" "1" "" \
  "ERROR|C01|not 'verify-formally-findings/2'" \
  "ERROR|C04|fewer_targets_reason is empty"

dir="$(make_copy p9-missing-targets-rejected)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
del data["targets"]
json.dump(data, open(path, "w"), indent=2)
'
check_case "p9-missing-targets-rejected" "$dir" "1" "C01" "missing 'targets'"

dir="$(make_copy p9b-targets-wrong-type-rejected)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"] = {}
json.dump(data, open(path, "w"), indent=2)
'
check_case "p9b-targets-wrong-type-rejected" "$dir" "1" "C01" "'targets' must be a list"

dir="$(make_copy p9c-top-level-not-object-rejected)"
printf '[]\n' > "$dir/verification/findings.json"
check_case "p9c-top-level-not-object-rejected" "$dir" "1" "C01" "top-level value must be a JSON object"

echo "== hardening: FIXED only in a reconcile run, and field names are not repaired =="
dir="$(make_copy p12-fixed-outside-reconcile-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["findings"][0]["status"] = "FIXED"
json.dump(data, open(path, "w"), indent=2)
'
check_case "p12-fixed-outside-reconcile-caught" "$dir" "1" "C01" "not a reconcile run" --no-exec

dir="$(make_copy p12b-fixed-in-reconcile-ok)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["findings"][0]["status"] = "FIXED"
data["invocation"] = "/verify-formally reconcile"
json.dump(data, open(path, "w"), indent=2)
'
check_lines "p12b-fixed-in-reconcile-ok" "$dir" "0" "--no-exec" "!ERROR|C01|"

dir="$(make_copy p13-singular-field-rejected)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
fi = data["findings"][0]
fi["repro_test"] = fi.pop("repro_tests")[0]
json.dump(data, open(path, "w"), indent=2)
'
check_case "p13-singular-field-rejected" "$dir" "1" "C01" "documented field is" --no-exec

echo "== hardening: C04 selection, attempts, and unlisted work =="
dir="$(make_copy p5-zero-modeled-with-reason-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][0]["status"] = "not_modeled"
data["targets"][0]["reason"] = "budget"
json.dump(data, open(path, "w"), indent=2)
'
check_lines "p5-zero-modeled-with-reason-caught" "$dir" "1" "--no-exec" \
  "ERROR|C04|has no modeled target" \
  "!ERROR|C04|no attempt on disk"

dir="$(make_copy p6-fewer-than-three-selected-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
del data["targets"][2]
json.dump(data, open(path, "w"), indent=2)
'
check_lines "p6-fewer-than-three-selected-caught" "$dir" "1" "--no-exec" \
  "ERROR|C04|lists only 2 selected target(s)" \
  "WARNING|C04|verification/models/shutdown-drain exists but is not listed"

dir="$(make_copy c04-attempt-without-reason-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][1]["reason"] = ""
json.dump(data, open(path, "w"), indent=2)
'
check_case "c04-attempt-without-reason-caught" "$dir" "1" "C04" \
  "target retry-budget is not_modeled without a reason" --no-exec

dir="$(make_copy p7a-attempt-without-results-caught)"
rm -rf "$dir/verification/models/retry-budget/results"
check_case "p7a-attempt-without-results-caught" "$dir" "1" "C04" \
  "target retry-budget is not_modeled but has no attempt on disk" --no-exec

dir="$(make_copy p7b-attempt-without-spec-caught)"
rm "$dir/verification/models/shutdown-drain/ShutdownDrain.tla"
check_case "p7b-attempt-without-spec-caught" "$dir" "1" "C04" \
  "target shutdown-drain is not_modeled but has no attempt on disk" --no-exec

dir="$(make_copy c04-unlisted-dir-warns)"
mkdir -p "$dir/verification/models/cost-accounting"
touch "$dir/verification/models/cost-accounting/notes.md"
check_lines "c04-unlisted-dir-warns" "$dir" "0" "--no-exec" \
  "WARNING|C04|verification/models/cost-accounting exists but is not listed in targets[]"

dir="$(make_copy p10-free-text-invocation-is-full-run)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["invocation"] = "/verify-formally (default mode, non-interactive)"
data["targets"] = []
data["fewer_targets_reason"] = "scope"
json.dump(data, open(path, "w"), indent=2)
'
check_lines "p10-free-text-invocation-is-full-run" "$dir" "1" "--no-exec" \
  "ERROR|C04|lists only 0 selected" \
  "ERROR|C04|has no modeled target"

dir="$(make_copy p10b-path-invocation-is-scoped)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["invocation"] = "/verify-formally demo/worker.py"
data["targets"] = [data["targets"][0]]
data["fewer_targets_reason"] = ""
json.dump(data, open(path, "w"), indent=2)
'
check_lines "p10b-path-invocation-is-scoped" "$dir" "0" "--no-exec" \
  "!ERROR|C04|" \
  "!WARNING|C04|"

echo "== hardening: C10/C11 find the interpreter in <repo>/.venv/bin =="
if command -v python >/dev/null 2>&1 && python -c 'import pytest' >/dev/null 2>&1; then
  echo "NOTE: ambient python has pytest; p11 is not discriminating here"
fi
dir="$(make_copy p11-venv-on-path)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
old_runner = data["baseline"]["repro_runner"]
new_runner = "PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider"
data["baseline"]["repro_runner"] = new_runner
data["baseline"]["collect_command"] = "python -m pytest --collect-only -q"
fi = data["findings"][0]
fi["repro_command"] = fi["repro_command"].replace(old_runner, new_runner)
json.dump(data, open(path, "w"), indent=2)
'
check_case "p11-venv-on-path" "$dir" "0" ""

dir="$(make_copy c10-runner-exit-127-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
old_runner = data["baseline"]["repro_runner"]
new_runner = "/no/such/interpreter -m pytest -q"
data["baseline"]["repro_runner"] = new_runner
fi = data["findings"][0]
fi["repro_command"] = fi["repro_command"].replace(old_runner, new_runner)
json.dump(data, open(path, "w"), indent=2)
'
check_case "c10-runner-exit-127-caught" "$dir" "1" "C10" "exit 127, command not found"

echo "== hardening: C16 result provenance =="
dir="$(make_copy p1-handwritten-sanity-json-caught)"
mutate "$dir" '
import json, os, sys
d = sys.argv[1]
results = d + "/verification/models/job-drain/results"
handwritten = {
    "result": "invariant_violation",
    "violated": "SanityAlwaysDraining",
    "states_generated": 12,
    "distinct_states": 12,
    "depth": 4,
    "constants": {"StopAfter": "3", "Fixed": "FALSE", "MutantNoRelease": "FALSE"},
    "invariants": [],
    "properties": [],
    "check_deadlock": True,
    "trace": [],
    "tlc_version": "2.19",
    "raw_log": "sanity check: reachability",
    "exit_code": 0,
    "spec": d + "/verification/models/job-drain/JobDrain.tla",
    "cfg": "sanity: SanityAlwaysDraining",
    "command": "sanity check - verifies reachability",
}
json.dump(handwritten, open(results + "/SanityAlwaysDraining.json", "w"))
os.remove(results + "/SanityAlwaysDraining.log")
'
check_case "p1-handwritten-sanity-json-caught" "$dir" "1" "C16" \
  "SanityAlwaysDraining.json: result JSON was not produced by run_tlc.sh" --no-exec

dir="$(make_copy p2-result-log-deleted-caught)"
rm "$dir/verification/models/job-drain/results/SpecFixed.log"
check_case "p2-result-log-deleted-caught" "$dir" "1" "C16" \
  "SpecFixed.json: no raw TLC log" --no-exec

dir="$(make_copy p2b-result-copied-into-attempt-caught)"
cp -p "$dir/verification/models/job-drain/results/SpecFixed.json" \
  "$dir/verification/models/retry-budget/results/Copied.json"
cp -p "$dir/verification/models/job-drain/results/SpecFixed.log" \
  "$dir/verification/models/retry-budget/results/Copied.log"
check_lines "p2b-result-copied-into-attempt-caught" "$dir" "1" "--no-exec" \
  "ERROR|C16|Copied.json: result JSON's spec" \
  "ERROR|C16|does not resolve to a file under"

dir="$(make_copy p3-edited-json-mismatches-log-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/models/job-drain/results/SpecFixed.json"
data = json.load(open(path))
data["distinct_states"] = 6
json.dump(data, open(path, "w"))
'
check_case "p3-edited-json-mismatches-log-caught" "$dir" "1" "C16" \
  "does not match its own TLC log (json distinct_states=6" --no-exec

dir="$(make_copy p4-forged-log-caught-by-rerun)"
mutate "$dir" '
import json, re, sys
d = sys.argv[1]
results = d + "/verification/models/job-drain/results"
data = json.load(open(results + "/SpecFixed.json"))
data["distinct_states"] = 6
json.dump(data, open(results + "/SpecFixed.json", "w"))
text = open(results + "/SpecFixed.log").read()
text, n = re.subn(r"\b5 distinct states found", "6 distinct states found", text)
assert n > 0
open(results + "/SpecFixed.log", "w").write(text)
path = d + "/verification/findings.json"
findings = json.load(open(path))
findings["targets"][0]["properties"][0]["runs"][1]["distinct_states"] = 6
json.dump(findings, open(path, "w"), indent=2)
'
check_lines "p4-forged-log-caught-by-rerun" "$dir" "1" "" \
  "ERROR|C16|gives distinct_states=5, stored 6" \
  "!ERROR|C16|does not match its own TLC log"

dir="$(make_copy p4b-spec-edited-mtime-restored-caught-by-rerun)"
mutate "$dir" '
import os, sys
p = sys.argv[1] + "/verification/models/job-drain/JobDrain.tla"
st = os.stat(p)
text = open(p).read()
old = "ELSE IF Fixed THEN outstanding"
assert old in text
open(p, "w").write(text.replace(old, "ELSE IF Fixed THEN 0"))
os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
'
check_lines "p4b-spec-edited-mtime-restored-caught-by-rerun" "$dir" "1" "" \
  "ERROR|C16|SpecFixed.cfg gives result='invariant_violation', stored 'pass'" \
  "!ERROR|C06|stale"

echo "== hardening: C16 reports a missing toolchain once, and --no-exec does not need it =="
dir="$(make_copy c16-failed-to-start)"
nohome="$work/nohome"
mkdir -p "$nohome"
if HOME="$nohome" TLA2TOOLS_JAR="$work/missing.jar" "$scripts_dir/run_tlc.sh" \
  "$dir/verification/models/job-drain/JobDrain.tla" \
  "$dir/verification/models/job-drain/Spec.cfg" >/dev/null 2>&1; then
  precondition_ec=0
else
  precondition_ec=$?
fi
if [ "$precondition_ec" != "2" ]; then
  echo "SKIP: c16-failed-to-start (a tla2tools jar is reachable outside HOME)"
else
  start_out="$(HOME="$nohome" TLA2TOOLS_JAR="$work/missing.jar" run_checker "$dir" 2>&1)"
  start_ec=$?
  start_count="$(printf '%s\n' "$start_out" | grep -c 'C16: cannot re-verify TLC results: run_tlc.sh failed to start' || true)"
  start_noexec_out="$(HOME="$nohome" TLA2TOOLS_JAR="$work/missing.jar" run_checker "$dir" --no-exec 2>&1)"
  start_noexec_ec=$?
  if [ "$start_ec" = "1" ] && [ "$start_count" = "1" ] && [ "$start_noexec_ec" = "0" ]; then
    pass "c16-failed-to-start"
  else
    fail "c16-failed-to-start (exit=$start_ec, C16 start-failure lines=$start_count, --no-exec exit=$start_noexec_ec; expected 1, 1, 0)"
    echo "$start_out" | sed 's/^/    /'
    echo "$start_noexec_out" | sed 's/^/    /'
  fi
fi

echo "== review round 4: C04 applies to every run except reconcile =="
dir="$(make_copy r4-scoped-nothing-modeled-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["invocation"] = "/verify-formally demo/worker.py"
data["targets"] = []
data["findings"] = []
data["fewer_targets_reason"] = ""
json.dump(data, open(path, "w"), indent=2)
'
check_lines "r4-scoped-nothing-modeled-caught" "$dir" "1" "--no-exec" \
  "ERROR|C04|a scoped run" \
  "ERROR|C04|no modeled target's files cover it" \
  "!ERROR|C04|write the invocation"

dir="$(make_copy r4-quick-with-not-modeled-target-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["invocation"] = "/verify-formally quick"
data["targets"] = [data["targets"][1]]
data["findings"] = []
json.dump(data, open(path, "w"), indent=2)
'
check_case "r4-quick-with-not-modeled-target-caught" "$dir" "1" "C04" "a scoped run" --no-exec

dir="$(make_copy r4-scoped-path-not-in-targets-caught)"
printf 'x = 1\n' > "$dir/demo/other.py"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["invocation"] = "/verify-formally demo/other.py"
data["targets"] = [data["targets"][0]]
json.dump(data, open(path, "w"), indent=2)
'
check_case "r4-scoped-path-not-in-targets-caught" "$dir" "1" "C04" \
  "names demo/other.py but no modeled target's files cover it" --no-exec

dir="$(make_copy r4-lean-only-unproved-target-not-modeled-caught)"
mutate "$dir" '
import json, os, sys
d = sys.argv[1]
path = d + "/verification/findings.json"
data = json.load(open(path))
data["targets"][0]["status"] = "not_modeled"
data["targets"][0]["reason"] = "ran out of budget"
data["findings"] = []
os.makedirs(d + "/verification/models/fake")
with open(d + "/verification/models/fake/CORRESPONDENCE.md", "w") as f:
    for i in range(25):
        f.write("- var x%d maps to demo/worker.py:%d\n" % (i, i + 1))
data["targets"].append({
    "id": "fake", "title": "t", "files": ["demo/worker.py"], "tools": ["lean"],
    "status": "modeled",
    "properties": [{"name": "P", "statement": "s", "source": "inferred", "tool": "lean",
                    "result": "unproved"}],
})
json.dump(data, open(path, "w"), indent=2)
'
check_case "r4-lean-only-unproved-target-not-modeled-caught" "$dir" "1" "C04" \
  "has no modeled target" --no-exec

dir="$(make_copy r4-dotted-word-invocation-is-full-run)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["invocation"] = "/verify-formally on the node.js service, e.g. the worker"
data["targets"] = [data["targets"][0]]
data["fewer_targets_reason"] = ""
json.dump(data, open(path, "w"), indent=2)
'
check_case "r4-dotted-word-invocation-is-full-run" "$dir" "1" "C04" \
  "lists only 1 selected target(s)" --no-exec

dir="$(make_copy r4-lean-attempt-accepted)"
rm -rf "$dir/verification/models/retry-budget"
printf 'theorem retry_budget_attempt : True := trivial\n' > "$dir/verification/lean/RetryBudget.lean"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][1]["tools"] = ["lean"]
json.dump(data, open(path, "w"), indent=2)
'
check_lines "r4-lean-attempt-accepted" "$dir" "0" "--no-exec" "!ERROR|C04|"

dir="$(make_copy r4-lean-attempt-without-results-caught)"
rm -rf "$dir/verification/models/retry-budget"
printf 'theorem retry_budget_attempt : True := trivial\n' > "$dir/verification/lean/RetryBudget.lean"
mv "$dir/verification/lean/results.json" "$dir/verification/lean/results.json.bak"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][1]["tools"] = ["lean"]
json.dump(data, open(path, "w"), indent=2)
'
check_case "r4-lean-attempt-without-results-caught" "$dir" "1" "C04" \
  "target retry-budget is not_modeled but has no attempt on disk" --no-exec

echo "== review round 4: C01 target ids and types =="
dir="$(make_copy r4-duplicate-target-ids-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][1]["id"] = "job-drain"
data["targets"][2]["id"] = "job-drain"
json.dump(data, open(path, "w"), indent=2)
'
check_case "r4-duplicate-target-ids-caught" "$dir" "1" "C01" "duplicates an earlier target" --no-exec

dir="$(make_copy r4-traversal-target-id-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][1]["id"] = "x/../job-drain"
data["targets"][2]["id"] = ".."
json.dump(data, open(path, "w"), indent=2)
'
check_case "r4-traversal-target-id-caught" "$dir" "1" "C01" "is not a non-empty string" --no-exec

dir="$(make_copy r4-list-target-id-no-traceback)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][1]["id"] = ["a"]
json.dump(data, open(path, "w"), indent=2)
'
check_case "r4-list-target-id-no-traceback" "$dir" "1" "C01" "is not a non-empty string" --no-exec

dir="$(make_copy r4-non-string-invocation-no-traceback)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["invocation"] = ["/verify-formally"]
json.dump(data, open(path, "w"), indent=2)
'
check_case "r4-non-string-invocation-no-traceback" "$dir" "1" "C01" \
  "'invocation' must be a string" --no-exec

dir="$(make_copy r4-vacuity-mutants-string-is-one-error)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][0]["properties"][0]["vacuity"]["mutants"] = "mutants/MutantFixedNoRelease.cfg"
json.dump(data, open(path, "w"), indent=2)
'
vac_out="$(run_checker "$dir" --no-exec 2>&1)"
vac_count="$(printf '%s\n' "$vac_out" | grep -c '^\[ERROR\] C01: .*vacuity\.mutants' || true)"
if [ "$vac_count" = "1" ] && ! printf '%s' "$vac_out" | grep -q 'vacuity\.mutants\['; then
  pass "r4-vacuity-mutants-string-is-one-error"
else
  fail "r4-vacuity-mutants-string-is-one-error (C01 lines naming vacuity.mutants: $vac_count, expected 1 and no per-character entries)"
  echo "$vac_out" | sed 's/^/    /'
fi

dir="$(make_copy r4-wrong-type-targets-reported-once)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"] = {}
data["baseline"] = []
json.dump(data, open(path, "w"), indent=2)
'
once_out="$(run_checker "$dir" --no-exec 2>&1)"
once_targets="$(printf '%s\n' "$once_out" | grep -c "^\[ERROR\] C01: .*'targets' must be" || true)"
once_baseline="$(printf '%s\n' "$once_out" | grep -c "^\[ERROR\] C01: .*'baseline' must be an object" || true)"
if [ "$once_targets" = "1" ] && [ "$once_baseline" = "1" ]; then
  pass "r4-wrong-type-targets-reported-once"
else
  fail "r4-wrong-type-targets-reported-once (targets lines: $once_targets, baseline lines: $once_baseline; expected 1 and 1)"
  echo "$once_out" | sed 's/^/    /'
fi

echo "== review round 4: C16 attempt provenance =="
dir="$(make_copy r4-hand-attempt-empty-log-caught)"
mutate "$dir" '
import json, os, sys
tdir = sys.argv[1] + "/verification/models/retry-budget"
for name in os.listdir(tdir + "/results"):
    os.remove(tdir + "/results/" + name)
spec = "verification/models/retry-budget/RetryBudget.tla"
cfg = "verification/models/retry-budget/RetryBudget.cfg"
handwritten = {
    "result": "timeout", "violated": None, "distinct_states": None, "constants": {},
    "command": "run_tlc.sh %s %s" % (spec, cfg), "spec": spec, "cfg": cfg,
}
json.dump(handwritten, open(tdir + "/results/RetryBudget.json", "w"))
open(tdir + "/results/RetryBudget.log", "w").close()
'
check_case "r4-hand-attempt-empty-log-caught" "$dir" "1" "C16" \
  "is not a TLC log of RetryBudget.tla" --no-exec

dir="$(make_copy r4-hand-attempt-command-names-other-spec-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/models/retry-budget/results/RetryBudget.json"
data = json.load(open(path))
data["command"] = "run_tlc.sh x"
json.dump(data, open(path, "w"))
'
check_case "r4-hand-attempt-command-names-other-spec-caught" "$dir" "1" "C16" \
  "does not name the spec and cfg recorded in the JSON" --no-exec

echo "== review round 5: scoped runs check attempts and what the modeled target covers =="
dir="$(make_copy r5-scoped-unattempted-target-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["invocation"] = "/verify-formally demo/worker.py"
data["fewer_targets_reason"] = ""
modeled = data["targets"][0]
modeled["files"] = ["demo/other.py"]
data["targets"] = [modeled, {
    "id": "worker-scope", "title": "x", "files": ["demo/worker.py"], "tools": ["tla"],
    "status": "not_modeled", "properties": [],
}]
json.dump(data, open(path, "w"), indent=2)
'
check_lines "r5-scoped-unattempted-target-caught" "$dir" "1" "--no-exec" \
  "ERROR|C04|target worker-scope is not_modeled without a reason" \
  "ERROR|C04|target worker-scope is not_modeled but has no attempt on disk" \
  "ERROR|C04|names demo/worker.py but no modeled target's files cover it"

dir="$(make_copy r5-scoped-symbol-not-in-modeled-files-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["invocation"] = "/verify-formally Worker.drain"
modeled = data["targets"][0]
modeled["files"] = ["demo/other.py"]
data["targets"] = [modeled, data["targets"][1]]
json.dump(data, open(path, "w"), indent=2)
'
check_case "r5-scoped-symbol-not-in-modeled-files-caught" "$dir" "1" "C04" \
  "names Worker.drain but no modeled target's files define it" --no-exec

dir="$(make_copy r5-symbol-common-word-is-full-run)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["invocation"] = "/verify-formally (non-interactive, the job.queue)"
data["targets"] = [data["targets"][0]]
data["fewer_targets_reason"] = ""
json.dump(data, open(path, "w"), indent=2)
'
check_case "r5-symbol-common-word-is-full-run" "$dir" "1" "C04" \
  "lists only 1 selected target(s)" --no-exec

dir="$(make_copy r5-reconcile-without-targets-caught)"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["invocation"] = "/verify-formally reconcile"
data["targets"] = []
data["findings"] = []
json.dump(data, open(path, "w"), indent=2)
'
check_case "r5-reconcile-without-targets-caught" "$dir" "1" "C04" \
  "a reconcile run" --no-exec

echo "== review round 5: what counts as modeled for Lean properties =="
lean_mutation='
import json, os, sys
d = sys.argv[1]
result = sys.argv[2]
theorems = json.loads(sys.argv[3])
if len(sys.argv) > 4:
    lean_path = d + "/verification/lean/results.json"
    audit = json.load(open(lean_path))
    audit["theorems"].extend({"name": t["name"], "status": "proved"} for t in theorems)
    json.dump(audit, open(lean_path, "w"))
path = d + "/verification/findings.json"
data = json.load(open(path))
data["targets"][0]["status"] = "not_modeled"
data["targets"][0]["reason"] = "ran out of budget"
data["findings"] = []
os.makedirs(d + "/verification/models/fake")
with open(d + "/verification/models/fake/CORRESPONDENCE.md", "w") as f:
    for i in range(25):
        f.write("- var x%d maps to demo/worker.py:%d\n" % (i, i + 1))
prop = {"name": "P", "statement": "s", "source": "inferred", "tool": "lean", "result": result}
if theorems:
    prop["theorems"] = theorems
data["targets"].append({
    "id": "fake", "title": "t", "files": ["demo/worker.py"], "tools": ["lean"],
    "status": "modeled", "properties": [prop],
})
json.dump(data, open(path, "w"), indent=2)
'
dir="$(make_copy r5-lean-no-violation-within-bounds-caught)"
mutate "$dir" "$lean_mutation" no_violation_within_bounds '[]'
check_lines "r5-lean-no-violation-within-bounds-caught" "$dir" "1" "--no-exec" \
  "ERROR|C01|lean property has result='no_violation_within_bounds'" \
  "ERROR|C04|has no modeled target"

dir="$(make_copy r5-lean-violated-without-theorem-caught)"
mutate "$dir" "$lean_mutation" violated '[]'
check_case "r5-lean-violated-without-theorem-caught" "$dir" "1" "C04" "has no modeled target" --no-exec

dir="$(make_copy r5-lean-violated-by-unproved-theorem-caught)"
mutate "$dir" "$lean_mutation" violated '[{"name": "Nope.not_in_results"}]'
check_case "r5-lean-violated-by-unproved-theorem-caught" "$dir" "1" "C04" "has no modeled target" --no-exec

dir="$(make_copy r5-lean-violated-by-proved-theorem-counts)"
mutate "$dir" "$lean_mutation" violated '[{"name": "Fake.not_released"}]' register
check_lines "r5-lean-violated-by-proved-theorem-counts" "$dir" "0" "--no-exec" \
  "!ERROR|C04|has no modeled target"

dir="$(make_copy r6-lean-violated-borrowed-theorem-caught)"
mutate "$dir" "$lean_mutation" violated '[{"name": "JobDrain.leases_released_after_drain"}]'
check_case "r6-lean-violated-borrowed-theorem-caught" "$dir" "1" "C04" "has no modeled target" --no-exec

dir="$(make_copy r6-lean-proved-borrowed-theorem-caught)"
mutate "$dir" "$lean_mutation" proved '[{"name": "JobDrain.leases_released_after_drain"}]'
check_case "r6-lean-proved-borrowed-theorem-caught" "$dir" "1" "C08" "belongs to another target" --no-exec

echo "== review round 5: a Lean attempt must be about its own target =="
dir="$(make_copy r5-lean-attempt-for-other-target-caught)"
rm -rf "$dir/verification/models/retry-budget"
printf 'theorem job_drain_only : True := trivial\n' > "$dir/verification/lean/JobDrain.lean"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][1]["tools"] = ["lean"]
json.dump(data, open(path, "w"), indent=2)
'
check_case "r5-lean-attempt-for-other-target-caught" "$dir" "1" "C04" \
  "target retry-budget is not_modeled but has no attempt on disk" --no-exec

dir="$(make_copy r5-lean-attempt-results-without-result-field-caught)"
rm -rf "$dir/verification/models/retry-budget"
printf 'theorem retry_budget_attempt : True := trivial\n' > "$dir/verification/lean/RetryBudget.lean"
printf '{"theorems": []}\n' > "$dir/verification/lean/results.json"
mutate "$dir" '
import json, sys
path = sys.argv[1] + "/verification/findings.json"
data = json.load(open(path))
data["targets"][1]["tools"] = ["lean"]
json.dump(data, open(path, "w"), indent=2)
'
check_case "r5-lean-attempt-results-without-result-field-caught" "$dir" "1" "C04" \
  "target retry-budget is not_modeled but has no attempt on disk" --no-exec

echo "== review round 5: C16 re-run limit and per-group failures =="
stub="$work/stub_run_tlc.sh"
cat > "$stub" <<'STUBEOF'
#!/usr/bin/env bash
out=""
args=("$@")
for ((i = 0; i < $#; i++)); do
  [ "${args[$i]}" = "--out" ] && out="${args[$((i + 1))]}"
done
case "$2" in
  *SpecFixed.cfg)
    printf '{"result": "pass", "distinct_states": 999}' > "$out"
    exit 0
    ;;
esac
echo "java: command not found" >&2
exit 2
STUBEOF
chmod +x "$stub"
unit_out="$("$python3_bin" - "$scripts_dir" "$good" "$stub" <<'PYEOF'
import os, sys

sys.path.insert(0, sys.argv[1])
import check_run

repo, stub = sys.argv[2], sys.argv[3]
bad = []

cores = os.cpu_count() or 1
limit_rows = [
    ((100, "1"), 401),
    ((100, "4"), 1601),
    ((1000, "4"), 1800),
    ((1, "1"), 120),
    ((100, "auto"), min(1800, max(120, 400 * cores + 1))),
]
for (elapsed, workers), want in limit_rows:
    got = check_run._c16_rerun_limit(elapsed, workers)
    if got != want:
        bad.append("limit(%r, %r) = %r, expected %r" % (elapsed, workers, got, want))
for tokens, want in (
    (["run_tlc.sh", "a.tla", "a.cfg"], "auto"),
    (["run_tlc.sh", "a.tla", "--workers", "2"], "2"),
    (["run_tlc.sh", "a.tla", "--workers", "auto"], "auto"),
):
    got = check_run._c16_workers(tokens)
    if got != want:
        bad.append("workers(%r) = %r, expected %r" % (tokens, got, want))

raw, err = check_run.load_json(os.path.join(repo, "verification", "findings.json"))
clean, _ = check_run.sanitize(raw)
check_run.RUN_TLC_SH = stub
issues = check_run.check_c16_exec(check_run.Context(repo, clean))
messages = [i["message"] for i in issues]
starts = [m for m in messages if "failed to start" in m]
mismatches = [m for m in messages if "gives distinct_states=999, stored 5" in m]
if len(starts) != 1:
    bad.append("expected one failed-to-start error, got %r" % starts)
if not mismatches:
    bad.append("the mismatch from the group that did start was dropped: %r" % messages)
for line in bad:
    print(line)
sys.exit(1 if bad else 0)
PYEOF
)"
if [ $? -eq 0 ]; then
  pass "c16-limit-workers-and-continue-after-failed-group"
else
  fail "c16-limit-workers-and-continue-after-failed-group"
  echo "$unit_out" | sed 's/^/    /'
fi

echo ""
echo "$pass_count passed, $fail_count failed"
[ "$fail_count" -eq 0 ]
