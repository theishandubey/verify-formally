#!/usr/bin/env bash
# Usage: check_toolchain.sh [--tla-only | --lean-only] [--json]
# Exits 0 when every required tool is present at a usable version, 1 otherwise.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/lib.sh"

need_tla=1 need_lean=1 json=0
for arg in "$@"; do
  case "$arg" in
    --tla-only) need_lean=0 ;;
    --lean-only) need_tla=0 ;;
    --json) json=1 ;;
    -h|--help) sed -n 2,3p "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

ok=1
declare -a rows=()
record() { rows+=("$1|$2|$3|$4"); [ "$2" = ok ] || ok=0; }

if [ "$need_tla" = 1 ]; then
  if java="$(resolve_java)"; then
    record java ok "$java" "$("$java" -version 2>&1 | head -1)"
  else
    record java missing "" "install Java 17+ (macOS: brew install openjdk@17; Debian: apt install openjdk-17-jre-headless) or set JAVA=/path/to/java"
  fi
  if jar="$(resolve_tla_jar)"; then
    if [ -n "${java:-}" ]; then
      ver="$("$java" -cp "$jar" tlc2.TLC -version 2>&1 | grep -m1 -o 'Version [^ ]*' || true)"
      record tla2tools ok "$jar" "${ver:-unknown version}"
    else
      record tla2tools ok "$jar" "version not checked (no java)"
    fi
  else
    record tla2tools missing "" "mkdir -p ~/.local/share/tla && curl -fL -o ~/.local/share/tla/tla2tools-$VERIFY_TLA_VERSION.jar https://github.com/tlaplus/tlaplus/releases/download/v$VERIFY_TLA_VERSION/tla2tools.jar (or set TLA2TOOLS_JAR)"
  fi
fi

if [ "$need_lean" = 1 ]; then
  if elan="$(resolve_elan)"; then
    record elan ok "$elan" "$("$elan" --version 2>&1 | head -1)"
  else
    record elan missing "" "curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain $VERIFY_LEAN_TOOLCHAIN"
  fi
  if lake="$(resolve_lake)"; then
    lean="$(dirname "$lake")/lean"
    record lake ok "$lake" "$("$lake" --version 2>&1 | head -1)"
    toolchain_installed=0
    while IFS= read -r toolchain_line; do
      case "$toolchain_line" in
        "$VERIFY_LEAN_TOOLCHAIN"|"$VERIFY_LEAN_TOOLCHAIN "*)
          toolchain_installed=1
          break
          ;;
      esac
    done < <("$elan" toolchain list 2>/dev/null)
    if [ "$toolchain_installed" = 1 ]; then
      record lean-toolchain ok "$VERIFY_LEAN_TOOLCHAIN" "$("$lean" +"$VERIFY_LEAN_TOOLCHAIN" --version 2>&1 | head -1)"
    else
      record lean-toolchain missing "" "elan toolchain install $VERIFY_LEAN_TOOLCHAIN"
    fi
  else
    record lake missing "" "installed with elan (see above)"
  fi
fi

if py="$(command -v python3)"; then
  pyver="$("$py" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  if "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
    record python3 ok "$py" "Python $pyver"
  else
    record python3 old "$py" "Python $pyver found; 3.8+ required"
  fi
else
  record python3 missing "" "install Python 3.8+"
fi

if [ "$json" = 1 ]; then
  printf '%s\n' "${rows[@]}" | python3 -c '
import json, sys
tools = {}
for line in sys.stdin.read().splitlines():
    name, status, path, detail = line.split("|", 3)
    tools[name] = {"status": status, "path": path, "detail": detail}
print(json.dumps({"ok": all(t["status"] == "ok" for t in tools.values()), "tools": tools}, indent=2))
'
else
  for row in "${rows[@]}"; do
    IFS='|' read -r name status path detail <<<"$row"
    printf '%-15s %-8s %s\n' "$name" "$status" "${path:+$path  }$detail"
  done
fi

[ "$ok" = 1 ] && exit 0 || exit 1
