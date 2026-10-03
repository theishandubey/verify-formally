#!/usr/bin/env python3
# Usage: check_run.py <repo-root> [--json] [--no-exec]
#
# Validates a verify-formally skill run against verification/findings.json and the files
# around it. Prints ERRORs and WARNINGs, each with a one-line fix. Exit codes:
#   0  no errors (warnings are allowed)
#   1  at least one error (this includes findings.json missing or unparseable)
#   2  usage problem (bad arguments, repo-root does not exist)
#
# --json prints a JSON array of {"level", "check", "message", "fix"} instead of text.
# --no-exec skips C10 and C11, which run commands from findings.json (repro_runner,
# collect_command); everything else is static (reads files, git, JSON) and always runs.
# --no-exec always emits a WARNING that C10/C11 were skipped.
#
# This validator fails closed: any field with the wrong type, an unrecognized status/result/
# tool value, or a missing required key is an ERROR, not a silent pass.
#
# Checks:
#   C01  findings.json exists, parses, has the required fields, and every closed-set field
#        (statuses, results, tools) is spelled exactly as documented
#   C02  findings.json commit matches `git -C <repo> rev-parse HEAD`
#   C03  git status is confined to verification/ and plans/ (baseline.dirty_files excepted)
#   C04  a full run models at least 3 targets, or states fewer_targets_reason
#   C05  each modeled target has a real, non-trivial CORRESPONDENCE.md and >=1 property
#   C06  each tla property's runs[] point at real, current, matching result JSON, inside the
#        target's own directory, and a "violated" claim is backed by a run that actually
#        violated that property by name (not a different invariant, not a bare deadlock)
#   C07  each tla property's vacuity check (mutants + sanity) is real and non-vacuous
#   C08  a lean property claiming "proved" is backed by verification/lean/results.json
#   C09  each CONFIRMED finding's repro/guard tests, locations, properties, evidence are real,
#        and at least one cited property has result="violated" (a lean-only finding gets there
#        by proving the negation theorem for the buggy configuration and recording "violated")
#   C10  (exec) CONFIRMED repro tests fail with an assertion on current code; guard tests pass.
#        The exception type comes from the junit "message" attribute, which pytest fills in
#        the same way regardless of --tb; a non-pytest repro_runner with {file}/{test}/{dir}
#        placeholders is substituted per test id and judged by exit code plus output instead
#        (a non-pytest runner without placeholders is an ERROR; crash markers such as
#        "panic:" fail a repro; a guard must show its test ran: not skipped, not "no tests
#        to run", and not matched only by a name prefix)
#   C11  (exec) the project's normal test collection does not include verification/repro
#   C12  verification/README.md has the required sections; verification/rerun.sh is executable
#   C13  each CONFIRMED high/medium finding has a real plan with the required sections
#   C14  every needs_decision/model_only/rejection entry has a reason or evidence (WARN)
#   C15  a CONFIRMED finding resting on an inferred-source property is flagged (WARN)
import argparse
import glob
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

VIOLATION_RESULTS = {
    "invariant_violation", "property_violation", "deadlock", "assertion_violation",
}
NON_CLEAN_RUN_RESULTS = {"error", "timeout", "vacuous", "pass_with_warnings"}
FINDING_STATUSES = {
    "CONFIRMED", "NEEDS-DECISION", "MODEL-ONLY", "BY-DESIGN", "OUT-OF-SCOPE", "FIXED",
}
REJECTION_STATUSES = {"BY-DESIGN", "OUT-OF-SCOPE"}
PROPERTY_RESULTS = {
    "violated", "no_violation_within_bounds", "proved", "unproved", "vacuous", "not_checked",
}
PROPERTY_TOOLS = {"tla", "lean"}
TARGET_STATUSES = {"modeled", "not_modeled"}
SEVERITIES = {"high", "medium", "low"}
FILE_LINE_RE = re.compile(r"\w+\.\w+:\d+")
DEFAULT_PYTEST_DISCOVERY = [re.compile(r"^test_.*\.py$"), re.compile(r".*_test\.py$")]
REQUIRED_README_HEADINGS = ["Defaults taken", "Findings", "Properties checked", "Not modeled"]
REPRO_TIMEOUT_SECONDS = 300
FULL_RUN_TOKENS = {"deep", "non-interactive", "noninteractive", "tla", "lean", "full", "run"}
SCOPED_TOKENS = {"quick", "repro", "reconcile"}
COMMAND_WORDS = {"verify-formally", "verify"}


def error(check, message, fix):
    return {"level": "ERROR", "check": check, "message": message, "fix": fix}


def warn(check, message, fix):
    return {"level": "WARNING", "check": check, "message": message, "fix": fix}


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def load_json(path):
    try:
        return json.loads(read_text(path)), None
    except (OSError, json.JSONDecodeError) as e:
        return None, str(e)


def resolve_path(repo, p):
    if p is None:
        return None
    return p if os.path.isabs(p) else os.path.join(repo, p)


def resolve_repo_relative(repo, raw_path):
    if not raw_path or not isinstance(raw_path, str):
        return None
    idx = raw_path.find("verification/")
    if idx != -1:
        candidate = os.path.join(repo, raw_path[idx:])
        if os.path.isfile(candidate):
            return candidate
    if os.path.isabs(raw_path):
        if os.path.isfile(raw_path):
            return raw_path
        return None
    candidate = os.path.join(repo, raw_path)
    if os.path.isfile(candidate):
        return candidate
    return None


def in_target_dir(path, target_dir):
    if not path:
        return False
    real_path = os.path.realpath(path)
    real_dir = os.path.realpath(target_dir)
    return real_path == real_dir or real_path.startswith(real_dir + os.sep)


def run_git(repo, args):
    try:
        proc = subprocess.run(
            ["git", "-C", repo] + args, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout).strip()
    return proc.stdout, None


def git_head(repo):
    return run_git(repo, ["rev-parse", "HEAD"])


def git_status_porcelain(repo):
    out, err = run_git(repo, ["status", "--porcelain"])
    if out is None:
        return None, err
    return [l for l in out.splitlines() if l.strip()], None


def git_is_ignored(repo, rel_path):
    out, _err = run_git(repo, ["check-ignore", "-q", rel_path])
    return out is not None


IGNORED_STATUS_PREFIXES = ("verification/", "plans/", "verification-plans/")
IGNORED_NAME_PARTS = {"__pycache__", ".pytest_cache", ".lake", ".venv"}
PLAN_NAME_RE = re.compile(r"^\d+-.*\.md$")


def is_full_run(invocation):
    inv = (invocation or "").strip()
    if not inv:
        return False
    cleaned = re.sub(r"[(),]", " ", inv)
    tokens = []
    for raw_tok in cleaned.split():
        t = raw_tok.strip().strip("-").strip(".:;").lower()
        if not t:
            continue
        bare = t.lstrip("/")
        if bare in COMMAND_WORDS:
            continue
        tokens.append(t)
    if not tokens:
        return True
    for t in tokens:
        if t in SCOPED_TOKENS:
            return False
        if "/" in t or "\\" in t:
            return False
        if t not in FULL_RUN_TOKENS:
            return False
    return True


def _ensure_list(container, key, item_type, check, issues, label):
    v = container.get(key)
    if v is None:
        return
    if not isinstance(v, list):
        issues.append(error(check, "%s must be a list, got %s" % (label, type(v).__name__),
                             "make %r a JSON array" % key))
        container[key] = []
        return
    cleaned = []
    for i, item in enumerate(v):
        if not isinstance(item, item_type):
            issues.append(error(
                check, "%s[%d] must be a %s, got %s"
                % (label, i, item_type.__name__, type(item).__name__),
                "fix the entry's type",
            ))
            continue
        cleaned.append(item)
    container[key] = cleaned


