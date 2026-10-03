# Usage: tlc_report.py --log RAW.log --cfg Spec.cfg --exit-code N --elapsed SECONDS
#            [--raw-log-path PATH] [--timed-out] [--jar JAR] [--spec Spec.tla] [--deadlock-flag]
#            [--command STR]
#
# Internal helper invoked by run_tlc.sh; see run_tlc.sh's header for the result value and
# exit code contract, and for the "spec"/"cfg"/"command" JSON fields.
import argparse
import json
import os
import re
import sys
import zipfile

MSG_RE = re.compile(r"@!@!@STARTMSG (\d+):(\d+) @!@!@\n(.*?)\n@!@!@ENDMSG \1 @!@!@", re.DOTALL)
STATE_HEADER_RE = re.compile(r"^(\d+):\s*(.*)$")
VAR_RE = re.compile(r"^(?:/\\ )?([A-Za-z_][A-Za-z0-9_]*) = (.*)$")
BACK_TO_RE = re.compile(r"^(\d+):\s*Back to state:")
STUTTER_HEADER_RE = re.compile(r"^(\d+):\s*Stuttering\s*$")
NESTED_MSG_RE = re.compile(r"^@!@!@(?:START|END)MSG\b.*$", re.MULTILINE)
DECLARED_CONST_BLOCK_RE = re.compile(
    r"\bCONSTANTS?\b(.*?)"
    r"(?=\n-{4,}|\n={4,}|\bVARIABLES?\b|\bASSUME\b|\bASSUMPTION\b|\bCONSTANTS?\b"
    r"|\bRECURSIVE\b|\bINSTANCE\b|\bLOCAL\b"
    r"|\n[A-Za-z_][A-Za-z0-9_']*\s*(?:\([^)]*\)|\[[^\]]*\])?\s*=="
    r"|\n[A-Za-z_][A-Za-z0-9_']*\s+\S+\s+[A-Za-z_][A-Za-z0-9_']*\s*=="
    r"|\Z)",
    re.DOTALL,
)

SEV_ERROR = 1
SEV_WARNING = 3

RESULT_BY_EXIT_CODE = {
    0: "pass",
    10: "assumption_violation",
    11: "deadlock",
    12: "invariant_violation",
    13: "property_violation",
    14: "assertion_violation",
    150: "error",
}

VIOLATION_CODES = {
    2104: "assumption_violation",
    2105: "error",
    2107: "invariant_violation",
    2108: "property_violation",
    2110: "invariant_violation",
    2111: "error",
    2112: "property_violation",
    2113: "error",
    2114: "deadlock",
    2116: "property_violation",
    2132: "assertion_violation",
    2146: "error",
    2229: "error",
}

PROPERTY_ATTRIBUTABLE_CODES = {2108, 2112, 2116}

SINGLE_VALUE_KEYWORDS = {
    "INIT", "NEXT", "SPECIFICATION", "VIEW", "CHECK_DEADLOCK", "POSTCONDITION", "ALIAS",
}
LIST_KEYWORDS = {
    "CONSTANT", "CONSTANTS", "INVARIANT", "INVARIANTS", "PROPERTY", "PROPERTIES",
    "CONSTRAINT", "CONSTRAINTS", "ACTION_CONSTRAINT", "ACTION_CONSTRAINTS", "SYMMETRY",
}
ALL_KEYWORDS = SINGLE_VALUE_KEYWORDS | LIST_KEYWORDS


