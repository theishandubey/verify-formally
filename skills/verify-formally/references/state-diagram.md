# State Diagrams

Every run writes `verification/state-diagram.html`: one self-contained page with a state diagram per modeled target, the counterexamples drawn on it step by step.
It is how a reader who does not read TLA+ sees what the system can do and where it goes wrong.
`scripts/render_diagram.py <repo>` writes the page from one `diagram.json` per target; never write or edit the page by hand, because `check_run.py` (C18) checks that the page was produced by the script from the current inputs.

---

## 1. TLA+ targets: derive the diagram from the checker

```json
{
  "kind": "tla",
  "spec": "Spec.tla",
  "cfg": "Spec.cfg",
  "project": ["phase", "lease"],
  "highlight": ["results/Spec.json"],
  "actions": {"LeaseBatch": "jobrunner/worker.py:98-104", "LogLeased": "jobrunner/worker.py:104"}
}
```

- `spec` and `cfg` are the buggy model and its primary cfg (relative to the target directory).
  The script drops the cfg's invariants and properties, runs TLC with `-deadlock` and `-dump dot`, and draws the whole reachable graph at the cfg's constants.
  The drawing is a picture of the model, not a checker result; it never counts toward a property.
- `project` lists the variables a node shows.
  The full graph of most models is too big to read, so pick the **control-state variables**: phase, status, `pc`, lifecycle flags, the one or two counters whose interplay is the bug.
  Leave out data, histories, and counters that only grow: they multiply nodes without adding shape.
  Each drawn node merges the states that agree on the projected variables, and shows how many it merges (`×12`); an edge is drawn when any merged state has that step.
  The script refuses more than `max_nodes` (default 40) drawn nodes; project fewer variables or shrink the constants, never raise the limit until the picture is unreadable.
- `highlight` lists `run_tlc.sh` result JSONs whose trace is drawn as numbered red edges.
  Highlight the counterexample of every violated property (C18 warns when a violating run is not highlighted); for a liveness violation the cycle that repeats forever is marked.
  A trace state outside the drawn graph (a trace from a different cfg) is drawn dashed.
- `actions` maps each action name to the code it stands for, copied from `CORRESPONDENCE.md`; the page lists it next to each transition.

A projection can merge states the property distinguishes, so a drawn edge is possible for some merged state, not for all of them.
Say so when you point a reader at the diagram, and keep the counterexample list under the drawing (the script writes it) as the precise record.

## 2. Targets without a TLA+ spec: a manual diagram

Code-level and Lean-only targets have no state graph to dump.
Draw the states the harness drives, by hand, each with the line it comes from:

```json
{
  "kind": "manual",
  "states": [
    {"id": "empty", "label": "empty (head == tail)", "code": "src/ring.c:14"},
    {"id": "partial", "label": "0 < count < CAP", "code": "src/ring.c:22-30"},
    {"id": "full", "label": "count == CAP", "code": "src/ring.c:31"}
  ],
  "initial": ["empty"],
  "transitions": [
    {"from": "empty", "to": "partial", "label": "push()", "code": "src/ring.c:22"},
    {"from": "partial", "to": "full", "label": "push()", "code": "src/ring.c:22"},
    {"from": "full", "to": "full", "label": "push() overwrites", "code": "src/ring.c:27"}
  ],
  "highlight": [{"name": "001 write past buf[CAP-1]", "path": ["empty", "partial", "full", "full"], "violated": "RingInBounds"}]
}
```

The page marks a manual diagram as hand-drawn, not checker-derived.
A target with a TLA+ property must use `"kind": "tla"` (C18 refuses a manual diagram for it).

## 3. When to render

Render after the last change to any spec, cfg, result JSON, or `diagram.json`, and before `check_run.py`:

```bash
<skill>/scripts/render_diagram.py <repo>
```

The page stamps the commit and a hash of every input; a later edit makes C18 report it stale.
Link the page from `verification/README.md`, and put `render_diagram.py` at the end of `verification/rerun.sh` so a rerun refreshes it.
When rendering fails for a target, the page still shows that target with the error, and C18 fails until it is fixed.