def sanitize(raw):
    issues = []
    if not isinstance(raw, dict):
        return {}, [error(
            "C01", "findings.json top-level value must be a JSON object, got %s"
            % type(raw).__name__,
            "findings.json's root must be an object (schema/commit/targets/findings/"
            "baseline/...)",
        )]
    data = dict(raw)

    if "baseline" in data and data["baseline"] is not None and not isinstance(
            data["baseline"], dict):
        issues.append(error(
            "C01", "findings.json 'baseline' must be an object, got %s"
            % type(data["baseline"]).__name__, "make 'baseline' an object",
        ))
        data["baseline"] = {}

    for key in ("needs_decision", "model_only", "rejections", "refuted_claims", "plans",
                "coverage_gaps"):
        _ensure_list(data, key, dict, "C01", issues, "findings.json %r" % key)

    _ensure_list(data, "targets", dict, "C01", issues, "findings.json 'targets'")
    for i, t in enumerate(data.get("targets") or []):
        _ensure_list(t, "properties", dict, "C01", issues,
                     "findings.json targets[%d].properties" % i)
        for j, p in enumerate(t.get("properties") or []):
            label = "findings.json targets[%d].properties[%d]" % (i, j)
            if "runs" in p and p["runs"] is not None:
                _ensure_list(p, "runs", dict, "C01", issues, label + ".runs")
            if "theorems" in p and p["theorems"] is not None:
                _ensure_list(p, "theorems", dict, "C01", issues, label + ".theorems")
            if "vacuity" in p and p["vacuity"] is not None and not isinstance(
                    p["vacuity"], dict):
                issues.append(error(
                    "C01", "%s.vacuity must be an object, got %s"
                    % (label, type(p["vacuity"]).__name__),
                    "make 'vacuity' an object with status/mutants/sanity",
                ))
                p["vacuity"] = None

    _ensure_list(data, "findings", dict, "C01", issues, "findings.json 'findings'")
    for i, f in enumerate(data.get("findings") or []):
        label = "findings.json findings[%d]" % i
        _ensure_list(f, "locations", dict, "C01", issues, label + ".locations")
        for key in ("repro_tests", "guard_tests", "properties", "targets", "tools"):
            if key in f and f[key] is not None:
                _ensure_list(f, key, str, "C01", issues, "%s.%s" % (label, key))

    return data, issues


def normalize(raw):
    issues = []
    data = dict(raw)

    if "tree" not in data and "tree_dirty" in data:
        data["tree"] = "dirty" if data["tree_dirty"] else "clean"
        issues.append(warn("C01", "findings.json uses 'tree_dirty' instead of 'tree'",
                            "use the documented 'tree': 'clean'|'dirty' field"))

    norm_targets = []
    for t in data.get("targets") or []:
        t = dict(t)
        norm_props = []
        for prop in t.get("properties") or []:
            prop = dict(prop)
            if "runs" not in prop and ("config" in prop or "model" in prop):
                prop["runs"] = [{
                    "cfg": prop.get("config"),
                    "json": None,
                    "result": None,
                    "constants": prop.get("bounds"),
                    "distinct_states": prop.get("distinct_states"),
                }]
                prop["_runs_synthesized"] = True
                issues.append(warn(
                    "C06",
                    "target %r property %r uses schema v1 fields (model/config/result) "
                    "instead of runs[]" % (t.get("id"), prop.get("name")),
                    "upgrade to schema verify-formally-findings/2 with runs[].json pointing at "
                    "results/<cfg-stem>.json",
                ))
            norm_props.append(prop)
        t["properties"] = norm_props
        norm_targets.append(t)
    data["targets"] = norm_targets

    norm_findings = []
    for f in data.get("findings") or []:
        f = dict(f)
        fid = f.get("id", "?")
        for plural, singular in (
            ("targets", "target"), ("properties", "property"),
            ("repro_tests", "repro_test"), ("guard_tests", "guard_test"),
        ):
            if plural not in f and singular in f:
                v = f[singular]
                f[plural] = [v] if v else []
                issues.append(warn(
                    "C09", "finding %s uses %r instead of %r" % (fid, singular, plural),
                    "use the documented %r list field" % plural,
                ))
        norm_findings.append(f)
    data["findings"] = norm_findings

    return data, issues


class Context:
    def __init__(self, repo, findings):
        self.repo = repo
        self.findings = findings

    @property
    def targets(self):
        return self.findings.get("targets") or []


def check_c01(ctx):
    issues = []
    d = ctx.findings
    for key in ("schema", "commit", "invocation"):
        if key not in d:
            issues.append(error("C01", "findings.json is missing %r" % key,
                                 "add a top-level %r field" % key))
    if "commit" in d and not (isinstance(d.get("commit"), str) and d.get("commit").strip()):
        issues.append(error("C01", "findings.json 'commit' is empty",
                             "set commit to the full HEAD SHA (git rev-parse HEAD)"))
    if "invocation" in d and not (
            isinstance(d.get("invocation"), str) and d.get("invocation").strip()):
        issues.append(error("C01", "findings.json 'invocation' is empty",
                             "set invocation to the exact command that started this run"))
    if not isinstance(d.get("targets"), list):
        issues.append(error("C01", "findings.json 'targets' is missing or not a list",
                             "add a top-level 'targets' list"))
    if not isinstance(d.get("findings"), list):
        issues.append(error("C01", "findings.json 'findings' is missing or not a list",
                             "add a top-level 'findings' list"))
    baseline = d.get("baseline")
    if not isinstance(baseline, dict):
        issues.append(error("C01", "findings.json is missing 'baseline'",
                             "add a 'baseline' object with test_command/repro_runner/"
                             "collect_command"))
        baseline = {}
    else:
        for key in ("test_command", "repro_runner", "collect_command"):
            if not baseline.get(key):
                issues.append(error("C01", "findings.json baseline is missing %r" % key,
                                     "add baseline.%s" % key))
        if "dirty_files" in baseline and not isinstance(baseline.get("dirty_files"), list):
            issues.append(error("C01", "findings.json baseline.dirty_files must be a list",
                                 "make dirty_files a JSON array of paths (empty if none)"))

    for i, t in enumerate(d.get("targets") or []):
        tid = t.get("id", "target[%d]" % i)
        if "status" not in t:
            issues.append(error("C01", "target %s is missing 'status'" % tid,
                                 "set status to 'modeled' or 'not_modeled'"))
        elif t.get("status") not in TARGET_STATUSES:
            issues.append(error(
                "C01", "target %s has status %r, not one of %s"
                % (tid, t.get("status"), sorted(TARGET_STATUSES)),
                "use exactly 'modeled' or 'not_modeled'",
            ))
        for j, p in enumerate(t.get("properties") or []):
            pname = p.get("name", "property[%d]" % j)
            tag = "%s/%s" % (tid, pname)
            if "tool" not in p:
                issues.append(error("C01", "%s is missing 'tool'" % tag,
                                     "set tool to 'tla' or 'lean'"))
            elif p.get("tool") not in PROPERTY_TOOLS:
                issues.append(error(
                    "C01", "%s has tool %r, not one of %s"
                    % (tag, p.get("tool"), sorted(PROPERTY_TOOLS)),
                    "use exactly 'tla' or 'lean'",
                ))
            if "result" not in p:
                issues.append(error("C01", "%s is missing 'result'" % tag,
                                     "set result to one of %s" % sorted(PROPERTY_RESULTS)))
            elif p.get("result") not in PROPERTY_RESULTS:
                issues.append(error(
                    "C01", "%s has result %r, not one of %s"
                    % (tag, p.get("result"), sorted(PROPERTY_RESULTS)),
                    "use exactly one of %s" % sorted(PROPERTY_RESULTS),
                ))

    for i, f in enumerate(d.get("findings") or []):
        fid = f.get("id", "finding[%d]" % i)
        if "status" not in f:
            issues.append(error("C01", "finding %s is missing 'status'" % fid,
                                 "set status to one of %s" % sorted(FINDING_STATUSES)))
        elif f.get("status") not in FINDING_STATUSES:
            issues.append(error(
                "C01", "finding %s has status %r, not one of %s"
                % (fid, f.get("status"), sorted(FINDING_STATUSES)),
                "use exactly one of %s" % sorted(FINDING_STATUSES),
            ))
        sev = f.get("severity")
        if sev is not None and sev not in SEVERITIES:
            issues.append(error(
                "C01", "finding %s has severity %r, not one of %s"
                % (fid, sev, sorted(SEVERITIES)),
                "use exactly one of 'high' | 'medium' | 'low'",
            ))

    for i, r in enumerate(d.get("rejections") or []):
        label = "rejections[%d]" % i
        if "status" not in r:
            issues.append(error("C01", "%s is missing 'status'" % label,
                                 "set status to 'BY-DESIGN' or 'OUT-OF-SCOPE'"))
        elif r.get("status") not in REJECTION_STATUSES:
            issues.append(error(
                "C01", "%s has status %r, not one of %s"
                % (label, r.get("status"), sorted(REJECTION_STATUSES)),
                "use exactly 'BY-DESIGN' or 'OUT-OF-SCOPE'",
            ))
    return issues