def strip_cfg_comments(text):
    out = []
    i = 0
    n = len(text)
    in_string = False
    block_depth = 0
    while i < n:
        c = text[i]
        if block_depth > 0:
            if text.startswith("(*", i):
                block_depth += 1
                out.append("  ")
                i += 2
                continue
            if text.startswith("*)", i):
                block_depth -= 1
                out.append("  ")
                i += 2
                continue
            out.append("\n" if c == "\n" else " ")
            i += 1
            continue
        if in_string:
            if c == "\\" and i + 1 < n:
                out.append(text[i:i + 2])
                i += 2
                continue
            out.append(c)
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if text.startswith("(*", i):
            block_depth = 1
            out.append("  ")
            i += 2
            continue
        if text.startswith("\\*", i):
            j = text.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def tokenize_cfg(text):
    tokens = []
    i = 0
    n = len(text)
    open_close = {"{": "}", "[": "]"}
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            j = min(j + 1, n)
            tokens.append(text[i:j])
            i = j
            continue
        if text.startswith("<<", i):
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if text.startswith("<<", j):
                    depth += 1
                    j += 2
                    continue
                if text.startswith(">>", j):
                    depth -= 1
                    j += 2
                    continue
                j += 1
            tokens.append(" ".join(text[i:j].split()))
            i = j
            continue
        if c in open_close:
            close = open_close[c]
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if text[j] == c:
                    depth += 1
                elif text[j] == close:
                    depth -= 1
                j += 1
            tokens.append(" ".join(text[i:j].split()))
            i = j
            continue
        if text.startswith("<-", i):
            tokens.append("<-")
            i += 2
            continue
        if c == "=":
            tokens.append("=")
            i += 1
            continue
        j = i
        while (
            j < n
            and not text[j].isspace()
            and text[j] not in "{}[]\"="
            and not text.startswith("<<", j)
            and not text.startswith(">>", j)
            and not text.startswith("<-", j)
        ):
            j += 1
        if j == i:
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


def parse_cfg_tokens(tokens):
    constants = {}
    invariants = []
    properties = []
    single_values = {}
    section = None
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in SINGLE_VALUE_KEYWORDS:
            section = None
            i += 1
            if i < n and tokens[i] not in ALL_KEYWORDS:
                single_values[tok] = tokens[i]
                i += 1
            continue
        if tok in LIST_KEYWORDS:
            section = tok
            i += 1
            continue
        if section in ("CONSTANT", "CONSTANTS"):
            if i + 2 < n and tokens[i + 1] in ("=", "<-"):
                constants[tok] = tokens[i + 2]
                i += 3
                continue
            i += 1
            continue
        if section in ("INVARIANT", "INVARIANTS"):
            invariants.append(tok)
            i += 1
            continue
        if section in ("PROPERTY", "PROPERTIES"):
            properties.append(tok)
            i += 1
            continue
        i += 1
    return constants, invariants, properties, single_values


def parse_cfg(path):
    with open(path) as f:
        raw = f.read()
    tokens = tokenize_cfg(strip_cfg_comments(raw))
    return parse_cfg_tokens(tokens)


EXTENDS_RE = re.compile(r"\bEXTENDS\b(.*?)(?=\n----|\Z)", re.DOTALL)


def parse_extended_module_names(text):
    m = EXTENDS_RE.search(text)
    if not m:
        return []
    names = []
    for tok in re.split(r"[,\n]", m.group(1)):
        tok = tok.strip()
        if not tok:
            continue
        nm = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)", tok)
        if nm:
            names.append(nm.group(1))
    return names


def parse_declared_constants(spec_path):
    if not spec_path:
        return None
    spec_dir = os.path.dirname(spec_path)
    names = set()
    visited = set()

    def visit(path):
        if not path or path in visited:
            return
        visited.add(path)
        try:
            with open(path, errors="replace") as f:
                text = f.read()
        except OSError:
            return
        text = re.sub(r"\(\*.*?\*\)", " ", text, flags=re.DOTALL)
        text = "\n".join(line.split("\\*", 1)[0] for line in text.split("\n"))
        for m in DECLARED_CONST_BLOCK_RE.finditer(text):
            for tok in re.split(r"[,\n]", m.group(1)):
                tok = tok.strip()
                if not tok:
                    continue
                nm = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)", tok)
                if nm:
                    names.add(nm.group(1))
        for module_name in parse_extended_module_names(text):
            candidate = os.path.join(spec_dir, module_name + ".tla")
            if os.path.isfile(candidate):
                visit(candidate)

    visit(spec_path)
    return names


