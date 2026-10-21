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
# --no-exec skips C10, C11 and the re-run halves of C16 and C17, which run commands from
# findings.json (repro_runner, collect_command) or re-run TLC and the code-level verifiers;
# everything else is static (reads files, git, JSON) and always runs. --no-exec always emits WARNINGs that these were skipped.
# Commands run by C10 and C11 get <repo>/.venv/bin and <repo>/node_modules/.bin prepended to
# PATH when those directories exist.
#
# This validator fails closed: any field with the wrong type, an unrecognized status/result/
# tool value, or a missing required key is an ERROR, not a silent pass.
#
# Checks:
#   C01  findings.json exists, parses, has schema verify-formally-findings/2 and the required
#        fields (commit and invocation are strings), and every closed-set field (statuses,
#        results, tools) is spelled exactly as documented; a lean property cannot have result
#        no_violation_within_bounds (a tla-only result); target ids are unique non-empty
#        strings of [A-Za-z0-9._-] other than "." and ".."; no field name is repaired; FIXED is
#        allowed only in a reconcile run
#   C02  findings.json commit matches `git -C <repo> rev-parse HEAD`
#   C03  git status is confined to verification/ and plans/ (baseline.dirty_files excepted)
#   C04  a reconcile run lists at least one target; every other run models at least 1 target.
#        A target is modeled when its status is "modeled" and it has a checked property: for
#        tla, result violated or no_violation_within_bounds; for lean, result proved, or
#        violated with a cited theorem that verification/lean/results.json lists as proved and
#        whose name or module mentions the target id. Every not_modeled target, in any
#        non-reconcile run, has a reason and an attempt on disk (a .tla and a results/*.json;
#        for a lean-only target a .lean file mentioning the target id plus a parseable
#        verification/lean/results.json with a "result" field). A scoped run (a keyword such
#        as quick, an existing repo path, or a symbol defined in a cited target file) needs
#        each named path covered by a modeled target's files and each named symbol defined in
#        one. A full run also selects at least 3 targets, fewer than 3 modeled needs
#        fewer_targets_reason, and work under verification/models/ must be listed (WARN)
#   C05  each modeled target has a real, non-trivial CORRESPONDENCE.md and >=1 property
#   C06  each tla property's runs[] point at real, current, matching result JSON, inside the
#        target's own directory, and a "violated" claim is backed by a run that actually
#        violated that property by name (not a different invariant, not a bare deadlock)
#   C07  each tla property's vacuity check (mutants + sanity) is real and non-vacuous
#   C08  a lean property claiming "proved" is backed by verification/lean/results.json, and each
#        cited theorem is proved there and belongs to the citing target (name or module
#        mentions the target id)
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
#   C16  every result JSON under a selected target's results/ (and every one cited by runs[],
#        mutants, sanity) was produced by run_tlc.sh: a sibling <stem>.log exists and replays
#        to the same result/violated/distinct_states/constants, its spec and cfg resolve inside
#        the target directory, its command names run_tlc.sh with the same spec and cfg, and its
#        log carries TLC's version banner and the spec name; (exec) every cited pass or
#        violation cfg is re-run with this skill's run_tlc.sh and must reproduce result,
#        violated, and (for pass) distinct_states
#   C17  each code property (tool "code") names a verifier, a harness file under its target
#        directory, and its bounds; every code-level result JSON (cited, or under a selected
#        target's results/) was produced by run_code_check.py: its log carries the runner's
#        header for the same tool and replays to the same result/violated/exhaustive, and a
#        mutant's patch is unchanged and touches only real code. violated needs a cited
#        violation; no_violation_within_bounds needs every cited run to be an exhaustive pass;
#        no_counterexample_found needs every cited run to pass; both pass claims need a passed
#        vacuity block with a violating --mutant run and a violating sanity run of the same
#        verifier. (exec) every cited pass or violation is re-run and must reproduce result,
#        violated, and (for pass) exhaustive
#   C18  verification/state-diagram.html was written by render_diagram.py; every modeled target has a diagram.json (kind tla when the target has a TLA+
#        property), a section that rendered without error, and input hashes that match the
#        current files (otherwise the page is stale); a page rendered at another commit, a
#        violating TLA+ run that is not highlighted, or a README that does not link the page,
#        is a WARNING
#   C19  (reconcile runs) every modeled target has a reconcile record: outcome in_sync, updated, bug,
#        blocked (or updated + bug); from_commit is an ancestor of HEAD; to_commit and the
#        CORRESPONDENCE.md sync commit are HEAD, or both stay at from_commit for blocked (with
#        a blocked_reason); every commit from from_commit to HEAD touching the target's files
#        is in commits_read and classified in changes, promise_changed with evidence and the
#        old and new property wording; pre_update_runs exist under results/before/; updated
#        needs a violating post_update_break; bug needs a CONFIRMED finding on the target
#        naming introduced_by; the README has a Reconcile section
import argparse
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_report  # noqa: E402
import render_diagram  # noqa: E402
import tlc_report  # noqa: E402

VIOLATION_RESULTS = {
    "invariant_violation", "property_violation", "deadlock", "assertion_violation",
}
NON_CLEAN_RUN_RESULTS = {"error", "timeout", "vacuous", "pass_with_warnings"}
FINDING_STATUSES = {
    "CONFIRMED", "NEEDS-DECISION", "MODEL-ONLY", "BY-DESIGN", "OUT-OF-SCOPE", "FIXED",
}
REJECTION_STATUSES = {"BY-DESIGN", "OUT-OF-SCOPE"}
PROPERTY_RESULTS = {
    "violated", "no_violation_within_bounds", "no_counterexample_found", "proved", "unproved",
    "vacuous", "not_checked",
}
PROPERTY_TOOLS = {"tla", "lean", "code"}
RECONCILE_OUTCOMES = {"in_sync", "updated", "bug", "blocked"}
CHANGE_EFFECTS = {"no_effect", "shape_changed", "promise_changed", "promise_maybe_broken"}
SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
TARGET_STATUSES = {"modeled", "not_modeled"}
SEVERITIES = {"high", "medium", "low"}
FILE_LINE_RE = re.compile(r"\w+\.\w+:\d+")
DEFAULT_PYTEST_DISCOVERY = [re.compile(r"^test_.*\.py$"), re.compile(r".*_test\.py$")]
REQUIRED_README_HEADINGS = ["Defaults taken", "Findings", "Properties checked", "Not modeled"]
REPRO_TIMEOUT_SECONDS = 300
SCOPED_TOKENS = {"quick", "repro", "reconcile"}
COMMAND_WORDS = {"verify-formally", "verify"}
INVOCATION_ABBREVIATIONS = {"e.g", "i.e", "etc", "vs", "cf", "a.k.a"}
SYMBOL_RE = re.compile(r"^[A-Za-z_]\w*((\.|::)[A-Za-z_]\w*)+$")
TARGET_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
LOG_VERSION_BANNER = "@!@!@STARTMSG 2262"
SCHEMA_ID = "verify-formally-findings/2"


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


def _invocation_tokens(invocation):
    if not isinstance(invocation, str):
        return []
    cleaned = re.sub(r"[(),]", " ", invocation)
    tokens = []
    for raw_tok in cleaned.split():
        t = raw_tok.strip().strip("-").rstrip(".:;")
        if not t:
            continue
        if t.lstrip("/").lower() in COMMAND_WORDS:
            continue
        tokens.append(t)
    return tokens


def _target_files(targets):
    files = []
    for t in targets or []:
        for f in t.get("files") or []:
            if isinstance(f, str) and f.strip():
                files.append(os.path.normpath(re.sub(r":\d+(-\d+)?$", "", f.strip())))
    return files


def _token_repo_path(repo, tok):
    base = re.sub(r":\d+(-\d+)?$", "", tok.split("::")[0]).replace("\\", "/")
    if not base:
        return None
    candidate = base if os.path.isabs(base) else os.path.join(repo, base)
    real_repo = os.path.realpath(repo)
    real = os.path.realpath(candidate)
    if os.path.exists(real) and (real == real_repo or real.startswith(real_repo + os.sep)):
        return os.path.relpath(real, real_repo)
    return None


CONTROL_WORDS = {
    "if", "elif", "else", "while", "for", "switch", "case", "return", "catch", "with", "assert",
    "not", "and", "or", "in", "is", "yield", "await", "throw", "new", "do", "try",
}


