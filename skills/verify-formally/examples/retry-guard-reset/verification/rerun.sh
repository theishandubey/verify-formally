#!/usr/bin/env bash
# Usage: rerun.sh
# Re-runs every cfg under models/*/ (plus sanity/ and mutants/), every code-level check, and
# the Lean audit, then re-renders state-diagram.html.
# A nonzero exit is expected: the buggy cfg, the liveness cfg, every mutant/sanity cfg, and
# the violating code-level checks are supposed to fail. Compare each result with the
# expectation in findings.json.
# The code-level checks run from the repository root (the directory above verification/),
# which must be a git repository's top level for run_code_check.py --mutant; they are skipped
# with a note otherwise. VERIFY_SKILL_DIR overrides where the skill's scripts are found.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill="${VERIFY_SKILL_DIR:-$(cd "$here/../../.." && pwd)}"
repo="$(cd "$here/.." && pwd)"
cd "$here"

status=0
for spec in models/*/*.tla; do
  dir=$(dirname "$spec")
  for cfg in "$dir"/*.cfg "$dir"/sanity/*.cfg "$dir"/mutants/*.cfg; do
    [ -f "$cfg" ] || continue
    stem=$(basename "$cfg" .cfg)
    sub=$(basename "$(dirname "$cfg")")
    case "$sub" in
      sanity) out="$dir/results/sanity-$stem.json" ;;
      mutants) out="$dir/results/mutant-$stem.json" ;;
      *) out="$dir/results/$stem.json" ;;
    esac
    "$skill/scripts/run_tlc.sh" "$here/$spec" "$here/$cfg" --out "$here/$out" --workers 1 --quiet || status=1
  done
done

toplevel="$(git -C "$repo" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$toplevel" ] && [ "$(cd "$toplevel" && pwd -P)" = "$(cd "$repo" && pwd -P)" ]; then
  code() { "$skill/scripts/run_code_check.py" --tool hypothesis --cwd "$repo" --quiet --timeout 600 "$@" || status=1; }
  T=verification/models/retry-guard
  P="$T/prop_retry_guard.py"
  pytest_cmd=(.venv/bin/python -m pytest -q -p no:cacheprovider)
  code --out "$repo/$T/results/CallsBounded.json" -- "${pytest_cmd[@]}" "$P::test_calls_bounded_by_both_limits"
  code --out "$repo/$T/results/StreakGivesUp.json" -- "${pytest_cmd[@]}" "$P::test_error_streak_gives_up_exactly"
  code --out "$repo/$T/results/sanity-StreakGivesUp.json" -- "${pytest_cmd[@]}" "$P::test_error_streak_sanity"
  code --out "$repo/$T/results/mutant-error-guard-off-by-one.json" \
    --mutant "$repo/$T/mutants/error-guard-off-by-one.patch" -- "${pytest_cmd[@]}" "$P::test_error_streak_gives_up_exactly"
else
  echo "skipping code-level checks: $repo is not a git repository's top level" >&2
fi

[ -d lean ] && { "$skill/scripts/lean_audit.sh" lean --out lean/results.json --quiet || status=1; }

"$skill/scripts/render_diagram.py" "$repo" || status=1

exit $status
