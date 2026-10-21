#!/usr/bin/env bash
# Usage: run_c_repro.sh <test-file.c> <test-name>
# Builds the test file with the unmodified src/frame.c under AddressSanitizer and
# UndefinedBehaviorSanitizer, runs one test, and exits non-zero when it fails. A sanitizer
# report counts as a failure of the property the test printed ("PROPERTY <name>"), and is
# reported as "FAIL <test>: <property> violated: <sanitizer summary>".
set -uo pipefail
file="$1"
test="$2"
bin="$(mktemp "${TMPDIR:-/tmp}/repro_bin.XXXXXX")"
log="$(mktemp "${TMPDIR:-/tmp}/repro_log.XXXXXX")"
trap 'rm -f "$bin" "$log"' EXIT
cc="${CC:-cc}"
if ! "$cc" -g -O0 -fsanitize=address,undefined -fno-sanitize-recover=all -Isrc \
    "$file" src/frame.c -o "$bin" 2>"$log"; then
  echo "build of $file failed:"
  cat "$log"
  exit 3
fi
ASAN_OPTIONS=detect_leaks=0 "$bin" "$test" > "$log" 2>&1
rc=$?
cat "$log"
if [ "$rc" -ne 0 ] && grep -q "Sanitizer" "$log"; then
  property="$(sed -n 's/^PROPERTY //p' "$log" | head -1)"
  summary="$(grep -m1 '^SUMMARY:' "$log" || grep -m1 'ERROR: ' "$log")"
  echo "FAIL $test: ${property:-unknown property} violated: $summary"
fi
exit "$rc"