def check_c02(ctx):
    head, err = git_head(ctx.repo)
    if head is None:
        return [error("C02", "cannot determine HEAD commit for %s (%s)" % (ctx.repo, err),
                       "run check_run.py against a git repository with at least one commit")]
    head = head.strip()
    commit = ctx.findings.get("commit")
    if not commit:
        return [error("C02", "findings.json 'commit' is empty",
                       "set commit to the full HEAD SHA (git rev-parse HEAD)")]
    if commit != head:
        return [error("C02", "findings.json commit %s does not match HEAD %s" % (commit, head),
                       "re-run the verify-formally skill, or update commit to match HEAD")]
    return []


def _c03_top_level_ignored(repo, path, cache):
    first = path.split("/", 1)[0]
    if first not in IGNORED_NAME_PARTS:
        return False
    if first not in cache:
        cache[first] = git_is_ignored(repo, first)
    return cache[first]


def check_c03(ctx):
    lines, err = git_status_porcelain(ctx.repo)
    if lines is None:
        return [error("C03", "cannot read git status for %s (%s)" % (ctx.repo, err),
                       "run check_run.py against a git repository")]
    dirty_files = set(ctx.findings.get("baseline", {}).get("dirty_files") or [])
    ignore_cache = {}
    offenders = []
    for line in lines:
        status_code = line[:2]
        path_part = line[3:]
        paths = [path_part]
        if " -> " in path_part:
            old, new = path_part.split(" -> ", 1)
            paths = [old, new]
        is_offender = False
        for raw_path in paths:
            path = raw_path.strip().strip('"')
            if path in dirty_files:
                continue
            if _c03_top_level_ignored(ctx.repo, path, ignore_cache):
                continue
            if path.startswith("verification/"):
                continue
            if path.startswith(("plans/", "verification-plans/")):
                index_status = status_code[0]
                untracked_or_added = status_code == "??" or index_status == "A"
                base = os.path.basename(path)
                allowed_name = bool(PLAN_NAME_RE.match(base)) or base == "README.md"
                if untracked_or_added or allowed_name:
                    continue
            is_offender = True
        if is_offender:
            offenders.append(line)
    if offenders:
        return [error(
            "C03",
            "git status shows changes outside verification/ and plans/: %s"
            % "; ".join(offenders[:5]),
            "the verify-formally skill never modifies source files or the project's own tests; "
            "revert or move the change",
        )]
    return []


def check_c04(ctx):
    inv = ctx.findings.get("invocation") or ""
    if not is_full_run(inv):
        return []
    modeled = [t for t in ctx.targets if t.get("status") == "modeled"]
    if len(modeled) >= 3:
        return []
    reason = ctx.findings.get("fewer_targets_reason")
    if reason and str(reason).strip():
        return [warn(
            "C04",
            "only %d modeled target(s) in a full run (%r); fewer_targets_reason=%r"
            % (len(modeled), inv, reason),
            "confirm the reason is real; a full run should usually model the top 3 targets",
        )]
    return [error(
        "C04",
        "only %d modeled target(s) in a full run (%r), and fewer_targets_reason is empty"
        % (len(modeled), inv),
        "model at least 3 targets, or set fewer_targets_reason explaining why fewer were "
        "modeled",
    )]


def check_c05(ctx):
    issues = []
    for t in ctx.targets:
        if t.get("status") != "modeled":
            continue
        tid = t.get("id", "?")
        corr = os.path.join(ctx.repo, "verification", "models", tid, "CORRESPONDENCE.md")
        if not os.path.isfile(corr):
            issues.append(error(
                "C05", "%s: CORRESPONDENCE.md not found at %s" % (tid, corr),
                "write verification/models/<id>/CORRESPONDENCE.md mapping variables/actions/"
                "guards/constants to file:line",
            ))
        else:
            text = read_text(corr)
            nonempty = [l for l in text.splitlines() if l.strip()]
            if len(nonempty) <= 20:
                issues.append(error(
                    "C05",
                    "%s: CORRESPONDENCE.md has only %d non-empty lines (need more than 20)"
                    % (tid, len(nonempty)),
                    "expand CORRESPONDENCE.md with the variable/action/guard/constant mapping",
                ))
            if not FILE_LINE_RE.search(text):
                issues.append(error(
                    "C05", "%s: CORRESPONDENCE.md has no file:line reference" % tid,
                    "cite the real code locations (path/file.py:NN) the model corresponds to",
                ))
        if not t.get("properties"):
            issues.append(error("C05", "%s: modeled target has no properties" % tid,
                                 "add at least one property with a statement and source"))
    return issues


def resolve_result_json(entry, target_dir, repo):
    if not isinstance(entry, str):
        return None
    m = re.search(r"([^\s'\"]+\.json)", entry)
    if m:
        raw = m.group(1)
        candidates = [resolve_path(repo, raw), os.path.join(target_dir, "results",
                                                              os.path.basename(raw))]
        for c in candidates:
            if c and os.path.isfile(c) and in_target_dir(c, target_dir):
                return c
    m2 = re.search(r"([^\s'\":/]+)\.(?:cfg|tla)", entry)
    stem = m2.group(1) if m2 else entry.split(":")[0].strip()
    stem = os.path.basename(stem)
    if not stem:
        return None
    for cand in (
        os.path.join(target_dir, "results", stem + ".json"),
        os.path.join(target_dir, "results", "mutant-" + stem + ".json"),
        os.path.join(target_dir, "results", "sanity-" + stem + ".json"),
    ):
        if os.path.isfile(cand):
            return cand
    return None


def _resolve_run_infos(ctx, prop):
    infos = []
    for run in prop.get("runs") or []:
        json_path = run.get("json")
        abs_json = resolve_repo_relative(ctx.repo, json_path) if json_path else None
        data = None
        if abs_json:
            data, err = load_json(abs_json)
            if err:
                data = None
        cfg_real = None
        if data is not None:
            resolved_cfg = resolve_repo_relative(ctx.repo, data.get("cfg"))
            if resolved_cfg:
                cfg_real = os.path.realpath(resolved_cfg)
        infos.append({"run": run, "data": data, "cfg_real": cfg_real})
    return infos


