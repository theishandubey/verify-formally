#!/usr/bin/env bash
# Usage: regenerate.sh
# Maintainer-only tool: rebuilds every committed CBMC result under verification/ (and
# state-diagram.html) by running verification/rerun.sh in a scratch git repository laid out
# like this example, then copies the results back with machine-specific paths made relative.
# Run it after editing src/, the harness, or a mutant patch, before committing.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill="$(cd "$here/../.." && pwd)"

work="$(mktemp -d "${TMPDIR:-/tmp}/frame_length_overflow_regen.XXXXXX")"
trap 'rm -rf "$work"' EXIT

git init -q "$work"
cp -R "$here/src" "$here/fix.patch" "$work/"
git -C "$work" add src fix.patch
git -C "$work" -c user.email=verify-example@example.com -c user.name="verify-formally example" \
  commit -q -m "add frame decoder"
cp -Rp "$here/verification" "$work/verification"
rm -f "$work"/verification/models/*/results/*

VERIFY_SKILL_DIR="$skill" "$work/verification/rerun.sh" || true

status=0
if grep -lq '"result": "\(error\|timeout\|incomplete\|vacuous\)"' "$work"/verification/models/*/results/*.json; then
  echo "a CBMC run was broken (error/timeout/incomplete/vacuous); nothing copied back" >&2
  status=1
fi

python3 - "$work" <<'PYEOF'
import glob, json, os, sys
work = sys.argv[1]
real = os.path.realpath(work)
def rel(text):
    for prefix in (real, work):
        text = text.replace(prefix + "/", "").replace(prefix, ".")
    return text
for path in glob.glob(os.path.join(work, "verification/models/*/results/*")):
    with open(path) as f:
        raw = f.read()
    if path.endswith(".json"):
        data = json.loads(raw)
        for key in ("command", "cwd", "raw_log"):
            if isinstance(data.get(key), str):
                data[key] = rel(data[key])
        raw = json.dumps(data, indent=2) + "\n"
    else:
        raw = rel(raw)
    with open(path, "w") as f:
        f.write(raw)
PYEOF
# The page hashes nothing a rewrite above touched for a manual diagram, but render it last anyway.
"$skill/scripts/render_diagram.py" "$work" > /dev/null || status=1

if [ "$status" -ne 0 ]; then
  exit "$status"
fi
for d in "$work"/verification/models/*/; do
  tid="$(basename "$d")"
  mkdir -p "$here/verification/models/$tid/results"
  cp -p "$d"results/* "$here/verification/models/$tid/results/"
done
cp -p "$work/verification/state-diagram.html" "$here/verification/state-diagram.html"
for f in "$here"/verification/models/*/results/*.json; do
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("%-10s %s" % (d["result"], sys.argv[1]))' "$f"
done
