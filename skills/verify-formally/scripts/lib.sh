#!/usr/bin/env bash

VERIFY_TLA_VERSION="${VERIFY_TLA_VERSION:-1.7.4}"
VERIFY_LEAN_TOOLCHAIN="${VERIFY_LEAN_TOOLCHAIN:-leanprover/lean4:v4.34.0}"

java_major() {
  "$1" -version 2>&1 | awk -F'"' '/version/ {split($2, v, "."); print (v[1] == "1" ? v[2] : v[1]); exit}'
}

resolve_java() {
  local c
  for c in "${JAVA:-}" "${JAVA_HOME:+$JAVA_HOME/bin/java}" "$(command -v java 2>/dev/null)" \
    /opt/homebrew/opt/openjdk@21/bin/java /opt/homebrew/opt/openjdk@17/bin/java /opt/homebrew/opt/openjdk/bin/java \
    /usr/local/opt/openjdk@17/bin/java /usr/lib/jvm/default-java/bin/java; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    local major
    major="$(java_major "$c")"
    if [ -n "$major" ] && [ "$major" -ge 17 ] 2>/dev/null; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

resolve_tla_jar() {
  local c
  for c in "${TLA2TOOLS_JAR:-}" "$HOME/.local/share/tla/tla2tools-$VERIFY_TLA_VERSION.jar" \
    "$HOME/.local/share/tla/tla2tools.jar" /usr/local/lib/tla2tools.jar /opt/tla/tla2tools.jar; do
    [ -n "$c" ] && [ -f "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

resolve_lake() {
  local c
  for c in "$(command -v lake 2>/dev/null)" "$HOME/.elan/bin/lake"; do
    [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}

resolve_elan() {
  local c
  for c in "$(command -v elan 2>/dev/null)" "$HOME/.elan/bin/elan"; do
    [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return 0; }
  done
  return 1
}