def check_c06(ctx):
    issues = []
    for t in ctx.targets:
        tid = t.get("id", "?")
        target_dir = os.path.join(ctx.repo, "verification", "models", tid)
        spec_files = sorted(glob.glob(os.path.join(target_dir, "*.tla")))
        spec_files += sorted(glob.glob(os.path.join(target_dir, "mutants", "*.tla")))
        spec_mtime = max((os.path.getmtime(f) for f in spec_files), default=None)
        for prop in t.get("properties") or []:
            if prop.get("tool") != "tla":
                continue
            name = prop.get("name", "?")
            tag = "%s/%s" % (tid, name)
            if prop.get("result") in ("proved", "unproved"):
                issues.append(error(
                    "C06",
                    "%s: tla property has result=%r, but 'proved'/'unproved' apply only to "
                    "lean theorems" % (tag, prop.get("result")),
                    "TLC checks a bounded model, it does not prove; use 'violated' or "
                    "'no_violation_within_bounds' for a tla property",
                ))
            runs = prop.get("runs")
            if not runs:
                issues.append(error("C06", "%s: tla property has no runs[]" % tag,
                                     "add at least one runs[] entry with cfg/json/result"))
                continue
            actual_results = []
            run_datas = []
            for i, run in enumerate(runs):
                json_path = run.get("json")
                if not json_path:
                    issues.append(error(
                        "C06", "%s: runs[%d] has no json field" % (tag, i),
                        "point runs[].json at results/<cfg-stem>.json as produced by "
                        "run_tlc.sh",
                    ))
                    continue
                abs_json = resolve_repo_relative(ctx.repo, json_path)
                if not abs_json:
                    issues.append(error(
                        "C06", "%s: runs[%d].json not found: %s" % (tag, i, json_path),
                        "regenerate %s with run_tlc.sh --out" % json_path,
                    ))
                    continue
                if not in_target_dir(abs_json, target_dir):
                    issues.append(error(
                        "C06",
                        "%s: runs[%d].json resolves outside this target's own directory %s: "
                        "%s" % (tag, i, target_dir, json_path),
                        "point runs[].json at a file under verification/models/%s/results/, "
                        "not another target or clone" % tid,
                    ))
                    continue
                data, err = load_json(abs_json)
                if err:
                    issues.append(error(
                        "C06", "%s: runs[%d].json does not parse (%s)" % (tag, i, err),
                        "regenerate the result JSON with run_tlc.sh",
                    ))
                    continue

                cfg_in_json = data.get("cfg")
                abs_cfg_in_json = resolve_repo_relative(ctx.repo, cfg_in_json)
                if not abs_cfg_in_json:
                    issues.append(error(
                        "C06",
                        "%s: runs[%d] result JSON's cfg path does not resolve to a real "
                        "file: %s" % (tag, i, cfg_in_json),
                        "rerun run_tlc.sh so the result JSON's cfg field points at a real "
                        "file",
                    ))

                recorded_result = run.get("result")
                actual_result = data.get("result")
                actual_results.append(actual_result)
                run_datas.append(data)
                if recorded_result != actual_result:
                    issues.append(error(
                        "C06",
                        "%s: runs[%d].result=%r does not match the result JSON's result=%r"
                        % (tag, i, recorded_result, actual_result),
                        "copy the result field from the result JSON instead of typing it "
                        "by hand",
                    ))
                if actual_result in NON_CLEAN_RUN_RESULTS:
                    issues.append(error(
                        "C06",
                        "%s: runs[%d] result JSON's result=%r is not a clean pass or "
                        "violation" % (tag, i, actual_result),
                        "resolve the %r result (fix the model/property, or the timeout/"
                        "error) before reporting this run" % actual_result,
                    ))

                recorded_constants = run.get("constants")
                actual_constants = data.get("constants")
                if recorded_constants is not None and recorded_constants != actual_constants:
                    issues.append(error(
                        "C06",
                        "%s: runs[%d].constants=%r does not match the result JSON's "
                        "constants=%r" % (tag, i, recorded_constants, actual_constants),
                        "copy constants from the result JSON instead of typing them by hand",
                    ))

                recorded_states = run.get("distinct_states")
                actual_states = data.get("distinct_states")
                if recorded_states is not None and recorded_states != actual_states:
                    issues.append(error(
                        "C06",
                        "%s: runs[%d].distinct_states=%r does not match the result JSON's "
                        "distinct_states=%r" % (tag, i, recorded_states, actual_states),
                        "copy distinct_states from the result JSON instead of typing it by "
                        "hand",
                    ))

                cfg_path = run.get("cfg")
                abs_cfg = resolve_path(ctx.repo, cfg_path) if cfg_path else None
                if abs_cfg and abs_cfg_in_json and os.path.realpath(abs_cfg) != os.path.realpath(
                        abs_cfg_in_json):
                    issues.append(error(
                        "C06",
                        "%s: runs[%d].cfg=%r does not match the result JSON's cfg=%r"
                        % (tag, i, cfg_path, cfg_in_json),
                        "point runs[].cfg at the same cfg file the result JSON was produced "
                        "from (do not swap runs[] entries between cfgs)",
                    ))

                json_mtime = os.path.getmtime(abs_json)
                if spec_mtime is not None and json_mtime < spec_mtime:
                    issues.append(error(
                        "C06",
                        "%s: runs[%d] result JSON is older than the spec file(s) in %s "
                        "(stale result)" % (tag, i, target_dir),
                        "run verification/rerun.sh again after editing the spec",
                    ))
                if abs_cfg_in_json and json_mtime < os.path.getmtime(abs_cfg_in_json):
                    issues.append(error(
                        "C06",
                        "%s: runs[%d] result JSON is older than its cfg file %s (stale "
                        "result)" % (tag, i, cfg_in_json),
                        "run verification/rerun.sh again after editing the cfg",
                    ))

            result_claim = prop.get("result")
            if result_claim == "violated":
                violating = [(i, d) for i, (r, d) in enumerate(zip(actual_results, run_datas))
                             if r in VIOLATION_RESULTS]
                if not violating:
                    issues.append(error(
                        "C06",
                        "%s: property result='violated' but none of its runs[] actually "
                        "violated (results=%r)" % (tag, actual_results),
                        "either fix the model so a run violates the property, or change "
                        "result to match the actual runs",
                    ))
                elif not any(
                        d.get("violated") == name or (
                            isinstance(d.get("violated_candidates"), list)
                            and len(d.get("violated_candidates")) == 1
                            and d.get("violated_candidates")[0] == name
                        )
                        for _, d in violating):
                    issues.append(error(
                        "C06",
                        "%s: property result='violated' but its violating run(s) actually "
                        "violated %r (candidates=%r), not the property %r itself"
                        % (tag, [d.get("violated") for _, d in violating],
                           [d.get("violated_candidates") for _, d in violating], name),
                        "check one PROPERTY/INVARIANT per run so TLC attributes the "
                        "violation to %r specifically" % name,
                    ))
            if result_claim == "no_violation_within_bounds" and any(
                    r != "pass" for r in actual_results):
                issues.append(error(
                    "C06",
                    "%s: property result='no_violation_within_bounds' but not every run "
                    "passed cleanly (results=%r)" % (tag, actual_results),
                    "every run backing a no_violation_within_bounds claim must have "
                    "result=pass",
                ))
    return issues


