#!/usr/bin/env python3
# Usage: render_diagram.py <repo-root> [--out verification/state-diagram.html] [--timeout SECONDS]
#                          [--text]
#
# Writes one self-contained HTML page with a state diagram for every target that has a
# verification/models/<id>/diagram.json (targets without one are left out; check_run.py C18
# requires one for every modeled target). Exit codes: 0 written, 1 a diagram could not be
# built (the page is still written, with the error in that target's section), 2 usage.
#
# diagram.json, kind "tla" (the diagram is derived from the checker's own state graph):
#   {"kind": "tla", "spec": "Spec.tla", "cfg": "Spec.cfg",
#    "project": ["phase", "attempts"],          optional: variables to keep (default: all)
#    "max_nodes": 40,                           optional: refuse to draw more nodes than this
#    "highlight": ["results/Spec.json"],        optional: run_tlc.sh results whose traces are
#                                               drawn on the diagram as numbered red paths
#    "actions": {"Retry": "retry.py:31"}}       optional: action -> code location
# The cfg's INVARIANT/PROPERTY/CHECK_DEADLOCK/POSTCONDITION/ALIAS sections are dropped and TLC
# runs with -deadlock, so the drawing is the whole reachable graph at the cfg's constants (its
# CONSTRAINT, ACTION_CONSTRAINT, SYMMETRY and VIEW are kept). Each node is one value of the
# projected variables, and the number on it counts the full states it merges. The run is a
# drawing aid only: it never counts as a checker result.
#
# diagram.json, kind "manual" (hand-drawn from the code; used for code-level targets and any
# target without a TLA+ spec; the page labels it as not derived by a checker):
#   {"kind": "manual",
#    "states": [{"id": "idle", "label": "idle", "code": "pool.c:40"}, ...],
#    "initial": ["idle"],
#    "transitions": [{"from": "idle", "to": "busy", "label": "acquire()",
#                     "code": "pool.c:52"}, ...],
#    "highlight": [{"name": "001 double release", "path": ["idle", "busy", "idle"],
#                   "violated": "NoDoubleFree"}]}
# Every state and transition of a manual diagram cites a file:line.
#
# --text also prints every diagram as plain text (numbered states, transitions, highlighted
# paths), for reviewing the drawing where no browser is available.
#
# The page embeds <script type="application/json" id="vf-diagram-meta"> with the commit, the
# generator, and for each target the sha256 of every input file; check_run.py uses it to tell
# a current page from a stale one.
import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tlc_report  # noqa: E402

GENERATOR = "render_diagram.py"
META_VERSION = 1
DEFAULT_MAX_NODES = 40
DROP_SECTIONS = {"INVARIANT", "INVARIANTS", "PROPERTY", "PROPERTIES"}
DROP_SINGLE = {"CHECK_DEADLOCK", "POSTCONDITION", "ALIAS"}
NODE_RE = re.compile(r'^(-?\d+) \[label="((?:[^"\\]|\\.)*)"(.*)\];?$')
EDGE_RE = re.compile(r'^(-?\d+) -> (-?\d+)(?: \[label="((?:[^"\\]|\\.)*)"[^\]]*\])?;?$')
ACTION_NAME_RE = re.compile(r"^<\s*([A-Za-z_][\w!]*)")
FILE_LINE_RE = re.compile(r"[\w./-]+\.\w+:\d+")
LABEL_VALUE_MAX = 36


