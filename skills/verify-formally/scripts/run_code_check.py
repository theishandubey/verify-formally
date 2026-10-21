#!/usr/bin/env python3
# Usage: run_code_check.py --tool TOOL --out results/<stem>.json [--cwd DIR] [--timeout SECONDS]
#                          [--mutant PATCH] [--quiet] -- <verifier command...>
#
# Runs one code-level verifier (a checker that runs on the real implementation, not on a
# hand-written model) and writes a JSON result plus the raw log next to it
# (results/<stem>.json and results/<stem>.log). TOOL is one of:
#
#   cbmc, jbmc   CBMC / JBMC bounded model checking of C, C++ or Java bytecode
#   esbmc        ESBMC bounded model checking of C/C++
#   kani         Kani bounded model checking of Rust (`cargo kani ...` or `kani file.rs`)
#   crosshair    CrossHair symbolic execution of Python contracts (`crosshair check ...`)
#   hypothesis   property-based tests run by pytest (generated inputs; never exhaustive)
#
# Exit codes:
#   0  result=pass
#   1  result=violation, incomplete, vacuous
#   2  result=error, timeout; also usage errors (before any result exists)
# Read the JSON "result" field for the specific reason; the field contract is at the top of
# code_report.py. The JSON also carries "kind" (bounded_model_check, symbolic_execution,
# generated_inputs), "exhaustive", "tool_version", "argv", "cwd", and "command", a
# copy-pasteable re-run command line (this script's absolute path, an explicit --cwd, and the
# original arguments, shell-quoted).
#
# --cwd is the directory the verifier runs in (default: the current directory). It must be
# inside the git repository that holds it; that repository is the one --mutant copies.
#
# --mutant PATCH runs the verifier against a scratch copy of the repository with PATCH applied
# (a unified diff, `git apply` format, paths relative to the repository root). The copy holds
# the tracked and untracked-but-not-ignored files; .venv, venv and node_modules are symlinked
# in. The patch may touch only files outside verification/ and plans/ (it mutates the real
# code, never the harness), and it must apply cleanly. Every occurrence of the repository's
# absolute path in the command is rewritten to the copy's path. The real repository is never
# written. The JSON "mutant" field records the patch path, its sha256, and the files it changes.
#
# Flags that switch checks off, change the output format the parser reads, or make a run
# non-exhaustive are rejected per tool (for example CBMC/ESBMC/Kani --no-* flags, CBMC
# --partial-loops/--depth, Kani --only-codegen, pytest --collect-only/--last-failed). CrossHair
# runs must pass --report_all so each condition reports whether it was confirmed; ESBMC runs
# that pass --unwind/--unwindset must also pass --unwinding-assertions.
import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_report  # noqa: E402

SELF = os.path.abspath(__file__)
LINKED_DIRS = (".venv", "venv", "node_modules")
PROTECTED_DIRS = ("verification", "plans", "verification-plans")


def usage_error(msg):
    print("run_code_check.py: %s" % msg, file=sys.stderr)
    sys.exit(2)


def _flag_name(arg):
    return arg.split("=", 1)[0]


def _base(arg):
    return os.path.basename(arg)


