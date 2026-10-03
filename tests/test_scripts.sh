#!/usr/bin/env bash
# Usage: test_scripts.sh
# Runs the run_tlc.sh and lean_audit.sh fixture suite; exits 0 iff every case passes.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scripts_dir="$(cd "$here/../skills/verify-formally/scripts" && pwd)"
fixtures="$here/fixtures"
stderr_tmp="$(mktemp "${TMPDIR:-/tmp}/test_scripts_stderr.XXXXXX")"
trap 'rm -f "$stderr_tmp"' EXIT

pass_count=0
fail_count=0

pass() {
  echo "PASS: $1"
  pass_count=$((pass_count + 1))
}

fail() {
  echo "FAIL: $1"
  fail_count=$((fail_count + 1))
}

check() {
  local name="$1" expected_result="$2" expected_exit="$3" json="$4" actual_exit="$5"
  local actual_result
  actual_result="$(printf '%s\n' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"])' 2>/dev/null)"
  if [ "$actual_result" = "$expected_result" ] && [ "$actual_exit" = "$expected_exit" ]; then
    pass "$name (result=$actual_result exit=$actual_exit)"
  else
    fail "$name (expected result=$expected_result exit=$expected_exit; got result=$actual_result exit=$actual_exit)"
  fi
}

field_contains() {
  local json="$1" field="$2" needle="$3"
  printf '%s\n' "$json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
v = d.get(\"$field\")
text = json.dumps(v)
sys.exit(0 if \"$needle\" in text else 1)
" 2>/dev/null
}

run_tlc_case() {
  local name="$1" spec="$2" expected_result="$3" expected_exit="$4"
  local json
  json="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/$spec" 2>"$stderr_tmp")"
  check "$name" "$expected_result" "$expected_exit" "$json" "$?"
}

echo "== run_tlc.sh fixtures =="
run_tlc_case "tlc-pass" "Pass.tla" "pass" "0"
run_tlc_case "tlc-invariant-violation" "InvViolation.tla" "invariant_violation" "1"
run_tlc_case "tlc-liveness-violation" "Liveness.tla" "property_violation" "1"
run_tlc_case "tlc-deadlock" "Deadlock.tla" "deadlock" "1"
run_tlc_case "tlc-syntax-error" "Syntax.tla" "error" "2"
run_tlc_case "tlc-vacuous-init" "EmptyInit.tla" "vacuous" "1"
run_tlc_case "tlc-liveness-with-constraint-warns" "LiveConstr.tla" "pass_with_warnings" "2"
run_tlc_case "tlc-undeclared-cfg-constant-warns" "UndeclConst.tla" "pass_with_warnings" "2"

json="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/RtErr.tla" 2>"$stderr_tmp")"
ec=$?
check "tlc-runtime-error-in-invariant" "error" "2" "$json" "$ec"
if field_contains "$json" "error_detail" "second argument of"; then
  pass "tlc-runtime-error-detail-is-real-message"
else
  fail "tlc-runtime-error-detail-is-real-message (error_detail did not contain the div-by-zero message)"
fi
if field_contains "$json" "trace" "Initial predicate"; then
  pass "tlc-runtime-error-keeps-trace"
else
  fail "tlc-runtime-error-keeps-trace (trace was empty)"
fi

check_flag_rejected() {
  local name="$1" flag="$2"
  local out
  out="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/Pass.tla" -- "$flag" 2>&1 >/dev/null)"
  local ec=$?
  if [ "$ec" = "2" ] && printf '%s' "$out" | grep -q "unsupported TLC flag"; then
    pass "$name (rejected with exit=$ec)"
  else
    fail "$name (expected usage rejection exit=2, got exit=$ec, stderr=$out)"
  fi
}

check_flag_rejected "tlc-continue-flag-rejected" "-continue"
check_flag_rejected "tlc-simulate-flag-rejected" "-simulate"
check_flag_rejected "tlc-generate-flag-rejected" "-generate"
check_flag_rejected "tlc-dump-flag-rejected" "-dump"
check_flag_rejected "tlc-dumptrace-flag-rejected" "-dumpTrace"
check_flag_rejected "tlc-dfid-flag-rejected" "-dfid"
check_flag_rejected "tlc-config-flag-rejected" "-config"
check_flag_rejected "tlc-metadir-flag-rejected" "-metadir"