class DiagramError(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve(cmd):
    here = os.path.dirname(os.path.abspath(__file__))
    p = subprocess.run(["bash", "-c", ". '%s/lib.sh' && %s" % (here, cmd)],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def diagram_cfg_text(cfg_path):
    with open(cfg_path) as fh:
        tokens = tlc_report.tokenize_cfg(tlc_report.strip_cfg_comments(fh.read()))
    kept = []
    section = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in DROP_SINGLE:
            i += 2 if i + 1 < len(tokens) and tokens[i + 1] not in tlc_report.ALL_KEYWORDS \
                else 1
            section = None
            continue
        if tok in tlc_report.ALL_KEYWORDS:
            section = tok
            if tok not in DROP_SECTIONS:
                kept.append("\n" + tok)
            i += 1
            continue
        if section not in DROP_SECTIONS:
            kept.append(tok)
        i += 1
    return " ".join(kept).strip() + "\n"


def unescape_dot(s):
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            out.append("\n" if nxt in "nlr" else nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse_state_label(label):
    lines = label.split("\n")
    state = {}
    current = None
    for line in lines:
        m = re.match(r"^/\\ ([A-Za-z_]\w*) = (.*)$", line)
        if m:
            current = m.group(1)
            state[current] = m.group(2)
        elif current is not None:
            state[current] += " " + line.strip()
        else:
            m = re.match(r"^([A-Za-z_]\w*) = (.*)$", line)
            if m:
                current = m.group(1)
                state[current] = m.group(2)
    return {k: " ".join(v.split()) for k, v in state.items()}


def parse_dot(text):
    nodes = {}
    initial = set()
    edges = []
    for raw in text.split("\n"):
        line = raw.strip()
        m = EDGE_RE.match(line)
        if m:
            edges.append((m.group(1), m.group(2), unescape_dot(m.group(3) or "")))
            continue
        m = NODE_RE.match(line)
        if m:
            nodes[m.group(1)] = parse_state_label(unescape_dot(m.group(2)))
            if "filled" in m.group(3):
                initial.add(m.group(1))
    return nodes, initial, edges


def run_tlc_dump(spec_path, cfg_path, timeout):
    java = resolve("resolve_java")
    jar = resolve("resolve_tla_jar")
    if not java or not jar:
        raise DiagramError("no Java 17+ or tla2tools jar found (run check_toolchain.sh)")
    work = tempfile.mkdtemp(prefix="render_diagram.")
    try:
        cfg = os.path.join(work, "diagram.cfg")
        with open(cfg, "w") as fh:
            fh.write(diagram_cfg_text(cfg_path))
        dump = os.path.join(work, "graph")
        cmd = [java, "-XX:+UseParallelGC", "-cp", jar, "tlc2.TLC", "-workers", "1",
               "-deadlock", "-cleanup", "-metadir",
               os.path.join(work, "meta"), "-config", cfg, "-dump", "dot,actionlabels", dump,
               os.path.basename(spec_path)]
        try:
            p = subprocess.run(cmd, cwd=os.path.dirname(spec_path), capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DiagramError("TLC did not finish the state-graph dump within %ss; shrink "
                               "the constants in the diagram cfg" % timeout)
        dot_path = dump + ".dot"
        if p.returncode != 0 or not os.path.isfile(dot_path):
            tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-8:])
            raise DiagramError("TLC state-graph dump failed (exit %s):\n%s" % (p.returncode,
                                                                               tail))
        with open(dot_path) as fh:
            return fh.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def action_name(raw):
    m = ACTION_NAME_RE.match(raw or "")
    if m:
        return m.group(1)
    return (raw or "").strip("<> ") or "?"


def project_key(state, project):
    return tuple((v, state.get(v, "?")) for v in project)


def build_tla_graph(tdir, spec, timeout):
    spec_path = os.path.join(tdir, spec["spec"])
    cfg_path = os.path.join(tdir, spec["cfg"])
    for p in (spec_path, cfg_path):
        if not os.path.isfile(p):
            raise DiagramError("%s not found" % os.path.relpath(p, tdir))
    nodes, initial, edges = parse_dot(run_tlc_dump(spec_path, cfg_path, timeout))
    if not nodes:
        raise DiagramError("TLC dumped no states")
    all_vars = sorted({v for s in nodes.values() for v in s})
    project = spec.get("project") or all_vars
    unknown = [v for v in project if v not in all_vars]
    if unknown:
        raise DiagramError("project names variables the spec does not have: %s (variables: "
                           "%s)" % (", ".join(unknown), ", ".join(all_vars)))
    keys = {sid: project_key(s, project) for sid, s in nodes.items()}
    graph = {"nodes": {}, "edges": {}, "initial": set(), "project": project,
             "full_states": len(nodes), "full_edges": len(edges)}
    for sid, key in keys.items():
        n = graph["nodes"].setdefault(key, {"count": 0, "terminal": True})
        n["count"] += 1
    for sid in initial:
        graph["initial"].add(keys[sid])
    moves = {sid: False for sid in nodes}
    for a, b, label in edges:
        if a not in keys or b not in keys:
            continue
        if a != b:
            moves[a] = True
        ek = (keys[a], keys[b])
        graph["edges"].setdefault(ek, set()).add(action_name(label))
    for sid, moved in moves.items():
        if moved:
            graph["nodes"][keys[sid]]["terminal"] = False
    return graph


def trace_overlay(tdir, rel, project):
    path = os.path.join(tdir, rel)
    with open(path) as fh:
        res = json.load(fh)
    trace = res.get("trace") or []
    if not trace:
        raise DiagramError("%s has no trace to highlight" % rel)
    keys = [project_key(st.get("vars") or {}, project) for st in trace]
    steps = []
    for i in range(1, len(trace)):
        steps.append((keys[i - 1], keys[i], action_name(trace[i].get("action"))))
    loop_to = res.get("loop_to")
    if isinstance(loop_to, int) and 1 <= loop_to <= len(keys):
        steps.append((keys[-1], keys[loop_to - 1], "back to state %d" % loop_to))
    return {"name": os.path.basename(rel), "source": rel, "keys": keys, "steps": steps,
            "violated": res.get("violated"), "result": res.get("result"),
            "loop_to": loop_to if isinstance(loop_to, int) else None,
            "constants": res.get("constants") or {}}


def build_manual_graph(spec):
    states = spec.get("states")
    transitions = spec.get("transitions")
    if not isinstance(states, list) or not states:
        raise DiagramError("a manual diagram needs a non-empty 'states' list")
    if not isinstance(transitions, list):
        raise DiagramError("a manual diagram needs a 'transitions' list")
    graph = {"nodes": {}, "edges": {}, "initial": set(), "project": ["state"],
             "codes": {}, "edge_codes": {}}
    for s in states:
        if not isinstance(s, dict) or not s.get("id"):
            raise DiagramError("every manual state needs an 'id'")
        if not FILE_LINE_RE.search(str(s.get("code", ""))):
            raise DiagramError("state %r cites no file:line in 'code'" % s["id"])
        key = (("state", str(s.get("label") or s["id"])),)
        graph["nodes"][key] = {"count": 1, "terminal": True, "id": s["id"]}
        graph["codes"][key] = s["code"]
    by_id = {n["id"]: k for k, n in graph["nodes"].items()}
    for sid in spec.get("initial") or []:
        if sid not in by_id:
            raise DiagramError("initial state %r is not in 'states'" % sid)
        graph["initial"].add(by_id[sid])
    if not graph["initial"]:
        raise DiagramError("a manual diagram needs at least one 'initial' state")
    for t in transitions:
        if not isinstance(t, dict) or t.get("from") not in by_id or t.get("to") not in by_id:
            raise DiagramError("transition %r names a state that is not in 'states'" % (t,))
        if not FILE_LINE_RE.search(str(t.get("code", ""))):
            raise DiagramError("transition %s -> %s cites no file:line in 'code'"
                               % (t["from"], t["to"]))
        ek = (by_id[t["from"]], by_id[t["to"]])
        label = str(t.get("label") or "")
        graph["edges"].setdefault(ek, set()).add(label)
        graph["edge_codes"].setdefault(ek, []).append("%s %s" % (label, t["code"]))
        if t["from"] != t["to"]:
            graph["nodes"][by_id[t["from"]]]["terminal"] = False
    overlays = []
    for h in spec.get("highlight") or []:
        path = h.get("path") or []
        if any(p not in by_id for p in path) or len(path) < 2:
            raise DiagramError("highlight %r needs a path of at least two known states"
                               % h.get("name"))
        keys = [by_id[p] for p in path]
        overlays.append({"name": h.get("name") or "highlight", "source": "diagram.json",
                         "keys": keys,
                         "steps": [(keys[i - 1], keys[i], "") for i in range(1, len(keys))],
                         "violated": h.get("violated"), "result": None, "constants": {}})
    return graph, overlays


# ---------------------------------------------------------------- layout and SVG

CHAR_W = 7.4
LINE_H = 16
PAD_X = 12
PAD_Y = 8
LAYER_GAP = 170
NODE_GAP = 34
MARGIN = 40


def node_lines(key, single):
    out = []
    for var, value in key:
        v = value if len(value) <= LABEL_VALUE_MAX else value[:LABEL_VALUE_MAX - 1] + "…"
        out.append(v if single else "%s = %s" % (var, v))
    return out


def layout(graph, overlays):
    keys = list(graph["nodes"].keys())
    for ov in overlays:
        for k in ov["keys"]:
            if k not in graph["nodes"]:
                graph["nodes"][k] = {"count": 0, "terminal": False, "trace_only": True}
                keys.append(k)
    succ = {k: [] for k in keys}
    pred = {k: [] for k in keys}
    for (a, b) in graph["edges"]:
        if a != b:
            succ[a].append(b)
            pred[b].append(a)
    for ov in overlays:
        for a, b, _ in ov["steps"]:
            if a != b and b not in succ[a]:
                succ[a].append(b)
                pred[b].append(a)
    layer = {}
    frontier = [k for k in keys if k in graph["initial"]] or keys[:1]
    for k in frontier:
        layer[k] = 0
    while frontier:
        nxt = []
        for k in frontier:
            for s in succ[k]:
                if s not in layer:
                    layer[s] = layer[k] + 1
                    nxt.append(s)
        frontier = nxt
    for k in keys:
        if k not in layer:
            layer[k] = max([layer[p] + 1 for p in pred[k] if p in layer] or [0])
    nlayers = max(layer.values()) + 1
    layers = [[k for k in keys if layer[k] == i] for i in range(nlayers)]
    for _ in range(8):
        for direction in (1, -1):
            rng = range(1, nlayers) if direction == 1 else range(nlayers - 2, -1, -1)
            for i in rng:
                ref = layers[i - direction]
                pos = {k: j for j, k in enumerate(ref)}

                def bary(k, pos=pos):
                    nb = [pos[n] for n in (pred[k] + succ[k]) if n in pos]
                    return sum(nb) / len(nb) if nb else float("inf")
                order = {k: j for j, k in enumerate(layers[i])}
                layers[i].sort(key=lambda k: (bary(k), order[k]))
    single = len(graph["project"]) == 1
    boxes = {}
    for k in keys:
        lines = node_lines(k, single)
        w = max(len(l) for l in lines) * CHAR_W + 2 * PAD_X + 30
        h = len(lines) * LINE_H + 2 * PAD_Y
        boxes[k] = {"lines": lines, "w": max(w, 56), "h": h}
    layer_w = [max(boxes[k]["w"] for k in layers[i]) for i in range(nlayers)]
    layer_h = [sum(boxes[k]["h"] for k in layers[i]) + NODE_GAP * (len(layers[i]) - 1)
               for i in range(nlayers)]
    total_h = max(layer_h)
    x = MARGIN
    for i in range(nlayers):
        y = MARGIN + 60 + (total_h - layer_h[i]) / 2
        for k in layers[i]:
            b = boxes[k]
            b["x"] = x + (layer_w[i] - b["w"]) / 2
            b["y"] = y
            b["layer"] = i
            y += b["h"] + NODE_GAP
        x += layer_w[i] + LAYER_GAP
    width = x - LAYER_GAP + MARGIN + 70
    height = MARGIN + 60 + total_h + MARGIN + 60
    return boxes, width, height


def esc(s):
    return html.escape(str(s), quote=True)


def bezier_mid(p0, p1, p2, p3):
    t = 0.5
    mt = 1 - t
    return (mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0],
            mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1])