def check_c07(ctx):
    issues = []
    for t in ctx.targets:
        tid = t.get("id", "?")
        target_dir = os.path.join(ctx.repo, "verification", "models", tid)
        spec_files = sorted(glob.glob(os.path.join(target_dir, "*.tla")))
        spec_files += sorted(glob.glob(os.path.join(target_dir, "mutants", "*.tla")))
        spec_mtime = max((os.path.getmtime(f) for f in spec_files), default=None)
        for prop in t.get("properties") or []:
            if prop.get("tool") != "tla":
                continue
            name = prop.get("name", "?")
            tag = "%s/%s" % (tid, name)
            vac = prop.get("vacuity")
            if not vac:
                issues.append(error("C07", "%s: no vacuity block" % tag,
                                     "add a vacuity block with status/mutants/sanity"))
                continue
            if vac.get("status") != "passed":
                issues.append(error(
                    "C07", "%s: vacuity.status=%r, not 'passed'" % (tag, vac.get("status")),
                    "fix the property or model until vacuity passes before reporting it",
                ))
            mutants = vac.get("mutants") or []
            if not mutants:
                issues.append(error("C07", "%s: vacuity.mutants is empty" % tag,
                                     "add at least one falsifiability mutant under mutants/"))
            sanity = vac.get("sanity") or []
            if not sanity:
                issues.append(error("C07", "%s: vacuity.sanity is empty" % tag,
                                     "add at least one sanity invariant under sanity/"))

            run_infos = _resolve_run_infos(ctx, prop)
            buggy_cfg_real = None
            for info in run_infos:
                if info["data"] and info["data"].get("result") in VIOLATION_RESULTS:
                    buggy_cfg_real = info["cfg_real"]
                    break

            counted = []
            violating = []
            for entry in mutants:
                resolved = resolve_result_json(entry, target_dir, ctx.repo)
                if not resolved:
                    issues.append(error(
                        "C07",
                        "%s: vacuity mutant %r has no matching result JSON under %s/results"
                        % (tag, entry, target_dir),
                        "run the mutant cfg with run_tlc.sh --out results/<stem>.json",
                    ))
                    continue
                data, err = load_json(resolved)
                if err:
                    issues.append(error(
                        "C07",
                        "%s: vacuity mutant %r result JSON does not parse (%s)"
                        % (tag, entry, err),
                        "regenerate the mutant result JSON",
                    ))
                    continue
                if data.get("result") not in VIOLATION_RESULTS:
                    issues.append(error(
                        "C07",
                        "%s: vacuity mutant %r did not violate (result=%r)"
                        % (tag, entry, data.get("result")),
                        "the mutant must make the property fail; fix the mutant or the "
                        "property",
                    ))
                    continue
                violated = data.get("violated")
                candidates = data.get("violated_candidates")
                name_matches = violated == name or (
                    isinstance(candidates, list) and len(candidates) == 1
                    and candidates[0] == name
                )
                if not name_matches:
                    issues.append(error(
                        "C07",
                        "%s: vacuity mutant %r violated %r (candidates=%r), not the "
                        "property %r itself" % (tag, entry, violated, candidates, name),
                        "the mutant must violate this property specifically (for a "
                        "temporal property, run its cfg with a single PROPERTY configured "
                        "so TLC can attribute the violation)",
                    ))
                    continue
                resolved_spec = resolve_repo_relative(ctx.repo, data.get("spec"))
                if not resolved_spec or not in_target_dir(resolved_spec, target_dir):
                    issues.append(error(
                        "C07",
                        "%s: vacuity mutant %r's spec %r is not under %s"
                        % (tag, entry, data.get("spec"), target_dir),
                        "point the mutant cfg at this target's own spec",
                    ))
                    continue
                resolved_cfg = resolve_repo_relative(ctx.repo, data.get("cfg"))
                mutant_cfg_real = os.path.realpath(resolved_cfg) if resolved_cfg else None

                if spec_mtime is not None and os.path.getmtime(resolved) < spec_mtime:
                    issues.append(error(
                        "C07",
                        "%s: vacuity mutant %r result JSON is older than the spec file(s) "
                        "in %s (stale result)" % (tag, entry, target_dir),
                        "run verification/rerun.sh again after editing the spec",
                    ))
                if resolved_cfg and os.path.getmtime(resolved) < os.path.getmtime(
                        resolved_cfg):
                    issues.append(error(
                        "C07",
                        "%s: vacuity mutant %r result JSON is older than its cfg %s "
                        "(stale result)" % (tag, entry, data.get("cfg")),
                        "run verification/rerun.sh again after editing the cfg",
                    ))

                violating.append(entry)
                if buggy_cfg_real is not None and mutant_cfg_real == buggy_cfg_real:
                    continue
                counted.append(entry)
            if violating and not counted:
                issues.append(error(
                    "C07",
                    "%s: every vacuity mutant uses the same cfg as the buggy run (mutant "
                    "equal to the buggy config); need a mutant against the fixed "
                    "configuration" % tag,
                    "add a mutant that flips a guard in the FIXED model, not a re-run of "
                    "the buggy config",
                ))

            resolved_sanities = []
            for entry in sanity:
                resolved = resolve_result_json(entry, target_dir, ctx.repo)
                if not resolved:
                    issues.append(error(
                        "C07",
                        "%s: vacuity sanity %r has no matching result JSON under %s/results"
                        % (tag, entry, target_dir),
                        "run the sanity cfg with run_tlc.sh --out results/<stem>.json",
                    ))
                    continue
                data, err = load_json(resolved)
                if err:
                    issues.append(error(
                        "C07",
                        "%s: vacuity sanity %r result JSON does not parse (%s)"
                        % (tag, entry, err),
                        "regenerate the sanity result JSON",
                    ))
                    continue
                if data.get("result") != "invariant_violation":
                    issues.append(error(
                        "C07",
                        "%s: vacuity sanity %r did not violate as expected (result=%r)"
                        % (tag, entry, data.get("result")),
                        "a sanity invariant must be violated to show the state is reachable",
                    ))
                    continue
                violated = data.get("violated")
                if not violated or violated == name:
                    issues.append(error(
                        "C07",
                        "%s: vacuity sanity %r violated=%r; a sanity check must violate a "
                        "separate reachability invariant, not the property itself"
                        % (tag, entry, violated),
                        "point sanity at a cfg whose INVARIANT is a distinct reachability "
                        "invariant, not this property",
                    ))
                    continue
                resolved_spec = resolve_repo_relative(ctx.repo, data.get("spec"))
                if not resolved_spec or not in_target_dir(resolved_spec, target_dir):
                    issues.append(error(
                        "C07",
                        "%s: vacuity sanity %r's spec %r is not under %s"
                        % (tag, entry, data.get("spec"), target_dir),
                        "point the sanity cfg at this target's own spec",
                    ))
                    continue
                resolved_cfg = resolve_repo_relative(ctx.repo, data.get("cfg"))
                if spec_mtime is not None and os.path.getmtime(resolved) < spec_mtime:
                    issues.append(error(
                        "C07",
                        "%s: vacuity sanity %r result JSON is older than the spec file(s) "
                        "in %s (stale result)" % (tag, entry, target_dir),
                        "run verification/rerun.sh again after editing the spec",
                    ))
                if resolved_cfg and os.path.getmtime(resolved) < os.path.getmtime(
                        resolved_cfg):
                    issues.append(error(
                        "C07",
                        "%s: vacuity sanity %r result JSON is older than its cfg %s (stale "
                        "result)" % (tag, entry, data.get("cfg")),
                        "run verification/rerun.sh again after editing the cfg",
                    ))
                if data.get("constants") is None:
                    issues.append(error(
                        "C07",
                        "%s: vacuity sanity %r result JSON has no 'constants' recorded"
                        % (tag, entry),
                        "record constants in the TLC result JSON (run_tlc.sh does this "
                        "automatically)",
                    ))
                    continue
                resolved_sanities.append(data)

            for info in run_infos:
                if not info["data"] or info["data"].get("result") != "pass":
                    continue
                run_constants = info["data"].get("constants")
                if run_constants is None:
                    issues.append(error(
                        "C07",
                        "%s: a passing run's result JSON has no 'constants' recorded" % tag,
                        "record constants in the TLC result JSON (run_tlc.sh does this "
                        "automatically)",
                    ))
                    continue
                if not any(s.get("constants") == run_constants for s in resolved_sanities):
                    issues.append(error(
                        "C07",
                        "%s: no sanity result shares constants %r with the passing run; "
                        "reachability not shown at the reported bounds" % (tag, run_constants),
                        "add a sanity cfg with the same constants as the passing run",
                    ))
    return issues


def check_c08(ctx):
    proved_props = []
    for t in ctx.targets:
        for p in t.get("properties") or []:
            if p.get("tool") == "lean" and p.get("result") == "proved":
                proved_props.append((t.get("id"), p))
    if not proved_props:
        return []
    results_path = os.path.join(ctx.repo, "verification", "lean", "results.json")
    if not os.path.isfile(results_path):
        return [error(
            "C08",
            "a property claims tool=lean result=proved but verification/lean/results.json "
            "does not exist",
            "run scripts/lean_audit.sh verification/lean --out verification/lean/results.json"
            " and confirm result=proved",
        )]
    data, err = load_json(results_path)
    if err:
        return [error("C08", "verification/lean/results.json does not parse (%s)" % err,
                       "regenerate it with scripts/lean_audit.sh")]
    issues = []
    if data.get("result") != "proved":
        issues.append(error(
            "C08", "verification/lean/results.json result=%r, not 'proved'"
            % data.get("result"),
            "a Lean theorem is proved only when the audit reports result=proved",
        ))
    audited = {}
    for th in (data.get("theorems") or []) + (data.get("user_theorems") or []):
        if isinstance(th, dict) and th.get("name"):
            audited[th["name"]] = th.get("status")
    for tid, p in proved_props:
        names = [th.get("name") for th in (p.get("theorems") or p.get("user_theorems") or [])
                 if isinstance(th, dict)]
        if not names:
            issues.append(error(
                "C08",
                "%s/%s: tool=lean result=proved but has no theorems[] listing theorem names"
                % (tid, p.get("name")),
                "list theorems[] with the exact theorem name(s)",
            ))
        for name in names:
            status = audited.get(name)
            if status != "proved":
                issues.append(error(
                    "C08",
                    "%s/%s: theorem %r is %r in verification/lean/results.json, not "
                    "'proved'" % (tid, p.get("name"), name, status),
                    "never claim proved unless lean_audit.sh reports this theorem proved",
                ))
    return issues


