#!/usr/bin/env bash
# Usage: test_code_and_diagram.sh
# Tests run_code_check.py (with fake verifiers that print recorded output, plus the real CBMC,
# Kani, CrossHair, and Hypothesis when they are installed) and render_diagram.py (with TLC).
# Exits 0 iff every case passes.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scripts_dir="$(cd "$here/../skills/verify-formally/scripts" && pwd)"
outputs="$here/fixtures/code"
rcc="$scripts_dir/run_code_check.py"
render="$scripts_dir/render_diagram.py"
work="$(mktemp -d "${TMPDIR:-/tmp}/test_code_and_diagram.XXXXXX")"
[ -n "${KEEP_WORK:-}" ] || trap 'rm -rf "$work"' EXIT

pass_count=0
fail_count=0
pass() { echo "PASS: $1"; pass_count=$((pass_count + 1)); }
fail() { echo "FAIL: $1"; fail_count=$((fail_count + 1)); }

jget() {
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); v=d
for k in sys.argv[2].split("."): v = v.get(k) if isinstance(v, dict) else None
print(json.dumps(v) if not isinstance(v, str) else v)' "$1" "$2" 2>/dev/null
}

# Fake verifiers: each prints $FAKE_OUT (a recorded output file) and exits $FAKE_EXIT. When
# $FAKE_BROKEN_MARK is set and the file named by $FAKE_WATCH contains it, $FAKE_OUT_BROKEN is
# printed instead (used to see whether --mutant really ran against the patched copy).
shims="$work/shims"
mkdir -p "$shims"
for tool in cbmc esbmc jbmc kani cargo crosshair pytest; do
  cat > "$shims/$tool" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo "fake-$(basename "$0") 1.0"; exit 0; fi
[ -n "${FAKE_SLEEP:-}" ] && sleep "$FAKE_SLEEP"
out="$FAKE_OUT"
if [ -n "${FAKE_BROKEN_MARK:-}" ] && grep -q "$FAKE_BROKEN_MARK" "${FAKE_WATCH:-/dev/null}" 2>/dev/null; then
  out="$FAKE_OUT_BROKEN"
fi
cat "$out"
exit "${FAKE_EXIT:-0}"
EOF
  chmod +x "$shims/$tool"
done
export PATH="$shims:$PATH"

repo="$work/repo"
mkdir -p "$repo/src" "$repo/verification/models/t/results" "$repo/verification/models/t/mutants"
git -C "$repo" init -q
printf 'int f(int i) { return i; }\n' > "$repo/src/f.c"
git -C "$repo" add src/f.c
git -C "$repo" -c user.email=t@e -c user.name=t commit -q -m init

code_case() {
  local name="$1" tool="$2" outfile="$3" fake_exit="$4" want_result="$5" want_exit="$6"
  shift 6
  local out="$repo/verification/models/t/results/$name.json"
  FAKE_OUT="$outputs/$outfile" FAKE_EXIT="$fake_exit" \
    python3 "$rcc" --tool "$tool" --cwd "$repo" --out "$out" --quiet -- "$@" > /dev/null 2>"$work/err"
  local ec=$?
  local got
  got="$(jget "$out" result)"
  if [ "$got" = "$want_result" ] && [ "$ec" = "$want_exit" ]; then
    pass "$name (result=$got exit=$ec)"
  else
    fail "$name (expected result=$want_result exit=$want_exit; got result=$got exit=$ec)"
    cat "$work/err" | sed 's/^/    /'
  fi
}

expect_field() {
  local name="$1" json="$2" field="$3" want="$4"
  local got
  got="$(jget "$json" "$field")"
  if [ "$got" = "$want" ]; then pass "$name"; else fail "$name ($field=$got, want $want)"; fi
}

