#!/usr/bin/env bash
# Usage: run_example.sh
# End-to-end self-check for the frame-length-overflow example; exits 0 only if every check
# holds (or prints SKIP and exits 0 when CBMC or a C compiler is missing). Never writes into
# this directory: everything runs in scratch git repositories under ${TMPDIR:-/tmp}.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scripts_dir="$(cd "$here/../../scripts" && pwd)"
skill="$(cd "$scripts_dir/.." && pwd)"

rows=()
ok=1
tmp_dirs=()
record() { rows+=("$1|$2|$3"); [ "$2" = PASS ] || ok=0; }
cleanup() { for d in "${tmp_dirs[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
trap cleanup EXIT

for tool in cbmc cc; do
  if ! command -v "$tool" > /dev/null; then
    echo "SKIP: $tool is not installed (see scripts/check_toolchain.sh --code)"
    exit 0
  fi
done

# A scratch git repository laid out like the project this example stands for.
make_scratch_repo() {
  local dir="$1"
  git init -q "$dir"
  git -C "$dir" config user.email "verify-example@example.com"
  git -C "$dir" config user.name "verify-formally example"
  cp -R "$here/src" "$here/fix.patch" "$dir/"
  git -C "$dir" add src fix.patch
  git -C "$dir" commit -q -m "add frame decoder" > /dev/null
}

echo "== repro test: buggy and fixed src/frame.c =="
repro_work="$(mktemp -d "${TMPDIR:-/tmp}/frame_repro.XXXXXX")"
tmp_dirs+=("$repro_work")
make_scratch_repo "$repro_work"
cp -R "$here/verification" "$repro_work/verification"
runner=(bash verification/repro/run_c_repro.sh verification/repro/repro_frame_decode.c)
buggy_out="$(cd "$repro_work" && "${runner[@]}" test_length_255_reads_past_input 2>&1)"
buggy_ec=$?
if [ "$buggy_ec" -ne 0 ] && grep -q "FrameReadsInBounds violated: SUMMARY: AddressSanitizer" <<<"$buggy_out"; then
  record "repro-buggy" PASS "fails on FrameReadsInBounds with an AddressSanitizer report"
else
  record "repro-buggy" FAIL "expected a FrameReadsInBounds sanitizer failure; got exit=$buggy_ec"
  printf '%s\n' "$buggy_out" >&2
fi
guard_out="$(cd "$repro_work" && "${runner[@]}" test_well_formed_frame_decodes 2>&1)"
if [ $? -eq 0 ]; then
  record "guard" PASS "well-formed frame decodes"
else
  record "guard" FAIL "guard test failed"
  printf '%s\n' "$guard_out" >&2
fi
(cd "$repro_work" && patch -s -p1 < fix.patch)
fixed_out="$(cd "$repro_work" && "${runner[@]}" test_length_255_reads_past_input 2>&1)"
if [ $? -eq 0 ]; then
  record "repro-fixed" PASS "passes after fix.patch"
else
  record "repro-fixed" FAIL "still fails after fix.patch"
  printf '%s\n' "$fixed_out" >&2
fi
fixed_cbmc="$repro_work/fixed.json"
"$scripts_dir/run_code_check.py" --tool cbmc --cwd "$repro_work" --out "$fixed_cbmc" --quiet -- \
  cbmc verification/models/frame-decode/harness_frame_decode.c src/frame.c -I src \
  --function harness --bounds-check --pointer-check --conversion-check --signed-overflow-check > /dev/null
if [ "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["result"], d["exhaustive"])' "$fixed_cbmc")" = "pass True" ]; then
  record "cbmc-fixed" PASS "every check passes exhaustively on the fixed code (the plan's done criterion)"
else
  record "cbmc-fixed" FAIL "CBMC does not pass on the fixed code"
fi

echo "== rerun.sh in a scratch git repository =="
rerun_work="$(mktemp -d "${TMPDIR:-/tmp}/frame_rerun.XXXXXX")"
tmp_dirs+=("$rerun_work")
make_scratch_repo "$rerun_work"
cp -Rp "$here/verification" "$rerun_work/verification"
rm -f "$rerun_work"/verification/models/*/results/*
VERIFY_SKILL_DIR="$skill" "$rerun_work/verification/rerun.sh" > "$rerun_work/rerun.log" 2>&1
detail="$(python3 - "$rerun_work/verification/models/frame-decode/results" <<'PYEOF'
import json, os, sys
want = {
    "Harness": ("violation", ["frame_decode.overflow.2", "frame_decode.precondition_instance.2"]),
    "Writes": ("pass", []),
    "mutant-drop-cap-check": ("violation", ["frame_decode.precondition_instance.3"]),
    "sanity-Writes": ("violation", ["harness.assertion.1"]),
}
bad = []
for stem, (result, violated) in want.items():
    try:
        d = json.load(open(os.path.join(sys.argv[1], stem + ".json")))
    except (OSError, ValueError):
        bad.append(stem + " unreadable")
        continue
    if (d["result"], d["violated"]) != (result, violated):
        bad.append("%s=%s %s" % (stem, d["result"], d["violated"]))
print("; ".join(bad))
sys.exit(1 if bad else 0)
PYEOF
)"
if [ $? -eq 0 ] && [ -f "$rerun_work/verification/state-diagram.html" ]; then
  record "rerun-sh" PASS "read and conversion violations found, write check passes, mutant caught, sanity fails, diagram rendered"
else
  record "rerun-sh" FAIL "${detail:-no state diagram}"
  cat "$rerun_work/rerun.log" >&2
fi

echo "== check_run.py against a fresh git copy of the committed results =="
check_work="$(mktemp -d "${TMPDIR:-/tmp}/frame_checkrun.XXXXXX")"
tmp_dirs+=("$check_work")
make_scratch_repo "$check_work"
check_commit="$(git -C "$check_work" rev-parse HEAD)"
cp -Rp "$here/verification" "$check_work/verification"
cp -Rp "$here/plans" "$check_work/plans"
# The committed files carry a placeholder commit; stamp the scratch repository's.
grep -rl 0000000000000000000000000000000000000000 "$check_work/verification" "$check_work/plans" \
  | xargs sed -i.bak "s/0000000000000000000000000000000000000000/$check_commit/g"
find "$check_work" -name '*.bak' -delete
python3 "$scripts_dir/render_diagram.py" "$check_work" > /dev/null
check_out="$(python3 "$scripts_dir/check_run.py" "$check_work" 2>&1)"
check_exit=$?
if [ "$check_exit" -eq 0 ]; then
  record "check-run" PASS "check_run.py exited 0 (C17 re-ran every cited CBMC result), re-stamped at $check_commit"
else
  record "check-run" FAIL "check_run.py exited $check_exit"
  printf '%s\n' "$check_out" >&2
fi

echo
printf '%-14s %-6s %s\n' "CHECK" "RESULT" "DETAIL"
for row in "${rows[@]}"; do
  IFS='|' read -r name status detail <<<"$row"
  printf '%-14s %-6s %s\n' "$name" "$status" "$detail"
done
[ "$ok" = 1 ] && exit 0 || exit 1
