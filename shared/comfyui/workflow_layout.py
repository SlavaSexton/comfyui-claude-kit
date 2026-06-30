#!/usr/bin/env python3
"""
Workflow layout: auto-arrange a ComfyUI (litegraph UI-format) node graph so nodes NEVER overlap, the flow
reads left-to-right by dependency depth, parallel branches stack vertically, and edge crossings are minimised.
Plus a code-only inspector so layout is verified from the JSON coordinates, never from a screenshot (cheap,
deterministic, no tokens spent looking at the canvas).

Rules implemented (a layered / Sugiyama layout):
  - Columns by dependency depth: sources on the left, each node one column right of its deepest input.
  - Within a column, nodes stack top-to-bottom with a gap >= node height, so two nodes can never overlap.
  - Column order is reordered by the median of connected nodes (barycenter passes) to reduce branch crossings.
  - Node sizes are estimated from real widget / slot counts (a CDL with 12 widgets is tall), so the spacing
    matches what ComfyUI will actually render. Auto-layout also writes the estimated size back onto each node
    so the canvas and the layout agree.

Usage:
  from workflow_layout import auto_layout, inspect
  wf = auto_layout(wf)                  # returns the same dict with new pos/size, no overlaps
  rep = inspect(wf); print(rep["summary"])   # overlaps / crossings / bounds, all from coordinates

CLI:
  python workflow_layout.py graph.json            # inspect only (report overlaps + crossings)
  python workflow_layout.py graph.json --apply     # auto-layout in place (writes graph.json)
  python workflow_layout.py graph.json --apply --out fixed.json
"""
import json
import sys

# litegraph-approximate metrics (px)
TITLE_H = 30
SLOT_H = 22          # per i/o row (rows = max(inputs, outputs))
WIDGET_H = 26        # per widget row
PAD_H = 14
H_GAP = 90           # horizontal gap between columns
V_GAP = 50           # vertical gap between stacked nodes
DEFAULT_W = 250


def _pos(node):
    p = node.get("pos", [0, 0])
    if isinstance(p, dict):
        return [float(p.get("0", 0)), float(p.get("1", 0))]
    return [float(p[0]), float(p[1])]


def est_size(node):
    """Estimate (w, h) the node will render at in ComfyUI, from its slots and widgets."""
    n_in = len(node.get("inputs", []) or [])
    n_out = len(node.get("outputs", []) or [])
    n_widg = len(node.get("widgets_values", []) or [])
    rows = max(n_in, n_out)
    h = TITLE_H + rows * SLOT_H + n_widg * WIDGET_H + PAD_H
    # keep a declared size if it is taller/wider (the node may know better)
    sz = node.get("size")
    if isinstance(sz, dict):
        sz = [sz.get("0"), sz.get("1")]
    w = DEFAULT_W
    if sz and sz[0]:
        w = max(w, float(sz[0]))
    if sz and sz[1]:
        h = max(h, float(sz[1]))
    return [round(w), round(h)]


def _edges(wf):
    """Return list of (src_id, dst_id) from the links array (litegraph: [lid, src, sslot, dst, dslot, type])."""
    out = []
    for lk in wf.get("links", []) or []:
        if isinstance(lk, list) and len(lk) >= 5:
            out.append((lk[1], lk[3]))
        elif isinstance(lk, dict):
            out.append((lk.get("origin_id"), lk.get("target_id")))
    return out


# --------------------------------------------------------------------------- inspector (code-only)