def check_c09(ctx):
    issues = []
    known_props_by_target = {}
    prop_result_by_target = {}
    for t in ctx.targets:
        tid = t.get("id")
        known_props_by_target[tid] = {p.get("name") for p in t.get("properties") or []}
        for p in t.get("properties") or []:
            prop_result_by_target[(tid, p.get("name"))] = p.get("result")

    for f in ctx.findings.get("findings") or []:
        if f.get("status") != "CONFIRMED":
            continue
        fid = f.get("id", "?")
        repro_tests = f.get("repro_tests") or []
        if not repro_tests:
            issues.append(error("C09", "finding %s: repro_tests is empty" % fid,
                                 "add repro_tests naming the failing test id(s)"))
        for rt in repro_tests:
            file_part = rt.split("::", 1)[0]
            if not file_part.startswith("verification/repro/"):
                issues.append(error(
                    "C09", "finding %s: repro test %r is not under verification/repro/"
                    % (fid, rt),
                    "move the repro test under verification/repro/",
                ))
                continue
            abs_path = resolve_path(ctx.repo, file_part)
            if not os.path.isfile(abs_path):
                issues.append(error(
                    "C09", "finding %s: repro test file not found: %s" % (fid, file_part),
                    "write the repro test file or fix the path",
                ))
                continue
            base = os.path.basename(file_part)
            if any(p.match(base) for p in DEFAULT_PYTEST_DISCOVERY):
                issues.append(error(
                    "C09",
                    "finding %s: repro test file %r matches pytest's default discovery (%s)"
                    % (fid, base, file_part),
                    "rename to verification/repro/repro_<slug>.py so the project's normal "
                    "suite does not collect it",
                ))
        guard_tests = f.get("guard_tests") or []
        if not guard_tests:
            issues.append(error("C09", "finding %s: guard_tests is empty" % fid,
                                 "add at least one passing guard test for this target"))
        locations = f.get("locations") or []
        if not locations:
            issues.append(error("C09", "finding %s: locations is empty" % fid,
                                 "list the file(s) that must change to fix this finding"))
        else:
            for loc in locations:
                if not isinstance(loc, dict):
                    issues.append(error(
                        "C09", "finding %s: location entry is not an object: %r" % (fid, loc),
                        "each locations[] entry is an object with a 'file' field",
                    ))
                    continue
                lf = loc.get("file")
                if not lf or not os.path.isfile(resolve_path(ctx.repo, lf)):
                    issues.append(error("C09", "finding %s: location file not found: %s"
                                         % (fid, lf), "fix the location's file path"))
        props = f.get("properties") or []
        if not props:
            issues.append(error("C09", "finding %s: properties is empty" % fid,
                                 "list the property name(s) this finding violates"))
        else:
            known = set()
            for tid in f.get("targets") or []:
                known |= known_props_by_target.get(tid, set())
            results = []
            for p in props:
                if p not in known:
                    issues.append(error(
                        "C09",
                        "finding %s: property %r is not defined on any of its targets %s"
                        % (fid, p, f.get("targets")),
                        "name a property that appears in targets[].properties[].name",
                    ))
                    continue
                for tid in f.get("targets") or []:
                    if p in known_props_by_target.get(tid, set()):
                        results.append(prop_result_by_target.get((tid, p)))
            if results and "violated" not in results:
                issues.append(error(
                    "C09",
                    "finding %s: CONFIRMED but none of its cited properties %s have "
                    "result='violated' (results=%r)" % (fid, props, results),
                    "a CONFIRMED finding needs at least one cited property with "
                    "result='violated' (for a lean-only finding, prove the negation theorem "
                    "for the buggy configuration and set that property's result to "
                    "'violated')",
                ))
        if not (f.get("evidence") or "").strip():
            issues.append(error("C09", "finding %s: evidence is empty" % fid,
                                 "copy the failing assertion/traceback into evidence"))
    return issues


def run_shell(repo, cmd, timeout):
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
    except OSError as e:
        return None, "", str(e), False
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return None, out, err, True


EXCEPTION_TYPE_FROM_MESSAGE_RE = re.compile(r"^([A-Za-z_][\w.]*):")


def _exception_type_from_message(message):
    if not message:
        return None
    m = EXCEPTION_TYPE_FROM_MESSAGE_RE.match(message)
    if m:
        return m.group(1).rsplit(".", 1)[-1]
    if message.lstrip().startswith("assert "):
        return "AssertionError"
    return None


def parse_junit_testcases(xml_path):
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError):
        return None
    cases = []
    for tc in tree.getroot().iter("testcase"):
        failure = tc.find("failure")
        err_el = tc.find("error")
        skipped = tc.find("skipped")
        exc_type = None
        if failure is not None:
            status = "failure"
            message = failure.get("message") or (failure.text or "")
            exc_type = _exception_type_from_message(failure.get("message"))
        elif err_el is not None:
            status = "error"
            message = err_el.get("message") or (err_el.text or "")
        elif skipped is not None:
            status = "skipped"
            message = skipped.get("message") or ""
        else:
            status = "pass"
            message = ""
        cases.append({
            "name": tc.get("name"), "classname": tc.get("classname"),
            "status": status, "message": message or "", "exception_type": exc_type,
        })
    return cases


def _run_pytest_junit(repo, runner, test_id, timeout):
    fd, xml_path = tempfile.mkstemp(suffix=".xml", prefix="check_run_junit_")
    os.close(fd)
    try:
        os.remove(xml_path)
    except OSError:
        pass
    try:
        cmd = "%s %s --junitxml=%s --tb=long" % (
            runner, shlex.quote(test_id), shlex.quote(xml_path))
        rc, _out, _err, timed_out = run_shell(repo, cmd, timeout)
        cases = parse_junit_testcases(xml_path) if os.path.isfile(xml_path) else None
        return rc, cases, timed_out
    finally:
        try:
            os.remove(xml_path)
        except OSError:
            pass


def _check_c10_pytest_repro(repo, runner, fid, rt, prop_names):
    rc, cases, timed_out = _run_pytest_junit(repo, runner, rt, REPRO_TIMEOUT_SECONDS)
    if timed_out:
        return [error(
            "C10", "finding %s: repro test %s timed out after %ds"
            % (fid, rt, REPRO_TIMEOUT_SECONDS),
            "fix the repro test so it fails fast (bound runaway retries/hangs)",
        )]
    if not cases:
        return [error(
            "C10",
            "finding %s: repro test %s produced no parseable junit result (exit %s); the "
            "runner may not have actually run it" % (fid, rt, rc),
            "fix the repro id/path and the repro_runner command so pytest actually runs it",
        )]
    errors_ = [c for c in cases if c["status"] == "error"]
    failures = [c for c in cases if c["status"] == "failure"]
    if errors_:
        return [error(
            "C10",
            "finding %s: repro test %s errored (%s), not an assertion failure: %s"
            % (fid, rt, errors_[0]["status"], errors_[0]["message"][:200]),
            "fix the repro test so it fails with an assertion on real behavior, not a setup "
            "error",
        )]
    if not failures:
        return [error(
            "C10",
            "finding %s: repro test %s passes on current code (no failure) -- it does not "
            "reproduce the bug" % (fid, rt),
            "the test likely asserts the buggy behavior instead of the violated property; "
            "rewrite it to assert the correct behavior so it fails on current code",
        )]
    if len(failures) != 1 or len(cases) != 1:
        return [error(
            "C10",
            "finding %s: repro test %s id matched %d test case(s) (%d failing); use a "
            "precise file::test id that selects exactly one failing test"
            % (fid, rt, len(cases), len(failures)),
            "narrow repro_tests to a single file::test (or file::test[param]) id",
        )]
    exc_type = failures[0]["exception_type"]
    if exc_type != "AssertionError":
        return [error(
            "C10",
            "finding %s: repro test %s failed with %s, not an assertion failure"
            % (fid, rt, exc_type or "an unrecognized exception"),
            "the repro must fail on a plain `assert` of the violated property, not raise a "
            "different exception (NameError, TypeError, ...)",
        )]
    issues = []
    msg = failures[0]["message"]
    if prop_names and not any(p in msg for p in prop_names):
        issues.append(error(
            "C10",
            "finding %s: repro test %s failure message does not name any of %s"
            % (fid, rt, prop_names),
            "state the violated property's name in the assertion message",
        ))
    return issues