def validate_command(tool, argv):
    """Returns an error string, or None when argv is acceptable for tool."""
    if not argv:
        return "no verifier command after --"
    flags = [a for a in argv if a.startswith("-")]
    names = [_flag_name(a) for a in flags]
    if tool in ("cbmc", "jbmc"):
        if _base(argv[0]) != tool:
            return "--tool %s needs a command that starts with %s, got %r" % (tool, tool, argv[0])
        for n in names:
            if n.startswith("--no-") or n.startswith("--show-") or n in (
                    "--partial-loops", "--depth", "--program-only", "--cover", "--outfile",
                    "--json-ui", "--xml-ui", "--verbosity", "--dimacs", "--smt2-outfile"):
                return "flag %s switches checks off or changes the output this script reads" % n
    elif tool == "esbmc":
        if _base(argv[0]) != "esbmc":
            return "--tool esbmc needs a command that starts with esbmc, got %r" % argv[0]
        for n in names:
            if n.startswith("--no-") or n.startswith("--show-") or n in (
                    "--partial-loops", "--depth", "--quiet", "--result-only", "--output",
                    "--witness-output"):
                return "flag %s switches checks off or changes the output this script reads" % n
        if ({"--unwind", "--unwindset"} & set(names)) and "--unwinding-assertions" not in names:
            return ("esbmc with --unwind/--unwindset must also pass --unwinding-assertions, "
                    "or a too-small bound reads as a pass")
    elif tool == "kani":
        if not (_base(argv[0]) == "kani" or (_base(argv[0]) == "cargo" and len(argv) > 1
                                             and argv[1] == "kani")):
            return "--tool kani needs `cargo kani ...` or `kani <file>`, got %r" % " ".join(
                argv[:2])
        for i, a in enumerate(argv):
            n = _flag_name(a)
            if n.startswith("--no-") or n in ("--only-codegen", "--output-format", "--quiet",
                                               "-q", "--dry-run"):
                return "flag %s switches checks off or changes the output this script reads" % n
            if n == "--concrete-playback":
                value = a.split("=", 1)[1] if "=" in a else (
                    argv[i + 1] if i + 1 < len(argv) else "")
                if value != "print":
                    return ("--concrete-playback must be 'print'; 'inplace' writes into the "
                            "source tree")
    elif tool == "crosshair":
        words = [_base(a) for a in argv]
        if "crosshair" not in words:
            return "--tool crosshair needs a `crosshair check ...` command"
        i = words.index("crosshair")
        if i + 1 >= len(argv) or argv[i + 1] != "check":
            return "only `crosshair check` is supported (got %r)" % " ".join(argv[i:i + 2])
        if "--report_all" not in names:
            return ("crosshair check must pass --report_all, so each condition reports "
                    "whether it was confirmed over all paths")
    elif tool == "hypothesis":
        words = [_base(a) for a in argv]
        is_pytest = words[0] in ("pytest", "py.test") or any(
            argv[i] == "-m" and i + 1 < len(argv) and argv[i + 1] == "pytest"
            for i in range(len(argv)))
        if not is_pytest:
            return "--tool hypothesis needs a pytest command (pytest ... or python -m pytest ...)"
        for i, a in enumerate(argv):
            n = _flag_name(a)
            if n in ("--co", "--collect-only", "--lf", "--last-failed", "--ff",
                     "--failed-first", "--sw", "--stepwise", "--sw-skip", "--stepwise-skip",
                     "--runxfail", "--junitxml", "--junit-xml", "-qq", "-qqq", "--pdb"):
                return "flag %s changes which tests run or what this script can read" % n
            plugin = argv[i + 1] if a == "-p" and i + 1 < len(argv) else (
                a[2:] if a.startswith("-pno:") else "")
            if plugin.startswith("no:") and "hypothesis" in plugin:
                return "-p %s disables Hypothesis's pytest plugin" % plugin
            if a.startswith("-r") and "N" in a[2:]:
                return "-rN hides the failure summary this script reads"
    else:
        return "unknown --tool %r (expected one of %s)" % (tool, ", ".join(code_report.TOOLS))
    return None