run_tlc_case "tlc-no-checks-configured-is-vacuous" "NoCheck.tla" "vacuous" "1"

json="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/NoCheck.tla" "$fixtures/tlc/NoCheck2.cfg" -- -deadlock 2>"$stderr_tmp")"
ec=$?
check "tlc-deadlock-flag-disables-deadlock-check-is-vacuous" "vacuous" "1" "$json" "$ec"
if field_contains "$json" "check_deadlock" "false"; then
  pass "tlc-deadlock-flag-recorded-in-check-deadlock-field"
else
  fail "tlc-deadlock-flag-recorded-in-check-deadlock-field (check_deadlock was not false)"
fi

tmpd="$(mktemp -d)"
json="$(TMPDIR="$tmpd" "$scripts_dir/run_tlc.sh" "$fixtures/tlc/Big.tla" --timeout 2 2>"$stderr_tmp")"
ec=$?
check "tlc-timeout" "timeout" "2" "$json" "$ec"
sleep 1
if pgrep -f "$fixtures/tlc/Big.tla" >/dev/null 2>&1; then
  fail "tlc-timeout-no-orphan-java (a java process for Big.tla is still running)"
else
  pass "tlc-timeout-no-orphan-java"
fi
after_dirs="$(ls -d "$tmpd"/run_tlc.* 2>/dev/null | wc -l | tr -d ' ')"
rm -rf "$tmpd"
if [ "$after_dirs" -eq 0 ]; then
  pass "tlc-timeout-no-leftover-workdir"
else
  fail "tlc-timeout-no-leftover-workdir (found $after_dirs run_tlc.* dirs left behind)"
fi

start_ts=$(date +%s)
"$scripts_dir/run_tlc.sh" "$fixtures/tlc/Pass.tla" --timeout 60 >/dev/null 2>"$stderr_tmp"
end_ts=$(date +%s)
elapsed=$((end_ts - start_ts))
if [ "$elapsed" -le 15 ]; then
  pass "tlc-timeout-returns-promptly-when-tlc-finishes-early (elapsed=${elapsed}s)"
else
  fail "tlc-timeout-returns-promptly-when-tlc-finishes-early (elapsed=${elapsed}s, expected <=15s)"
fi

echo "== run_tlc.sh regression fixtures (round 3) =="

json="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/CfgDirDecoy/CfgDirDecoy.tla" "$fixtures/tlc/CfgDirDecoy/cfgs/CfgDirDecoy.cfg" 2>"$stderr_tmp")"
ec=$?
check "tlc-cfg-in-subdir-not-shadowed-by-decoy" "invariant_violation" "1" "$json" "$ec"
if field_contains "$json" "invariants" "Inv" && field_contains "$json" "violated" "Inv"; then
  pass "tlc-cfg-in-subdir-uses-the-named-cfg-not-the-decoy"
else
  fail "tlc-cfg-in-subdir-uses-the-named-cfg-not-the-decoy (invariants/violated did not reflect cfgs/CfgDirDecoy.cfg's Inv)"
fi

run_tlc_case "tlc-assert-violation" "Assert.tla" "assertion_violation" "1"
json="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/Assert.tla" 2>"$stderr_tmp")"
if field_contains "$json" "error_detail" "evaluated to FALSE" && field_contains "$json" "trace" "Initial predicate"; then
  pass "tlc-assert-violation-has-detail-and-trace"
else
  fail "tlc-assert-violation-has-detail-and-trace (error_detail or trace missing)"
fi

json="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/Stutter.tla" 2>"$stderr_tmp")"
ec=$?
check "tlc-stuttering-trace-property-violation" "property_violation" "1" "$json" "$ec"
if field_contains "$json" "trace" "Stuttering"; then
  pass "tlc-stuttering-step-kept-in-trace"
else
  fail "tlc-stuttering-step-kept-in-trace (trace did not contain a Stuttering step)"
fi

run_tlc_case "tlc-extends-local-constants-no-false-warning" "ExtendsMain.tla" "pass" "0"

out_subdir="$(mktemp -d "${TMPDIR:-/tmp}/test_scripts_outdir.XXXXXX")"
rmdir "$out_subdir"
json="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/Pass.tla" --out "$out_subdir/nested/result.json" 2>"$stderr_tmp")"
ec=$?
check "tlc-out-nonexistent-dir-created" "pass" "0" "$json" "$ec"
if [ -f "$out_subdir/nested/result.json" ]; then
  pass "tlc-out-nonexistent-dir-file-written"
