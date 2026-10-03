# Usage: lean_audit.py forbidden-scan <project_dir>
#        lean_audit.py gen-checker <project_dir>
#        lean_audit.py modules <project_dir>
#        lean_audit.py finalize --build-log LOG --lean-version VER [--forbidden FILE]
#            [--modules FILE] [--build-failed] [--checker-stdout FILE] [--checker-stderr FILE]
#            [--checker-exit N] [--kernel-stdout FILE] [--kernel-stderr FILE] [--kernel-exit N]
#            [--allow-native-decide] [--gen-checker-failed] [--gen-checker-stderr FILE]
#
# Internal helper invoked by lean_audit.sh; see lean_audit.sh's header for the result value,
# exit code contract, and the "user_theorems" JSON field.
import argparse
import json
import os
import re
import sys

ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
LEGACY_NATIVE_TRUST_AXIOMS = {"Lean.ofReduceBool", "Lean.trustCompiler"}
NATIVE_DECIDE_AXIOM_RE = re.compile(r"\._native\.(?:native_decide|decide)\.ax_\d+_\d+$")

JSON_MARKER = "===LEAN_AUDIT_JSON==="

FORBIDDEN_PATTERNS = [
    ("sorry", re.compile(r"\bsorry\b")),
    ("admit", re.compile(r"\badmit\b")),
    ("axiom", re.compile(r"\baxiom\b")),
    ("native_decide", re.compile(r"\bnative_decide\b")),
    ("decide_native", re.compile(r"\bdecide\s+\+native\b")),
    ("implemented_by", re.compile(r"\bimplemented_by\b")),
    ("extern", re.compile(r"\bextern\b")),
    ("unsafe", re.compile(r"\bunsafe\b")),
    ("skip_kernel_tc", re.compile(r"\bdebug\.skipKernelTC\b")),
    ("add_decl", re.compile(r"\baddDecl\b")),
    ("run_cmd", re.compile(r"\brun_cmd\b")),
    ("run_elab", re.compile(r"\brun_elab\b")),
    ("run_meta", re.compile(r"\brun_meta\b")),
]

SORRY_WARNING_RE = re.compile(r"warning: (\S+):(\d+):\d+: declaration uses `sorry`")

THEOREM_LEMMA_DECL_RE = re.compile(r"\b(?:theorem|lemma)\s+([^\s:({\[]+)")

CHAR_LITERAL_RE = re.compile(r"'(?:\\.|[^\\'\n])'")


def is_ident_continuation(c):
    if c is None:
        return False
    if c.isalnum() or c in "_'":
        return True
    return ord(c) > 127


def mask_lean_source(text):
    out = list(text)
    i = 0
    n = len(text)
    in_string = False
    in_char = False
    block_depth = 0
    line_comment = False
    while i < n:
        c = text[i]
        if line_comment:
            if c == "\n":
                line_comment = False
            else:
                out[i] = " "
            i += 1
            continue
        if block_depth > 0:
            if text.startswith("/-", i):
                block_depth += 1
                out[i] = " "
                out[i + 1] = " "
                i += 2
                continue
            if text.startswith("-/", i):
                block_depth -= 1
                out[i] = " "
                out[i + 1] = " "
                i += 2
                continue
            if c != "\n":
                out[i] = " "
            i += 1
            continue
        if in_string:
            if c == "\\" and i + 1 < n:
                out[i] = " "
                out[i + 1] = " "
                i += 2
                continue
            if c != "\n":
                out[i] = " "
            if c == '"':
                in_string = False
            i += 1
            continue
        if in_char:
            if c == "\\" and i + 1 < n:
                out[i] = " "
                out[i + 1] = " "
                i += 2
                continue
            if c != "\n":
                out[i] = " "
            if c == "'":
                in_char = False
            i += 1
            continue
        if text.startswith("--", i):
            line_comment = True
            out[i] = " "
            out[i + 1] = " "
            i += 2
            continue
        if text.startswith("/-", i):
            block_depth = 1
            out[i] = " "
            out[i + 1] = " "
            i += 2
            continue
        if c == "r" and not is_ident_continuation(text[i - 1] if i > 0 else None):
            j = i + 1
            hashes = 0
            while j < n and text[j] == "#":
                hashes += 1
                j += 1
            if j < n and text[j] == '"':
                j += 1
                closing = '"' + ("#" * hashes)
                end = text.find(closing, j)
                end = n if end == -1 else end + len(closing)
                for k in range(i, end):
                    if text[k] != "\n":
                        out[k] = " "
                i = end
                continue
        if c == '"':
            in_string = True
            i += 1
            continue
        if c == "'" and not is_ident_continuation(text[i - 1] if i > 0 else None) \
                and CHAR_LITERAL_RE.match(text, i):
            in_char = True
            i += 1
            continue
        i += 1
    return "".join(out)