def parse_messages(log_text):
    return [(int(code), int(sev), body) for code, sev, body in MSG_RE.findall(log_text)]


def clean_body(body):
    lines = [line for line in body.splitlines() if not NESTED_MSG_RE.match(line.strip())]
    return "\n".join(lines).strip()


def parse_trace_block(body):
    lines = body.splitlines()
    header = STATE_HEADER_RE.match(lines[0])
    index = int(header.group(1))
    action = header.group(2).strip()
    variables = {}
    last_name = None
    for line in lines[1:]:
        if not line.strip():
            continue
        m = VAR_RE.match(line)
        if m:
            last_name = m.group(1)
            variables[last_name] = m.group(2)
        elif last_name is not None:
            variables[last_name] += "\n" + line.strip()
    return {"index": index, "action": action, "vars": variables}


def resolve_tla2tools_release(jar_path):
    if not jar_path:
        return None
    try:
        with zipfile.ZipFile(jar_path) as zf:
            try:
                manifest = zf.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
            except KeyError:
                manifest = ""
        for key in ("X-Git-Tag", "Implementation-Version"):
            m = re.search(r"^" + key + r":\s*(.+)$", manifest, re.MULTILINE)
            if m:
                value = m.group(1).strip()
                if key == "X-Git-Tag" and value.startswith("v"):
                    value = value[1:]
                if key == "Implementation-Version":
                    value = value.split()[0]
                return value
    except (OSError, zipfile.BadZipFile):
        pass
    m = re.search(r"tla2tools-([0-9][0-9.]*)\.jar$", jar_path)
    if m:
        return m.group(1)
    return None