else
  fail "tlc-out-nonexistent-dir-file-written (result.json was not created)"
fi
rm -rf "$out_subdir"

run_lean_case() {
  local name="$1" project="$2" expected_result="$3" expected_exit="$4"
  shift 4
  local json
  json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/$project" "$@" 2>"$stderr_tmp")"
  check "$name" "$expected_result" "$expected_exit" "$json" "$?"
}

echo "== lean_audit.sh fixtures =="
run_lean_case "lean-clean" "clean" "proved" "0"
run_lean_case "lean-sorry" "with_sorry" "unproved" "1"
run_lean_case "lean-axiom" "with_axiom" "unproved" "1"
run_lean_case "lean-string-literal-hides-real-axiom" "strhide" "unproved" "1"
run_lean_case "lean-attribute-prefixed-theorem-discovered" "attr" "unproved" "1"
run_lean_case "lean-unicode-theorem-name" "uni" "proved" "0"
run_lean_case "lean-mutual-and-dotted-namespaces" "mut" "proved" "0"
run_lean_case "lean-private-theorem" "priv" "proved" "0"
run_lean_case "lean-native-decide-blocked-by-default" "nd" "unproved" "1"
run_lean_case "lean-native-decide-allowed-with-flag" "nd" "proved" "0" --allow-native-decide
run_lean_case "lean-empty-project" "empty" "no_theorems" "1"
run_lean_case "lean-lakefile-lean-project" "lakefile_lean" "proved" "0"
run_lean_case "lean-build-failed" "broken" "build_failed" "1"

json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/nd" --allow-native-decide 2>"$stderr_tmp")"
if field_contains "$json" "trust_escalations" "native_decide"; then
  pass "lean-native-decide-reports-trust-escalation"
else
  fail "lean-native-decide-reports-trust-escalation (trust_escalations missing native_decide axiom)"
fi

run_lean_case "lean-kernel-check-bypass-detected" "kern" "unproved" "1"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/kern" 2>"$stderr_tmp")"
if field_contains "$json" "kernel_check" "false"; then
  pass "lean-kernel-check-field-records-failure"
else
  fail "lean-kernel-check-field-records-failure (kernel_check.ok was not false)"
fi

run_lean_case "lean-orphan-module-not-audited" "orph" "unaudited_modules" "1"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/orph" 2>"$stderr_tmp")"
if field_contains "$json" "unaudited_modules" "Orph.Extra"; then
  pass "lean-orphan-module-listed-in-unaudited-modules"
else
  fail "lean-orphan-module-listed-in-unaudited-modules (Orph.Extra missing from unaudited_modules)"
fi

run_lean_case "lean-roots-list-all-imported-and-audited" "rts" "proved" "0"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/rts" 2>"$stderr_tmp")"
if field_contains "$json" "theorems" "lemma_claim"; then
  pass "lean-roots-list-second-root-theorem-discovered"
else
  fail "lean-roots-list-second-root-theorem-discovered (lemma_claim missing from theorems)"
fi

run_lean_case "lean-hidden-name-heuristic-fixed" "hidden" "unproved" "1"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/hidden" 2>"$stderr_tmp")"
if field_contains "$json" "theorems" "proof_of_safety" && field_contains "$json" "theorems" "match_1"; then
  pass "lean-hidden-name-theorems-appear-in-report"
else
  fail "lean-hidden-name-theorems-appear-in-report (proof_of_safety or match_1 missing from theorems)"
fi

run_lean_case "lean-apostrophe-tokenizer-fixed" "prime" "unproved" "1"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/prime" 2>"$stderr_tmp")"
if field_contains "$json" "forbidden_hits" "axiom"; then
  pass "lean-apostrophe-does-not-hide-forbidden-axiom"
else
  fail "lean-apostrophe-does-not-hide-forbidden-axiom (axiom forbidden_hit missing)"
fi

echo "== lean_audit.sh regression fixtures (round 3) =="

run_lean_case "lean-srcdir-honored-not-falsely-unaudited" "srcdir" "proved" "0"

