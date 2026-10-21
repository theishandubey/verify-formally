#!/usr/bin/env bash
# Usage: rerun.sh
# Re-runs every CBMC check of the frame-decode target through run_code_check.py, then
# re-renders state-diagram.html. A nonzero exit is expected: the buggy-code run, the mutant,
# and the sanity run are supposed to report violations. Compare each result with the
# expectation in findings.json.
# The checks run from the repository root (the directory above verification/), which must be
# a git repository's top level for run_code_check.py --mutant. VERIFY_SKILL_DIR overrides
# where the skill's scripts are found.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill="${VERIFY_SKILL_DIR:-$(cd "$here/../../.." && pwd)}"
repo="$(cd "$here/.." && pwd)"

toplevel="$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$toplevel" ] || [ "$(cd "$toplevel" && pwd -P)" != "$(cd "$repo" && pwd -P)" ]; then
  echo "rerun.sh: $repo must be a git repository's top level (run it from run_example.sh's scratch copy)" >&2
  exit 2
fi

status=0
T=verification/models/frame-decode
R="$repo/$T/results"
code() { "$skill/scripts/run_code_check.py" --tool cbmc --cwd "$repo" --quiet --timeout 600 "$@" || status=1; }
cbmc_cmd=(cbmc "$T/harness_frame_decode.c" src/frame.c -I src --function harness)

# Every check the target's properties claim, on the unmodified code.
code --out "$R/Harness.json" -- "${cbmc_cmd[@]}" \
  --bounds-check --pointer-check --conversion-check --signed-overflow-check
# FrameWritesInBounds alone: the memcpy destination check (the read and conversion checks
# above fail on this code, so the write check is isolated with --property).
code --out "$R/Writes.json" -- "${cbmc_cmd[@]}" --bounds-check --pointer-check \
  --property frame_decode.precondition_instance.3
code --out "$R/mutant-drop-cap-check.json" --mutant "$repo/$T/mutants/drop-cap-check.patch" -- \
  "${cbmc_cmd[@]}" --bounds-check --pointer-check --property frame_decode.precondition_instance.3
code --out "$R/sanity-Writes.json" -- "${cbmc_cmd[@]}" --bounds-check --pointer-check \
  -DVF_SANITY --property harness.assertion.1

"$skill/scripts/render_diagram.py" "$repo" || status=1

exit $status
