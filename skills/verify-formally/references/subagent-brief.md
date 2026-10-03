# Subagent Brief

How to delegate Phases C to E for one target, and what comes back.
Subagents do not inherit this skill's context, and their replies can be lost (a usage limit, a host that cannot route messages back), so the brief must be complete and the result must land in a file.

---

## 1. When to delegate

- Model a target yourself when it is small (one control loop, a few program points) or when it is the only target.
  A subagent for a small target costs as much as doing it, plus a brief and a vetting pass.
- Delegate when two or more selected targets each need substantial modeling, one subagent per target, run in parallel.
- Use your most capable available model (the same model as this session) with a general-purpose agent type.
  Fidelity mistakes (idealized fix flags, ground truth assigned in lockstep, patched methods under test) are where cheaper models fail, and each one costs a vetting round.
- If spawning a named subagent fails, spawn it unnamed.

## 2. Ownership

Parallel subagents must not collide:

| Path | Owner |
|---|---|
| `verification/models/<slug>/` | the subagent for `<slug>` |
| `verification/repro/repro_<slug_with_underscores>.py` (and a `verification/repro/conftest.py` only if you assign it) | the subagent for `<slug>` |
| `verification/lean/` | exactly one owner: you, or the one subagent whose target needs Lean (name it in the brief) |
| `verification/README.md`, `findings.json`, `findings/`, `rerun.sh`, `plans/` | you |

## 3. Brief template

Fill every placeholder; delete nothing.
Long parts (the hard rules, the recon facts) can be written once to `verification/.brief-common.md` and referenced by absolute path, since subagents can read files.

```markdown
You are modeling one target for the verify-formally skill (formal verification for bug finding).
Work only on this target; another agent vets your results.

## Read first (absolute paths)
- <skill>/references/tla-playbook.md (or lean-playbook.md)
- <skill>/references/correspondence.md
- <skill>/references/pitfalls.md
- <skill>/references/reproduction.md
- <skill>/references/finding-format.md, section 5 (RESULT.md format)
- Worked example: <skill>/examples/retry-guard-reset/README.md
- Scripts: <skill>/scripts/run_tlc.sh, <skill>/scripts/lean_audit.sh (use --out ... --quiet)

## Repository
- Path: <abs repo path>, commit <SHA> (<clean | dirty: details>)
- Test command: <cmd>; single file: <cmd> <file>; collect-only: <cmd>
- Environment: <activation, env vars>
- Test doubles and fixtures to reuse: <paths and names>
- Do not run the full suite; use single files and collect-only.
- Resource limits: <e.g. pytest -n 4 at most, run_tlc.sh --workers 2>

## Target <slug>
- Files and line ranges: <list>
- Property (plain words) and its source: <property>; source: <doc/test/comment/commit/inferred with location>
- Suspected defect, if any: <one line>
- Out of scope (another target covers it): <list>
- Environment behaviors the model must include: <model replies, user inputs, interrupts at every interference point, errors>

## Ownership
You may write only: verification/models/<slug>/ and verification/repro/repro_<slug_with_underscores>.py<, and verification/lean/ if assigned>.
Everything else under verification/ and plans/ belongs to the lead; do not touch it.

## Rules (verbatim)
<Hard rules 1 to 8 from SKILL.md, with rule 1 narrowed by the Ownership section above>

## Required
- CORRESPONDENCE.md for the model.
- Fix flags model the exact code change you would propose, including new interference points it creates; if several fixes are plausible, one flag each.
- Vacuity per property at the reported bounds: mutant, sanity invariants one per cfg under sanity/, and for fixed models a mutant that breaks the property by a different mechanism than re-enabling the bug.
- Keep every cfg that produced a result; results under results/<cfg-stem>.json.
- Repro tests for every counterexample and at least one guard test, run by explicit file path.
- Checkpoint as you go: write CORRESPONDENCE.md and the spec early, and append each checker result to RESULT.md as it lands, so a run cut off by a usage limit can be resumed from files.
- Before replying, finish verification/models/<slug>/RESULT.md in the return format, then reply with the same content.
```

## 4. What comes back

`RESULT.md` in the format of finding-format.md section 5.
When a reply does not arrive, read `RESULT.md`; when neither exists, reconstruct from `results/*.json` (each carries its `command`, `spec`, `cfg`, and `constants`) and re-run what you cannot account for.

## 5. Vetting a subagent's work

Beyond the Phase F checklist, the recurring subagent errors are:

- a fix flag that models an idealized fix instead of the code change (the fixed model passes, the real fix would not);
- a truthfulness property whose ground truth is assigned by the same step as the value it checks (vacuous by construction);
- a missing interference point between an append and the next step (a window the real code has);
- a repro that patches the method under test instead of an environment seam;
- sanity invariants checked in the same cfg as real ones, or at different constants than the property;
- stale results after a spec edit;
- far more cfgs and result files than questions asked; ask for one cfg per question.