stale_tmp="$(mktemp -d "${TMPDIR:-/tmp}/test_scripts_stale.XXXXXX")"
cp -r "$fixtures/lean/stale2" "$stale_tmp/proj"
json="$("$scripts_dir/lean_audit.sh" "$stale_tmp/proj" 2>"$stderr_tmp")"
ec=$?
check "lean-stale-olean-phase1-imported-module-proved" "proved" "0" "$json" "$ec"
cat > "$stale_tmp/proj/S2.lean" <<'EOF'
theorem ok1 : True := trivial
EOF
cat > "$stale_tmp/proj/S2/Main.lean" <<'EOF'
theorem main_claim : 1 = 2 := by decide
EOF
json="$("$scripts_dir/lean_audit.sh" "$stale_tmp/proj" 2>"$stderr_tmp")"
ec=$?
if [ "$ec" = "0" ] || field_contains "$json" "theorems" "main_claim"; then
  fail "lean-stale-olean-not-reused-after-import-removed (stale build.olean was reused: exit=$ec json=$json)"
else
  pass "lean-stale-olean-not-reused-after-import-removed (exit=$ec)"
fi
rm -f "$stale_tmp/proj/S2/Main.lean"
json="$("$scripts_dir/lean_audit.sh" "$stale_tmp/proj" 2>"$stderr_tmp")"
ec=$?
if field_contains "$json" "theorems" "main_claim"; then
  fail "lean-stale-olean-not-reused-after-source-deleted (deleted module's stale olean was still audited)"
else
  pass "lean-stale-olean-not-reused-after-source-deleted (exit=$ec)"
fi
rm -rf "$stale_tmp"

run_lean_case "lean-gen-checker-failure-yields-json-error" "nomodules" "error" "2"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/nomodules" 2>"$stderr_tmp")"
if field_contains "$json" "gen_checker_error_tail" "no built modules found"; then
  pass "lean-gen-checker-failure-has-error-tail"
else
  fail "lean-gen-checker-failure-has-error-tail (gen_checker_error_tail missing expected text)"
fi

gen_checker_out="$(mktemp "${TMPDIR:-/tmp}/test_scripts_out.XXXXXX.json")"
"$scripts_dir/lean_audit.sh" "$fixtures/lean/clean" --out "$gen_checker_out" >/dev/null 2>"$stderr_tmp"
"$scripts_dir/lean_audit.sh" "$fixtures/lean/nomodules" --out "$gen_checker_out" >/dev/null 2>"$stderr_tmp"
if field_contains "$(cat "$gen_checker_out")" "result" "error"; then
  pass "lean-out-file-overwritten-not-left-stale-on-gen-checker-failure"
else
  fail "lean-out-file-overwritten-not-left-stale-on-gen-checker-failure (--out still shows a stale prior result)"
fi
rm -f "$gen_checker_out"

run_lean_case "lean-kernel-attribution-per-module" "k2" "unproved" "1"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/k2" 2>"$stderr_tmp")"
if field_contains "$json" "theorems" "_hidden_claim" && field_contains "$json" "theorems" "Foo._helper"; then
  pass "lean-underscore-theorem-names-visible"
else
  fail "lean-underscore-theorem-names-visible (_hidden_claim or Foo._helper missing from theorems)"
fi
if printf '%s\n' "$json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
by_module = {}
for t in d['theorems']:
    by_module.setdefault(t['module'], set()).add(t['status'])
ok = by_module.get('K2') == {'proved'} and by_module.get('K2.A') == {'proved'} and by_module.get('K2.Z') == {'kernel_unverified'}
sys.exit(0 if ok else 1)
" 2>/dev/null; then
  pass "lean-kernel-unverified-status-scoped-to-failing-module"
else
  fail "lean-kernel-unverified-status-scoped-to-failing-module (status attribution across K2/K2.A/K2.Z was wrong)"
fi

run_lean_case "lean-raw-string-does-not-hide-forbidden-axiom" "rawstr" "unproved" "1"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/rawstr" 2>"$stderr_tmp")"
if field_contains "$json" "forbidden_hits" "axiom"; then
  pass "lean-raw-string-forbidden-hit-detected"
else
  fail "lean-raw-string-forbidden-hit-detected (axiom forbidden_hit missing)"
fi

echo "== round 4 regression fixtures =="