def tool_version(tool, argv, cwd, env):
    probes = {
        "cbmc": [argv[0], "--version"],
        "jbmc": [argv[0], "--version"],
        "esbmc": [argv[0], "--version"],
        "kani": (["cargo", "kani", "--version"] if _base(argv[0]) == "cargo"
                 else [argv[0], "--version"]),
    }
    if tool == "crosshair":
        probe = None
        if _base(argv[0]) == "crosshair":
            launcher = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
            try:
                with open(launcher) as fh:
                    first = fh.readline()
                if first.startswith("#!"):
                    probe = [first[2:].strip().split()[0]]
            except OSError:
                probe = None
        else:
            probe = [argv[0]]
        if probe:
            probe += ["-c", "from importlib.metadata import version; "
                            "print('crosshair-tool ' + version('crosshair-tool'))"]
    elif tool == "hypothesis":
        py = argv[0] if _base(argv[0]).startswith("python") else None
        if py is None and _base(argv[0]) in ("pytest", "py.test"):
            launcher = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
            try:
                with open(launcher) as fh:
                    first = fh.readline()
                py = first[2:].strip().split()[0] if first.startswith("#!") else None
            except OSError:
                py = None
        probe = [py, "-c", "from importlib.metadata import version as v; "
                           "print('pytest %s hypothesis %s' % (v('pytest'), v('hypothesis')))"
                 ] if py else None
    else:
        probe = probes[tool]
    if not probe:
        return None
    try:
        p = subprocess.run(probe, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (p.stdout or p.stderr or "").strip().splitlines()
    return text[0].strip() if p.returncode == 0 and text else None


def git_toplevel(path):
    try:
        p = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True)
    except OSError:
        return None
    return os.path.realpath(p.stdout.strip()) if p.returncode == 0 else None


def patch_files(patch_text):
    files = []
    for line in patch_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            path = line[4:].split("\t")[0].strip()
            if path == "/dev/null":
                continue
            if path.startswith("a/") or path.startswith("b/"):
                path = path[2:]
            if path not in files:
                files.append(path)
    return files