def edge_path(ba, bb, bend):
    if ba is bb:
        x = ba["x"] + ba["w"]
        y = ba["y"] + ba["h"] / 2
        d = "M %.1f %.1f C %.1f %.1f, %.1f %.1f, %.1f %.1f" % (
            x, y - 7, x + 34, y - 26, x + 34, y + 26, x + 1, y + 7)
        return d, (x + 44, y + 4)
    if bb["layer"] > ba["layer"]:
        p0 = (ba["x"] + ba["w"], ba["y"] + ba["h"] / 2)
        p3 = (bb["x"], bb["y"] + bb["h"] / 2)
        dx = max(40, (p3[0] - p0[0]) / 2)
        p1 = (p0[0] + dx, p0[1] + bend)
        p2 = (p3[0] - dx, p3[1] + bend)
    else:
        p0 = (ba["x"] + ba["w"] / 2, ba["y"])
        p3 = (bb["x"] + bb["w"] / 2, bb["y"])
        lift = 46 + 0.18 * abs(p3[0] - p0[0]) + abs(bend)
        if bb["layer"] == ba["layer"] and bb["y"] < ba["y"]:
            p0 = (ba["x"], ba["y"] + ba["h"] / 2)
            p3 = (bb["x"], bb["y"] + bb["h"] / 2)
            p1 = (p0[0] - lift, p0[1])
            p2 = (p3[0] - lift, p3[1])
        else:
            p1 = (p0[0], p0[1] - lift)
            p2 = (p3[0], p3[1] - lift)
    d = "M %.1f %.1f C %.1f %.1f, %.1f %.1f, %.1f %.1f" % (p0 + p1 + p2 + p3)
    return d, bezier_mid(p0, p1, p2, p3)