def _definition_patterns(name):
    return [
        re.compile(
            r"^\s*(?P<pre>(?:[A-Za-z_]+\s+)*)(?:def|class|func|function|fn)\s+"
            r"(?:\([^)]*\)\s*)?%s\b" % name, re.MULTILINE),
        re.compile(r"^\s*(?P<pre>(?:[A-Za-z_]+\s+)*)%s\s*[=:](?!=)" % name, re.MULTILINE),
        re.compile(
            r"^\s*(?P<pre>(?:[A-Za-z_<>\[\]]+\s+)*)%s\s*\([^)]*\)\s*(?::\s*[^{=;]+)?\s*\{"
            % name, re.MULTILINE),
    ]


def _symbol_defined_in(repo, files, tok):
    if not SYMBOL_RE.match(tok):
        return False
    patterns = _definition_patterns(re.escape(re.split(r"\.|::", tok)[-1]))
    for rel in set(files):
        path = os.path.join(repo, rel)
        if not os.path.isfile(path):
            continue
        text = read_text(path)
        for pattern in patterns:
            for m in pattern.finditer(text):
                if not CONTROL_WORDS & set(m.group("pre").split()):
                    return True
    return False


def _symbol_in_target_files(repo, targets, tok):
    return _symbol_defined_in(repo, _target_files(targets), tok)


def invocation_scope(invocation, repo, targets):
    keywords, paths, symbols = [], [], []
    for tok in _invocation_tokens(invocation):
        low = tok.lower()
        if low in SCOPED_TOKENS:
            keywords.append(low)
        elif low in INVOCATION_ABBREVIATIONS:
            continue
        elif (rel := _token_repo_path(repo, tok)):
            paths.append(rel)
        elif _symbol_in_target_files(repo, targets, tok):
            symbols.append(tok)
    return keywords, paths, symbols


def is_full_run(invocation, repo, targets):
    if not isinstance(invocation, str) or not invocation.strip():
        return False
    return not any(invocation_scope(invocation, repo, targets))


def is_reconcile_run(invocation):
    return "reconcile" in [t.lower() for t in _invocation_tokens(invocation)]


def _ensure_list(container, key, item_type, check, issues, label, report_type=True):
    v = container.get(key)
    if v is None:
        return
    if not isinstance(v, list):
        if report_type:
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
    data = dict(raw)

    if "baseline" in data and data["baseline"] is not None and not isinstance(
            data["baseline"], dict):
        data["baseline"] = {}

    for key in ("needs_decision", "model_only", "rejections", "refuted_claims", "plans",
                "coverage_gaps"):
        _ensure_list(data, key, dict, "C01", issues, "findings.json %r" % key)

    _ensure_list(data, "targets", dict, "C01", issues, "findings.json 'targets'",
                 report_type=False)
    for i, t in enumerate(data.get("targets") or []):
        if not isinstance(t.get("id"), str):
            t["id"] = "target[%d]" % i
        _ensure_list(t, "properties", dict, "C01", issues,
                     "findings.json targets[%d].properties" % i)
        if "reconcile" in t and t["reconcile"] is not None and not isinstance(
                t["reconcile"], dict):
            issues.append(error(
                "C01", "findings.json targets[%d].reconcile must be an object, got %s"
                % (i, type(t["reconcile"]).__name__),
                "make 'reconcile' an object (see references/reconcile.md)",
            ))
            t["reconcile"] = None
        if isinstance(t.get("reconcile"), dict):
            rec = t["reconcile"]
            rlabel = "findings.json targets[%d].reconcile" % i
            for key, typ in (("outcome", str), ("commits_read", str), ("changes", dict),
                             ("pre_update_runs", str), ("post_update_break", str)):
                _ensure_list(rec, key, typ, "C01", issues, "%s.%s" % (rlabel, key))
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
            if isinstance(p.get("vacuity"), dict):
                for key in ("mutants", "sanity"):
                    _ensure_list(p["vacuity"], key, str, "C01", issues, label + ".vacuity." + key)

    _ensure_list(data, "findings", dict, "C01", issues, "findings.json 'findings'",
                 report_type=False)
    for i, f in enumerate(data.get("findings") or []):
        label = "findings.json findings[%d]" % i
        _ensure_list(f, "locations", dict, "C01", issues, label + ".locations")
        for key in ("repro_tests", "guard_tests", "properties", "targets", "tools"):
            if key in f and f[key] is not None:
                _ensure_list(f, key, str, "C01", issues, "%s.%s" % (label, key))

    return data, issues


class Context:
    def __init__(self, repo, findings):
        self.repo = repo
        self.findings = findings
        self.cache = {}

    @property
    def targets(self):
        return self.findings.get("targets") or []


REQUIRED_TOP_LEVEL = (
    ("commit", str), ("invocation", str), ("baseline", dict), ("targets", list),
    ("findings", list),
)
TYPE_DESCRIPTIONS = {
    str: ("a string", "a JSON string"), list: ("a list", "a JSON array"),
    dict: ("an object", "a JSON object"),
}
SINGULAR_FIELDS = (
    ("targets", "target"), ("properties", "property"), ("repro_tests", "repro_test"),
    ("guard_tests", "guard_test"),
)


def check_c01_raw(raw):
    if not isinstance(raw, dict):
        return [error(
            "C01", "findings.json top-level value must be a JSON object, got %s"
            % type(raw).__name__,
            "findings.json's root must be an object (schema/commit/targets/findings/"
            "baseline/...)",
        )]
    issues = []
    if raw.get("schema") != SCHEMA_ID:
        issues.append(error(
            "C01", "findings.json schema is %r, not %r" % (raw.get("schema"), SCHEMA_ID),
            "set schema to exactly %r and use the field names in references/finding-format.md"
            % SCHEMA_ID,
        ))
    for key, expected in REQUIRED_TOP_LEVEL:
        if key not in raw:
            issues.append(error(
                "C01", "findings.json is missing %r" % key,
                "add a top-level %r field (see references/finding-format.md section 3)" % key,
            ))
        elif not isinstance(raw[key], expected):
            issues.append(error(
                "C01", "findings.json %r must be %s, got %s"
                % (key, TYPE_DESCRIPTIONS[expected][0], type(raw[key]).__name__),
                "make %r %s" % (key, TYPE_DESCRIPTIONS[expected][1]),
            ))
    if isinstance(raw.get("targets"), list):
        seen = set()
        for i, t in enumerate(raw["targets"]):
            if not isinstance(t, dict):
                continue
            tid = t.get("id")
            if not (isinstance(tid, str) and TARGET_ID_RE.match(tid) and tid not in (".", "..")):
                issues.append(error(
                    "C01", "targets[%d].id %r is not a non-empty string of [A-Za-z0-9._-] "
                    "(not '.' or '..')" % (i, tid),
                    "give every target a short unique directory-safe id; it names "
                    "verification/models/<id>/",
                ))
            elif tid in seen:
                issues.append(error(
                    "C01", "targets[%d].id %r duplicates an earlier target" % (i, tid),
                    "every target needs its own id; duplicate ids make one target's files "
                    "count for several",
                ))
            else:
                seen.add(tid)
    return issues