echo "== run_code_check.py: parsing recorded verifier output =="
R="$repo/verification/models/t/results"
code_case cbmc-pass cbmc cbmc-pass.txt 0 pass 0 cbmc src/f.c --unwind 3
expect_field cbmc-pass-is-exhaustive "$R/cbmc-pass.json" exhaustive true
expect_field cbmc-pass-kind "$R/cbmc-pass.json" kind bounded_model_check
code_case cbmc-violation cbmc cbmc-fail.txt 10 violation 1 cbmc src/f.c --unwind 7
expect_field cbmc-violation-names-check "$R/cbmc-violation.json" violated '["push.array_bounds.1"]'
code_case cbmc-unwinding-incomplete cbmc cbmc-unwind.txt 10 incomplete 1 cbmc src/f.c --unwind 2
code_case cbmc-no-checks-vacuous cbmc cbmc-nochecks.txt 0 vacuous 1 cbmc src/f.c
code_case cbmc-no-verdict-error cbmc cbmc-error.txt 6 error 2 cbmc src/f.c
code_case esbmc-pass esbmc esbmc-pass.txt 0 pass 0 esbmc src/f.c
code_case esbmc-violation esbmc esbmc-fail.txt 1 violation 1 esbmc src/f.c
code_case esbmc-unwinding-incomplete esbmc esbmc-unwind.txt 1 incomplete 1 esbmc src/f.c --unwind 2 --unwinding-assertions
code_case kani-pass kani kani-pass.txt 0 pass 0 cargo kani
code_case kani-violation kani kani-fail.txt 1 violation 1 cargo kani
expect_field kani-violation-names-harness "$R/kani-violation.json" violated '["proofs::check_idx::idx.assertion.2"]'
code_case kani-unwinding-incomplete kani kani-unwind.txt 1 incomplete 1 cargo kani --harness check_loop
code_case kani-no-harness-vacuous kani kani-noharness.txt 0 vacuous 1 cargo kani
code_case crosshair-confirmed crosshair crosshair-confirmed.txt 0 pass 0 crosshair check m --report_all
expect_field crosshair-confirmed-is-exhaustive "$R/crosshair-confirmed.json" exhaustive true
code_case crosshair-not-confirmed crosshair crosshair-unconfirmed.txt 0 pass 0 crosshair check m --report_all
expect_field crosshair-not-confirmed-is-not-exhaustive "$R/crosshair-not-confirmed.json" exhaustive false
code_case crosshair-violation crosshair crosshair-fail.txt 1 violation 1 crosshair check m --report_all
code_case crosshair-nothing-checkable-vacuous crosshair crosshair-none.txt 0 vacuous 1 crosshair check m --report_all
code_case crosshair-import-error crosshair crosshair-importerror.txt 2 error 2 crosshair check m --report_all
code_case hypothesis-pass hypothesis pytest-pass.txt 0 pass 0 pytest -q t.py
expect_field hypothesis-pass-never-exhaustive "$R/hypothesis-pass.json" exhaustive false
code_case hypothesis-violation hypothesis pytest-fail.txt 1 violation 1 pytest -q t.py
code_case hypothesis-no-tests-vacuous hypothesis pytest-notests.txt 5 vacuous 1 pytest -q t.py
code_case hypothesis-all-skipped-vacuous hypothesis pytest-skipped.txt 0 vacuous 1 pytest -q t.py
code_case hypothesis-xfail-error hypothesis pytest-xfail.txt 0 error 2 pytest -q t.py