def render_svg(graph, overlays, actions):
    boxes, width, height = layout(graph, overlays)
    hl_steps = {}
    hl_nodes = {}
    for oi, ov in enumerate(overlays):
        for si, (a, b, _) in enumerate(ov["steps"]):
            hl_steps.setdefault((a, b), []).append("%s%d" % (chr(65 + oi) if len(overlays) > 1
                                                             else "", si + 1))
        if ov.get("violated") and ov["keys"]:
            if ov.get("loop_to"):
                hl_nodes[ov["keys"][ov["loop_to"] - 1]] = "%s: this cycle can repeat " \
                    "forever" % ov["violated"]
            else:
                hl_nodes[ov["keys"][-1]] = "violates %s" % ov["violated"]
        for k in ov["keys"]:
            hl_nodes.setdefault(k, None)
    all_edges = dict((k, set(v)) for k, v in graph["edges"].items())
    for ov in overlays:
        for a, b, lab in ov["steps"]:
            if (a, b) not in all_edges:
                all_edges[(a, b)] = {lab} if lab and not lab.startswith("back to") else set()
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" class="diagram" width="%d" height="%d" '
             'viewBox="0 0 %d %d" role="img">' % (width, height, width, height)]
    parts.append('<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" '
                 'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                 '<path d="M0 0 L10 5 L0 10 z" class="arrowhead"/></marker>'
                 '<marker id="arr-hl" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
                 'markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" '
                 'class="arrowhead-hl"/></marker></defs>')
    pair_count = {}
    labels = []
    for (a, b), acts in sorted(all_edges.items(), key=lambda e: (boxes[e[0][0]]["layer"],
                                                                  str(e[0]))):
        ba, bb = boxes[a], boxes[b]
        rev = (b, a) in all_edges and a != b
        n = pair_count.get((a, b), 0)
        pair_count[(a, b)] = n + 1
        bend = (14 if rev and str(a) < str(b) else -14 if rev else 0) + 10 * n
        d, mid = edge_path(ba, bb, bend)
        steps = hl_steps.get((a, b))
        cls = "edge hl" if steps else "edge"
        marker = "arr-hl" if steps else "arr"
        title = ", ".join(sorted(x for x in acts if x)) or "step"
        parts.append('<path d="%s" class="%s" marker-end="url(#%s)"><title>%s</title></path>'
                     % (d, cls, marker, esc(title)))
        text = ", ".join(sorted(x for x in acts if x))
        if len(text) > 34:
            text = text[:33] + "…"
        labels.append((mid, text, steps))
    for k, b in boxes.items():
        node = graph["nodes"][k]
        cls = ["node"]
        if k in graph["initial"]:
            cls.append("initial")
        if node.get("terminal") and node.get("count"):
            cls.append("terminal")
        if node.get("trace_only"):
            cls.append("trace-only")
        if k in hl_nodes:
            cls.append("on-trace")
        if hl_nodes.get(k):
            cls.append("bad")
        tip = "\n".join("%s = %s" % kv for kv in k)
        if node.get("count"):
            tip += "\n(%d full state%s)" % (node["count"], "" if node["count"] == 1 else "s")
        parts.append('<g class="%s"><title>%s</title>' % (" ".join(cls), esc(tip)))
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="9"/>'
                     % (b["x"], b["y"], b["w"], b["h"]))
        if node.get("terminal") and node.get("count"):
            parts.append('<rect class="inner" x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                         'rx="6"/>' % (b["x"] + 3, b["y"] + 3, b["w"] - 6, b["h"] - 6))
        for i, line in enumerate(b["lines"]):
            parts.append('<text x="%.1f" y="%.1f" class="nlabel">%s</text>'
                         % (b["x"] + PAD_X, b["y"] + PAD_Y + 12 + i * LINE_H, esc(line)))
        if node.get("count", 0) > 1:
            parts.append('<text x="%.1f" y="%.1f" class="count">×%d</text>'
                         % (b["x"] + b["w"] - 7, b["y"] + PAD_Y + 10, node["count"]))
        if k in graph["initial"]:
            parts.append('<text x="%.1f" y="%.1f" class="tag">start</text>'
                         % (b["x"], b["y"] - 6))
        if hl_nodes.get(k):
            parts.append('<text x="%.1f" y="%.1f" class="tag bad-tag">%s</text>'
                         % (b["x"], b["y"] + b["h"] + 14, esc(hl_nodes[k])))
        parts.append("</g>")
    placed = [(b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]) for b in boxes.values()]

    def clashes(x0, y0, x1, y1):
        return sum(1 for p in placed if x0 < p[2] and p[0] < x1 and y0 < p[3] and p[1] < y1)

    offsets = sorted(((dx, dy) for dx in (0, -36, 36, -72, 72)
                      for dy in range(-84, 85, 12)), key=lambda o: abs(o[0]) * 1.5 + abs(o[1]))
    moved = []
    for (mx, my), text, steps in labels:
        tw = max(6.6 * len(text), 8 + 6.6 * len(",".join(steps or []))) / 2 + 4
        best = None
        for dx, dy in offsets:
            n = clashes(mx + dx - tw, my + dy - 16, mx + dx + tw, my + dy + 18)
            if best is None or n < best[0]:
                best = (n, dx, dy)
            if n == 0:
                break
        _, dx, dy = best
        placed.append((mx + dx - tw, my + dy - 16, mx + dx + tw, my + dy + 18))
        moved.append(((mx + dx, my + dy), text, steps))
    for (mx, my), text, steps in moved:
        if text:
            parts.append('<text x="%.1f" y="%.1f" class="elabel">%s</text>'
                         % (mx, my - 5, esc(text)))
        if steps:
            badge = ",".join(steps)
            w = 8 + 6.6 * len(badge)
            parts.append('<g class="step"><rect x="%.1f" y="%.1f" width="%.1f" height="16" '
                         'rx="8"/><text x="%.1f" y="%.1f">%s</text></g>'
                         % (mx - w / 2, my + 1, w, mx, my + 13, esc(badge)))
    parts.append("</svg>")
    return "".join(parts)