def inspect(wf):
    nodes = {n["id"]: n for n in wf.get("nodes", [])}
    box = {}
    for nid, n in nodes.items():
        x, y = _pos(n)
        w, h = est_size(n)  # real render footprint (widget-driven), not the optimistic declared size
        box[nid] = (x, y, x + float(w), y + float(h))

    ids = list(box)
    overlaps = []
    for i in range(len(ids)):
        ax0, ay0, ax1, ay1 = box[ids[i]]
        for j in range(i + 1, len(ids)):
            bx0, by0, bx1, by1 = box[ids[j]]
            ox = min(ax1, bx1) - max(ax0, bx0)
            oy = min(ay1, by1) - max(ay0, by0)
            if ox > 0 and oy > 0:
                overlaps.append((ids[i], ids[j], round(ox * oy)))

    # edge crossings: segment-intersection between right-edge of src and left-edge of dst
    def seg(a, b):
        ax0, ay0, ax1, ay1 = box[a]
        bx0, by0, bx1, by1 = box[b]
        return (ax1, (ay0 + ay1) / 2), (bx0, (by0 + by1) / 2)

    def ccw(p, q, r):
        return (r[1] - p[1]) * (q[0] - p[0]) > (q[1] - p[1]) * (r[0] - p[0])

    def crosses(s1, s2):
        a, b = s1; c, d = s2
        return ccw(a, c, d) != ccw(b, c, d) and ccw(a, b, c) != ccw(a, b, d)

    edges = [(s, t) for s, t in _edges(wf) if s in box and t in box]
    segs = [seg(s, t) for s, t in edges]
    crossings = 0
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if edges[i][0] in edges[j] or edges[i][1] in edges[j]:
                continue  # shared endpoint, not a crossing
            if crosses(segs[i], segs[j]):
                crossings += 1

    xs = [b[0] for b in box.values()] + [b[2] for b in box.values()]
    ys = [b[1] for b in box.values()] + [b[3] for b in box.values()]
    bounds = [min(xs), min(ys), max(xs), max(ys)] if box else [0, 0, 0, 0]
    summary = (f"nodes={len(nodes)} edges={len(edges)} overlaps={len(overlaps)} "
               f"crossings={crossings} bounds={[round(v) for v in bounds]}")
    return {"summary": summary, "overlaps": overlaps, "crossings": crossings,
            "bounds": bounds, "n_nodes": len(nodes), "n_edges": len(edges)}


# --------------------------------------------------------------------------- auto layout (layered)

def auto_layout(wf, origin=(40, 40)):
    nodes = {n["id"]: n for n in wf.get("nodes", [])}
    if not nodes:
        return wf
    edges = [(s, t) for s, t in _edges(wf) if s in nodes and t in nodes]
    succ = {nid: [] for nid in nodes}
    pred = {nid: [] for nid in nodes}
    for s, t in edges:
        succ[s].append(t)
        pred[t].append(s)

    # 1. longest-path layering (rank = column)
    rank = {}

    def depth(nid, seen):
        if nid in rank:
            return rank[nid]
        if nid in seen:
            return 0  # cycle guard (DAG expected)
        seen = seen | {nid}
        r = 0 if not pred[nid] else 1 + max(depth(p, seen) for p in pred[nid])
        rank[nid] = r
        return r

    for nid in nodes:
        depth(nid, set())

    layers = {}
    for nid, r in rank.items():
        layers.setdefault(r, []).append(nid)

    # 2. ordering within each layer: barycenter passes to reduce crossings
    order = {r: sorted(layers[r]) for r in layers}
    pos_in_layer = {nid: i for r in order for i, nid in enumerate(order[r])}
    for _ in range(4):
        for r in sorted(order):
            if r == 0:
                continue
            order[r].sort(key=lambda n: (
                sum(pos_in_layer[p] for p in pred[n]) / len(pred[n]) if pred[n] else pos_in_layer[n]))
            for i, nid in enumerate(order[r]):
                pos_in_layer[nid] = i
        for r in sorted(order, reverse=True):
            if not succ:
                break
            order[r].sort(key=lambda n: (
                sum(pos_in_layer[s] for s in succ[n]) / len(succ[n]) if succ[n] else pos_in_layer[n]))
            for i, nid in enumerate(order[r]):
                pos_in_layer[nid] = i

    # 3. set sizes, then assign coordinates (x by column width, y stacked with gaps)
    for n in nodes.values():
        n["size"] = est_size(n)

    ox, oy = origin
    col_x = {}
    x = ox
    for r in sorted(order):
        col_x[r] = x
        col_w = max((nodes[nid]["size"][0] for nid in order[r]), default=DEFAULT_W)
        x += col_w + H_GAP

    for r in sorted(order):
        y = oy
        for nid in order[r]:
            n = nodes[nid]
            n["pos"] = [round(col_x[r]), round(y)]
            y += n["size"][1] + V_GAP

    # refresh last_node_id / last_link_id if present (harmless)
    return wf


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    path = args[0]
    wf = _load(path)
    apply = "--apply" in args
    out = path
    if "--out" in args:
        out = args[args.index("--out") + 1]
    before = inspect(wf)
    print("BEFORE:", before["summary"])
    if before["overlaps"]:
        print("  overlapping pairs (id,id,area):", before["overlaps"][:12])
    if apply:
        wf = auto_layout(wf)
        after = inspect(wf)
        print("AFTER :", after["summary"])
        with open(out, "w", encoding="utf-8") as f:
            json.dump(wf, f, indent=2)
        print("wrote:", out)
