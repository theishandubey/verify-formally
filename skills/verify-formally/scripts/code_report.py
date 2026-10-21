#!/usr/bin/env python3
# Parses the raw output of a code-level verifier into the result record that
# run_code_check.py writes and check_run.py replays. One parser per tool; each takes the raw
# log text (the tool's stdout and stderr, without run_code_check.py's header line), the exit
# code, and whether the run timed out, and returns a dict with:
#
#   result           pass | violation | incomplete | vacuous | error | timeout
#   exhaustive       true only for a pass that covers every behavior within the stated bounds:
#                    a bounded model checker with no unwinding failure and no undetermined
#                    check, or CrossHair with every condition "Confirmed over all paths"
#   violated         sorted list of failing check ids (unwinding failures excluded)
#   failed_checks    [{"id", "description", "location"}] for every failing check
#   unwinding_failures  ids of failed unwinding assertions (the bound was too small)
#   checks_total     number of checks the tool reported (null when the tool does not say)
#   counterexample   the first lines of the counterexample or failure report, or ""
#
# Results:
#   pass        the tool finished and found no failing check
#   violation   at least one property check failed (a counterexample exists)
#   incomplete  no property failed, but the bound was too small (unwinding assertion failed) or
#               a check stayed undetermined; this is never a pass
#   vacuous     the tool ran but checked nothing (no harness, no contract, no test collected,
#               or every test skipped)
#   error       the tool did not run to a verdict (build error, bad arguments, crash)
#   timeout     run_code_check.py killed the tool at --timeout
import re

TOOLS = ("cbmc", "jbmc", "esbmc", "kani", "crosshair", "hypothesis")
KIND = {
    "cbmc": "bounded_model_check",
    "jbmc": "bounded_model_check",
    "esbmc": "bounded_model_check",
    "kani": "bounded_model_check",
    "crosshair": "symbolic_execution",
    "hypothesis": "generated_inputs",
}
HEADER_PREFIX = "@@verify-formally run_code_check"
COUNTEREXAMPLE_LINES = 60


def strip_header(text):
    lines = text.split("\n")
    if lines and lines[0].startswith(HEADER_PREFIX):
        return "\n".join(lines[1:])
    return text


def _base(result):
    return {
        "result": result,
        "exhaustive": False,
        "violated": [],
        "failed_checks": [],
        "unwinding_failures": [],
        "checks_total": None,
        "counterexample": "",
    }


def _excerpt(lines, start):
    return "\n".join(lines[start:start + COUNTEREXAMPLE_LINES]).strip()


def _is_unwind(check_id, description):
    return ".unwind." in check_id or "unwinding assertion" in (description or "").lower()


CBMC_CHECK_RE = re.compile(
    r"^\[(?P<id>[^\]]+)\] (?:line (?P<line>\d+) )?(?P<desc>.*): "
    r"(?P<status>SUCCESS|FAILURE|UNKNOWN)$")


def parse_cbmc(text, exit_code, timed_out):
    lines = text.split("\n")
    out = _base("error")
    checks = []
    for line in lines:
        m = CBMC_CHECK_RE.match(line.strip())
        if m:
            checks.append(m)
    verdict = None
    for line in lines:
        s = line.strip()
        if s == "VERIFICATION SUCCESSFUL":
            verdict = "ok"
        elif s == "VERIFICATION FAILED":
            verdict = "failed"
    out["checks_total"] = len(checks)
    undetermined = False
    for m in checks:
        if m.group("status") == "UNKNOWN":
            undetermined = True
        if m.group("status") != "FAILURE":
            continue
        cid, desc = m.group("id"), m.group("desc")
        loc = "line %s" % m.group("line") if m.group("line") else ""
        out["failed_checks"].append({"id": cid, "description": desc, "location": loc})
        if _is_unwind(cid, desc):
            out["unwinding_failures"].append(cid)
        else:
            out["violated"].append(cid)
    trace_start = next((i for i, l in enumerate(lines) if l.startswith("Trace for ")), None)
    if trace_start is None:
        trace_start = next((i for i, l in enumerate(lines) if "** Results:" in l), None)
    if out["failed_checks"] and trace_start is not None:
        out["counterexample"] = _excerpt(lines, trace_start)
    out["violated"].sort()
    if timed_out:
        out["result"] = "timeout"
    elif verdict is None:
        out["result"] = "error"
    elif out["violated"]:
        out["result"] = "violation"
    elif out["unwinding_failures"] or undetermined:
        out["result"] = "incomplete"
    elif verdict == "failed":
        out["result"] = "error"
    elif not checks:
        out["result"] = "vacuous"
    else:
        out["result"] = "pass"
        out["exhaustive"] = True
    return out