def find_lean_files(project_dir):
    files = []
    for root, dirs, filenames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d != ".lake" and not d.startswith(".git")]
        for name in filenames:
            if name.endswith(".lean"):
                files.append(os.path.join(root, name))
    return sorted(files)


def scan_forbidden(project_dir):
    hits = []
    for path in find_lean_files(project_dir):
        rel = os.path.relpath(path, project_dir)
        with open(path, errors="replace") as f:
            raw = f.read()
        masked = mask_lean_source(raw)
        lines = masked.split("\n")
        for lineno, line in enumerate(lines, start=1):
            for pattern_name, regex in FORBIDDEN_PATTERNS:
                if regex.search(line):
                    hits.append({"file": rel, "line": lineno, "pattern": pattern_name})
    return hits


def scan_theorem_lemma_names(project_dir):
    names = set()
    for path in find_lean_files(project_dir):
        with open(path, errors="replace") as f:
            raw = f.read()
        masked = mask_lean_source(raw)
        for m in THEOREM_LEMMA_DECL_RE.finditer(masked):
            names.add(m.group(1).rsplit(".", 1)[-1])
    return names


def cmd_forbidden_scan(args):
    hits = scan_forbidden(args.project_dir)
    declared_names = sorted(scan_theorem_lemma_names(args.project_dir))
    json.dump({"forbidden_hits": hits, "declared_theorem_names": declared_names}, sys.stdout, indent=2)
    sys.stdout.write("\n")


def path_to_module(rel_path_no_ext):
    return rel_path_no_ext.replace(os.sep, ".")


def find_built_modules(project_dir):
    build_lib_dir = os.path.join(project_dir, ".lake", "build", "lib", "lean")
    modules = set()
    if not os.path.isdir(build_lib_dir):
        return modules
    for root, dirs, filenames in os.walk(build_lib_dir):
        for name in filenames:
            if not name.endswith(".olean"):
                continue
            rel = os.path.relpath(os.path.join(root, name), build_lib_dir)
            modules.add(path_to_module(rel[: -len(".olean")]))
    return modules


LEAN_LIB_TOML_KEY_RE = re.compile(r'^(\w+)\s*=\s*"([^"]*)"\s*$')