def _check_c10_pytest_guard(repo, runner, fid, gt):
    rc, cases, timed_out = _run_pytest_junit(repo, runner, gt, REPRO_TIMEOUT_SECONDS)
    if timed_out:
        return [error(
            "C10", "finding %s: guard test %s timed out after %ds"
            % (fid, gt, REPRO_TIMEOUT_SECONDS),
            "fix the guard test so it terminates",
        )]
    if not cases:
        return [error(
            "C10",
            "finding %s: guard test %s produced no parseable junit result / matched no "
            "tests (exit %s)" % (fid, gt, rc),
            "fix the guard test id/path",
        )]
    bad = [c for c in cases if c["status"] in ("failure", "error", "skipped")]
    if bad:
        return [error(
            "C10", "finding %s: guard test %s did not pass (%s: %s)"
            % (fid, gt, bad[0]["status"], bad[0]["message"][:200]),
            "fix the guard test so it passes on current code (a skipped guard does not count)",
        )]
    if not any(c["status"] == "pass" for c in cases):
        return [error(
            "C10", "finding %s: guard test %s has no passing result" % (fid, gt),
            "fix the guard test id/path so a test actually runs and passes",
        )]
    return []


REPRO_RUNNER_PLACEHOLDER_RE = re.compile(r"\{(file|test|dir)\}")
REPRO_CRASH_MARKERS = (
    "panic:", "[build failed]", "TypeError", "ReferenceError", "Error: Cannot find module",
)
NO_TESTS_RAN_MARKERS = ("no tests to run", "No test files found")
SKIP_MARKERS = ("--- SKIP:", "\u2193", " skipped")


def _split_repro_id(rt):
    file_part, _, test_part = rt.partition("::")
    return file_part, test_part, os.path.dirname(file_part)


def _substitute_repro_runner(runner, rt):
    file_part, test_part, dir_part = _split_repro_id(rt)
    cmd = runner.replace("{file}", shlex.quote(file_part))
    cmd = cmd.replace("{test}", shlex.quote(test_part))
    cmd = cmd.replace("{dir}", shlex.quote(dir_part))
    return cmd


def _check_c10_placeholder_repro(repo, runner, fid, rt, prop_names):
    _file_part, test_part, _dir_part = _split_repro_id(rt)
    cmd = _substitute_repro_runner(runner, rt)
    rc, out, err, timed_out = run_shell(repo, cmd, REPRO_TIMEOUT_SECONDS)
    if timed_out:
        return [error(
            "C10", "finding %s: repro test %s timed out after %ds"
            % (fid, rt, REPRO_TIMEOUT_SECONDS),
            "fix the repro test so it fails fast (bound runaway retries/hangs)",
        )]
    if rc == 0:
        return [error(
            "C10",
            "finding %s: repro test %s passes on current code (exit 0) -- it does not "
            "reproduce the bug" % (fid, rt),
            "the test likely asserts the buggy behavior instead of the violated property; "
            "rewrite it to assert the correct behavior so it fails on current code",
        )]
    combined = out + err
    if any(marker in combined for marker in REPRO_CRASH_MARKERS):
        return [error(
            "C10",
            "finding %s: repro test %s crashed instead of failing on the property (output "
            "contains a crash marker such as 'panic:' or '[build failed]')" % (fid, rt),
            "fix the repro so it builds and fails by asserting the violated property, "
            "not by crashing",
        )]
    if "=== RUN" in combined and not re.search(
            r"^--- FAIL:\s+%s(\s|$)" % re.escape(test_part), combined, re.MULTILINE):
        return [error(
            "C10",
            "finding %s: repro test %s exited %s but the runner never reported %r as failed "
            "(an abrupt exit such as os.Exit is not a property failure)"
            % (fid, rt, rc, test_part),
            "make the repro fail through the test framework (t.Fatal/t.Errorf) on the "
            "violated property",
        )]
    names_test = bool(test_part) and test_part in combined
    without_test_name = combined.replace(test_part, "") if test_part else combined
    names_prop = bool(prop_names) and any(p in without_test_name for p in prop_names)
    if names_test and names_prop:
        return []
    return [error(
        "C10",
        "finding %s: repro test %s exited %s, but its output does not show that the test "
        "%r actually ran and failed on the named propert%s %s"
        % (fid, rt, rc, test_part, "y" if len(prop_names) == 1 else "ies", prop_names),
        "confirm repro_runner's {file}/{test}/{dir} substitution actually selects and runs "
        "%s, and that its failure output names the violated property" % rt,
    )]


def _check_c10_placeholder_guard(repo, runner, fid, gt):
    _file_part, test_part, _dir_part = _split_repro_id(gt)
    cmd = _substitute_repro_runner(runner, gt)
    rc, out, err, timed_out = run_shell(repo, cmd, REPRO_TIMEOUT_SECONDS)
    if timed_out:
        return [error(
            "C10", "finding %s: guard test %s timed out after %ds"
            % (fid, gt, REPRO_TIMEOUT_SECONDS),
            "fix the guard test so it terminates",
        )]
    if rc != 0:
        return [error(
            "C10", "finding %s: guard test %s did not pass (exit %s)" % (fid, gt, rc),
            "fix the guard test so it passes on current code",
        )]
    combined = out + err
    lines = combined.splitlines()
    has_run_lines = any(line.startswith("=== RUN") for line in lines)
    exact_run = re.search(r"^=== RUN\s+%s\s*$" % re.escape(test_part), combined, re.MULTILINE)
    if (test_part not in combined
            or any(marker in combined for marker in NO_TESTS_RAN_MARKERS)
            or (has_run_lines and not exact_run)):
        return [error(
            "C10",
            "finding %s: guard test %s exited 0 but its output does not show that the test "
            "%r actually ran" % (fid, gt, test_part),
            "confirm the guard id exists, that the runner anchors the test name (for example "
            "-run '^{test}$'), and that its output names each test it runs (for example "
            "go test -v)",
        )]
    if any(test_part in line and any(m in line for m in SKIP_MARKERS) for line in lines):
        return [error(
            "C10",
            "finding %s: guard test %s was skipped, not passed" % (fid, gt),
            "remove the skip so the guard actually runs and passes on current code",
        )]
    return []


def check_c10(ctx):
    issues = []
    baseline = ctx.findings.get("baseline") or {}
    runner = baseline.get("repro_runner")
    if not runner:
        return issues
    pytest_runner = "pytest" in runner.lower()
    has_placeholders = not pytest_runner and bool(REPRO_RUNNER_PLACEHOLDER_RE.search(runner))
    if not pytest_runner and not has_placeholders:
        return [error(
            "C10",
            "baseline.repro_runner is not a pytest command and has no {file}/{test}/{dir} "
            "placeholders, so C10 cannot tell an assertion failure from a broken invocation",
            "use a pytest-based repro_runner, or put {file}, {test} and/or {dir} placeholders "
            "in the runner (for example go test -v -tags verify_repro ./{dir} -run '^{test}$')",
        )]
    for f in ctx.findings.get("findings") or []:
        if f.get("status") != "CONFIRMED":
            continue
        fid = f.get("id", "?")
        prop_names = [p for p in (f.get("properties") or []) if isinstance(p, str)]
        for rt in f.get("repro_tests") or []:
            if not isinstance(rt, str) or "::" not in rt:
                issues.append(error(
                    "C10",
                    "finding %s: repro test %r has no '::' (a file::test id is required)"
                    % (fid, rt),
                    "use a file::testname id, not a whole-file path",
                ))
                continue
            if pytest_runner:
                issues.extend(_check_c10_pytest_repro(ctx.repo, runner, fid, rt, prop_names))
            else:
                issues.extend(
                    _check_c10_placeholder_repro(ctx.repo, runner, fid, rt, prop_names))
        for gt in f.get("guard_tests") or []:
            if not isinstance(gt, str) or "::" not in gt:
                issues.append(error(
                    "C10",
                    "finding %s: guard test %r has no '::' (a file::test id is required)"
                    % (fid, gt),
                    "use a file::testname id, not a whole-file path",
                ))
                continue
            if pytest_runner:
                issues.extend(_check_c10_pytest_guard(ctx.repo, runner, fid, gt))
            else:
                issues.extend(_check_c10_placeholder_guard(ctx.repo, runner, fid, gt))
    return issues