CSS = """
:root { --bg:#fbfaf7; --panel:#ffffff; --ink:#1d1f24; --muted:#5f6670; --line:#9aa1ab;
  --node:#ffffff; --node-line:#7b8491; --start:#2f6fde; --hl:#d23c2a; --hl-soft:#fde9e5;
  --term:#2c8a55; --border:#e3e1dc; --code:#f2f0eb; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#15171b;
  --panel:#1c1f24; --ink:#e7e9ec; --muted:#9aa2ad; --line:#6f7782; --node:#22262c;
  --node-line:#8a94a1; --start:#6aa1ff; --hl:#ff6b57; --hl-soft:#3a201c; --term:#5cc58b;
  --border:#2c3036; --code:#23272d; } }
:root[data-theme="dark"] { --bg:#15171b; --panel:#1c1f24; --ink:#e7e9ec; --muted:#9aa2ad;
  --line:#6f7782; --node:#22262c; --node-line:#8a94a1; --start:#6aa1ff; --hl:#ff6b57;
  --hl-soft:#3a201c; --term:#5cc58b; --border:#2c3036; --code:#23272d; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 20px; margin: 0 0 6px; }
.sub { color: var(--muted); margin: 0 0 24px; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px; }
code { background: var(--code); padding: 1px 5px; border-radius: 4px; }
section.target { background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 20px; margin: 0 0 28px; }
.badge { display:inline-block; font-size:12px; padding:2px 8px; border-radius:999px;
  border:1px solid var(--border); color:var(--muted); margin-left:8px; vertical-align: 2px; }
.badge.manual { border-color: var(--hl); color: var(--hl); }
.facts { color: var(--muted); font-size: 14px; margin: 4px 0 12px; }
.canvas { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg); margin: 12px 0; }
.legend { display:flex; flex-wrap:wrap; gap:16px; font-size:13px; color:var(--muted);
  margin: 0 0 24px; }
.legend span::before { content:""; display:inline-block; width:14px; height:10px;
  border-radius:3px; margin-right:6px; vertical-align:0; border:1.5px solid var(--node-line); }
.legend .l-start::before { border-color: var(--start); border-width: 2.5px; }
.legend .l-term::before { border-color: var(--term); border-style: double; border-width: 3px; }
.legend .l-hl::before { border-color: var(--hl); background: var(--hl-soft); }
.legend .l-only::before { border-style: dashed; }
svg.diagram { display:block; max-width:100%; height:auto; min-width:640px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
  monospace; }
svg .edge { fill:none; stroke:var(--line); stroke-width:1.3; }
svg .edge.hl { stroke:var(--hl); stroke-width:2.6; }
svg .arrowhead { fill: var(--line); }
svg .arrowhead-hl { fill: var(--hl); }
svg .node rect { fill:var(--node); stroke:var(--node-line); stroke-width:1.3; }
svg .node rect.inner { fill:none; stroke:var(--term); stroke-width:1.2; }
svg .node.terminal > rect:first-of-type { stroke: var(--term); stroke-width: 1.8; }
svg .node.initial > rect:first-of-type { stroke:var(--start); stroke-width:2.6; }
svg .node.on-trace > rect:first-of-type { fill: var(--hl-soft); }
svg .node.bad > rect:first-of-type { stroke:var(--hl); stroke-width:2.8; }
svg .node.trace-only > rect:first-of-type { stroke-dasharray: 5 4; }
svg .nlabel { font-size:12px; fill:var(--ink); }
svg .count { font-size:11px; fill:var(--muted); text-anchor:end; }
svg .tag { font-size:11px; fill:var(--start); font-family: system-ui, sans-serif; }
svg .bad-tag { fill: var(--hl); font-weight: 600; }
svg .elabel { font-size:11px; fill:var(--muted); text-anchor:middle; paint-order:stroke;
  stroke:var(--bg); stroke-width:4px; stroke-linejoin:round; }
svg .step rect { fill: var(--hl); }
svg .step text { font-size: 11px; fill: #fff; text-anchor: middle; font-weight: 600; }
table { border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 8px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border);
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
td.mono { word-break: break-word; }
details { margin-top: 10px; }
summary { cursor: pointer; color: var(--muted); }
.err { color: var(--hl); white-space: pre-wrap; font-family: ui-monospace, monospace;
  font-size: 13px; }
ol.trace { padding-left: 22px; margin: 6px 0; font-size: 13px; }
@media (max-width: 640px) { main { padding: 20px 16px 48px; } section.target { padding: 14px; } }
"""


