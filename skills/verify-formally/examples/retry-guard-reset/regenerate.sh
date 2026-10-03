#!/usr/bin/env bash
# Usage: regenerate.sh
# Maintainer-only tool: rebuilds every committed TLC/Lean result under verification/ from the
# specs and Lean sources on disk, deterministically (--workers 1). Run this after editing any
# .tla, .cfg, or .lean file, before committing.
#
# All TLC/Lean work happens in a scratch copy under ${TMPDIR:-/tmp}, so no incidental
# toolchain output (.lake/build, TLC's per-run metadir) ever touches this directory; only the
# final results/*.json, results/*.log, and lean/results.json are copied back (with cp -p, no
# touch), and their spec/cfg/raw_log/command fields (and each .log's embedded path) are
# rewritten to be relative to this repository, so the committed files never name the machine
# regenerate.sh happened to run on. If any run comes back "error"/"timeout" (a broken run, not
# a checked violation) or lean_audit.sh fails, nothing is copied back and this exits non-zero.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill="$(cd "$here/../.." && pwd)"
scripts_dir="$skill/scripts"

work="$(mktemp -d "${TMPDIR:-/tmp}/retry_guard_reset_regen.XXXXXX")"
trap 'rm -rf "$work"' EXIT

cp -Rp "$here/verification" "$work/verification"

# Stale committed JSON must not survive a run_tlc.sh that crashes before writing its --out.
rm -f "$work"/verification/models/*/results/*.json

status=0
for spec in "$work"/verification/models/*/*.tla; do
  dir=$(dirname "$spec")
  for cfg in "$dir"/*.cfg "$dir"/sanity/*.cfg "$dir"/mutants/*.cfg; do
    [ -f "$cfg" ] || continue
    stem=$(basename "$cfg" .cfg); sub=$(basename "$(dirname "$cfg")")
    case "$sub" in sanity) out="$dir/results/sanity-$stem.json" ;; mutants) out="$dir/results/mutant-$stem.json" ;; *) out="$dir/results/$stem.json" ;; esac
    # run_tlc.sh exits 1 for a checked violation (buggy/mutant/sanity cfgs are supposed to
    # violate); that is not a broken run, so it does not set status here, only a result of
    # "error"/"timeout" (checked below) or lean_audit.sh failing does.
    "$scripts_dir/run_tlc.sh" "$spec" "$cfg" --out "$out" --workers 1 --quiet
    [ -f "$out" ] || { echo "run_tlc.sh wrote no $out" >&2; status=1; }
  done
done

if [ -d "$work/verification/lean" ]; then
  "$scripts_dir/lean_audit.sh" "$work/verification/lean" --out "$work/verification/lean/results.json" --quiet || status=1
fi

if grep -rq '"result": "error"\|"result": "timeout"' \
    "$work/verification/models"/*/results/*.json "$work/verification/lean/results.json" \
    2>/dev/null; then
  status=1
fi

python3 - "$work" "$skill" <<'PYEOF'
import glob
import json
import os
import re
import sys

work, skill = sys.argv[1], sys.argv[2]
skill_prefix = skill + "/"
# Any absolute path (however the OS/shell/JVM happened to spell it - double slashes, the
# /private symlink resolution TLC's own log lines use, this machine's scratch dir, ...) that
# leads up to "verification/" collapses to the repo-relative "verification/..." this JSON is
# already conventionally keyed by (see resolve_repo_relative in check_run.py).
VERIFICATION_PREFIX_RE = re.compile(r"\S*/verification/")
# TLC extracts the TLA+ standard modules it EXTENDS (Naturals.tla, ...) to a fresh OS temp
# path on every run, unrelated to this repo or to $work; any absolute path to a bare .tla
# file still left after the substitution above is exactly that noise, so drop it to its
# basename rather than naming this machine's temp directory.
STDLIB_TLA_RE = re.compile(r"(?<!\S)/\S*/([A-Za-z0-9_]+\.tla)")


def relativize_text(text):
    text = VERIFICATION_PREFIX_RE.sub("verification/", text)
    text = text.replace(skill_prefix, "")
    text = STDLIB_TLA_RE.sub(r"\1", text)
    return text


for path in glob.glob(os.path.join(work, "verification", "models", "*", "results", "*.json")):
    with open(path) as f:
        data = json.load(f)
    for key in ("spec", "cfg", "raw_log"):
        if key in data and isinstance(data[key], str):
            data[key] = relativize_text(data[key])
    if "command" in data and isinstance(data["command"], str):
        data["command"] = relativize_text(data["command"])
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

for path in glob.glob(os.path.join(work, "verification", "models", "*", "results", "*.log")):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(relativize_text(text))

lean_results = os.path.join(work, "verification", "lean", "results.json")
if os.path.isfile(lean_results):
    with open(lean_results, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    with open(lean_results, "w", encoding="utf-8") as f:
        f.write(relativize_text(raw))
PYEOF

if [ "$status" -ne 0 ]; then
  echo "run_tlc.sh or lean_audit.sh reported a broken run (not a checked buggy/mutant/sanity" \
    "result); leaving the committed verification/ results untouched, see the output above" >&2
  exit $status
fi

for target_dir in "$work"/verification/models/*/; do
  tid="$(basename "$target_dir")"
  mkdir -p "$here/verification/models/$tid/results"
  shopt -s nullglob
  for f in "$target_dir"results/*.json "$target_dir"results/*.log; do
    cp -p "$f" "$here/verification/models/$tid/results/"
  done
  shopt -u nullglob
done

if [ -f "$work/verification/lean/results.json" ]; then
  cp -p "$work/verification/lean/results.json" "$here/verification/lean/results.json"
fi

echo
printf '%-40s %s\n' "RESULT" "STEM"
for f in "$here"/verification/models/*/results/*.json; do
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print("%-40s %s" % (d.get("result"), sys.argv[1]))
' "$f"
done

exit $status