def make_mutant_copy(repo, patch_path):
    try:
        with open(patch_path) as fh:
            patch_text = fh.read()
    except OSError as e:
        return None, None, "cannot read --mutant patch: %s" % e
    files = patch_files(patch_text)
    if not files:
        return None, None, "--mutant patch changes no files"
    for f in files:
        norm = os.path.normpath(f)
        if norm.startswith("..") or os.path.isabs(norm):
            return None, None, "--mutant patch path %r leaves the repository" % f
        if norm.split(os.sep)[0] in PROTECTED_DIRS:
            return None, None, ("--mutant patch changes %s; a mutant must change the real code, "
                                "not the verification harness" % f)
    listing = subprocess.run(
        ["git", "-C", repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True)
    if listing.returncode != 0:
        return None, None, "git ls-files failed in %s" % repo
    copy = tempfile.mkdtemp(prefix="run_code_check_mutant.")
    for rel in listing.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not rel:
            continue
        src = os.path.join(repo, rel)
        if not os.path.lexists(src) or os.path.isdir(src) and not os.path.islink(src):
            continue
        dst = os.path.join(copy, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)
    for d in LINKED_DIRS:
        if os.path.isdir(os.path.join(repo, d)) and not os.path.lexists(os.path.join(copy, d)):
            os.symlink(os.path.join(repo, d), os.path.join(copy, d))
    p = subprocess.run(["git", "apply", "--whitespace=nowarn", os.path.abspath(patch_path)],
                       cwd=copy, capture_output=True, text=True)
    if p.returncode != 0:
        shutil.rmtree(copy, ignore_errors=True)
        return None, None, "--mutant patch does not apply cleanly: %s" % (
            p.stderr.strip() or p.stdout.strip())
    info = {
        "patch": os.path.relpath(os.path.realpath(patch_path), repo),
        "sha256": hashlib.sha256(patch_text.encode("utf-8")).hexdigest(),
        "files": files,
    }
    return copy, info, None


def run(argv, cwd, env, timeout, log_path, header):
    start = time.time()
    timed_out = False
    with open(log_path, "w") as log:
        log.write(header + "\n")
        log.flush()
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            log.write("run_code_check.py: cannot start %s: %s\n" % (argv[0], e))
            return 127, False, time.time() - start
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
            code = proc.returncode
    return code, timed_out, time.time() - start


def main():
    if "--" not in sys.argv[1:]:
        if any(a in ("-h", "--help") for a in sys.argv[1:]):
            print(open(SELF).read().split("\nimport ")[0])
            return 0
        usage_error("missing `-- <verifier command...>`")
    split = sys.argv.index("--", 1)
    own, command = sys.argv[1:split], sys.argv[split + 1:]
    p = argparse.ArgumentParser(prog="run_code_check.py", add_help=False)
    p.add_argument("--tool", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--cwd", default=".")
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--mutant", default=None)
    p.add_argument("--quiet", action="store_true")
    try:
        args = p.parse_args(own)
    except SystemExit:
        usage_error("bad arguments before --")
    if args.tool not in code_report.TOOLS:
        usage_error("unknown --tool %r (expected one of %s)" % (args.tool,
                                                                ", ".join(code_report.TOOLS)))
    problem = validate_command(args.tool, command)
    if problem:
        usage_error(problem)
    if args.timeout is not None and args.timeout <= 0:
        usage_error("--timeout must be a positive number of seconds")
    cwd = os.path.realpath(args.cwd)
    if not os.path.isdir(cwd):
        usage_error("--cwd is not a directory: %s" % args.cwd)
    repo = git_toplevel(cwd)

    out = os.path.abspath(args.out)
    out_dir = os.path.dirname(out)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.basename(out)
    stem = stem[:-5] if stem.endswith(".json") else stem
    log_path = os.path.join(out_dir, stem + ".log")
    for stale in (out, log_path):
        if os.path.exists(stale):
            os.remove(stale)

    env = dict(os.environ)
    if args.tool in ("crosshair", "hypothesis"):
        # Importing the code under verification must not leave __pycache__ in the source tree.
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    mutant_info = None
    copy = None
    run_cwd, run_argv = cwd, list(command)
    if args.mutant:
        if repo is None:
            usage_error("--mutant needs --cwd inside a git repository")
        copy, mutant_info, err = make_mutant_copy(repo, args.mutant)
        if err:
            usage_error(err)
        run_cwd = os.path.join(copy, os.path.relpath(cwd, repo))
        run_argv = [a.replace(repo, copy) for a in command]

    header = "%s tool=%s" % (code_report.HEADER_PREFIX, args.tool)
    version = tool_version(args.tool, run_argv, run_cwd, env)
    try:
        exit_code, timed_out, elapsed = run(run_argv, run_cwd, env, args.timeout, log_path,
                                            header)
    finally:
        if copy:
            shutil.rmtree(copy, ignore_errors=True)

    with open(log_path) as fh:
        text = fh.read()
    rec = code_report.parse(args.tool, text, exit_code, timed_out)
    if exit_code == 127 and rec["result"] == "error":
        rec["error_detail"] = "the verifier command could not be started"

    command_parts = [SELF]
    skip = False
    for i, a in enumerate(own):
        if skip:
            skip = False
            continue
        if a == "--cwd":
            skip = True
            continue
        if a.startswith("--cwd="):
            continue
        if a in ("--out", "--mutant") and i + 1 < len(own):
            command_parts += [a, os.path.abspath(own[i + 1])]
            skip = True
            continue
        command_parts.append(a)
    command_parts += ["--cwd", cwd, "--"] + command

    def rel(path):
        real = os.path.realpath(path)
        if repo and (real == repo or real.startswith(repo + os.sep)):
            return os.path.relpath(real, repo)
        return path

    rec.update({
        "generator": "run_code_check.py",
        "tool": args.tool,
        "tool_version": version,
        "argv": command,
        "cwd": rel(cwd) if repo else cwd,
        "mutant": mutant_info,
        "exit_code": exit_code,
        "elapsed_seconds": round(elapsed, 3),
        "raw_log": rel(log_path),
        "command": " ".join(shlex.quote(c) for c in command_parts),
    })
    with open(out, "w") as fh:
        json.dump(rec, fh, indent=2)
        fh.write("\n")
    if args.quiet:
        violated = ",".join(rec["violated"]) or "-"
        print("%s %s checks=%s exhaustive=%s -> %s" % (
            rec["result"], violated, rec["checks_total"] if rec["checks_total"] is not None
            else "-", str(rec["exhaustive"]).lower(), args.out))
    else:
        print(json.dumps(rec, indent=2))
    if rec["result"] == "pass":
        return 0
    if rec["result"] in ("violation", "incomplete", "vacuous"):
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