def git_head(repo):
    p = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def fmt_key(k, single):
    return "; ".join(v if single else "%s = %s" % (n, v) for n, v in k)


def target_section(tid, title, spec, graph, overlays, error, actions):
    out = ['<section class="target" id="t-%s">' % esc(tid)]
    kind = (spec or {}).get("kind")
    badge = ('<span class="badge">derived from TLC state graph</span>' if kind == "tla" else
             '<span class="badge manual">hand-drawn from code, not checker-derived</span>'
             if kind == "manual" else "")
    out.append("<h2>%s%s</h2>" % (esc(title or tid), badge))
    out.append('<div class="facts mono">target <code>%s</code></div>' % esc(tid))
    if error:
        out.append('<p class="err">%s</p></section>' % esc(error))
        return "".join(out)
    single = len(graph["project"]) == 1
    if kind == "tla":
        facts = ("spec <code>%s</code>, cfg <code>%s</code>; %d reachable states and %d "
                 "transitions, drawn as %d nodes over <code>%s</code>"
                 % (esc(spec["spec"]), esc(spec["cfg"]), graph["full_states"],
                    graph["full_edges"], len([n for n in graph["nodes"].values()
                                              if n.get("count")]),
                    esc(", ".join(graph["project"]))))
        consts = graph.get("constants") or {}
        if consts:
            facts += "; constants " + ", ".join("<code>%s = %s</code>" % (esc(k), esc(v))
                                                 for k, v in sorted(consts.items()))
        out.append('<p class="facts">%s.</p>' % facts)
    else:
        out.append('<p class="facts">Drawn by hand from the code; every state and transition '
                   'cites the line it comes from. It shows the shape the harness exercises, '
                   'not a set of states a checker enumerated.</p>')
    out.append('<div class="canvas">%s</div>' % render_svg(graph, overlays, actions))
    for oi, ov in enumerate(overlays):
        prefix = chr(65 + oi) if len(overlays) > 1 else ""
        head = "Highlighted path %s<code>%s</code>" % (prefix + " " if prefix else "",
                                                      esc(ov["name"]))
        if ov.get("violated") and ov.get("loop_to"):
            head += " ending in a cycle that violates <strong>%s</strong>" % esc(
                ov["violated"])
        elif ov.get("violated"):
            head += " ending in a state that violates <strong>%s</strong>" % esc(
                ov["violated"])
        out.append("<p>%s:</p><ol class=\"trace\">" % head)
        out.append("<li class=\"mono\">%s</li>" % esc(fmt_key(ov["keys"][0], single)))
        for a, b, lab in ov["steps"]:
            out.append('<li class="mono">%s%s</li>' % (
                ("<strong>%s</strong> → " % esc(lab)) if lab else "",
                esc(fmt_key(b, single))))
        out.append("</ol>")
    rows = []
    codes = graph.get("edge_codes") or {}
    for (a, b), acts in sorted(graph["edges"].items(), key=lambda e: str(e[0])):
        names = sorted(x for x in acts if x)
        where = "; ".join(codes.get((a, b), [])) or "; ".join(
            "%s: %s" % (n, actions[n]) for n in names if n in actions)
        rows.append("<tr><td class=\"mono\">%s</td><td class=\"mono\">%s</td><td>%s</td>"
                    "<td class=\"mono\">%s</td></tr>" % (
                        esc(fmt_key(a, single)), esc(fmt_key(b, single)),
                        esc(", ".join(names)), esc(where)))
    out.append("<details><summary>Transitions (%d)</summary><table><thead><tr><th>From</th>"
               "<th>To</th><th>Action</th><th>Code</th></tr></thead><tbody>%s</tbody></table>"
               "</details>" % (len(rows), "".join(rows)))
    if graph.get("codes"):
        srows = "".join("<tr><td class=\"mono\">%s</td><td class=\"mono\">%s</td></tr>"
                        % (esc(fmt_key(k, single)), esc(c)) for k, c in graph["codes"].items())
        out.append("<details><summary>States (%d)</summary><table><thead><tr><th>State</th>"
                   "<th>Code</th></tr></thead><tbody>%s</tbody></table></details>"
                   % (len(graph["codes"]), srows))
    out.append("</section>")
    return "".join(out)


