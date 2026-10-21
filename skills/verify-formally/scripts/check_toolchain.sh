#!/usr/bin/env bash
# Usage: check_toolchain.sh [--tla-only | --lean-only | --code-only] [--code] [--json]
# Exits 0 when every required tool is present at a usable version, 1 otherwise.
# --code also lists the code-level verifiers run_code_check.py drives (cbmc, esbmc, kani, jbmc,
# crosshair, hypothesis); each is optional and reported as "absent" with an install hint, since
# a run needs only the one that fits the target's language. --code-only skips TLA+ and Lean and
# exits 1 unless at least one code-level verifier is present.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/lib.sh"

need_tla=1 need_lean=1 need_code=0 json=0
for arg in "$@"; do
  case "$arg" in
    --tla-only) need_lean=0 ;;
    --lean-only) need_tla=0 ;;
    --code) need_code=1 ;;
    --code-only) need_tla=0; need_lean=0; need_code=1 ;;
    --json) json=1 ;;
    -h|--help) sed -n 2,7p "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

ok=1
declare -a rows=()
record() { rows+=("$1|$2|$3|$4"); [ "$2" = ok ] || [ "$2" = absent ] || ok=0; }

code_found=0
record_code() {
  local name="$1" path="$2" detail="$3" hint="$4"
  if [ -n "$path" ] && [ -n "$detail" ]; then
    record "$name" ok "$path" "$detail"
    code_found=1
  else
    record "$name" absent "" "$hint"
  fi
}

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

if [ "$need_code" = 1 ]; then
  p="$(command -v cbmc 2>/dev/null || true)"
  record_code cbmc "$p" "${p:+$("$p" --version 2>/dev/null | head -1)}" \
    "C/C++: install a release from https://github.com/diffblue/cbmc/releases (Debian/Ubuntu .deb: apt install ./ubuntu-*-cbmc-*.deb; macOS: brew install cbmc)"
  p="$(command -v esbmc 2>/dev/null || true)"
  record_code esbmc "$p" "${p:+$("$p" --version 2>/dev/null | head -1)}" \
    "C/C++ (alternative to cbmc): https://github.com/esbmc/esbmc/releases"
  p="$(command -v jbmc 2>/dev/null || true)"
  record_code jbmc "$p" "${p:+$("$p" --version 2>/dev/null | head -1)}" \
    "Java: ships with the cbmc release packages"
  p="$(command -v cargo 2>/dev/null || true)"
  kv=""
  [ -n "$p" ] && kv="$(cargo kani --version 2>/dev/null | head -1)"
  record_code kani "${kv:+$p}" "$kv" \
    "Rust: cargo install --locked kani-verifier && cargo kani setup"
  p="$(command -v crosshair 2>/dev/null || true)"
  cv=""
  if [ -n "$p" ]; then
    cpy="$(head -1 "$p" 2>/dev/null | sed -n 's/^#! *//p' | awk '{print $1}')"
    cv="$("${cpy:-python3}" -c 'from importlib.metadata import version; print("crosshair-tool " + version("crosshair-tool"))' 2>/dev/null)"
  fi
  record_code crosshair "${cv:+$p}" "$cv" \
    "Python: pip install crosshair-tool (into the project's virtualenv)"
  hv="$(python3 -c 'from importlib.metadata import version; print("hypothesis " + version("hypothesis"))' 2>/dev/null)"
  record_code hypothesis "${hv:+$(command -v python3)}" "$hv" \
    "Python: pip install hypothesis pytest (into the project's virtualenv; check the venv's python, not this one)"
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
print(json.dumps({"ok": all(t["status"] in ("ok", "absent") for t in tools.values()), "tools": tools}, indent=2))
'
else
  for row in "${rows[@]}"; do
    IFS='|' read -r name status path detail <<<"$row"
    printf '%-15s %-8s %s\n' "$name" "$status" "${path:+$path  }$detail"
  done
fi

if [ "$need_code" = 1 ] && [ "$need_tla" = 0 ] && [ "$need_lean" = 0 ] && [ "$code_found" = 0 ]; then
  ok=0
fi
[ "$ok" = 1 ] && exit 0 || exit 1