def build_result(log_text, cfg_path, exit_code, elapsed_seconds, raw_log_path, timed_out, jar_path,
                  spec_path=None, deadlock_flag=False, command=None):
    constants, invariants, properties, single_values = parse_cfg(cfg_path)
    messages = parse_messages(log_text)
    declared_constants = parse_declared_constants(spec_path)

    tlc_version = None
    trace = []
    loop_to = None
    states_generated = None
    distinct_states = None
    depth = None
    violated = None
    violated_candidates = None
    violated_note = None
    log_elapsed = None
    error_detail = None
    warnings = []
    message_result = None
    message_result_code = None
    saw_completion_message = False

    for code, sev, body in messages:
        if code == 2262:
            m = re.search(r"Version (\S+)", body)
            if m:
                tlc_version = m.group(1)
        elif code == 2217:
            trace.append(parse_trace_block(body))
        elif code == 2218:
            m = STUTTER_HEADER_RE.match(body.strip())
            if m:
                trace.append({"index": int(m.group(1)), "action": "Stuttering", "vars": {}})
        elif code == 2122:
            m = BACK_TO_RE.match(body.strip())
            if m:
                loop_to = int(m.group(1))
        elif code == 2193:
            saw_completion_message = True
        elif code == 2199:
            m = re.search(r"(\d+) states generated, (\d+) distinct states found", body)
            if m:
                states_generated = int(m.group(1))
                distinct_states = int(m.group(2))
        elif code == 2194:
            m = re.search(r"search is (\d+)", body)
            if m:
                depth = int(m.group(1))
        elif code == 2186:
            m = re.search(r"Finished in (\d+)ms", body)
            if m:
                log_elapsed = int(m.group(1)) / 1000.0

        if sev == SEV_WARNING:
            warnings.append(clean_body(body))

        if code in VIOLATION_CODES and message_result is None:
            message_result = VIOLATION_CODES[code]
            message_result_code = code
            if code in (2110, 2107):
                m = re.search(r"Invariant (\S+) is violated", body)
                if m:
                    violated = m.group(1)
            elif code in PROPERTY_ATTRIBUTABLE_CODES:
                if len(properties) == 1:
                    violated = properties[0]
                elif len(properties) > 1:
                    violated_candidates = list(properties)
                    violated_note = (
                        "multiple PROPERTY entries are configured; TLC's -tool output does "
                        "not name which one was violated. Re-run with a single PROPERTY "
                        "configured to get attribution."
                    )
    if depth is None and trace:
        depth = len(trace)

    if timed_out:
        result = "timeout"
    elif message_result is not None:
        result = message_result
    elif exit_code in RESULT_BY_EXIT_CODE:
        result = RESULT_BY_EXIT_CODE[exit_code]
    else:
        result = "error"

    if message_result_code is not None:
        for code, sev, body in messages:
            if code == message_result_code:
                error_detail = clean_body(body)
                break
    if error_detail is None:
        for code, sev, body in messages:
            if sev == SEV_ERROR and code != 2121:
                error_detail = clean_body(body)
                break

    if declared_constants is not None:
        undeclared = sorted(set(constants) - declared_constants)
        if undeclared:
            warnings.append(
                "cfg assigns constant(s) not declared in the spec: " + ", ".join(undeclared)
            )

    check_deadlock = single_values.get("CHECK_DEADLOCK", "").strip().upper() != "FALSE"
    if deadlock_flag:
        check_deadlock = False

    if result == "pass" and not (saw_completion_message and distinct_states is not None):
        result = "error"
        if error_detail is None:
            error_detail = (
                "TLC exited 0 but the log has neither the completion message (2193) nor "
                "the state-count stats (2199); an exhaustive pass cannot be confirmed "
                "(this happens with non-exhaustive TLC modes such as -dfid)."
            )

    no_checks_configured = not invariants and not properties and not check_deadlock
    if result == "pass" and (distinct_states == 0 or no_checks_configured):
        result = "vacuous"
    elif result == "pass" and warnings:
        result = "pass_with_warnings"

    keep_trace_results = {
        "invariant_violation", "property_violation", "deadlock", "assumption_violation",
        "assertion_violation", "timeout", "error",
    }
    if result not in keep_trace_results:
        trace = []
        loop_to = None

    out = {
        "result": result,
        "violated": violated,
        "states_generated": states_generated,
        "distinct_states": distinct_states,
        "depth": depth,
        "constants": constants,
        "invariants": invariants,
        "properties": properties,
        "check_deadlock": check_deadlock,
        "trace": trace,
        "tlc_version": tlc_version,
        "tla2tools_release": resolve_tla2tools_release(jar_path),
        "elapsed_seconds": log_elapsed if log_elapsed is not None else elapsed_seconds,
        "raw_log": raw_log_path,
        "exit_code": exit_code,
        "spec": spec_path,
        "cfg": cfg_path,
        "command": command,
    }
    if loop_to is not None:
        out["loop_to"] = loop_to
    if error_detail is not None:
        out["error_detail"] = error_detail
    if warnings:
        out["warnings"] = warnings
    if violated_candidates is not None:
        out["violated_candidates"] = violated_candidates
    if violated_note is not None:
        out["violated_note"] = violated_note
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--log", required=True)
    p.add_argument("--cfg", required=True)
    p.add_argument("--exit-code", required=True, type=int)
    p.add_argument("--elapsed", required=True, type=float)
    p.add_argument("--raw-log-path", default=None)
    p.add_argument("--timed-out", action="store_true")
    p.add_argument("--jar", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--deadlock-flag", action="store_true")
    p.add_argument("--command", default=None)
    args = p.parse_args()

    with open(args.log, errors="replace") as f:
        log_text = f.read()

    result = build_result(
        log_text, args.cfg, args.exit_code, args.elapsed, args.raw_log_path, args.timed_out, args.jar,
        args.spec, args.deadlock_flag, args.command,
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