def text_summary(tid, spec, graph, overlays, error):
    """The diagram as plain text, for reviewing it where no browser is available."""
    lines = ["== %s (%s)" % (tid, (spec or {}).get("kind") if isinstance(spec, dict) else "?")]
    if error:
        return "\n".join(lines + ["  ERROR: %s" % error])
    single = len(graph["project"]) == 1
    lines.append("  projected on: %s" % ", ".join(graph["project"]))
    names = {}
    for i, (key, node) in enumerate(graph["nodes"].items()):
        names[key] = "S%d" % (i + 1)
        flags = [f for f, on in (("start", key in graph["initial"]),
                                 ("final", node.get("terminal") and node.get("count")),
                                 ("trace only", node.get("trace_only"))) if on]
        lines.append("  %s %s%s%s" % (
            names[key], fmt_key(key, single),
            "  x%d" % node["count"] if node.get("count", 0) > 1 else "",
            "  [%s]" % ", ".join(flags) if flags else ""))
    order = {key: i for i, key in enumerate(graph["nodes"])}
    for (a, b), acts in sorted(graph["edges"].items(), key=lambda e: (order[e[0][0]],
                                                                       order[e[0][1]])):
        lines.append("  %s -> %s: %s" % (names[a], names[b],
                                         ", ".join(sorted(x for x in acts if x)) or "step"))
    for ov in overlays:
        path = [names.get(ov["keys"][0], "?")] + [
            "%s%s" % ("(%s) " % lab if lab else "", names.get(b, "?"))
            for _a, b, lab in ov["steps"]]
        lines.append("  highlighted %s%s: %s" % (
            ov["name"], " (violates %s)" % ov["violated"] if ov.get("violated") else "",
            " -> ".join(path)))
    return "\n".join(lines)


def load_targets(repo):
    findings = os.path.join(repo, "verification", "findings.json")
    order, titles = [], {}
    try:
        with open(findings) as fh:
            data = json.load(fh)
        for t in data.get("targets") or []:
            if isinstance(t, dict) and isinstance(t.get("id"), str):
                order.append(t["id"])
                titles[t["id"]] = t.get("title")
    except (OSError, ValueError, AttributeError):
        pass
    models = os.path.join(repo, "verification", "models")
    if os.path.isdir(models):
        for name in sorted(os.listdir(models)):
            if name not in order and os.path.isfile(os.path.join(models, name,
                                                                 "diagram.json")):
                order.append(name)
    return order, titles