def check_c11(ctx):
    collect_cmd = (ctx.findings.get("baseline") or {}).get("collect_command")
    if not collect_cmd:
        return []
    rc, out, err, timed_out = run_shell(ctx.repo, collect_cmd, REPRO_TIMEOUT_SECONDS)
    if timed_out:
        return [error("C11", "collect_command timed out after %ds" % REPRO_TIMEOUT_SECONDS,
                       "fix collect_command so it runs quickly")]
    issues = []
    if rc not in (0, 5):
        issues.append(error(
            "C11", "collect_command exited %s (expected 0, or 5 for 'no tests collected')" % rc,
            "fix collect_command so it actually runs (check the interpreter/venv path)",
        ))
    if "verification/repro" in (out + err):
        issues.append(error(
            "C11",
            "collect_command output mentions verification/repro; the project's normal "
            "suite would collect the repro tests",
            "rename the repro files outside the project's default test discovery pattern",
        ))
    return issues


def check_c12(ctx):
    issues = []
    readme = os.path.join(ctx.repo, "verification", "README.md")
    if not os.path.isfile(readme):
        issues.append(error("C12", "verification/README.md not found",
                             "write verification/README.md from the finding-format.md "
                             "template"))
    else:
        text = read_text(readme)
        for h in REQUIRED_README_HEADINGS:
            if h not in text:
                issues.append(error(
                    "C12", "verification/README.md is missing the %r section" % h,
                    "add a '## %s' section to verification/README.md" % h,
                ))
    rerun = os.path.join(ctx.repo, "verification", "rerun.sh")
    if not os.path.isfile(rerun):
        issues.append(error("C12", "verification/rerun.sh not found",
                             "write verification/rerun.sh that re-runs every cfg and the "
                             "Lean audit"))
    elif not os.access(rerun, os.X_OK):
        issues.append(error("C12", "verification/rerun.sh is not executable",
                             "chmod +x verification/rerun.sh"))
    return issues


def check_c13(ctx):
    issues = []
    plans_by_id = {str(p.get("id")): p.get("file") for p in ctx.findings.get("plans") or []}
    for f in ctx.findings.get("findings") or []:
        if f.get("status") != "CONFIRMED":
            continue
        sev = f.get("severity")
        if sev not in ("high", "medium"):
            continue
        fid = f.get("id", "?")
        plan_val = f.get("plan")
        if not plan_val:
            issues.append(error(
                "C13", "finding %s (severity %s) has no plan" % (fid, sev),
                "set plan to an existing plans/NNN-*.md, or \"not planned: <reason>\"",
            ))
            continue
        if isinstance(plan_val, str) and plan_val.startswith("not planned:"):
            issues.append(warn(
                "C13", "finding %s (severity %s) is not planned: %s"
                % (fid, sev, plan_val[len("not planned:"):].strip()),
                "plan this finding, or confirm the reason still applies",
            ))
            continue
        plan_path = None
        if isinstance(plan_val, str) and ("/" in plan_val or plan_val.endswith(".md")):
            plan_path = plan_val
        else:
            plan_path = plans_by_id.get(str(plan_val))
            if not plan_path:
                for base in ("plans", "verification-plans"):
                    matches = glob.glob(os.path.join(ctx.repo, base, "%s-*.md" % plan_val))
                    if matches:
                        plan_path = os.path.relpath(matches[0], ctx.repo)
                        break
        if not plan_path:
            issues.append(error(
                "C13", "finding %s: plan %r does not resolve to a plan file" % (fid, plan_val),
                "set plan to an existing plans/NNN-<slug>.md",
            ))
            continue
        abs_plan = resolve_path(ctx.repo, plan_path)
        if not os.path.isfile(abs_plan):
            issues.append(error(
                "C13", "finding %s: plan file not found: %s" % (fid, plan_path),
                "write the plan file or fix the plan reference",
            ))
            continue
        text = read_text(abs_plan)
        if "Planned at" not in text:
            issues.append(error(
                "C13", "finding %s: plan %s has no 'Planned at' section" % (fid, plan_path),
                "add 'Planned at' (commit SHA) to the plan",
            ))
        if "Done criteria" not in text:
            issues.append(error(
                "C13", "finding %s: plan %s has no 'Done criteria' section" % (fid, plan_path),
                "add 'Done criteria' to the plan",
            ))
        if not re.search(r"\.cfg\b", text):
            issues.append(error(
                "C13", "finding %s: plan %s has no model-check line referencing a .cfg"
                % (fid, plan_path),
                "add a line naming the per-plan cfg that must pass",
            ))
    return issues


def _first_nonempty(entry, keys):
    for k in keys:
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return True
        if isinstance(v, list) and v:
            return True
    return False


def check_c14(ctx):
    issues = []
    for i, e in enumerate(ctx.findings.get("needs_decision") or []):
        if not _first_nonempty(e, ["question", "reading_a", "reading_b", "evidence",
                                    "summary"]):
            issues.append(warn(
                "C14", "needs_decision[%d] (%s) has no question/evidence"
                % (i, e.get("id", "?")),
                "add the question and the evidence for each reading",
            ))
    for i, e in enumerate(ctx.findings.get("model_only") or []):
        if not _first_nonempty(e, ["why_not_reproducible", "reason", "trace_summary"]):
            issues.append(warn("C14", "model_only[%d] has no reason" % i,
                                "explain why the trace is not reproducible"))
    for i, e in enumerate(ctx.findings.get("rejections") or []):
        if not _first_nonempty(e, ["evidence", "assumption", "reason"]):
            issues.append(warn(
                "C14", "rejections[%d] has no evidence/assumption" % i,
                "cite the doc/test/comment that makes this BY-DESIGN or OUT-OF-SCOPE",
            ))
    return issues


def check_c15(ctx):
    issues = []
    prop_source = {}
    for t in ctx.targets:
        for p in t.get("properties") or []:
            prop_source[(t.get("id"), p.get("name"))] = p.get("source")
    for f in ctx.findings.get("findings") or []:
        if f.get("status") != "CONFIRMED":
            continue
        for tid in f.get("targets") or []:
            for pname in f.get("properties") or []:
                source = prop_source.get((tid, pname))
                if isinstance(source, str) and source.strip().lower().startswith("inferred"):
                    issues.append(warn(
                        "C15",
                        "finding %s: property %r (target %s) has source %r"
                        % (f.get("id", "?"), pname, tid, source),
                        "CONFIRMED needs documented intent or an any-intent-wrong "
                        "consequence; confirm before treating as CONFIRMED",
                    ))
    return issues


STATIC_CHECKS = [
    check_c01, check_c02, check_c03, check_c04, check_c05, check_c06, check_c07,
    check_c08, check_c09, check_c12, check_c13, check_c14, check_c15,
]
EXEC_CHECKS = [check_c10, check_c11]


def print_text(issues):
    for i in issues:
        print("[%s] %s: %s" % (i["level"], i["check"], i["message"]))
        print("  fix: %s" % i["fix"])


def main():
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("repo_root")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-exec", action="store_true")
    args = p.parse_args()

    repo = os.path.abspath(args.repo_root)
    if not os.path.isdir(repo):
        print("repo-root not found: %s" % repo, file=sys.stderr)
        return 2

    findings_path = os.path.join(repo, "verification", "findings.json")
    if not os.path.isfile(findings_path):
        issue = error("C01", "findings.json not found at %s" % findings_path,
                       "run the verify-formally skill to produce verification/findings.json")
        if args.json:
            print(json.dumps([issue], indent=2))
        else:
            print_text([issue])
        return 1

    raw, err = load_json(findings_path)
    if err is not None:
        issue = error("C01", "findings.json is not valid JSON: %s" % err,
                       "fix the JSON syntax error")
        if args.json:
            print(json.dumps([issue], indent=2))
        else:
            print_text([issue])
        return 1

    clean, sanitize_issues = sanitize(raw)
    data, norm_issues = normalize(clean)
    ctx = Context(repo, data)

    issues = list(sanitize_issues) + list(norm_issues)
    for check in STATIC_CHECKS:
        issues.extend(check(ctx))
    if not args.no_exec:
        for check in EXEC_CHECKS:
            issues.extend(check(ctx))
    else:
        issues.append(warn(
            "C10",
            "--no-exec was given; C10 (repro/guard tests) and C11 (test collection) were "
            "skipped",
            "run check_run.py without --no-exec before trusting this run",
        ))

    if args.json:
        print(json.dumps(issues, indent=2))
    else:
        print_text(issues)

    return 1 if any(i["level"] == "ERROR" for i in issues) else 0


if __name__ == "__main__":
    sys.exit(main())