echo "== run_code_check.py: the log replays to the stored result =="
replay_bad=""
for f in "$R"/*.json; do
  if ! python3 - "$f" "$scripts_dir" <<'PYEOF'
import json, sys
sys.path.insert(0, sys.argv[2])
import code_report
d = json.load(open(sys.argv[1]))
log = open(sys.argv[1][:-5] + ".log").read()
r = code_report.parse(d["tool"], log, d["exit_code"], d["result"] == "timeout")
sys.exit(0 if all(r[k] == d[k] for k in ("result", "violated", "exhaustive")) else 1)
PYEOF
  then replay_bad="$replay_bad $(basename "$f")"; fi
done
if [ -z "$replay_bad" ]; then pass "every-result-replays-from-its-log"; else fail "replay mismatch:$replay_bad"; fi

echo "== run_code_check.py: rejected commands =="
reject_case() {
  local name="$1" tool="$2" needle="$3"
  shift 3
  local err
  err="$(FAKE_OUT="$outputs/cbmc-pass.txt" python3 "$rcc" --tool "$tool" --cwd "$repo" \
    --out "$R/rejected.json" --quiet -- "$@" 2>&1 >/dev/null)"
  local ec=$?
  if [ "$ec" = 2 ] && grep -qF -- "$needle" <<<"$err" && [ ! -f "$R/rejected.json" ]; then
    pass "$name"
  else
    fail "$name (exit=$ec, stderr=$err)"
  fi
}
reject_case reject-cbmc-no-bounds-check cbmc "--no-bounds-check" cbmc src/f.c --no-bounds-check
reject_case reject-cbmc-no-unwinding-assertions cbmc "--no-unwinding-assertions" cbmc src/f.c --no-unwinding-assertions
reject_case reject-cbmc-partial-loops cbmc "--partial-loops" cbmc src/f.c --partial-loops
reject_case reject-cbmc-json-ui cbmc "--json-ui" cbmc src/f.c --json-ui
reject_case reject-tool-command-mismatch cbmc "needs a command that starts with cbmc" esbmc src/f.c
reject_case reject-esbmc-unwind-without-assertions esbmc "--unwinding-assertions" esbmc src/f.c --unwind 4
reject_case reject-kani-only-codegen kani "--only-codegen" cargo kani --only-codegen
reject_case reject-kani-no-overflow kani "--no-overflow-checks" cargo kani --no-overflow-checks
reject_case reject-kani-playback-inplace kani "inplace" cargo kani --concrete-playback=inplace
reject_case reject-crosshair-without-report-all crosshair "--report_all" crosshair check m
reject_case reject-crosshair-watch crosshair "only \`crosshair check\`" crosshair watch m --report_all
reject_case reject-pytest-collect-only hypothesis "--collect-only" pytest --collect-only t.py
reject_case reject-pytest-last-failed hypothesis "--lf" pytest --lf t.py
reject_case reject-pytest-no-hypothesis-plugin hypothesis "Hypothesis" pytest -p no:hypothesispytest t.py
reject_case reject-not-pytest hypothesis "pytest command" python3 t.py
reject_case reject-unknown-tool frama "unknown --tool" frama-c src/f.c

echo "== run_code_check.py: --mutant runs on a patched scratch copy =="
cat > "$repo/verification/models/t/mutants/break.patch" <<'EOF'
--- a/src/f.c
+++ b/src/f.c
@@ -1 +1 @@
-int f(int i) { return i; }
+int f(int i) { return i; } /* BROKEN */
EOF
before="$(cat "$repo/src/f.c")"
FAKE_OUT="$outputs/cbmc-pass.txt" FAKE_OUT_BROKEN="$outputs/cbmc-fail.txt" FAKE_EXIT=0 \
  FAKE_BROKEN_MARK=BROKEN FAKE_WATCH=src/f.c \
  python3 "$rcc" --tool cbmc --cwd "$repo" --out "$R/mutant.json" --quiet \
  --mutant "$repo/verification/models/t/mutants/break.patch" -- cbmc src/f.c > /dev/null 2>"$work/err"
if [ "$(jget "$R/mutant.json" result)" = violation ] && [ "$(cat "$repo/src/f.c")" = "$before" ] \
    && [ "$(jget "$R/mutant.json" mutant.files)" = '["src/f.c"]' ]; then
  pass "mutant-applies-to-copy-and-leaves-repo-untouched"
else
  fail "mutant-applies-to-copy-and-leaves-repo-untouched (result=$(jget "$R/mutant.json" result))"
  cat "$work/err"
fi
cat > "$repo/verification/models/t/mutants/harness.patch" <<'EOF'
--- a/verification/models/t/h.c
+++ b/verification/models/t/h.c
@@ -0,0 +1 @@
+x
EOF
err="$(FAKE_OUT="$outputs/cbmc-pass.txt" python3 "$rcc" --tool cbmc --cwd "$repo" --out "$R/m2.json" \
  --mutant "$repo/verification/models/t/mutants/harness.patch" -- cbmc src/f.c 2>&1 >/dev/null)"
if [ $? = 2 ] && grep -q "must change the real code" <<<"$err"; then
  pass "mutant-touching-verification-rejected"
else
  fail "mutant-touching-verification-rejected ($err)"
fi
printf -- '--- a/src/f.c\n+++ b/src/f.c\n@@ -1 +1 @@\n-int nope;\n+int nope2;\n' > "$repo/verification/models/t/mutants/stale.patch"
err="$(FAKE_OUT="$outputs/cbmc-pass.txt" python3 "$rcc" --tool cbmc --cwd "$repo" --out "$R/m3.json" \
  --mutant "$repo/verification/models/t/mutants/stale.patch" -- cbmc src/f.c 2>&1 >/dev/null)"
