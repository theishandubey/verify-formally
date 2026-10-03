#!/usr/bin/env bash
# Usage: lean_audit.sh <lake-project-dir> [--out result.json] [--allow-native-decide] [--quiet]
#
# Exit codes:
#   0  result=proved
#   1  result=unproved, no_theorems, build_failed, unaudited_modules
#   2  result=error; also usage errors (before any result exists)
# Read the JSON "result" field for the specific reason.
#
# unaudited_modules: one or more .lean files under the project were never built by
# `lake build` (e.g. missing from every lean_lib's roots/globs), so they were never
# checked at all; the JSON "unaudited_modules" field lists them.
#
# Every theorem-like declaration in a built module is also replayed through the Lean 4
# kernel via `lake env leanchecker`, independently of the elaborator that produced it.
# A kernel mismatch forces result=unproved; the outcome is always recorded in the JSON
# "kernel_check" field (kernel_check.ok is false and kernel_check.detail has the
# leanchecker output when the replay fails). Theorems in a module leanchecker flagged
# (or every theorem, if the failing module can't be attributed) get status
# "kernel_unverified" instead of "proved".
#
# The JSON "user_theorems" field lists the names, from the full "theorems" field, of only
# those theorems whose last name component appears textually after a `theorem`/`lemma`
# keyword in the project's own masked source; it is informational and never affects the
# proved/unproved decision.
#
# --quiet, combined with --out, suppresses the JSON on stdout and prints one summary line
# instead: "<result> theorems=<n proved>/<n total> -> <out path>". --quiet without --out has
# no effect.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/lib.sh"

out="" allow_native_decide=0 quiet=0
positionals=()

while [ $# -gt 0 ]; do
  case "$1" in
    --out) out="$2"; shift 2 ;;
    --allow-native-decide) allow_native_decide=1; shift ;;
    --quiet) quiet=1; shift ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    --*) echo "unknown option: $1" >&2; exit 2 ;;
    *) positionals+=("$1"); shift ;;
  esac
done

if [ -n "$out" ]; then
  out_dir="$(dirname "$out")"
  if ! mkdir -p "$out_dir" 2>/dev/null; then
    echo "cannot create --out directory: $out_dir" >&2
    exit 2
  fi
  rm -f "$out"
fi

project_dir="${positionals[0]:-}"
if [ -z "$project_dir" ]; then
  echo "usage: lean_audit.sh <lake-project-dir> [--out result.json] [--allow-native-decide] [--quiet]" >&2
  exit 2
fi
if [ ! -d "$project_dir" ]; then
  echo "project directory not found: $project_dir" >&2
  exit 2
fi
if [ ! -f "$project_dir/lakefile.toml" ] && [ ! -f "$project_dir/lakefile.lean" ]; then
  echo "no lakefile.toml or lakefile.lean in $project_dir" >&2
  exit 2
fi

lake="$(resolve_lake)" || { echo "no lake executable found (install elan; see check_toolchain.sh)" >&2; exit 2; }
lean="$(dirname "$lake")/lean"
leanchecker="$(dirname "$lake")/leanchecker"
if [ ! -x "$leanchecker" ]; then
  echo "no leanchecker executable found at $leanchecker (ships with the Lean 4 toolchain next to lake/lean)" >&2
  exit 2
fi

project_abs="$(cd "$project_dir" && pwd)"

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/lean_audit.XXXXXX")"
build_log="$work_dir/build.log"
forbidden_json="$work_dir/forbidden.json"
checker_stdout="$work_dir/checker.stdout"
checker_stderr="$work_dir/checker.stderr"
trap 'rm -rf "$work_dir"' EXIT

rm -rf "$project_abs/.lake/build"
(cd "$project_abs" && "$lake" build) > "$build_log" 2>&1
build_exit=$?

lean_version="$(cd "$project_abs" && "$lake" env "$lean" --version 2>&1 | head -1 | sed -n 's/.*version \([0-9][^,]*\),.*/\1/p')"

python3 "$here/lean_audit.py" forbidden-scan "$project_abs" > "$forbidden_json"

finalize_args=(python3 "$here/lean_audit.py" finalize --build-log "$build_log" --forbidden "$forbidden_json" --lean-version "$lean_version")
if [ "$allow_native_decide" = 1 ]; then
  finalize_args+=(--allow-native-decide)
fi

if [ "$build_exit" -ne 0 ]; then
  finalize_args+=(--build-failed)
  json="$("${finalize_args[@]}")"
else
  modules_json="$work_dir/modules.json"
  python3 "$here/lean_audit.py" modules "$project_abs" > "$modules_json"
  finalize_args+=(--modules "$modules_json")

  checker_lean="$work_dir/checker.lean"
  gen_checker_stderr="$work_dir/gen_checker.stderr"
  if ! python3 "$here/lean_audit.py" gen-checker "$project_abs" > "$checker_lean" 2>"$gen_checker_stderr"; then
    finalize_args+=(--gen-checker-failed --gen-checker-stderr "$gen_checker_stderr")
    json="$("${finalize_args[@]}")"
  else
    (cd "$project_abs" && "$lake" env "$lean" "$checker_lean") \
      > "$checker_stdout" 2>"$checker_stderr"
    checker_exit=$?
    finalize_args+=(
      --checker-stdout "$checker_stdout" --checker-stderr "$checker_stderr" --checker-exit "$checker_exit"
    )

    built_modules=()
    while IFS= read -r m; do
      [ -n "$m" ] && built_modules+=("$m")
    done < <(python3 -c '
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
for m in d.get("built", []):
    print(m)
' "$modules_json")

    if [ ${#built_modules[@]} -gt 0 ]; then
      kernel_stdout="$work_dir/kernel.stdout"
      kernel_stderr="$work_dir/kernel.stderr"
      (cd "$project_abs" && "$lake" env "$leanchecker" "${built_modules[@]}") \
        > "$kernel_stdout" 2>"$kernel_stderr"
      kernel_exit=$?
      finalize_args+=(--kernel-stdout "$kernel_stdout" --kernel-stderr "$kernel_stderr" --kernel-exit "$kernel_exit")
    fi

    json="$("${finalize_args[@]}")"
  fi
fi

if [ -n "$out" ]; then
  printf '%s\n' "$json" > "$out"
fi
if [ "$quiet" = 1 ] && [ -n "$out" ]; then
  summary="$(printf '%s\n' "$json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
theorems = d.get("theorems") or []
proved = sum(1 for t in theorems if t.get("status") == "proved")
print("%s theorems=%d/%d" % (d["result"], proved, len(theorems)))
')"
  echo "$summary -> $out"
else
  echo "$json"
fi

result="$(printf '%s\n' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"])')"

case "$result" in
  proved) exit 0 ;;
  unproved|no_theorems|build_failed|unaudited_modules) exit 1 ;;
  *) exit 2 ;;
esac