run_lean_case "lean-file-outside-srcdir-not-collides-with-built-module" "collide" "unaudited_modules" "1"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/collide" 2>"$stderr_tmp")"
if field_contains "$json" "unaudited_modules" "Foo.lean"; then
  pass "lean-file-outside-srcdir-listed-in-unaudited-modules"
else
  fail "lean-file-outside-srcdir-listed-in-unaudited-modules (Foo.lean missing from unaudited_modules)"
fi
if field_contains "$json" "theorems" "bad"; then
  fail "lean-file-outside-srcdir-not-collides-with-built-module (root Foo.lean's 'bad' theorem was audited as if it were src/Foo.lean)"
else
  pass "lean-file-outside-srcdir-not-audited-as-built-module"
fi

run_lean_case "lean-injeq-named-theorem-with-sorry-stays-unproved" "injhide" "unproved" "1"

run_tlc_case "tlc-constants-block-comment-line-not-truncated" "ConstComment.tla" "pass" "0"

out_subdir="$(mktemp -d "${TMPDIR:-/tmp}/test_scripts_lean_outdir.XXXXXX")"
rmdir "$out_subdir"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/clean" --out "$out_subdir/nested/result.json" 2>"$stderr_tmp")"
ec=$?
check "lean-out-nonexistent-dir-created" "proved" "0" "$json" "$ec"
if [ -f "$out_subdir/nested/result.json" ]; then
  pass "lean-out-nonexistent-dir-file-written"
else
  fail "lean-out-nonexistent-dir-file-written (result.json was not created)"
fi
rm -rf "$out_subdir"

blocker_tmp="$(mktemp "${TMPDIR:-/tmp}/test_scripts_lean_blocker.XXXXXX")"
out_json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/clean" --out "$blocker_tmp/nested/result.json" 2>"$stderr_tmp")"
ec=$?
if [ "$ec" = "2" ] && [ -z "$out_json" ] && grep -q "cannot create --out directory" "$stderr_tmp"; then
  pass "lean-out-dir-creation-failure-exits-2"
else
  fail "lean-out-dir-creation-failure-exits-2 (expected exit=2 with stderr message; got exit=$ec stderr=$(cat "$stderr_tmp"))"
fi
rm -f "$blocker_tmp"

stale_out="$(mktemp "${TMPDIR:-/tmp}/test_scripts_tlc_stale_out.XXXXXX.json")"
echo '{"result":"stale"}' > "$stale_out"
stale_log="${stale_out%.json}.log"
echo "stale log" > "$stale_log"
"$scripts_dir/run_tlc.sh" "$fixtures/tlc/DoesNotExist.tla" --out "$stale_out" >/dev/null 2>"$stderr_tmp"
ec=$?
if [ "$ec" = "2" ] && [ ! -f "$stale_out" ] && [ ! -f "$stale_log" ]; then
  pass "tlc-stale-out-removed-on-usage-error"
else
  fail "tlc-stale-out-removed-on-usage-error (stale out/log not removed; exit=$ec)"
fi
rm -f "$stale_out" "$stale_log"

echo "== round 5 additions =="

json="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/Pass.tla" 2>"$stderr_tmp")"
if field_contains "$json" "spec" "Pass.tla" && field_contains "$json" "cfg" "Pass.cfg"; then
  pass "tlc-spec-and-cfg-fields-report-absolute-paths"
else
  fail "tlc-spec-and-cfg-fields-report-absolute-paths (spec or cfg field missing/wrong)"
fi
if field_contains "$json" "command" "run_tlc.sh" && field_contains "$json" "command" "Pass.tla"; then
  pass "tlc-command-field-is-rerunnable"
else
  fail "tlc-command-field-is-rerunnable (command field missing run_tlc.sh or Pass.tla)"
fi

quiet_out="$(mktemp "${TMPDIR:-/tmp}/test_scripts_tlc_quiet.XXXXXX.json")"
rm -f "$quiet_out"
stdout_line="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/InvViolation.tla" --out "$quiet_out" --quiet 2>"$stderr_tmp")"
ec=$?
if [ "$ec" = "1" ] && printf '%s' "$stdout_line" | grep -qE "^invariant_violation Inv states=[0-9]+ depth=[0-9]+ -> $quiet_out\$"; then
  pass "tlc-quiet-with-out-prints-summary-line"
else
  fail "tlc-quiet-with-out-prints-summary-line (got: $stdout_line)"