if [ $? = 2 ] && grep -q "does not apply" <<<"$err"; then
  pass "mutant-that-does-not-apply-rejected"
else
  fail "mutant-that-does-not-apply-rejected ($err)"
fi

echo "== run_code_check.py: timeout =="
FAKE_OUT="$outputs/cbmc-pass.txt" FAKE_SLEEP=5 python3 "$rcc" --tool cbmc --cwd "$repo" \
  --out "$R/slow.json" --timeout 1 --quiet -- cbmc src/f.c > /dev/null 2>&1
ec=$?
if [ "$ec" = 2 ] && [ "$(jget "$R/slow.json" result)" = timeout ]; then
  pass "timeout-kills-and-reports-timeout"
else
  fail "timeout-kills-and-reports-timeout (exit=$ec result=$(jget "$R/slow.json" result))"
fi

echo "== run_code_check.py: the command field re-runs =="
cmd="$(jget "$R/cbmc-pass.json" command)"
rm -f "$R/cbmc-pass.json"
(cd / && FAKE_OUT="$outputs/cbmc-pass.txt" bash -c "$cmd" > /dev/null 2>&1)
if [ "$(jget "$R/cbmc-pass.json" result)" = pass ]; then
  pass "command-field-rerunnable-from-elsewhere"
else
  fail "command-field-rerunnable-from-elsewhere"
fi

echo "== run_code_check.py: real verifiers (skipped when not installed) =="
PATH="${PATH#"$shims:"}"
real="$work/real"
mkdir -p "$real/r"
git -C "$real" init -q
cat > "$real/rb.c" <<'EOF'
#define CAP 4
int buf[CAP]; unsigned head;
void push(int v) { buf[head] = v; head = (head + 1) % (CAP + 1); }
int nondet_int(void);
void harness(void) { head = 0; for (int i = 0; i < 6; i++) push(nondet_int()); }
EOF
git -C "$real" add rb.c && git -C "$real" -c user.email=t@e -c user.name=t commit -q -m init
if command -v cbmc > /dev/null; then
  python3 "$rcc" --tool cbmc --cwd "$real" --out "$real/r/v.json" --quiet -- cbmc rb.c --function harness --unwind 7 > /dev/null 2>&1
  python3 "$rcc" --tool cbmc --cwd "$real" --out "$real/r/u.json" --quiet -- cbmc rb.c --function harness --unwind 3 > /dev/null 2>&1
  sed 's/% (CAP + 1)/% CAP/' "$real/rb.c" > "$work/rb_fixed.c"
  diff -u --label a/rb.c --label b/rb.c "$real/rb.c" "$work/rb_fixed.c" > "$work/fix.patch"
  python3 "$rcc" --tool cbmc --cwd "$real" --out "$real/r/f.json" --quiet --mutant "$work/fix.patch" -- cbmc rb.c --function harness --unwind 7 > /dev/null 2>&1
  if [ "$(jget "$real/r/v.json" result)" = violation ] && [ "$(jget "$real/r/u.json" result)" = incomplete ] \
      && [ "$(jget "$real/r/f.json" result)" = pass ] && [ "$(jget "$real/r/f.json" exhaustive)" = true ]; then
    pass "real-cbmc-violation-incomplete-and-patched-pass"
  else
    fail "real-cbmc (v=$(jget "$real/r/v.json" result) u=$(jget "$real/r/u.json" result) f=$(jget "$real/r/f.json" result))"
  fi
else
  echo "SKIP: real-cbmc (cbmc not installed)"
fi
if command -v crosshair > /dev/null; then
  cat > "$real/m.py" <<'EOF'
def clamp(i: int, n: int) -> int:
    """
    pre: n > 0
    post: 0 <= __return__ < n
    """
    return n if i >= n else max(i, 0)
EOF
  (cd "$real" && python3 "$rcc" --tool crosshair --out r/c.json --quiet -- crosshair check m.py --report_all --per_condition_timeout 10 > /dev/null 2>&1)
  if [ "$(jget "$real/r/c.json" result)" = violation ]; then
    pass "real-crosshair-violation"
  else
    fail "real-crosshair-violation (got $(jget "$real/r/c.json" result))"
  fi
