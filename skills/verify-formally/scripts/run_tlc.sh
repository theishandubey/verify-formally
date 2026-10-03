#!/usr/bin/env bash
# Usage: run_tlc.sh <Spec.tla> [<Spec.cfg>] [--out result.json] [--workers N] [--timeout SECONDS]
#            [--quiet] [-- extra TLC args]
#
# Exit codes:
#   0  result=pass
#   1  result=invariant_violation, property_violation, deadlock, assumption_violation,
#      assertion_violation, vacuous
#   2  result=pass_with_warnings, error, timeout; also usage errors (before any result exists)
# Read the JSON "result" field for the specific reason.
#
# The JSON "spec" and "cfg" fields are the absolute paths of the spec and cfg files actually
# passed to TLC. The JSON "command" field is a copy-pasteable re-run command line: the
# absolute path of this script followed by the original arguments, shell-quoted.
#
# --quiet, combined with --out, suppresses the JSON on stdout and prints one summary line
# instead: "<result> <violated or -> states=<distinct> depth=<depth> -> <out path>". --quiet
# without --out has no effect.
#
# TLC's -simulate/-continue/-generate/-dump/-dumpTrace/-dfid modes are rejected: this skill
# needs exhaustive, stop-at-first-violation runs, and those flags make a run non-exhaustive,
# non-stopping, or (for -dfid) leave the log without the completion/stats messages this
# script relies on to confirm the run was exhaustive.
#
# Extra TLC arguments after `--` are checked against an ALLOWLIST instead of a blocklist:
#   -deadlock, -difftrace, -noGenerateSpecTE        (no value)
#   -fp N, -checkpoint N, -lncheck MODE, -coverage N (take a value)
# -config, -metadir, -workers, -tool, and -cleanup are rejected because run_tlc.sh already
# manages them (a user-supplied -config would silently run against a different cfg file than
# the one this script parses for its JSON report). Any other flag, and any bare operand
# (e.g. a second .tla file), is rejected too.
#
# result=vacuous also fires when the cfg (as effectively run, accounting for -deadlock) has
# no INVARIANT, no PROPERTY, and deadlock checking disabled: such a run checks nothing.
# The JSON "check_deadlock" field always reports whether deadlock checking was effectively
# enabled for this run.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/lib.sh"

out="" workers="auto" timeout_secs="" quiet=0
extra_args=()
positionals=()
orig_args=("$@")
spec_arg_index=-1
cfg_arg_index=-1

while [ $# -gt 0 ]; do
  arg_index=$((${#orig_args[@]} - $#))
  case "$1" in
    --out) out="$2"; shift 2 ;;
    --workers) workers="$2"; shift 2 ;;
    --timeout) timeout_secs="$2"; shift 2 ;;
    --quiet) quiet=1; shift ;;
    --) shift; extra_args=("$@"); break ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    --*) echo "unknown option: $1" >&2; exit 2 ;;
    *)
      positionals+=("$1")
      if [ "$spec_arg_index" = "-1" ]; then
        spec_arg_index=$arg_index
      elif [ "$cfg_arg_index" = "-1" ]; then
        cfg_arg_index=$arg_index
      fi
      shift
      ;;
  esac
done

if [ -n "$out" ]; then
  out_stale_base="$(basename "$out")"
  out_stale_base="${out_stale_base%.json}"
  rm -f "$out" "$(dirname "$out")/$out_stale_base.log"
fi

spec="${positionals[0]:-}"
cfg="${positionals[1]:-}"

if [ -z "$spec" ]; then
  echo "usage: run_tlc.sh <Spec.tla> [<Spec.cfg>] [--out result.json] [--workers N] [--timeout SECONDS] [--quiet] [-- extra TLC args]" >&2
  exit 2
fi
if [ ! -f "$spec" ]; then
  echo "spec file not found: $spec" >&2
  exit 2
fi
if [ -z "$cfg" ]; then
  cfg="${spec%.tla}.cfg"
fi
if [ ! -f "$cfg" ]; then
  echo "cfg file not found: $cfg" >&2
  exit 2
fi
if [ -n "$timeout_secs" ] && ! [[ "$timeout_secs" =~ ^[0-9]+$ ]]; then
  echo "--timeout must be a positive integer number of seconds" >&2
  exit 2
fi

deadlock_flag=0
if [ ${#extra_args[@]} -gt 0 ]; then
  i=0
  n=${#extra_args[@]}
  while [ $i -lt $n ]; do
    a="${extra_args[$i]}"
    case "$a" in
      -simulate|-continue|-generate|-dump|-dumpTrace|-dfid)
        echo "unsupported TLC flag: $a (run_tlc.sh requires an exhaustive, stop-at-first-violation run)" >&2
        exit 2
        ;;
      -tool|-cleanup|-workers|-metadir|-config)
        echo "unsupported TLC flag: $a (run_tlc.sh manages this flag internally; use --workers/--out instead)" >&2
        exit 2
        ;;
      -deadlock)
        deadlock_flag=1
        i=$((i + 1))
        ;;
      -difftrace|-noGenerateSpecTE)
        i=$((i + 1))
        ;;
      -fp|-checkpoint|-lncheck|-coverage)
        if [ $((i + 1)) -ge $n ]; then
          echo "unsupported TLC flag: $a requires a value" >&2
          exit 2
        fi
        i=$((i + 2))
        ;;
      -*)
        echo "unsupported TLC flag: $a (not in run_tlc.sh's allowed extra-flag list)" >&2
        exit 2
        ;;
      *)
        echo "unsupported TLC argument: $a (extra positional operands after -- are not allowed)" >&2
        exit 2
        ;;
    esac
  done