ESBMC_LOC_RE = re.compile(r"^\s*file (?P<file>\S+) line (?P<line>\d+)")


def parse_esbmc(text, exit_code, timed_out):
    lines = text.split("\n")
    out = _base("error")
    verdict = None
    for line in lines:
        s = line.strip()
        if s == "VERIFICATION SUCCESSFUL":
            verdict = "ok"
        elif s == "VERIFICATION FAILED":
            verdict = "failed"
        elif s == "VERIFICATION UNKNOWN":
            verdict = "unknown"
    for i, line in enumerate(lines):
        if line.strip() != "Violated property:":
            continue
        loc_line = lines[i + 1] if i + 1 < len(lines) else ""
        desc = lines[i + 2].strip() if i + 2 < len(lines) else ""
        m = ESBMC_LOC_RE.match(loc_line)
        loc = "%s:%s" % (m.group("file"), m.group("line")) if m else loc_line.strip()
        cid = "%s %s" % (loc, desc) if loc else desc
        out["failed_checks"].append({"id": cid, "description": desc, "location": loc})
        if _is_unwind(cid, desc):
            out["unwinding_failures"].append(cid)
        else:
            out["violated"].append(cid)
    start = next((i for i, l in enumerate(lines) if l.strip() == "[Counterexample]"), None)
    if start is not None:
        out["counterexample"] = _excerpt(lines, start)
    out["violated"].sort()
    if timed_out:
        out["result"] = "timeout"
    elif verdict is None:
        out["result"] = "error"
    elif out["violated"]:
        out["result"] = "violation"
    elif verdict == "unknown" or out["unwinding_failures"]:
        out["result"] = "incomplete"
    elif verdict == "failed":
        out["result"] = "error"
    else:
        out["result"] = "pass"
        out["exhaustive"] = True
    return out


KANI_HARNESS_RE = re.compile(r"^Checking harness (?P<name>\S+?)\.\.\.$")
KANI_CHECK_RE = re.compile(r"^Check \d+: (?P<id>.+)$")
KANI_FIELD_RE = re.compile(r"^\s*- (?P<key>Status|Description|Location): (?P<value>.*)$")


def parse_kani(text, exit_code, timed_out):
    lines = text.split("\n")
    out = _base("error")
    harnesses = []
    current = None
    check = None
    for line in lines:
        s = line.rstrip()
        m = KANI_HARNESS_RE.match(s.strip())
        if m:
            current = {"name": m.group("name"), "checks": [], "verdict": None}
            harnesses.append(current)
            check = None
            continue
        if current is None:
            continue
        m = KANI_CHECK_RE.match(s)
        if m:
            check = {"id": m.group("id").strip(), "status": None, "description": "",
                     "location": ""}
            current["checks"].append(check)
            continue
        m = KANI_FIELD_RE.match(s)
        if m and check is not None:
            value = m.group("value").strip()
            if m.group("key") == "Description" and len(value) >= 2 and value[0] == '"' \
                    and value[-1] == '"':
                value = value[1:-1]
            check[m.group("key").lower()] = value
            continue
        if s.strip() == "VERIFICATION:- SUCCESSFUL":
            current["verdict"] = "ok"
        elif s.strip() == "VERIFICATION:- FAILED":
            current["verdict"] = "failed"
    no_harness = any("No proof harnesses" in l for l in lines)
    undetermined = False
    total = 0
    for h in harnesses:
        for c in h["checks"]:
            total += 1
            if c["status"] == "UNDETERMINED":
                undetermined = True
            if c["status"] != "FAILURE":
                continue
            cid = "%s::%s" % (h["name"], c["id"])
            out["failed_checks"].append({"id": cid, "description": c["description"],
                                         "location": c["location"]})
            if _is_unwind(c["id"], c["description"]):
                out["unwinding_failures"].append(cid)
            else:
                out["violated"].append(cid)
    out["checks_total"] = total
    out["harnesses"] = [h["name"] for h in harnesses]
    start = next((i for i, l in enumerate(lines) if l.startswith("SUMMARY:")), None)
    if out["failed_checks"] and start is not None:
        out["counterexample"] = _excerpt(lines, start)
    out["violated"].sort()
    verdicts = [h["verdict"] for h in harnesses]
    if timed_out:
        out["result"] = "timeout"
    elif no_harness and not harnesses:
        out["result"] = "vacuous"
    elif not harnesses or any(v is None for v in verdicts):
        out["result"] = "error"
    elif out["violated"]:
        out["result"] = "violation"
    elif out["unwinding_failures"] or undetermined:
        out["result"] = "incomplete"
    elif any(v == "failed" for v in verdicts):
        out["result"] = "error"
    elif total == 0:
        out["result"] = "vacuous"
    else:
        out["result"] = "pass"
        out["exhaustive"] = True
    return out