else
  echo "SKIP: real-crosshair (crosshair not installed)"
fi

echo "== render_diagram.py =="
dg="$work/dg"
mkdir -p "$dg/verification/models/m/results"
git -C "$dg" init -q
touch "$dg/x" && git -C "$dg" add x && git -C "$dg" -c user.email=t@e -c user.name=t commit -q -m init
cat > "$dg/verification/models/m/M.tla" <<'EOF'
---- MODULE M ----
EXTENDS Naturals
VARIABLES phase, n
Init == phase = "idle" /\ n = 0
Start == phase = "idle" /\ phase' = "busy" /\ n' = n
Work == phase = "busy" /\ n < 3 /\ n' = n + 1 /\ phase' = "busy"
Finish == phase = "busy" /\ phase' = "done" /\ n' = n
Stay == phase = "done" /\ UNCHANGED <<phase, n>>
Next == Start \/ Work \/ Finish \/ Stay
Spec == Init /\ [][Next]_<<phase, n>>
Small == n < 3
====
EOF
printf 'SPECIFICATION Spec\nINVARIANT Small\n' > "$dg/verification/models/m/M.cfg"
"$scripts_dir/run_tlc.sh" "$dg/verification/models/m/M.tla" "$dg/verification/models/m/M.cfg" \
  --out "$dg/verification/models/m/results/M.json" --quiet > /dev/null 2>&1
cat > "$dg/verification/models/m/diagram.json" <<'EOF'
{"kind": "tla", "spec": "M.tla", "cfg": "M.cfg", "project": ["phase"],
 "highlight": ["results/M.json"], "actions": {"Work": "m.py:3"}}
EOF
python3 "$render" "$dg" > "$work/render.out" 2>&1
ec=$?
page="$dg/verification/state-diagram.html"
meta_ok="$(python3 - "$page" <<'PYEOF'
import json, re, sys
t = open(sys.argv[1]).read()
m = re.search(r'id="vf-diagram-meta">(.*?)</script>', t, re.S)
d = json.loads(m.group(1).replace("<\\/", "</"))
e = d["targets"][0]
ok = (d["generator"] == "render_diagram.py" and e["nodes"] == 3 and e["error"] is None
      and "M.tla" in e["inputs"] and "results/M.json" in e["inputs"] and "<svg" in t
      and "violates Small" in t and "×4" in t)
print("ok" if ok else json.dumps(d))
PYEOF
)"
if [ "$ec" = 0 ] && [ "$meta_ok" = ok ]; then
  pass "render-tla-projects-whole-graph-and-highlights-trace"
else
  fail "render-tla-projects-whole-graph-and-highlights-trace (exit=$ec, meta=$meta_ok)"
  cat "$work/render.out"
fi

render_err_case() {
  local name="$1" diagram="$2" needle="$3"
  printf '%s\n' "$diagram" > "$dg/verification/models/m/diagram.json"
  python3 "$render" "$dg" > "$work/render.out" 2>&1
  local ec=$?
  if [ "$ec" = 1 ] && grep -qF -- "$needle" "$work/render.out" && grep -qF -- "$needle" "$page"; then
    pass "$name"
  else
    fail "$name (exit=$ec)"
    cat "$work/render.out"
  fi
}
render_err_case render-unknown-projection-variable \
  '{"kind": "tla", "spec": "M.tla", "cfg": "M.cfg", "project": ["nope"]}' "variables the spec does not have"
render_err_case render-too-many-nodes \
  '{"kind": "tla", "spec": "M.tla", "cfg": "M.cfg", "max_nodes": 3}' "max_nodes 3"
render_err_case render-manual-state-without-code \
  '{"kind": "manual", "states": [{"id": "a"}], "initial": ["a"], "transitions": []}' "cites no file:line"
render_err_case render-manual-unknown-state \
  '{"kind": "manual", "states": [{"id": "a", "code": "f.c:1"}], "initial": ["a"], "transitions": [{"from": "a", "to": "b", "code": "f.c:2"}]}' "names a state"
render_err_case render-bad-kind '{"kind": "graphviz"}' "kind"