fi

java="$(resolve_java)" || { echo "no usable Java 17+ found (set JAVA=/path/to/java)" >&2; exit 2; }
jar="$(resolve_tla_jar)" || { echo "no tla2tools jar found (set TLA2TOOLS_JAR)" >&2; exit 2; }

spec_abs="$(cd "$(dirname "$spec")" && pwd)/$(basename "$spec")"
cfg_abs="$(cd "$(dirname "$cfg")" && pwd)/$(basename "$cfg")"
spec_dir="$(dirname "$spec_abs")"
spec_name="$(basename "$spec_abs")"

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/run_tlc.XXXXXX")"
metadir="$work_dir/meta"
mkdir -p "$metadir"

pid=""
cleanup() {
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null
    sleep 1
    kill -KILL "$pid" 2>/dev/null
  fi
  rm -rf "$work_dir"
}
trap cleanup EXIT

if [ -n "$out" ]; then
  out_dir_raw="$(dirname "$out")"
  if ! mkdir -p "$out_dir_raw" 2>/dev/null; then
    echo "cannot create --out directory: $out_dir_raw" >&2
    exit 2
  fi
  out_dir="$(cd "$out_dir_raw" && pwd)"
  out_base="$(basename "$out")"
  out_base="${out_base%.json}"
  raw_log="$out_dir/$out_base.log"
else
  raw_log="$work_dir/raw.log"
fi

cmd=("$java" -XX:+UseParallelGC -cp "$jar" tlc2.TLC -tool -cleanup -workers "$workers" -metadir "$metadir" -config "$cfg_abs")
if [ ${#extra_args[@]} -gt 0 ]; then
  cmd+=("${extra_args[@]}")
fi
cmd+=("$spec_name")

timeout_flag="$work_dir/timed_out"
start_seconds="$SECONDS"

(
  cd "$spec_dir" || exit 127
  exec "${cmd[@]}"
) > "$raw_log" 2>&1 &
pid=$!

watcher=""
if [ -n "$timeout_secs" ]; then
  (
    remaining="$timeout_secs"
    while [ "$remaining" -gt 0 ]; do
      kill -0 "$pid" 2>/dev/null || exit 0
      sleep 1
      remaining=$((remaining - 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      touch "$timeout_flag"
      kill -TERM "$pid" 2>/dev/null
      sleep 2
      kill -KILL "$pid" 2>/dev/null
    fi
  ) &
  watcher=$!
fi

wait "$pid" 2>/dev/null
exit_code=$?
pid=""

if [ -n "$watcher" ]; then
  kill "$watcher" 2>/dev/null
  wait "$watcher" 2>/dev/null
fi

elapsed=$((SECONDS - start_seconds))
timed_out_args=()
if [ -f "$timeout_flag" ]; then
  timed_out_args=(--timed-out)
fi

self_abs="$here/$(basename "${BASH_SOURCE[0]}")"
command_parts=("$self_abs")
if [ ${#orig_args[@]} -gt 0 ]; then
  command_parts+=("${orig_args[@]}")
fi
if [ "$spec_arg_index" != "-1" ]; then
  command_parts[$((spec_arg_index + 1))]="$spec_abs"
fi
if [ "$cfg_arg_index" != "-1" ]; then
  command_parts[$((cfg_arg_index + 1))]="$cfg_abs"
fi
command_str="$(printf '%q ' "${command_parts[@]}")"
command_str="${command_str% }"

report_cmd=(python3 "$here/tlc_report.py" --log "$raw_log" --cfg "$cfg_abs" --exit-code "$exit_code" \
  --elapsed "$elapsed" --jar "$jar" --spec "$spec_abs" --command "$command_str")
if [ -n "$out" ]; then
  report_cmd+=(--raw-log-path "$raw_log")
fi
if [ "$deadlock_flag" = 1 ]; then
  report_cmd+=(--deadlock-flag)
fi
if [ ${#timed_out_args[@]} -gt 0 ]; then
  report_cmd+=("${timed_out_args[@]}")
fi
json="$("${report_cmd[@]}")"

if [ -n "$out" ]; then
  printf '%s\n' "$json" > "$out"
fi
if [ "$quiet" = 1 ] && [ -n "$out" ]; then
  summary="$(printf '%s\n' "$json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
violated = d.get("violated") or "-"
states = d.get("distinct_states")
states = states if states is not None else "-"
depth = d.get("depth")
depth = depth if depth is not None else "-"
print("%s %s states=%s depth=%s" % (d["result"], violated, states, depth))
')"
  echo "$summary -> $out"
else
  echo "$json"
fi

result="$(printf '%s\n' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"])')"

case "$result" in
  pass) exit 0 ;;
  invariant_violation|property_violation|deadlock|assumption_violation|assertion_violation|vacuous) exit 1 ;;
  *) exit 2 ;;
esac