fi
if [ -f "$quiet_out" ] && field_contains "$(cat "$quiet_out")" "result" "invariant_violation"; then
  pass "tlc-quiet-with-out-still-writes-out-file"
else
  fail "tlc-quiet-with-out-still-writes-out-file (out file missing or wrong)"
fi
rm -f "$quiet_out"

json="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/Pass.tla" --quiet 2>"$stderr_tmp")"
if field_contains "$json" "result" "pass"; then
  pass "tlc-quiet-without-out-has-no-effect"
else
  fail "tlc-quiet-without-out-has-no-effect (full JSON was not printed)"
fi

echo "== lean_audit.sh round 5 additions =="

run_lean_case "lean-user-theorems-excludes-def-declared" "usertheorems" "proved" "0"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/usertheorems" 2>"$stderr_tmp")"
if field_contains "$json" "user_theorems" "real_theorem" && ! field_contains "$json" "user_theorems" "def_fact"; then
  pass "lean-user-theorems-only-lists-theorem-keyword-declarations"
else
  fail "lean-user-theorems-only-lists-theorem-keyword-declarations (user_theorems did not filter def-declared entries)"
fi
if field_contains "$json" "theorems" "def_fact"; then
  pass "lean-theorems-field-unchanged-still-includes-def-declared"
else
  fail "lean-theorems-field-unchanged-still-includes-def-declared (def_fact missing from theorems)"
fi

quiet_out="$(mktemp "${TMPDIR:-/tmp}/test_scripts_lean_quiet.XXXXXX.json")"
rm -f "$quiet_out"
stdout_line="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/usertheorems" --out "$quiet_out" --quiet 2>"$stderr_tmp")"
ec=$?
if [ "$ec" = "0" ] && [ "$stdout_line" = "proved theorems=2/2 -> $quiet_out" ]; then
  pass "lean-quiet-with-out-prints-summary-line"
else
  fail "lean-quiet-with-out-prints-summary-line (got: $stdout_line)"
fi
rm -f "$quiet_out"

echo "== round 6 additions =="

run_lean_case "lean-srcdir-stray-file-not-silently-dropped" "srcstray" "unaudited_modules" "1"
json="$("$scripts_dir/lean_audit.sh" "$fixtures/lean/srcstray" 2>"$stderr_tmp")"
if field_contains "$json" "unaudited_modules" "src.lean"; then
  pass "lean-srcdir-stray-file-listed-in-unaudited-modules"
else
  fail "lean-srcdir-stray-file-listed-in-unaudited-modules (src.lean missing from unaudited_modules)"
fi

run_lean_case "lean-dot-srcdir-mixed-with-other-srcdir-passes" "dotmix" "proved" "0"

json="$("$scripts_dir/run_tlc.sh" "$fixtures/tlc/ConstFunc.tla" 2>"$stderr_tmp")"
ec=$?
check "tlc-constants-function-def-terminates-list" "pass_with_warnings" "2" "$json" "$ec"
if field_contains "$json" "warnings" "Fact"; then
  pass "tlc-constants-function-def-warning-names-undeclared-constant"
else
  fail "tlc-constants-function-def-warning-names-undeclared-constant (warnings did not mention Fact)"
fi

relcmd_dir="$(mktemp -d "${TMPDIR:-/tmp}/test_scripts_relcmd.XXXXXX")"
cp "$fixtures/tlc/Pass.tla" "$fixtures/tlc/Pass.cfg" "$relcmd_dir/"
json="$(cd "$relcmd_dir" && "$scripts_dir/run_tlc.sh" Pass.tla Pass.cfg 2>"$stderr_tmp")"
rerun_cmd="$(printf '%s\n' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["command"])')"
rerun_json="$(cd / && bash -c "$rerun_cmd" 2>"$stderr_tmp")"
rerun_result="$(printf '%s\n' "$rerun_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"])' 2>/dev/null)"
if [ "$rerun_result" = "pass" ]; then
  pass "tlc-command-field-uses-absolute-paths-rerunnable-from-elsewhere"
else
  fail "tlc-command-field-uses-absolute-paths-rerunnable-from-elsewhere (rerun from / gave result=$rerun_result)"
fi
rm -rf "$relcmd_dir"

echo
echo "$pass_count passed, $fail_count failed"
[ "$fail_count" -eq 0 ]