printf '%s\n' '{"kind": "manual", "states": [{"id": "a", "label": "empty", "code": "f.c:1"}, {"id": "b", "label": "full", "code": "f.c:9"}], "initial": ["a"], "transitions": [{"from": "a", "to": "b", "label": "push()", "code": "f.c:4"}, {"from": "b", "to": "b", "label": "push() overflows", "code": "f.c:6"}], "highlight": [{"name": "001", "path": ["a", "b", "b"], "violated": "InBounds"}]}' \
  > "$dg/verification/models/m/diagram.json"
python3 "$render" "$dg" > "$work/render.out" 2>&1
if [ $? = 0 ] && grep -q "hand-drawn from code" "$page" && grep -q "violates InBounds" "$page" \
    && grep -q "f.c:6" "$page"; then
  pass "render-manual-diagram-labeled-and-highlighted"
else
  fail "render-manual-diagram-labeled-and-highlighted"
  cat "$work/render.out"
fi


echo "== check_run.py on the C example: claimed checks, sanity naming, code-level plans =="
example="$here/../skills/verify-formally/examples/frame-length-overflow"
check_run="$scripts_dir/check_run.py"
if command -v cbmc > /dev/null && command -v cc > /dev/null; then
  cx="$work/cexample"
  git init -q "$cx"
  cp -R "$example/src" "$example/fix.patch" "$cx/"
  git -C "$cx" add src fix.patch
  git -C "$cx" -c user.email=t@e -c user.name=t commit -q -m init
  cp -Rp "$example/verification" "$example/plans" "$cx/"
  sha="$(git -C "$cx" rev-parse HEAD)"
  grep -rl 0000000000000000000000000000000000000000 "$cx/verification" "$cx/plans" \
    | xargs sed -i.bak "s/0000000000000000000000000000000000000000/$sha/g"
  find "$cx" -name '*.bak' -delete
  python3 "$render" "$cx" > /dev/null
  if python3 "$check_run" "$cx" > "$work/cx.out" 2>&1; then
    pass "c-example-validates"
  else
    fail "c-example-validates"
    cat "$work/cx.out"
  fi
  cvariant() {
    local name="$1" edit="$2" needle="$3"
    local d="$work/$name"
    cp -Rp "$cx" "$d"
    python3 - "$d" "$edit" <<'PYEOF'
import json, sys
d, edit = sys.argv[1], sys.argv[2]
path = d + "/verification/findings.json"
f = json.load(open(path))
props = {p["name"]: p for p in f["targets"][0]["properties"]}
exec(edit)
json.dump(f, open(path, "w"), indent=2)
PYEOF
    python3 "$check_run" "$d" --no-exec > "$work/$name.out" 2>&1
    local ec=$?
    if [ "$ec" = 1 ] && grep "^\[ERROR\] C1[37]: " "$work/$name.out" | grep -qF -- "$needle"; then
      pass "$name"
    else
      fail "$name (exit=$ec)"
      cat "$work/$name.out"
    fi
  }
  cvariant claimed-check-without-flag-caught \
    'props["NoLengthTruncation"]["checks"].append("unsigned_overflow")' "does not enable --unsigned-overflow-check"
  cvariant memory-safety-without-checks-caught \
    'del props["FrameWritesInBounds"]["checks"]' "must list the checks it claims"
  cvariant unknown-check-caught \
    'props["FrameWritesInBounds"]["checks"].append("nan")' "cannot claim check(s) ['nan']"
  cvariant sanity-failing-on-real-bug-caught \
    'props["FrameWritesInBounds"]["vacuity"]["sanity"] = ["verification/models/frame-decode/results/Harness.json"]' "not on a sanity check"
  d="$work/plan-without-code-check"
  cp -Rp "$cx" "$d"
  sed -i.bak 's/run_code_check.py/the verifier script/g' "$d/plans/001-widen-frame-total.md" && rm -f "$d"/plans/*.bak
  python3 "$check_run" "$d" --no-exec > "$work/plan.out" 2>&1
  if grep -q "^\[ERROR\] C13: .*run_code_check.py" "$work/plan.out"; then
    pass "code-finding-plan-without-code-check-caught"
  else
    fail "code-finding-plan-without-code-check-caught"
  fi
else
  echo "SKIP: c-example cases (cbmc or cc not installed)"
fi
echo
echo "$pass_count passed, $fail_count failed"
[ "$fail_count" -eq 0 ]
