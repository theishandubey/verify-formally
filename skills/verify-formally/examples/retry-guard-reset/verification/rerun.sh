#!/usr/bin/env bash
# Usage: rerun.sh
# Re-runs every cfg under models/*/ (plus sanity/ and mutants/) and the Lean audit.
# A nonzero exit is expected: the buggy cfg, the liveness cfg, and every mutant/sanity
# cfg are supposed to fail. Compare each result with the expectation in findings.json.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill="$(cd "$here/../../.." && pwd)"
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
    "$skill/scripts/run_tlc.sh" "$spec" "$cfg" --out "$out" --workers 1 --quiet || status=1
  done
done

[ -d lean ] && { "$skill/scripts/lean_audit.sh" lean --out lean/results.json --quiet || status=1; }

exit $status