def parse_toml_lib_src_dirs(path):
    try:
        with open(path, errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    src_dirs = []
    in_lib = False
    current_src_dir = "."
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[["):
            if in_lib:
                src_dirs.append(current_src_dir)
            in_lib = stripped == "[[lean_lib]]"
            current_src_dir = "."
            continue
        if stripped.startswith("[") and not stripped.startswith("[["):
            if in_lib:
                src_dirs.append(current_src_dir)
            in_lib = False
            continue
        if in_lib:
            m = LEAN_LIB_TOML_KEY_RE.match(stripped)
            if m and m.group(1) == "srcDir":
                current_src_dir = m.group(2)
    if in_lib:
        src_dirs.append(current_src_dir)
    return src_dirs


LEAN_LIB_DECL_RE = re.compile(r'^\s*(?:@\[[^\]]*\]\s*)?lean_lib\b')
SRC_DIR_ASSIGN_RE = re.compile(r'srcDir\s*:=\s*"([^"]*)"')


def parse_lakefile_lean_src_dirs(path):
    try:
        with open(path, errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    src_dirs = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if LEAN_LIB_DECL_RE.match(line):
            src_dir = "."
            m = SRC_DIR_ASSIGN_RE.search(line)
            if m:
                src_dir = m.group(1)
            j = i + 1
            while j < n:
                nxt = lines[j]
                if nxt.strip() == "":
                    j += 1
                    continue
                if not nxt[:1].isspace():
                    break
                m2 = SRC_DIR_ASSIGN_RE.search(nxt)
                if m2:
                    src_dir = m2.group(1)
                j += 1
            src_dirs.append(src_dir)
            i = j
            continue
        i += 1
    return src_dirs


def find_lib_src_dirs(project_dir):
    src_dirs = []
    toml_path = os.path.join(project_dir, "lakefile.toml")
    lean_path = os.path.join(project_dir, "lakefile.lean")
    if os.path.isfile(toml_path):
        src_dirs.extend(parse_toml_lib_src_dirs(toml_path))
    if os.path.isfile(lean_path):
        src_dirs.extend(parse_lakefile_lean_src_dirs(lean_path))
    normalized = []
    has_dot_src_dir = False
    for d in src_dirs:
        d = d.strip().replace("/", os.sep)
        while d.endswith(os.sep):
            d = d[:-1]
        if d in ("", "."):
            has_dot_src_dir = True
            continue
        normalized.append(d)
    return sorted(set(normalized), key=len, reverse=True), has_dot_src_dir


def find_source_modules(project_dir):
    src_dirs, has_dot_src_dir = find_lib_src_dirs(project_dir)
    modules = {}
    for path in find_lean_files(project_dir):
        rel = os.path.relpath(path, project_dir)
        if rel == "lakefile.lean":
            continue
        rel_no_ext = rel[: -len(".lean")]
        matched = rel_no_ext if not src_dirs else None
        for d in src_dirs:
            prefix = d + os.sep
            if rel_no_ext.startswith(prefix):
                matched = rel_no_ext[len(prefix):]
                break
        if matched is None and has_dot_src_dir:
            matched = rel_no_ext
        key = path_to_module(matched) if matched is not None else rel
        if key in modules:
            key = rel
        modules[key] = rel
    return modules


CHECKER_TEMPLATE = '''import Lean
{imports}

open Lean Meta

def builtModules : List Lean.Name := [{modules}]

def isGeneratedSuffix (s : String) : Bool :=
  let digitsSuffix (pfx : String) : Bool :=
    s.startsWith pfx && (s.drop pfx.length).length > 0 && (s.drop pfx.length).all Char.isDigit
  s == "_sunfold" || s == "_unsafe_rec" || s == "_cstage1" || s == "_cstage2" ||
  s == "_impl" || s == "_elabAsElim" || s == "_regBuiltin" ||
  s == "eq_def" || s == "injEq" || s == "inj" || s == "sizeOf_spec" || s == "_arg_pusher" ||
  digitsSuffix "_eq_" || digitsSuffix "_proof_" || digitsSuffix "_unfold_" || digitsSuffix "eq_"

def isAuxDecl (env : Lean.Environment) (n : Lean.Name) : MetaM Bool := do
  if Lean.isAuxRecursor env n then return true
  if Lean.isNoConfusion env n then return true
  if ← Lean.Meta.isMatcher n then return true
  if n.hasMacroScopes then return true
  let userName := if Lean.isPrivateName n then Lean.privateToUserName n else n
  match userName with
  | .str _ s => return isGeneratedSuffix s
  | _ => return false

def displayName (n : Lean.Name) : Lean.Name :=
  if Lean.isPrivateName n then Lean.privateToUserName n else n

#eval show MetaM Unit from do
  let env ← getEnv
  let mut results : Array Lean.Json := #[]
  for (name, info) in env.constants.toList do
    if info.isAxiom then continue
    let some modIdx := env.getModuleIdxFor? name | continue
    let modName := env.header.moduleNames[modIdx.toNat]!
    if !(builtModules.contains modName) then continue
    if (← isAuxDecl env name) then continue
    let isCand ←
      if info.isTheorem then pure true
      else isProp info.type
    if !isCand then continue
    let axioms ← Lean.collectAxioms name
    let axNames := axioms.toList.map toString
    let ranges ← Lean.findDeclarationRanges? name
    let line := match ranges with
      | some r => r.range.pos.line
      | none => 0
    results := results.push (Lean.Json.mkObj [
      ("name", Lean.Json.str (toString (displayName name))),
      ("internal_name", Lean.Json.str (toString name)),
      ("module", Lean.Json.str (toString modName)),
      ("line", Lean.Json.num line),
      ("axioms", Lean.Json.arr (axNames.map Lean.Json.str).toArray)
    ])
  IO.println "{marker}"
  IO.println (Lean.Json.pretty (Lean.Json.mkObj [("theorems", Lean.Json.arr results)]))
'''


def cmd_gen_checker(args):
    modules = sorted(find_built_modules(args.project_dir))
    if not modules:
        print("no built modules found under .lake/build/lib/lean", file=sys.stderr)
        sys.exit(2)
    imports = "\n".join(f"import {m}" for m in modules)
    modules_literal = ", ".join(f"`{m}" for m in modules)
    sys.stdout.write(CHECKER_TEMPLATE.format(imports=imports, modules=modules_literal, marker=JSON_MARKER))


def cmd_modules(args):
    built = find_built_modules(args.project_dir)
    source = find_source_modules(args.project_dir)
    unaudited = sorted(set(source) - built)
    json.dump({"built": sorted(built), "unaudited": unaudited}, sys.stdout, indent=2)
    sys.stdout.write("\n")


def parse_checker_output(text):
    idx = text.find(JSON_MARKER)
    if idx == -1:
        return None
    payload = text[idx + len(JSON_MARKER):].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def axiom_allowed(ax, allow_native_decide):
    if ax in ALLOWED_AXIOMS:
        return True
    if not allow_native_decide:
        return False
    return ax in LEGACY_NATIVE_TRUST_AXIOMS or bool(NATIVE_DECIDE_AXIOM_RE.search(ax))


def escalated_axioms(axioms, allow_native_decide):
    if not allow_native_decide:
        return []
    return [
        ax for ax in axioms
        if ax not in ALLOWED_AXIOMS
        and (ax in LEGACY_NATIVE_TRUST_AXIOMS or NATIVE_DECIDE_AXIOM_RE.search(ax))
    ]


def cmd_finalize(args):
    forbidden_hits = []
    declared_theorem_names = set()
    if args.forbidden and os.path.exists(args.forbidden):
        with open(args.forbidden) as f:
            forbidden_data = json.load(f)
        forbidden_hits = forbidden_data.get("forbidden_hits", [])
        declared_theorem_names = set(forbidden_data.get("declared_theorem_names", []))

    if args.build_failed:
        tail = args.build_log[-4000:]
        out = {
            "result": "build_failed",
            "theorems": [],
            "user_theorems": [],
            "forbidden_hits": forbidden_hits,
            "lean_version": args.lean_version,
            "build_error_tail": tail,
        }
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    sorry_warnings = set()
    for m in SORRY_WARNING_RE.finditer(args.build_log):
        sorry_warnings.add((m.group(1), int(m.group(2))))
    existing = {(h["file"], h["line"], h["pattern"]) for h in forbidden_hits}
    for file, line in sorry_warnings:
        key = (file, line, "sorry")
        if key not in existing:
            forbidden_hits.append({"file": file, "line": line, "pattern": "sorry"})
            existing.add(key)

    unaudited_modules = []
    built_modules = []
    if args.modules and os.path.exists(args.modules):
        with open(args.modules) as f:
            modules_data = json.load(f)
        unaudited_modules = modules_data.get("unaudited", [])
        built_modules = modules_data.get("built", [])

    if args.gen_checker_failed:
        tail = ""
        if args.gen_checker_stderr and os.path.exists(args.gen_checker_stderr):
            with open(args.gen_checker_stderr, errors="replace") as f:
                tail = f.read()[-4000:].strip()
        out = {
            "result": "error",
            "theorems": [],
            "user_theorems": [],
            "forbidden_hits": forbidden_hits,
            "lean_version": args.lean_version,
            "gen_checker_error_tail": tail,
        }
        if unaudited_modules:
            out["unaudited_modules"] = unaudited_modules
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    checker_stdout = ""
    if args.checker_stdout and os.path.exists(args.checker_stdout):
        with open(args.checker_stdout, errors="replace") as f:
            checker_stdout = f.read()
    checker_stderr = ""
    if args.checker_stderr and os.path.exists(args.checker_stderr):
        with open(args.checker_stderr, errors="replace") as f:
            checker_stderr = f.read()

    if args.checker_exit != 0:
        out = {
            "result": "error",
            "theorems": [],
            "user_theorems": [],
            "forbidden_hits": forbidden_hits,
            "lean_version": args.lean_version,
            "checker_exit_code": args.checker_exit,
            "checker_error_tail": (checker_stdout + "\n" + checker_stderr)[-4000:].strip(),
        }
        if unaudited_modules:
            out["unaudited_modules"] = unaudited_modules
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    parsed = parse_checker_output(checker_stdout)
    if parsed is None:
        out = {
            "result": "error",
            "theorems": [],
            "user_theorems": [],
            "forbidden_hits": forbidden_hits,
            "lean_version": args.lean_version,
            "checker_error_tail": (checker_stdout + "\n" + checker_stderr)[-4000:].strip(),
        }
        if unaudited_modules:
            out["unaudited_modules"] = unaudited_modules
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    theorems = []
    trust_escalations = []
    any_unproved = False
    for t in parsed.get("theorems", []):
        axioms = t.get("axioms", [])
        escalated = escalated_axioms(axioms, args.allow_native_decide)
        if escalated:
            trust_escalations.append({"theorem": t["name"], "axioms": escalated})
        proved = all(axiom_allowed(ax, args.allow_native_decide) for ax in axioms)
        status = "proved" if proved else "unproved"
        if status == "unproved":
            any_unproved = True
        theorems.append({
            "name": t.get("name"),
            "internal_name": t.get("internal_name"),
            "module": t.get("module"),
            "line": t.get("line"),
            "status": status,
            "axioms": axioms,
        })

    forbidden_blocking = False
    for hit in forbidden_hits:
        if hit["pattern"] in ("native_decide", "decide_native") and args.allow_native_decide:
            continue
        forbidden_blocking = True

    kernel_check = None
    if args.kernel_exit is not None:
        kernel_ok = args.kernel_exit == 0
        kernel_detail = None
        if not kernel_ok:
            kernel_stdout_text = ""
            if args.kernel_stdout and os.path.exists(args.kernel_stdout):
                with open(args.kernel_stdout, errors="replace") as f:
                    kernel_stdout_text = f.read()
            kernel_stderr_text = ""
            if args.kernel_stderr and os.path.exists(args.kernel_stderr):
                with open(args.kernel_stderr, errors="replace") as f:
                    kernel_stderr_text = f.read()
            kernel_detail = (kernel_stdout_text + "\n" + kernel_stderr_text)[-4000:].strip()
        kernel_check = {"ok": kernel_ok, "modules_checked": built_modules, "detail": kernel_detail}
    kernel_failed = kernel_check is not None and not kernel_check["ok"]

    if kernel_failed:
        failing_modules = set(re.findall(r"found a problem in (\S+)", kernel_check["detail"] or ""))
        for t in theorems:
            if t["status"] != "proved":
                continue
            if not failing_modules or t["module"] in failing_modules:
                t["status"] = "kernel_unverified"

    if unaudited_modules:
        result = "unaudited_modules"
    elif not theorems:
        result = "no_theorems"
    elif any_unproved or forbidden_blocking or kernel_failed:
        result = "unproved"
    else:
        result = "proved"

    user_theorems = [
        t["name"] for t in theorems
        if t["name"] and t["name"].rsplit(".", 1)[-1] in declared_theorem_names
    ]

    out = {
        "result": result,
        "theorems": theorems,
        "user_theorems": user_theorems,
        "forbidden_hits": forbidden_hits,
        "lean_version": args.lean_version,
    }
    if trust_escalations:
        out["trust_escalations"] = trust_escalations
    if unaudited_modules:
        out["unaudited_modules"] = unaudited_modules
    if kernel_check is not None:
        out["kernel_check"] = kernel_check
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="mode", required=True)

    p_forbidden = sub.add_parser("forbidden-scan")
    p_forbidden.add_argument("project_dir")
    p_forbidden.set_defaults(func=cmd_forbidden_scan)

    p_gen = sub.add_parser("gen-checker")
    p_gen.add_argument("project_dir")
    p_gen.set_defaults(func=cmd_gen_checker)

    p_modules = sub.add_parser("modules")
    p_modules.add_argument("project_dir")
    p_modules.set_defaults(func=cmd_modules)

    p_final = sub.add_parser("finalize")
    p_final.add_argument("--build-failed", action="store_true")
    p_final.add_argument("--build-log", required=True)
    p_final.add_argument("--forbidden", default=None)
    p_final.add_argument("--modules", default=None)
    p_final.add_argument("--gen-checker-failed", action="store_true")
    p_final.add_argument("--gen-checker-stderr", default=None)
    p_final.add_argument("--checker-stdout", default=None)
    p_final.add_argument("--checker-stderr", default=None)
    p_final.add_argument("--checker-exit", type=int, default=0)
    p_final.add_argument("--kernel-stdout", default=None)
    p_final.add_argument("--kernel-stderr", default=None)
    p_final.add_argument("--kernel-exit", type=int, default=None)
    p_final.add_argument("--lean-version", default=None)
    p_final.add_argument("--allow-native-decide", action="store_true")
    p_final.set_defaults(func=cmd_finalize)

    args = p.parse_args()
    if args.mode == "finalize":
        with open(args.build_log, errors="replace") as f:
            args.build_log = f.read()
    args.func(args)


if __name__ == "__main__":
    main()