def check_c01(ctx):
    issues = []
    d = ctx.findings
    if isinstance(d.get("commit"), str) and not d["commit"].strip():
        issues.append(error("C01", "findings.json 'commit' is empty",
                             "set commit to the full HEAD SHA (git rev-parse HEAD)"))
    if isinstance(d.get("invocation"), str) and not d["invocation"].strip():
        issues.append(error("C01", "findings.json 'invocation' is empty",
                             "set invocation to the exact command that started this run"))
    baseline = d.get("baseline")
    if isinstance(baseline, dict):
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
                                     "set tool to 'tla', 'lean', or 'code'"))
            elif p.get("tool") not in PROPERTY_TOOLS:
                issues.append(error(
                    "C01", "%s has tool %r, not one of %s"
                    % (tag, p.get("tool"), sorted(PROPERTY_TOOLS)),
                    "use exactly 'tla', 'lean', or 'code'",
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
            elif p.get("tool") == "lean" and p.get("result") == "no_violation_within_bounds":
                issues.append(error(
                    "C01", "%s: lean property has result='no_violation_within_bounds', which "
                    "applies only to tla properties" % tag,
                    "a Lean theorem is 'proved', 'unproved', or 'violated' by a proved "
                    "negation theorem; use one of those",
                ))
            elif p.get("tool") != "code" and p.get("result") == "no_counterexample_found":
                issues.append(error(
                    "C01", "%s: result='no_counterexample_found' applies only to code "
                    "properties (a non-exhaustive search on the real code)" % tag,
                    "a TLA+ property is 'no_violation_within_bounds'; a Lean property is "
                    "'proved' or 'unproved'",
                ))
            elif p.get("tool") == "code" and p.get("result") == "proved":
                issues.append(error(
                    "C01", "%s: a code property is never 'proved'; a bounded model checker "
                    "pass is 'no_violation_within_bounds' and a non-exhaustive search is "
                    "'no_counterexample_found'" % tag,
                    "use 'no_violation_within_bounds' with the bounds in 'bounds'",
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
        if f.get("status") == "FIXED" and not is_reconcile_run(d.get("invocation")):
            issues.append(error(
                "C01", "finding %s has status FIXED but the invocation %r is not a reconcile "
                "run" % (fid, d.get("invocation")),
                "FIXED is set only by /verify-formally reconcile; in any other run a repro "
                "that passes on current code is not a finding: remove it or re-check the "
                "model",
            ))
        for plural, singular in SINGULAR_FIELDS:
            if singular in f and plural not in f:
                issues.append(error(
                    "C01", "finding %s uses %r; the documented field is %r"
                    % (fid, singular, plural),
                    "rename to the plural list field; the validator does not repair field "
                    "names",
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
    if not isinstance(commit, str):
        return []
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
    dirty_files = set((ctx.findings.get("baseline") or {}).get("dirty_files") or [])
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


def _lean_audited_theorems(repo):
    data, err = load_json(os.path.join(repo, "verification", "lean", "results.json"))
    if err or not isinstance(data, dict):
        return {}
    audited = {}
    for key in ("theorems", "user_theorems"):
        for th in data.get(key) or []:
            if isinstance(th, dict) and th.get("name"):
                audited[th["name"]] = th
    return audited


def _theorem_belongs_to(target_id, name, audited_entry):
    wanted = _alnum(str(target_id))
    owner = "%s %s" % (name, (audited_entry or {}).get("module") or "")
    return bool(wanted) and wanted in _alnum(owner)


def _property_is_checked(repo, target_id, prop):
    result = prop.get("result")
    if prop.get("tool") == "tla":
        return result in ("violated", "no_violation_within_bounds")
    if prop.get("tool") == "code":
        return result in ("violated", "no_violation_within_bounds", "no_counterexample_found")
    if prop.get("tool") == "lean":
        if result == "proved":
            return True
        audited = _lean_audited_theorems(repo)
        theorems = prop.get("theorems") or prop.get("user_theorems") or []
        return result == "violated" and any(
            isinstance(th, dict) and th.get("name") in audited
            and audited[th["name"]].get("status") == "proved"
            and _theorem_belongs_to(target_id, th["name"], audited[th["name"]])
            for th in theorems)
    return False


def _is_modeled(repo, target):
    return target.get("status") == "modeled" and any(
        _property_is_checked(repo, target.get("id"), p) for p in target.get("properties") or [])


def _alnum(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _has_attempt(repo, target, tdir):
    specs = glob.glob(os.path.join(tdir, "*.tla")) + glob.glob(
        os.path.join(tdir, "mutants", "*.tla"))
    if specs and glob.glob(os.path.join(tdir, "results", "*.json")):
        return True
    if any(_is_code_result(p) for p in glob.glob(os.path.join(tdir, "results", "*.json"))):
        return True
    if target.get("tools") != ["lean"]:
        return False
    lean_dir = os.path.join(repo, "verification", "lean")
    results, err = load_json(os.path.join(lean_dir, "results.json"))
    if err or not isinstance(results, dict) or "result" not in results:
        return False
    wanted = _alnum(str(target.get("id", "")))
    for f in glob.glob(os.path.join(lean_dir, "**", "*.lean"), recursive=True):
        if ".lake" in f.split(os.sep):
            continue
        if wanted and wanted in _alnum(os.path.relpath(f, lean_dir) + "\n" + read_text(f)):
            return True
    return False


def _path_in_target_files(path, files):
    return any(f == path or f.startswith(path + os.sep) or path == "." for f in files)


def check_c04(ctx):
    inv = ctx.findings.get("invocation")
    inv = inv if isinstance(inv, str) else ""
    models_dir = os.path.join(ctx.repo, "verification", "models")
    targets = ctx.targets
    issues = []
    if is_reconcile_run(inv):
        if not targets:
            issues.append(error(
                "C04", "a reconcile run (%r) lists no target in targets[]" % inv,
                "list the targets whose findings were reconciled in targets[]",
            ))
        return issues
    keywords, paths, symbols = invocation_scope(inv, ctx.repo, targets)
    full = bool(inv.strip()) and not (keywords or paths or symbols)
    modeled = [t for t in targets if _is_modeled(ctx.repo, t)]
    attempts = [t for t in targets if t.get("status") == "not_modeled"]
    reason = str(ctx.findings.get("fewer_targets_reason") or "")

    for t in attempts:
        tid = str(t.get("id", "?"))
        tdir = os.path.join(models_dir, tid)
        if not str(t.get("reason") or "").strip():
            issues.append(error(
                "C04", "target %s is not_modeled without a reason" % tid,
                "add targets[].reason saying why modeling was abandoned",
            ))
        if not _has_attempt(ctx.repo, t, tdir):
            issues.append(error(
                "C04",
                "target %s is not_modeled but has no attempt on disk under %s (need a .tla "
                "and at least one results/*.json from run_tlc.sh; a lean-only target needs a "
                ".lean file naming the target and a results.json with a result field under "
                "verification/lean/)" % (tid, tdir),
                "an abandoned target keeps its spec and its run_tlc.sh result (timeout or "
                "error included); if nothing was ever run, select the next ranked target "
                "instead",
            ))

    if not full:
        modeled_files = _target_files(modeled)
        for path in paths:
            if not _path_in_target_files(path, modeled_files):
                issues.append(error(
                    "C04",
                    "the invocation %r names %s but no modeled target's files cover it"
                    % (inv, path),
                    "model a target whose files cover %s, or name the path of the target you "
                    "did model" % path,
                ))
        for sym in symbols:
            if not _symbol_defined_in(ctx.repo, modeled_files, sym):
                issues.append(error(
                    "C04",
                    "the invocation %r names %s but no modeled target's files define it"
                    % (inv, sym),
                    "model a target whose files define %s" % sym,
                ))
        if not modeled:
            issues.append(error(
                "C04",
                "a scoped run (%r) has no modeled target (a modeled target needs a TLA+ "
                "property with result violated or no_violation_within_bounds, or a Lean "
                "property proved or violated by a proved theorem); fewer_targets_reason=%r "
                "does not excuse this" % (inv, reason),
                "model at least one target completely (spec, vacuity, runs) before reporting; "
                "a run with no modeled target is INCOMPLETE",
            ))
        return issues

    if len(targets) < 3:
        issues.append(error(
            "C04",
            "a full run (%r) lists only %d selected target(s) in targets[]; a full run "
            "selects at least 3" % (inv, len(targets)),
            "list every selected target (default: the top 3 ranked) in targets[], as "
            "'modeled' or as 'not_modeled' with a reason and an attempt on disk",
        ))

    if not modeled:
        issues.append(error(
            "C04",
            "a full run (%r) has no modeled target (a modeled target needs a TLA+ property "
            "with result violated or no_violation_within_bounds, or a Lean property proved or "
            "violated by a proved theorem); fewer_targets_reason=%r does not excuse this"
            % (inv, reason),
            "model at least one target completely (spec, vacuity, runs) before reporting; a "
            "run with no modeled target is INCOMPLETE",
        ))

    if len(modeled) < 3:
        if not reason.strip():
            issues.append(error(
                "C04",
                "only %d modeled target(s) in a full run (%r), and fewer_targets_reason is "
                "empty" % (len(modeled), inv),
                "model at least 3 targets, or set fewer_targets_reason explaining why fewer "
                "were modeled",
            ))
        elif not issues:
            issues.append(warn(
                "C04",
                "only %d modeled target(s) in a full run (%r); fewer_targets_reason=%r"
                % (len(modeled), inv, reason),
                "confirm the reason is real; each not_modeled target above has an attempt on "
                "disk",
            ))

    listed = {str(t.get("id")) for t in targets}
    if os.path.isdir(models_dir):
        for name in sorted(os.listdir(models_dir)):
            if name not in listed and os.path.isdir(os.path.join(models_dir, name)):
                issues.append(warn(
                    "C04",
                    "verification/models/%s exists but is not listed in targets[]" % name,
                    "list it as modeled or not_modeled (with reason); work that is not listed "
                    "is not reported",
                ))
    return issues


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
    audited_entries = {}
    for th in (data.get("theorems") or []) + (data.get("user_theorems") or []):
        if isinstance(th, dict) and th.get("name"):
            audited[th["name"]] = th.get("status")
            audited_entries[th["name"]] = th
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
            elif not _theorem_belongs_to(tid, name, audited_entries.get(name)):
                issues.append(error(
                    "C08",
                    "%s/%s: theorem %r belongs to another target (neither its name nor its "
                    "module mentions %r)" % (tid, p.get("name"), name, tid),
                    "cite only theorems of this target's own Lean model, namespaced or "
                    "housed in a module named after the target id",
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


def tool_env(repo):
    env = dict(os.environ)
    extra = [d for d in (os.path.join(repo, ".venv", "bin"),
                         os.path.join(repo, "node_modules", ".bin")) if os.path.isdir(d)]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def run_shell(repo, cmd, timeout, env=None):
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, env=env,
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


COMMAND_NOT_FOUND_FIX = (
    "name the interpreter explicitly (.venv/bin/python -m pytest --collect-only -q) or create "
    "the project's .venv"
)
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
        rc, _out, _err, timed_out = run_shell(repo, cmd, timeout, tool_env(repo))
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
    if not cases and rc == 127:
        return [error(
            "C10",
            "finding %s: repro test %s produced no parseable junit result (exit 127, command "
            "not found; the runner's interpreter is not on PATH)" % (fid, rt),
            COMMAND_NOT_FOUND_FIX,
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
    if not cases and rc == 127:
        return [error(
            "C10",
            "finding %s: guard test %s produced no parseable junit result (exit 127, command "
            "not found; the runner's interpreter is not on PATH)" % (fid, gt),
            COMMAND_NOT_FOUND_FIX,
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
    rc, out, err, timed_out = run_shell(repo, cmd, REPRO_TIMEOUT_SECONDS, tool_env(repo))
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
    rc, out, err, timed_out = run_shell(repo, cmd, REPRO_TIMEOUT_SECONDS, tool_env(repo))
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
    rc, out, err, timed_out = run_shell(
        ctx.repo, collect_cmd, REPRO_TIMEOUT_SECONDS, tool_env(ctx.repo))
    if timed_out:
        return [error("C11", "collect_command timed out after %ds" % REPRO_TIMEOUT_SECONDS,
                       "fix collect_command so it runs quickly")]
    issues = []
    if rc == 127:
        issues.append(error(
            "C11",
            "collect_command exited 127 (command not found) even with %s/.venv/bin on PATH: "
            "%s" % (ctx.repo, (err or out).strip()[:200]),
            COMMAND_NOT_FOUND_FIX,
        ))
    elif rc not in (0, 5):
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


RUN_TLC_SH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_tlc.sh")


def _c16_add(entries, path, tid, label, cited):
    e = entries.setdefault(os.path.normpath(path), {"target": tid, "labels": [], "cited": False})
    if label and label not in e["labels"]:
        e["labels"].append(label)
    e["cited"] = e["cited"] or cited


def cited_results(ctx):
    if "cited" not in ctx.cache:
        ctx.cache["cited"] = _collect_cited_results(ctx)
    return ctx.cache["cited"]


def _collect_cited_results(ctx):
    entries = {}
    for t in ctx.targets:
        tid = str(t.get("id", "?"))
        target_dir = os.path.join(ctx.repo, "verification", "models", tid)
        for path in sorted(glob.glob(os.path.join(target_dir, "results", "*.json"))):
            if not _is_code_result(path):
                _c16_add(entries, path, tid, None, False)
        for prop in t.get("properties") or []:
            if prop.get("tool") != "tla":
                continue
            label = "%s/%s" % (tid, prop.get("name", "?"))
            for run in prop.get("runs") or []:
                path = resolve_repo_relative(ctx.repo, run.get("json"))
                if path and in_target_dir(path, target_dir):
                    _c16_add(entries, path, tid, label, True)
            vac = prop.get("vacuity") or {}
            for key in ("mutants", "sanity"):
                for entry in vac.get(key) or []:
                    path = resolve_result_json(entry, target_dir, ctx.repo)
                    if path:
                        _c16_add(entries, path, tid, label, True)
    return entries


def _c16_extra_args(tokens):
    return tokens[tokens.index("--") + 1:] if "--" in tokens else []


def _c16_command_positionals(tokens):
    positionals = []
    skip = False
    for tok in tokens[1:]:
        if skip:
            skip = False
        elif tok == "--":
            break
        elif tok in ("--out", "--workers", "--timeout"):
            skip = True
        elif not tok.startswith("--"):
            positionals.append(tok)
    return positionals


def _c16_command_names(ctx, tokens, resolved):
    positionals = _c16_command_positionals(tokens)
    if not positionals:
        return False
    spec_arg = positionals[0]
    cfg_arg = positionals[1] if len(positionals) > 1 else re.sub(r"\.tla$", "", spec_arg) + ".cfg"
    for arg, key in ((spec_arg, "spec"), (cfg_arg, "cfg")):
        found = resolve_repo_relative(ctx.repo, arg)
        if not found or os.path.realpath(found) != os.path.realpath(resolved[key]):
            return False
    return True


def _c16_inspect(ctx, path, info):
    inspected = ctx.cache.setdefault("inspect", {})
    if path not in inspected:
        inspected[path] = _c16_inspect_uncached(ctx, path, info)
    return inspected[path]


def _c16_inspect_uncached(ctx, path, info):
    rel = os.path.relpath(path, ctx.repo)
    target_dir = os.path.join(ctx.repo, "verification", "models", info["target"])
    data, err = load_json(path)
    if err or not isinstance(data, dict):
        return None, error("C16", "%s: result JSON does not parse" % rel,
                           "regenerate the result JSON with run_tlc.sh --out")
    command = data.get("command")
    tokens = None
    if isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = None
    if not tokens or os.path.basename(tokens[0]) != "run_tlc.sh":
        return None, error(
            "C16", "%s: result JSON was not produced by run_tlc.sh (command=%r)"
            % (rel, command),
            "never write or edit a result JSON by hand; delete it and produce it with "
            "run_tlc.sh --out",
        )
    resolved = {}
    for key in ("spec", "cfg"):
        found = resolve_repo_relative(ctx.repo, data.get(key))
        if not found or not in_target_dir(found, target_dir):
            return None, error(
                "C16", "%s: result JSON's %s %r does not resolve to a file under %s"
                % (rel, key, data.get(key), target_dir),
                "re-run run_tlc.sh with a spec and cfg inside this target's directory",
            )
        resolved[key] = found
    if not _c16_command_names(ctx, tokens, resolved):
        return None, error(
            "C16", "%s: command %r does not name the spec and cfg recorded in the JSON"
            % (rel, command),
            "never write or edit a result JSON by hand; delete it and produce it with "
            "run_tlc.sh --out",
        )
    log_path = path[:-len(".json")] + ".log" if path.endswith(".json") else path + ".log"
    if not os.path.isfile(log_path):
        return None, error(
            "C16", "%s: no raw TLC log %s next to the result JSON" % (rel, log_path),
            "re-run the cfg with run_tlc.sh --out; it writes both files",
        )
    log_text = read_text(log_path)
    if LOG_VERSION_BANNER not in log_text or os.path.basename(resolved["spec"]) not in log_text:
        return None, error(
            "C16", "%s: %s is not a TLC log of %s (no TLC version banner or spec name)"
            % (rel, os.path.relpath(log_path, ctx.repo), os.path.basename(resolved["spec"])),
            "re-run the cfg with run_tlc.sh --out; it writes both files",
        )
    extra_args = _c16_extra_args(tokens)
    try:
        replayed = tlc_report.build_result(
            log_text, resolved["cfg"], data.get("exit_code") or 0, 0, None,
            data.get("result") == "timeout", None, resolved["spec"], "-deadlock" in extra_args,
            None,
        )
    except Exception as e:
        return None, error(
            "C16", "%s: result JSON cannot be replayed from its TLC log (%s: %s)"
            % (rel, type(e).__name__, e),
            "the JSON was edited after the run; re-run the cfg with run_tlc.sh",
        )
    for key in ("result", "violated", "distinct_states", "constants"):
        if replayed.get(key) != data.get(key):
            return None, error(
                "C16",
                "%s: result JSON does not match its own TLC log (json %s=%r, log %s=%r)"
                % (rel, key, data.get(key), key, replayed.get(key)),
                "the JSON was edited after the run; re-run the cfg with run_tlc.sh",
            )
    return {
        "data": data, "tokens": tokens, "extra_args": extra_args, "spec": resolved["spec"],
        "cfg": resolved["cfg"],
    }, None


def check_c16_static(ctx):
    issues = []
    for path, info in cited_results(ctx).items():
        _details, issue = _c16_inspect(ctx, path, info)
        if issue:
            issues.append(issue)
    return issues


def _c16_exec_groups(ctx):
    groups = {}
    for path, info in cited_results(ctx).items():
        if not info["cited"]:
            continue
        details, issue = _c16_inspect(ctx, path, info)
        if issue or (details["data"].get("result") != "pass"
                     and details["data"].get("result") not in VIOLATION_RESULTS):
            continue
        key = (os.path.realpath(details["spec"]), os.path.realpath(details["cfg"]),
               tuple(details["extra_args"]))
        groups.setdefault(key, []).append((path, info, details))
    return groups


def _c16_workers(tokens):
    if "--workers" in tokens:
        i = tokens.index("--workers")
        if i + 1 < len(tokens) and tokens[i + 1].isdigit():
            return tokens[i + 1]
    return "auto"


def _c16_rerun_limit(elapsed, workers):
    factor = int(workers) if workers.isdigit() else (os.cpu_count() or 1)
    return min(1800, max(120, int(4 * elapsed * max(1, factor)) + 1))


def _c16_number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def check_c16_exec(ctx):
    issues = []
    toolchain_reported = False
    for (spec, cfg, extra_args), members in _c16_exec_groups(ctx).items():
        _path, _info, first = members[0]
        stored_elapsed = _c16_number(first["data"].get("elapsed_seconds"))
        workers = _c16_workers(first["tokens"])
        limit = _c16_rerun_limit(stored_elapsed, workers)
        tmpdir = tempfile.mkdtemp(prefix="check_run_tlc_")
        out_json = os.path.join(tmpdir, "rerun.json")
        try:
            cmd = [RUN_TLC_SH, spec, cfg, "--out", out_json, "--quiet", "--workers",
                   workers, "--timeout", str(limit)]
            if extra_args:
                cmd += ["--"] + list(extra_args)
            rc, out, err, timed_out = run_shell(ctx.repo, shlex.join(cmd), limit + 60)
            rerun, load_err = (load_json(out_json) if os.path.isfile(out_json)
                               else (None, "no result JSON written"))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if rerun is None and not timed_out:
            if not toolchain_reported:
                toolchain_reported = True
                detail = (err or out or load_err or "").strip().splitlines()
                issues.append(error(
                    "C16", "cannot re-verify TLC results: run_tlc.sh failed to start (%s)"
                    % (detail[-1][:200] if detail else "exit %s" % rc),
                    "install Java 17+ and tla2tools (scripts/check_toolchain.sh), or pass "
                    "--no-exec to downgrade this to a WARNING",
                ))
            continue
        rel_cfg = os.path.relpath(first["cfg"], ctx.repo)
        for path, info, details in members:
            who = ", ".join(info["labels"]) or os.path.relpath(path, ctx.repo)
            stored = details["data"]
            if timed_out or rerun.get("result") == "timeout":
                issues.append(error(
                    "C16", "%s: re-run did not finish within %ds (stored elapsed %.1fs)"
                    % (who, limit, stored_elapsed),
                    "shrink the model so it re-checks quickly, or re-run on an idle machine",
                ))
                continue
            fields = ["result"]
            if stored.get("result") == "pass":
                fields.append("distinct_states")
            else:
                fields += ["violated", "violated_candidates"]
            for field in fields:
                if rerun.get(field) != stored.get(field):
                    issues.append(error(
                        "C16", "%s: re-running %s gives %s=%r, stored %r"
                        % (who, rel_cfg, field, rerun.get(field), stored.get(field)),
                        "the stored result does not describe the current spec/cfg; run "
                        "verification/rerun.sh and copy results from the new JSON",
                    ))
                    break
    return issues


def c16_skipped_warning(ctx):
    return warn(
        "C16",
        "--no-exec was given; C16 did not re-run the %d cited TLC cfg(s)"
        % len(_c16_exec_groups(ctx)),
        "run check_run.py without --no-exec before trusting this run",
    )


RUN_CODE_CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_code_check.py")
CODE_PASS_RESULTS = ("no_violation_within_bounds", "no_counterexample_found")


def _is_code_result(path):
    data, err = load_json(path)
    return not err and isinstance(data, dict) and data.get("generator") == "run_code_check.py"


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def _c17_inspect(ctx, path, target_dir):
    cache = ctx.cache.setdefault("c17", {})
    if path not in cache:
        cache[path] = _c17_inspect_uncached(ctx, path, target_dir)
    return cache[path]


def _c17_inspect_uncached(ctx, path, target_dir):
    rel = os.path.relpath(path, ctx.repo)
    redo = "never write or edit a result JSON by hand; produce it with run_code_check.py --out"
    if not in_target_dir(path, target_dir):
        return None, error("C17", "%s is outside the target directory %s"
                           % (rel, os.path.relpath(target_dir, ctx.repo)),
                           "keep every code-level result under verification/models/<id>/"
                           "results/")
    data, err = load_json(path)
    if err or not isinstance(data, dict):
        return None, error("C17", "%s: result JSON does not parse" % rel, redo)
    tool = data.get("tool")
    command = data.get("command")
    try:
        tokens = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        tokens = []
    if (data.get("generator") != "run_code_check.py" or tool not in code_report.TOOLS
            or not tokens or os.path.basename(tokens[0]) != "run_code_check.py"
            or "--tool" not in tokens or tokens[tokens.index("--tool") + 1:][:1] != [tool]):
        return None, error("C17", "%s: not a run_code_check.py result (generator=%r, tool=%r, "
                           "command=%r)" % (rel, data.get("generator"), tool, command), redo)
    if not isinstance(data.get("argv"), list) or not data["argv"]:
        return None, error("C17", "%s: result JSON has no argv" % rel, redo)
    log_path = path[:-len(".json")] + ".log"
    if not os.path.isfile(log_path):
        return None, error("C17", "%s: no raw log %s next to the result JSON"
                           % (rel, os.path.relpath(log_path, ctx.repo)),
                           "re-run with run_code_check.py --out; it writes both files")
    log_text = read_text(log_path)
    header = "%s tool=%s" % (code_report.HEADER_PREFIX, tool)
    if not log_text.startswith(header):
        return None, error("C17", "%s: %s is not a run_code_check.py log for tool %s"
                           % (rel, os.path.relpath(log_path, ctx.repo), tool), redo)
    exit_code = data.get("exit_code")
    replayed = code_report.parse(tool, log_text, exit_code if isinstance(exit_code, int) else 0,
                                 data.get("result") == "timeout")
    for key in ("result", "violated", "exhaustive", "unwinding_failures"):
        if replayed.get(key) != data.get(key):
            return None, error(
                "C17", "%s: result JSON does not match its own log (json %s=%r, log %s=%r)"
                % (rel, key, data.get(key), key, replayed.get(key)),
                "the JSON was edited after the run; re-run with run_code_check.py")
    mutant = data.get("mutant")
    if mutant is not None:
        patch = resolve_repo_relative(ctx.repo, (mutant or {}).get("patch")) \
            if isinstance(mutant, dict) else None
        if not patch or not os.path.isfile(patch):
            return None, error("C17", "%s: mutant patch %r not found"
                               % (rel, (mutant or {}).get("patch") if isinstance(mutant, dict)
                                  else mutant),
                               "keep the mutant patch under verification/models/<id>/mutants/")
        if _sha256(patch) != mutant.get("sha256"):
            return None, error("C17", "%s: mutant patch %s changed after the run"
                               % (rel, os.path.relpath(patch, ctx.repo)),
                               "re-run the mutant with run_code_check.py --mutant")
        files = mutant.get("files") or []
        if not files or any(os.path.normpath(f).split(os.sep)[0] in
                            ("verification", "plans", "verification-plans") for f in files):
            return None, error("C17", "%s: mutant must change the real code, not the harness "
                               "(files=%r)" % (rel, files),
                               "write a patch that breaks the code under verification")
    return data, None


def _c17_entry_path(ctx, entry, target_dir):
    raw = entry.get("json") if isinstance(entry, dict) else entry
    if not isinstance(raw, str):
        return None
    for base in (ctx.repo, target_dir):
        p = os.path.normpath(os.path.join(base, raw))
        if os.path.isfile(p):
            return p
    return None


def _c17_code_jsons(ctx):
    """Every code-level result a selected target cites or keeps under results/."""
    entries = {}
    for t in ctx.targets:
        tid = str(t.get("id", "?"))
        target_dir = os.path.join(ctx.repo, "verification", "models", tid)
        for path in sorted(glob.glob(os.path.join(target_dir, "results", "*.json"))):
            if _is_code_result(path):
                entries.setdefault(os.path.normpath(path), (target_dir, []))
        for prop in t.get("properties") or []:
            if prop.get("tool") != "code":
                continue
            vac = prop.get("vacuity") if isinstance(prop.get("vacuity"), dict) else {}
            for e in list(prop.get("runs") or []) + list(vac.get("mutants") or []) + list(
                    vac.get("sanity") or []):
                p = _c17_entry_path(ctx, e, target_dir)
                if p:
                    entries.setdefault(p, (target_dir, []))[1].append(
                        "%s/%s" % (tid, prop.get("name", "?")))
    return entries


def check_c17(ctx):
    issues = []
    for path, (target_dir, _labels) in _c17_code_jsons(ctx).items():
        _data, issue = _c17_inspect(ctx, path, target_dir)
        if issue:
            issues.append(issue)
    for t in ctx.targets:
        tid = str(t.get("id", "?"))
        target_dir = os.path.join(ctx.repo, "verification", "models", tid)
        for prop in t.get("properties") or []:
            if prop.get("tool") != "code":
                continue
            tag = "%s/%s" % (tid, prop.get("name", "?"))
            result = prop.get("result")
            verifier = prop.get("verifier")
            if verifier not in code_report.TOOLS:
                issues.append(error("C17", "%s: verifier %r is not one of %s"
                                    % (tag, verifier, list(code_report.TOOLS)),
                                    "set verifier to the --tool given to run_code_check.py"))
            harness = resolve_repo_relative(ctx.repo, prop.get("harness"))
            if not harness or not os.path.isfile(harness) or not in_target_dir(harness,
                                                                               target_dir):
                issues.append(error(
                    "C17", "%s: harness %r is not a file under %s"
                    % (tag, prop.get("harness"), os.path.relpath(target_dir, ctx.repo)),
                    "put the harness (proof harness, contract wrapper, or property test) in "
                    "verification/models/<id>/ and cite it in 'harness'"))
            if not str(prop.get("bounds") or "").strip():
                issues.append(error(
                    "C17", "%s: no 'bounds' (unwind depth, input domain, search budget)" % tag,
                    "state the bounds the result holds under; a code-level pass covers only "
                    "its harness's inputs and unwinding"))
            if result not in ("violated",) + CODE_PASS_RESULTS:
                continue
            runs = []
            for i, run in enumerate(prop.get("runs") or []):
                p = _c17_entry_path(ctx, run, target_dir)
                if not p:
                    issues.append(error("C17", "%s: runs[%d] json %r not found"
                                        % (tag, i, run.get("json")),
                                        "cite the result JSON run_code_check.py wrote"))
                    continue
                data, issue = _c17_inspect(ctx, p, target_dir)
                if issue:
                    continue
                if data.get("tool") != verifier:
                    issues.append(error("C17", "%s: runs[%d] was produced by %s, not %s"
                                        % (tag, i, data.get("tool"), verifier),
                                        "cite runs of the property's own verifier"))
                if data.get("mutant") is not None:
                    issues.append(error("C17", "%s: runs[%d] is a mutant run; runs[] cites "
                                        "runs on the unmodified code" % (tag, i),
                                        "move mutant results to vacuity.mutants"))
                    continue
                if run.get("result") != data.get("result"):
                    issues.append(error("C17", "%s: runs[%d] says result=%r, the JSON says %r"
                                        % (tag, i, run.get("result"), data.get("result")),
                                        "copy result from the JSON"))
                runs.append(data)
            if not runs:
                issues.append(error("C17", "%s: result=%r but no valid run is cited"
                                    % (tag, result),
                                    "cite at least one run_code_check.py result in runs[]"))
                continue
            if result == "violated" and not any(r.get("result") == "violation" for r in runs):
                issues.append(error("C17", "%s: result=violated but no cited run is a "
                                    "violation" % tag,
                                    "cite the run whose counterexample shows the violation"))
            if result in CODE_PASS_RESULTS:
                bad = [r.get("result") for r in runs if r.get("result") != "pass"]
                if bad:
                    issues.append(error(
                        "C17", "%s: result=%s but cited runs are %s" % (tag, result, bad),
                        "a pass claim needs every cited run to pass; incomplete means the "
                        "bound was too small, vacuous means nothing was checked"))
                if result == "no_violation_within_bounds" and not all(
                        r.get("exhaustive") for r in runs):
                    issues.append(error(
                        "C17", "%s: result=no_violation_within_bounds but a cited run is not "
                        "exhaustive (kind %s)" % (tag, sorted({r.get("kind") for r in runs})),
                        "a non-exhaustive search (Hypothesis, CrossHair with unconfirmed "
                        "conditions) is 'no_counterexample_found'"))
                vac = prop.get("vacuity") if isinstance(prop.get("vacuity"), dict) else {}
                if vac.get("status") != "passed":
                    issues.append(error("C17", "%s: vacuity.status=%r, not 'passed'"
                                        % (tag, vac.get("status")),
                                        "show the check can fail before reporting a pass"))
                for key, want_mutant, what in (
                        ("mutants", True, "a --mutant run that breaks the real code"),
                        ("sanity", False, "a sanity harness that must fail")):
                    good = 0
                    for entry in vac.get(key) or []:
                        p = _c17_entry_path(ctx, entry, target_dir)
                        data, issue = (_c17_inspect(ctx, p, target_dir) if p
                                       else (None, None))
                        if not p:
                            issues.append(error("C17", "%s: vacuity.%s entry %r not found"
                                                % (tag, key, entry),
                                                "cite the result JSON path"))
                            continue
                        if issue:
                            continue
                        if (data.get("mutant") is not None) != want_mutant:
                            issues.append(error(
                                "C17", "%s: vacuity.%s entry %s %s a --mutant run"
                                % (tag, key, os.path.relpath(p, ctx.repo),
                                   "is not" if want_mutant else "is"),
                                "mutants come from run_code_check.py --mutant; sanity runs "
                                "use the unmodified code"))
                            continue
                        if data.get("tool") != verifier:
                            issues.append(error("C17", "%s: vacuity.%s entry %s uses %s, not "
                                                "%s" % (tag, key, os.path.relpath(p, ctx.repo),
                                                        data.get("tool"), verifier),
                                                "check vacuity with the property's verifier"))
                            continue
                        if data.get("result") != "violation":
                            issues.append(error(
                                "C17", "%s: vacuity.%s entry %s has result %r; it must be a "
                                "violation" % (tag, key, os.path.relpath(p, ctx.repo),
                                               data.get("result")),
                                "a mutant the check does not catch, or a sanity check that "
                                "passes, means the property cannot fail: fix the harness"))
                            continue
                        good += 1
                    if not good:
                        issues.append(error("C17", "%s: no valid vacuity.%s (need %s)"
                                            % (tag, key, what),
                                            "see references/code-level-playbook.md section 4"))
    return issues


def _c17_rerun_limit(elapsed):
    return min(1800, max(120, int(4 * elapsed) + 1))


def check_c17_exec(ctx):
    issues = []
    for path, (target_dir, labels) in _c17_code_jsons(ctx).items():
        if not labels:
            continue
        data, issue = _c17_inspect(ctx, path, target_dir)
        if issue or data.get("result") not in ("pass", "violation"):
            continue
        who = ", ".join(sorted(set(labels)))
        rel = os.path.relpath(path, ctx.repo)
        limit = _c17_rerun_limit(_c16_number(data.get("elapsed_seconds")))
        tmpdir = tempfile.mkdtemp(prefix="check_run_code_")
        out_json = os.path.join(tmpdir, "rerun.json")
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else "."
        cwd = cwd if os.path.isabs(cwd) else os.path.join(ctx.repo, cwd)
        cmd = [sys.executable, RUN_CODE_CHECK, "--tool", data["tool"], "--out", out_json,
               "--quiet", "--timeout", str(limit), "--cwd", cwd]
        if isinstance(data.get("mutant"), dict):
            cmd += ["--mutant", os.path.join(ctx.repo, data["mutant"]["patch"])]
        cmd += ["--"] + [str(a) for a in data["argv"]]
        try:
            rc, out, err, timed_out = run_shell(ctx.repo, shlex.join(cmd), limit + 60,
                                                env=tool_env(ctx.repo))
            rerun, load_err = (load_json(out_json) if os.path.isfile(out_json)
                               else (None, "no result JSON written"))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if rerun is None:
            detail = (err or out or load_err or "").strip().splitlines()
            issues.append(error(
                "C17", "%s (%s): re-run failed to produce a result (%s)"
                % (who, rel, detail[-1][:200] if detail else "exit %s" % rc),
                "install the verifier (scripts/check_toolchain.sh --code) or pass --no-exec "
                "to downgrade this to a WARNING"))
            continue
        for field in ("result", "violated") + (("exhaustive",) if data["result"] == "pass"
                                               else ()):
            if rerun.get(field) != data.get(field):
                issues.append(error(
                    "C17", "%s: re-running %s gives %s=%r, stored %r"
                    % (who, rel, field, rerun.get(field), data.get(field)),
                    "the stored result does not describe the current code or harness; re-run "
                    "verification/rerun.sh and copy results from the new JSON"))
                break
    return issues


def c17_skipped_warning(ctx):
    n = sum(1 for _p, (_d, labels) in _c17_code_jsons(ctx).items() if labels)
    if not n:
        return None
    return warn("C17", "--no-exec was given; C17 did not re-run the %d cited code-level "
                "result(s)" % n, "run check_run.py without --no-exec before trusting this run")


DIAGRAM_META_RE = re.compile(
    r'<script type="application/json" id="vf-diagram-meta">(.*?)</script>', re.S)


def check_c18(ctx):
    issues = []
    page = os.path.join(ctx.repo, "verification", "state-diagram.html")
    render = ("run scripts/render_diagram.py <repo> after the last change to any model, cfg, "
              "result, or diagram.json")
    modeled = [t for t in ctx.targets if t.get("status") == "modeled"]
    if not modeled and not os.path.isfile(page):
        return issues
    if not os.path.isfile(page):
        return [error("C18", "verification/state-diagram.html not found", render)]
    m = DIAGRAM_META_RE.search(read_text(page))
    try:
        meta = json.loads(m.group(1).replace("<\\/", "</")) if m else None
    except ValueError:
        meta = None
    if not isinstance(meta, dict) or meta.get("generator") != render_diagram.GENERATOR:
        return [error("C18", "verification/state-diagram.html was not written by "
                      "render_diagram.py (no readable vf-diagram-meta block)",
                      "never write or edit the page by hand; " + render)]
    if meta.get("commit") != ctx.findings.get("commit"):
        issues.append(warn("C18", "state-diagram.html was rendered at commit %r, findings.json "
                           "is at %r" % (meta.get("commit"), ctx.findings.get("commit")),
                           render + " so the page names the commit it describes"))
    by_id = {e.get("id"): e for e in meta.get("targets") or [] if isinstance(e, dict)}
    readme = read_text(os.path.join(ctx.repo, "verification", "README.md")) or ""
    if "state-diagram.html" not in readme:
        issues.append(warn("C18", "verification/README.md does not link state-diagram.html",
                           "link the page from the README so a reader finds the diagrams"))
    for t in modeled:
        tid = str(t.get("id", "?"))
        tdir = os.path.join(ctx.repo, "verification", "models", tid)
        dpath = os.path.join(tdir, "diagram.json")
        if not os.path.isfile(dpath):
            issues.append(error("C18", "%s: no verification/models/%s/diagram.json" % (tid, tid),
                                "write diagram.json (kind 'tla' for a TLA+ target, 'manual' "
                                "otherwise) and " + render))
            continue
        spec, err = load_json(dpath)
        if err or not isinstance(spec, dict):
            issues.append(error("C18", "%s: diagram.json does not parse" % tid,
                                "fix diagram.json"))
            continue
        has_tla = any(p.get("tool") == "tla" for p in t.get("properties") or [])
        if has_tla and spec.get("kind") != "tla":
            issues.append(error("C18", "%s: the target has a TLA+ model but diagram.json kind "
                                "is %r" % (tid, spec.get("kind")),
                                "use kind 'tla' so the diagram is derived from the checked "
                                "spec, not drawn by hand"))
        entry = by_id.get(tid)
        if entry is None:
            issues.append(error("C18", "%s: state-diagram.html has no section for this target"
                                % tid, render))
            continue
        if entry.get("error"):
            issues.append(error("C18", "%s: the diagram failed to render: %s"
                                % (tid, entry["error"]),
                                "fix diagram.json (or the projection) and " + render))
            continue
        if not entry.get("nodes"):
            issues.append(error("C18", "%s: the diagram has no states" % tid, render))
        expected = render_diagram.input_files(tdir, spec)
        inputs = entry.get("inputs") if isinstance(entry.get("inputs"), dict) else {}
        missing = [f for f in expected if f not in inputs]
        stale = []
        for f in expected:
            p = os.path.join(tdir, f)
            if f in inputs and (not os.path.isfile(p) or render_diagram.sha256_file(p)
                                != inputs[f]):
                stale.append(f)
        if missing or stale:
            issues.append(error("C18", "%s: state-diagram.html is stale (changed since "
                                "rendering: %s)" % (tid, ", ".join(missing + stale)), render))
        if spec.get("kind") == "tla":
            violated = set()
            for p in t.get("properties") or []:
                if p.get("tool") == "tla" and p.get("result") == "violated":
                    for run in p.get("runs") or []:
                        if run.get("result") in VIOLATION_RESULTS and run.get("json"):
                            violated.add(os.path.normpath(run["json"]))
            shown = {os.path.normpath(os.path.join(os.path.relpath(tdir, ctx.repo), h))
                     for h in spec.get("highlight") or []}
            if violated and not violated & shown:
                issues.append(warn("C18", "%s: no violating run is highlighted on the diagram"
                                   % tid, "add a violating results/<cfg>.json to "
                                   "diagram.json 'highlight' so the counterexample is drawn"))
    return issues


def _git_ok(repo, args):
    p = subprocess.run(["git", "-C", repo] + args, capture_output=True, text=True)
    return p.returncode == 0, p.stdout


def _correspondence_sha(path):
    for line in read_text(path).splitlines():
        if "commit" in line.lower():
            m = SHA_RE.search(line)
            if m:
                return m.group(0)
    return None


def check_c19(ctx):
    if not is_reconcile_run(ctx.findings.get("invocation")):
        return []
    issues = []
    head, _ = git_head(ctx.repo)
    head = (head or "").strip()
    readme = read_text(os.path.join(ctx.repo, "verification", "README.md")) or ""
    if "Reconcile" not in readme:
        issues.append(error("C19", "verification/README.md has no 'Reconcile' section",
                            "add '## Reconcile' with each target's outcome, the commits read, "
                            "what changed in the verification and why"))
    findings = ctx.findings.get("findings") or []
    for t in ctx.targets:
        if t.get("status") != "modeled":
            continue
        tid = str(t.get("id", "?"))
        tdir = os.path.join(ctx.repo, "verification", "models", tid)
        rec = t.get("reconcile")
        fix = "record the reconcile pass in targets[].reconcile (references/reconcile.md)"
        if not isinstance(rec, dict):
            issues.append(error("C19", "%s: no reconcile record" % tid, fix))
            continue
        outcome = set(rec.get("outcome") or [])
        if not outcome or not outcome <= RECONCILE_OUTCOMES or (
                len(outcome) > 1 and outcome != {"updated", "bug"}):
            issues.append(error(
                "C19", "%s: reconcile.outcome %r is not one of in_sync, updated, bug, blocked "
                "(or updated + bug)" % (tid, sorted(outcome)), fix))
            continue
        frm = str(rec.get("from_commit") or "")
        to = str(rec.get("to_commit") or "")
        if not SHA_RE.fullmatch(frm):
            issues.append(error("C19", "%s: reconcile.from_commit %r is not a full SHA"
                                % (tid, frm), "use the sync commit recorded in "
                                "CORRESPONDENCE.md (or the last commit that changed the "
                                "verification files, and say so)"))
            continue
        ok, _ = _git_ok(ctx.repo, ["merge-base", "--is-ancestor", frm, "HEAD"])
        if not ok:
            issues.append(error("C19", "%s: from_commit %s is not an ancestor of HEAD"
                                % (tid, frm[:12]), "use the sync commit the verification was "
                                "last confirmed at"))
            continue
        blocked = outcome == {"blocked"}
        if blocked:
            if not str(rec.get("blocked_reason") or "").strip():
                issues.append(error("C19", "%s: blocked without blocked_reason" % tid,
                                    "say exactly why the check could not be brought back in "
                                    "line or could not run"))
            if to != frm:
                issues.append(error("C19", "%s: a blocked check keeps its sync commit; "
                                    "to_commit %s should equal from_commit %s"
                                    % (tid, to[:12], frm[:12]),
                                    "do not move the sync commit for a blocked check"))
        elif to != head:
            issues.append(error("C19", "%s: to_commit %s is not HEAD %s"
                                % (tid, to[:12], head[:12]),
                                "set to_commit to the commit you checked (HEAD)"))
        corr = os.path.join(tdir, "CORRESPONDENCE.md")
        stamp = _correspondence_sha(corr) if os.path.isfile(corr) else None
        want = frm if blocked else head
        if stamp != want:
            issues.append(error(
                "C19", "%s: CORRESPONDENCE.md sync commit is %s, expected %s"
                % (tid, (stamp or "missing")[:12], want[:12]),
                "a blocked check keeps the old sync commit; every other outcome moves it to "
                "HEAD after the verification is confirmed to describe the code there"))
        files = [f for f in (t.get("files") or []) + (rec.get("files_before") or [])
                 if isinstance(f, str)]
        ok, out = _git_ok(ctx.repo, ["rev-list", "%s..HEAD" % frm, "--"] + files) if files \
            else (True, "")
        must_read = [c for c in out.split() if c]
        read = [str(c) for c in rec.get("commits_read") or []]
        unread = [c for c in must_read if not any(r and c.startswith(r) for r in read)]
        if unread:
            issues.append(error(
                "C19", "%s: %d commit(s) touching the covered files were not read: %s"
                % (tid, len(unread), ", ".join(c[:12] for c in unread[:8])),
                "read every commit from the sync commit to HEAD that touches covered code, and "
                "list it in commits_read"))
        changes = rec.get("changes") or []
        classified = {str(c.get("commit") or "") for c in changes}
        for c in read:
            if not any(x and (x.startswith(c) or c.startswith(x)) for x in classified):
                issues.append(error("C19", "%s: commit %s is in commits_read but has no "
                                    "entry in changes" % (tid, c[:12]),
                                    "classify every commit read: no_effect, shape_changed, "
                                    "promise_changed (with evidence), or promise_maybe_broken"))
        effects = set()
        for i, c in enumerate(changes):
            eff = c.get("effect")
            effects.add(eff)
            if eff not in CHANGE_EFFECTS:
                issues.append(error("C19", "%s: changes[%d].effect %r is not one of %s"
                                    % (tid, i, eff, sorted(CHANGE_EFFECTS)), fix))
            if eff == "promise_changed" and not str(c.get("evidence") or "").strip():
                issues.append(error(
                    "C19", "%s: changes[%d] says the promise changed on purpose but cites no "
                    "evidence" % (tid, i),
                    "cite the doc comment, test, changelog, or commit message that says the "
                    "new behavior is intended; without it the change is promise_maybe_broken"))
            if eff == "promise_changed" and not (isinstance(c.get("property_change"), dict)
                                                 and c["property_change"].get("old")
                                                 and c["property_change"].get("new")):
                issues.append(error(
                    "C19", "%s: changes[%d] changes the promise but does not record the "
                    "property's old and new wording" % (tid, i),
                    "add property_change: {old, new}"))
        if "in_sync" in outcome and effects - {"no_effect"}:
            issues.append(error("C19", "%s: outcome in_sync but a change is %s"
                                % (tid, sorted(effects - {"no_effect"})),
                                "a change that alters what the verification describes makes "
                                "the outcome updated (or bug)"))
        if "updated" in outcome and not effects & {"shape_changed", "promise_changed"}:
            issues.append(error("C19", "%s: outcome updated but no change is shape_changed or "
                                "promise_changed" % tid,
                                "classify the change that made you update the verification"))
        if not blocked:
            pre = rec.get("pre_update_runs") or []
            if not pre:
                issues.append(error("C19", "%s: no pre_update_runs" % tid,
                                    "run every check once before changing anything and keep "
                                    "the results under results/before/"))
            for entry in pre:
                p = resolve_repo_relative(ctx.repo, entry) or os.path.join(tdir, entry)
                if not (p and os.path.isfile(p) and os.path.isfile(p[:-5] + ".log")
                        and in_target_dir(p, os.path.join(tdir, "results", "before"))):
                    issues.append(error(
                        "C19", "%s: pre_update_runs entry %r is not a result JSON with its log "
                        "under results/before/" % (tid, entry),
                        "keep the before-update runs (run_tlc.sh or run_code_check.py --out "
                        "results/before/<stem>.json)"))
        if "updated" in outcome:
            breaks = rec.get("post_update_break") or []
            good = 0
            for entry in breaks:
                p = resolve_repo_relative(ctx.repo, entry) or os.path.join(tdir, entry)
                data, err = load_json(p) if p and os.path.isfile(p) else (None, "missing")
                result = (data or {}).get("result") if isinstance(data, dict) else None
                if result in VIOLATION_RESULTS or result == "violation":
                    good += 1
                else:
                    issues.append(error("C19", "%s: post_update_break entry %r is not a "
                                        "violating result" % (tid, entry),
                                        "break the new code the way the property guards "
                                        "against and show the updated check catches it"))
            if not good:
                issues.append(error(
                    "C19", "%s: outcome updated but no post_update_break shows the updated "
                    "check can still fail" % tid,
                    "run a mutant of the changed code (run_code_check.py --mutant, or a TLA+ "
                    "mutant cfg of the changed action) and cite its violating result"))
        if "bug" in outcome:
            mine = [f for f in findings if tid in (f.get("targets") or [])
                    and f.get("status") == "CONFIRMED"]
            if not mine:
                issues.append(error("C19", "%s: outcome bug but no CONFIRMED finding lists this "
                                    "target" % tid,
                                    "a bug is reported as a finding with a failing repro test"))
            for f in mine:
                if not SHA_RE.fullmatch(str(f.get("introduced_by") or "")):
                    issues.append(error(
                        "C19", "finding %s: a reconcile bug names the commit that introduced it "
                        "(introduced_by, full SHA)" % f.get("id"),
                        "find the first commit after from_commit where the counterexample "
                        "replays against the code"))
    return issues


STATIC_CHECKS = [
    check_c01, check_c02, check_c03, check_c04, check_c05, check_c06, check_c07,
    check_c08, check_c09, check_c12, check_c13, check_c14, check_c15, check_c16_static,
    check_c17, check_c18, check_c19,
]
EXEC_CHECKS = [check_c10, check_c11, check_c16_exec, check_c17_exec]


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

    issues = check_c01_raw(raw)
    clean, sanitize_issues = sanitize(raw if isinstance(raw, dict) else {})
    ctx = Context(repo, clean)

    issues.extend(sanitize_issues)
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
        issues.append(c16_skipped_warning(ctx))
        skipped = c17_skipped_warning(ctx)
        if skipped:
            issues.append(skipped)
    issues.sort(key=lambda i: (i["check"], i["level"]))

    if args.json:
        print(json.dumps(issues, indent=2))
    else:
        print_text(issues)

    return 1 if any(i["level"] == "ERROR" for i in issues) else 0


if __name__ == "__main__":
    sys.exit(main())
