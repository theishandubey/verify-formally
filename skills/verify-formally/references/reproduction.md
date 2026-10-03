# Reproduction: From Trace to Failing Test

A counterexample becomes a finding only when a test drives the real, unmodified code along the trace and fails for the reason the trace predicts.
This file covers how to write that test and how to classify the outcome.

---

## 1. Read the trace as a script

For each state transition in the trace:
1. Name the action that fired (the `action` field) and look it up in `CORRESPONDENCE.md` to get the code location.
2. Diff the state against the previous one to see what changed.
3. Identify the **environment choice** made at that step (which model reply, which user input, which exception, which interrupt, which thread ran).

The environment choices, in order, are the test's inputs.
The program's own steps are what the real code will do on its own when given those inputs.
If a program step in the trace is something the real code would not do, stop: the model is wrong (MODEL-ONLY), fix it and re-check before writing a test.

## 2. Realize environment choices through seams

Drive choices only through the seams the code already exposes, never by patching the logic under test:

| Choice | Typical seam |
|---|---|
| model/LLM reply | the project's deterministic fake model (look in `tests/`, `conftest.py`, or a fakes/fixtures module) |
| user input | the prompt function the tests already mock (patch the module attribute the code calls) |
| command outcome | a fake environment object, or a real command whose output is deterministic |
| exception at a point | a fake dependency that raises on the Nth call |
| interrupt (Ctrl-C, cancel) at a point | patch an environment callable that runs at that program point (a logger, a progress display's `__exit__`, a sleep) to raise once; never a method of the class under test |
| interrupt during a blocking system call | deliver a real signal: `signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)` from a timer thread (or `os.kill(os.getpid(), signal.SIGINT)`); `_thread.interrupt_main()` does not interrupt a blocking `select()` or `read()`, and a background `&` process in a non-interactive shell ignores SIGINT |
| time | patch the clock function the code reads, or set the limit to 0/1 |
| thread interleaving | barriers or events inside fakes to force the order; never rely on sleep-based timing |
| subprocess behavior | a real short shell command (`sleep`, `echo`, a small script) when the bug is about process handling |

Rules:
- **Reuse the project's test doubles and mocking style.**
  A test that looks like the project's own tests is credible to maintainers and is easy to move into the suite later.
  If the project's test helpers are not importable from `verification/repro/` (package layout), copy the minimal helper and note where it came from.
  Fixtures from the project's `tests/conftest.py` are not available there either; reset shared state explicitly in the test (or in a `verification/repro/conftest.py`), and restore anything you change (use `monkeypatch`, not assignment).
- **The project's test conventions still apply to the moved test.**
  If the project forbids mocking in its tests (check `AGENTS.md`/`CONTRIBUTING`), the repro may still patch environment seams because it has to force a trace, but say so in the finding, and have the plan's "move into the suite" step adapt the test to the project's rules or keep it in a clearly separate module.
- **One-shot injections.**
  Raise the interrupt or error on exactly one call (count calls in the fake), or later steps re-trigger it and the failure becomes a different bug.
- **Bound runaway behavior.**
  For non-termination or runaway retry, the fake raises a distinct error after a safety cap (for example 100 calls), so the test fails fast with a message naming the property instead of hanging.
  For hangs, use a subprocess or thread with a timeout and assert on the timeout, never let the test hang the runner.
- **Assert the property, not a symptom.**
  The assertion states the violated property on observable state, and its message names it: `assert_leases_settled(queue)` with "LeasesSettled violated: job 'j1' is still leased after the worker exited".
  Name the property only in the failing assertion's message.
  Never print or log it anywhere else in the test.
  Never wrap the scenario in a catch-all handler that turns arbitrary errors into a property-named assertion failure (`except Exception: assert False, "<Property> violated"`); a typo or crash would then look like a reproduced bug.
  `check_run.py` cannot tell these apart for non-pytest runners.

## 3. File naming and commands

The failing test must not break the project's normal test run.
- pytest: `verification/repro/repro_<slug_with_underscores>.py` (default discovery matches only `test_*.py` and `*_test.py`).
  Run with `<test command> verification/repro/repro_<slug_with_underscores>.py`; an explicit file path is collected regardless of name.
  A directory argument (`pytest verification/repro/`) collects nothing and reports "no tests ran"; to run all repros at once use `<test command> verification/repro/repro_*.py` or `<test command> -o python_files='repro_*.py' verification/repro`.
- If the project customizes discovery (`python_files` in `pytest.ini`/`pyproject.toml`/`setup.cfg`), pick a name outside its patterns.
- vitest/jest: default discovery matches `*.test.*` and `*.spec.*`; name the file `verification/repro/<slug>.repro.ts` and run it with a dedicated config (`verification/repro/vitest.config.ts` that sets `root` to the repo root and `include: ['verification/repro/**/*.repro.ts']`, run `npx vitest run --config verification/repro/vitest.config.ts`), or the jest equivalent (`--testMatch`).
  If the project's main config is needed for path aliases or setup files, have the repro config import and extend it (`mergeConfig`) and override only `include`.
  Record the runner as the placeholder form `npx vitest run --config verification/repro/vitest.config.ts --reporter=verbose {file} -t {test}`.
- Go: put the test in a `_test.go` file under a build tag (`//go:build verify_repro`).
  Record the runner as the placeholder form `go test -v -tags verify_repro ./{dir} -run '^{test}$'`.
  Always anchor Go's `-run` as `'^{test}$'`: unanchored, the guard id `TestGuard` would also match `TestGuardLeasesReleased`, and `check_run.py` rejects such a guard.
- Other runners: find the discovery rule, choose a name outside it, and document the exact command as a `baseline.repro_runner` template with `{file}`, `{test}` and/or `{dir}` placeholders (see `finding-format.md`).
- For non-pytest runners, the runner output must name each test it runs (`go test -v`, `vitest --reporter=verbose`), and a repro's failure output must name the violated property; `check_run.py` rejects a repro whose output shows only the test name, and a guard whose output never shows it ran or shows it skipped.

Confirm both directions every time:
1. The repro command runs the test and it fails.
2. The project's normal test command (or its collect/list mode: `pytest --collect-only -q`, `vitest list`) does not include the repro file.

Record the exact repro command in the finding.
Prefix Python repro commands with `PYTHONDONTWRITEBYTECODE=1` so no `__pycache__/` appears under `verification/repro/` (`-p no:cacheprovider` only stops pytest's own cache).

When a full-suite run fails in tests unrelated to your files (a container out of disk space, a network timeout), re-run just those tests; if they pass alone or fail identically at the baseline, record them as environmental and move on.

## 4. Guard tests

For each target, write at least one test that follows a **non-violating** trace through the same harness and passes.
It shows that the harness itself can pass and that the failing test is about the code.
Good guards: the same scenario with the environment choice that avoids the bug (a clean shutdown instead of a signal; an interrupt at a different point).
Guard tests live in the same repro file and are expected to pass before and after the fix.

## 5. Classification

- **CONFIRMED**: the repro test fails on the current code, the failure message shows the property violation the trace predicts, and the guard test passes.
  Re-run once more to rule out flakiness; a race repro must fail deterministically (forced ordering), not sometimes.
  The property's source is documented intent, or the consequence is wrong under any reasonable intent (crash, hang, data loss, an external API rejecting the next request, a limit the docs promise being exceeded).
- **NEEDS-DECISION**: the repro fails as predicted, but the property is `inferred` or the documents disagree, and a maintainer could reasonably call the behavior intended.
  Record both readings with their evidence (the docstring says "consecutive failures", the commit says "any retry resets the counter") and the question to ask.
  Two careful auditors should reach the same classification; if you can argue either way, it belongs here.
- **MODEL-ONLY**: the real code does not follow the trace, or no test seam can drive the real code along it (an interrupt inside a third-party network call); say which, and never count it as a bug.
  Diagnose which step diverges, then either fix the model (the abstraction admitted a behavior the code cannot have) and re-check, or record the gap.
  Common causes: an environment choice the real environment cannot make, a guard paraphrased wrongly, an action boundary that is not a real interference point.
- **BY-DESIGN**: the code does follow the trace, but the behavior is intended.
  Evidence required: a doc sentence, a code comment stating the intent, a test that asserts this behavior, or a maintainer statement.
  "It seems reasonable" is not evidence; without evidence it is NEEDS-DECISION (if intent is genuinely open) or CONFIRMED (if the consequence is wrong under any intent).
- **OUT-OF-SCOPE**: the violation requires environment behavior the property explicitly assumes away (a user who never answers a prompt, a provider that never replies).
  Record the assumption; if the assumption itself is questionable, say so.

## 6. Severity

Rate impact first, then adjust for how likely the trigger is:

| Impact | Common trigger (default config, normal use) | Uncommon trigger (user interrupt, error recovery, non-default config) | Needs precise timing or several unusual conditions |
|---|---|---|---|
| Data loss, unbounded cost or hang with no other bound, security boundary broken, external API rejects the next request | high | high | medium |
| Wrong result, wrong or misleading record the program itself acts on, a documented limit exceeded while another bound still applies, a hang the user can end | medium | medium | low |
| Record-keeping inaccuracy nobody acts on, cosmetic output | low | low | low |

State the realistic trigger in one sentence next to the severity ("operator sends SIGTERM while a batch is being logged"), and name the other bound when one limits the damage ("runs until the daily spend cap, $10 by default").

## 7. Prototyping a fix without touching the repo

A plan's post-fix expectations are much stronger when observed than when predicted.
Prototype every plan's fix whenever the host permits it; a prototype catches wrong fixes, wrong test oracles, and missed fixtures that the model cannot.
The suite runs inside the copy do not count toward the "full suite at most twice" rule, which protects the repo's own environment.
If a combined command is denied, try the steps separately:

```
tmp=$(mktemp -d)
git -C <repo> archive HEAD | tar -x -C "$tmp"
cp -R <repo>/verification "$tmp"/
# apply the planned change inside "$tmp", then run the repro and the suite there
```

- Make sure the copy's code is what gets imported: an editable install of the project points at the repo, so set `PYTHONPATH="$tmp/src"` (or the package root) and check with `python -c 'import pkg; print(pkg.__file__)'`.
- Never write into the repo, never `git worktree add` (it writes into the repo's `.git`), and delete the copy afterwards.
- If the host denies it, say in the plan that post-fix results are predicted by the fixed model, not observed.
- Try the most obvious fix first: when it fails (it opens a new window, the repro errors instead of passing), that is exactly what the plan's STOP conditions and steps must prevent.