CROSSHAIR_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+): (?P<level>error|info|warning): (?P<msg>.*)$")


def parse_crosshair(text, exit_code, timed_out):
    lines = text.split("\n")
    out = _base("error")
    confirmed = 0
    unconfirmed = 0
    for line in lines:
        m = CROSSHAIR_RE.match(line.strip())
        if not m:
            continue
        # CrossHair prints absolute paths for module targets; the basename keeps ids stable
        # when the repository moves.
        loc = "%s:%s" % (m.group("file").replace("\\", "/").rsplit("/", 1)[-1],
                         m.group("line"))
        msg = m.group("msg").strip()
        if m.group("level") == "error":
            cid = "%s %s" % (loc, msg.split(" when calling ")[0])
            out["failed_checks"].append({"id": cid, "description": msg, "location": loc})
            out["violated"].append(cid)
        elif m.group("level") == "info" and msg.startswith("Confirmed over all paths"):
            confirmed += 1
        elif m.group("level") == "info" and msg.startswith("Not confirmed"):
            unconfirmed += 1
    out["checks_total"] = confirmed + unconfirmed + len(out["failed_checks"])
    out["conditions_confirmed"] = confirmed
    out["conditions_not_confirmed"] = unconfirmed
    out["violated"] = sorted(set(out["violated"]))
    if out["violated"]:
        out["counterexample"] = "\n".join(c["description"] for c in out["failed_checks"])
    no_checkable = any("contain no checkable functions" in l for l in lines)
    if timed_out:
        out["result"] = "timeout"
    elif out["violated"] and exit_code in (0, 1):
        out["result"] = "violation"
    elif exit_code not in (0, 1) or any(l.startswith("Traceback") for l in lines):
        out["result"] = "error"
    elif no_checkable or out["checks_total"] == 0:
        out["result"] = "vacuous"
    else:
        out["result"] = "pass"
        out["exhaustive"] = unconfirmed == 0
    return out


PYTEST_SUMMARY_RE = re.compile(r"^=*\s*(?P<body>\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|no tests ran).*?) in [\d.]+s(?: \([^)]*\))?\s*=*$")
PYTEST_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")
PYTEST_FAILED_RE = re.compile(r"^FAILED (?P<id>\S+)")


def parse_hypothesis(text, exit_code, timed_out):
    lines = text.split("\n")
    out = _base("error")
    counts = {}
    for line in lines:
        m = PYTEST_SUMMARY_RE.match(line.strip())
        if m:
            counts = {}
            for n, kind in PYTEST_COUNT_RE.findall(m.group("body")):
                kind = "errors" if kind.startswith("error") else kind
                counts[kind] = counts.get(kind, 0) + int(n)
    for line in lines:
        m = PYTEST_FAILED_RE.match(line.strip())
        if m:
            out["failed_checks"].append({"id": m.group("id"), "description": line.strip(),
                                         "location": m.group("id").split("::")[0]})
            out["violated"].append(m.group("id"))
    out["violated"] = sorted(set(out["violated"]))
    out["checks_total"] = sum(counts.get(k, 0) for k in ("passed", "failed"))
    out["test_counts"] = counts
    error_lines = [l[1:].strip() for l in lines if l.startswith("E ")]
    if out["violated"] and error_lines:
        out["counterexample"] = "\n".join(error_lines[:COUNTEREXAMPLE_LINES])
    elif out["violated"]:
        start = next((i for i, l in enumerate(lines) if l.startswith("FAILED ")), None)
        if start is not None:
            out["counterexample"] = _excerpt(lines, start)
    if timed_out:
        out["result"] = "timeout"
    elif exit_code == 5 or (counts and not counts.get("passed") and not counts.get("failed")
                            and not counts.get("errors")):
        out["result"] = "vacuous"
    elif counts.get("xfailed") or counts.get("xpassed"):
        out["result"] = "error"
        out["error_detail"] = "xfail markers can hide a failing property; remove them"
    elif counts.get("errors") or exit_code not in (0, 1) or not counts:
        out["result"] = "error"
    elif out["violated"] or counts.get("failed"):
        out["result"] = "violation"
    else:
        out["result"] = "pass"
    return out


PARSERS = {
    "cbmc": parse_cbmc,
    "jbmc": parse_cbmc,
    "esbmc": parse_esbmc,
    "kani": parse_kani,
    "crosshair": parse_crosshair,
    "hypothesis": parse_hypothesis,
}


def parse(tool, text, exit_code, timed_out):
    rec = PARSERS[tool](strip_header(text), exit_code, timed_out)
    rec["kind"] = KIND[tool]
    if rec["result"] != "pass":
        rec["exhaustive"] = False
    return rec