def input_files(tdir, spec):
    files = ["diagram.json"]
    if spec.get("kind") == "tla":
        files += sorted(os.path.relpath(p, tdir) for p in
                        __import__("glob").glob(os.path.join(tdir, "*.tla")))
        if spec.get("cfg"):
            files.append(spec["cfg"])
        files += list(spec.get("highlight") or [])
    seen = []
    for f in files:
        if f not in seen:
            seen.append(f)
    return seen


def build_target(repo, tid, timeout):
    tdir = os.path.join(repo, "verification", "models", tid)
    dpath = os.path.join(tdir, "diagram.json")
    if not os.path.isfile(dpath):
        return None, None, [], "no verification/models/%s/diagram.json" % tid, {}
    try:
        with open(dpath) as fh:
            spec = json.load(fh)
    except ValueError as e:
        return None, None, [], "diagram.json is not valid JSON: %s" % e, {"diagram.json": None}
    if not isinstance(spec, dict) or spec.get("kind") not in ("tla", "manual"):
        return spec, None, [], "diagram.json needs \"kind\": \"tla\" or \"manual\"", {}
    hashes = {}
    for f in input_files(tdir, spec):
        p = os.path.join(tdir, f)
        hashes[f] = sha256_file(p) if os.path.isfile(p) else None
    try:
        if spec["kind"] == "tla":
            for key in ("spec", "cfg"):
                if not isinstance(spec.get(key), str):
                    raise DiagramError("a tla diagram needs %r" % key)
            graph = build_tla_graph(tdir, spec, timeout)
            consts, _, _, _ = tlc_report.parse_cfg(os.path.join(tdir, spec["cfg"]))
            graph["constants"] = consts
            overlays = [trace_overlay(tdir, rel, graph["project"])
                        for rel in spec.get("highlight") or []]
        else:
            graph, overlays = build_manual_graph(spec)
        limit = spec.get("max_nodes") or DEFAULT_MAX_NODES
        drawn = len(graph["nodes"])
        if drawn > limit:
            raise DiagramError(
                "%d nodes after projection (max_nodes %d); list fewer variables in "
                "'project' (the control-state variables, not counters or data) or shrink "
                "the constants" % (drawn, limit))
    except (DiagramError, OSError, ValueError) as e:
        return spec, None, [], str(e), hashes
    return spec, graph, overlays, None, hashes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo_root")
    ap.add_argument("--out", default=None)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--text", action="store_true",
                    help="also print each diagram as text (states, transitions, highlighted "
                         "paths), for review without a browser")
    args = ap.parse_args()
    repo = os.path.abspath(args.repo_root)
    if not os.path.isdir(os.path.join(repo, "verification")):
        print("no verification/ directory under %s" % repo, file=sys.stderr)
        return 2
    out = os.path.abspath(args.out or os.path.join(repo, "verification", "state-diagram.html"))
    order, titles = load_targets(repo)
    sections, meta_targets, failures = [], [], []
    for tid in order:
        tdir = os.path.join(repo, "verification", "models", tid)
        if not os.path.isfile(os.path.join(tdir, "diagram.json")):
            continue
        spec, graph, overlays, err, hashes = build_target(repo, tid, args.timeout)
        if err:
            failures.append("%s: %s" % (tid, err))
        if args.text:
            print(text_summary(tid, spec, graph, overlays, err))
        sections.append(target_section(tid, titles.get(tid), spec, graph, overlays, err,
                                       (spec or {}).get("actions") or {}))
        meta_targets.append({
            "id": tid,
            "kind": (spec or {}).get("kind") if isinstance(spec, dict) else None,
            "inputs": hashes,
            "error": err,
            "nodes": len([n for n in graph["nodes"].values() if n.get("count")]) if graph
            else 0,
            "edges": len(graph["edges"]) if graph else 0,
            "highlights": [o["source"] for o in overlays],
        })
    commit = git_head(repo)
    meta = {"generator": GENERATOR, "version": META_VERSION, "commit": commit,
            "targets": meta_targets}
    legend = ('<div class="legend"><span class="l-start">initial state</span>'
              '<span class="l-term">no further moves</span>'
              '<span class="l-hl">on a highlighted counterexample</span>'
              '<span class="l-only">appears only in the trace</span></div>')
    body = "".join(sections) or "<p>No target has a diagram.json yet.</p>"
    page = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>Verification state diagrams</title><style>%s</style></head><body><main>"
            "<h1>State diagrams</h1><p class=\"sub\">One diagram per verified target, at "
            "commit <code>%s</code>. Numbered red edges follow a counterexample step by step. "
            "Hover a node or edge for its full values.</p>%s%s</main>"
            "<script type=\"application/json\" id=\"vf-diagram-meta\">%s</script>"
            "</body></html>\n" % (CSS, esc((commit or "unknown")[:12]), legend, body,
                                   json.dumps(meta, sort_keys=True).replace("</", "<\\/")))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        fh.write(page)
    for f in failures:
        print("diagram error: %s" % f, file=sys.stderr)
    print("%s %d target(s) -> %s" % ("error" if failures else "ok", len(meta_targets),
                                     os.path.relpath(out, os.getcwd())))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
