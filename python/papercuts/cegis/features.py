"""Structural features of the selects, read off the miter's BTOR2, and of candidate sets.

The synthesizer sees selects as anonymous Booleans. Here they get back what the
encoding dropped: what kind of cut each one is, how far its logic reaches, and which
other selects it can interact with.

Cones follow the spec's one-frame query (comb module docstring): registers are
free sources, so a cone stops at next-state functions and at the miter's outputs.
A *sink* is one thing the spec compares: one next-state function, or one output
comparison, taken as a disjunct of `bad`.

Independence. The spec is a conjunction over sinks, and each sink reads only the
selects in its support, so

    F(S)  =  AND over sinks k of  F_k(S restricted to support(k))

Selects that share no sink, even transitively, cannot interact, and each such
component can be decided on its own. `Cones.decomposable` says this applies: it
does not on a design read through a second frame, where R also depends on selects,
or where a constraint depends on one. Exclusivity joins components too, since it
restricts the assignments.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from papercuts.cegis.btor2z3 import Design
from papercuts.cegis.trace import bits
from papercuts.manifest import Manifest

ARITH_OPS = {"add", "sub", "mul", "udiv", "sdiv", "urem", "srem", "smod", "neg"}
CMP_OPS = {"eq", "neq", "ult", "ulte", "ugt", "ugte", "slt", "slte", "sgt", "sgte"}
FAMILIES = ("bitshrink", "ternary", "if", "case", "binop", "force-const")
VARIANTS = ("keep-false", "keep-true", "keep-left", "keep-right", "0", "1")

NODE_SCALARS = ("fanout", "cone_nodes", "cone_frac", "cone_mux", "cone_arith", "cone_cmp", "sinks", "sinks_frac",
                "sinks_output", "sinks_next", "sink_dist", "component")


def operands(op: str, args: list) -> list[int]:
    """The arguments that are nodes: slice and extensions also carry plain integers."""
    if op in ("const", "input", "state"):
        return []
    if op in ("slice", "uext", "sext"):
        return [abs(args[0])]
    return [abs(a) for a in args]


@dataclass
class Cones:
    selects: list[str]
    sink_names: list[str]
    sink_masks: list[int]  # per sink, the selects in its support
    node_masks: dict[int, int]
    fanout: list[int]  # nodes reading the select directly
    cone_nodes: list[int]
    cone_mux: list[int]
    cone_arith: list[int]
    cone_cmp: list[int]
    sink_dist: list[int]  # fewest nodes from the select to a sink; -1 if it reaches none
    n_nodes: int
    constraint_mask: int  # selects a constraint depends on

    def sinks_of(self, i: int) -> list[int]:
        return [k for k, m in enumerate(self.sink_masks) if m >> i & 1]


def split_bad(d: Design, nid: int, masks: dict[int, int]) -> list[int]:
    """The disjuncts of `bad`: the miter asserts one `eq`, an AND of per-output
    comparisons, under an enable that no select reaches. An AND with such a side
    distributes over the disjunction, so it does not matter which disjunct it
    gates. Anything else is a leaf, which is always safe: a coarser sink can only
    merge components."""

    def disj(n: int) -> list[int]:
        nd = d.nodes[n]
        if nd.op == "or":
            return [x for a in nd.args for x in disj(abs(a))]
        if nd.op == "not":
            return conj(abs(nd.args[0])) or [n]
        if nd.op == "and":
            a, b = abs(nd.args[0]), abs(nd.args[1])
            if not masks[a]:
                return disj(b)
            if not masks[b]:
                return disj(a)
        return [n]

    def conj(n: int) -> list[int]:
        nd = d.nodes[n]
        if nd.op == "and":
            return [x for a in nd.args for x in conj(abs(a))]
        return [n]

    return disj(nid)


def analyze(d: Design, selects: list[str]) -> Cones:
    idx = {s: i for i, s in enumerate(selects)}
    n = len(selects)
    masks: dict[int, int] = {}
    fanout = [0] * n
    readers: dict[int, list[int]] = {}
    sel_node = {d.inputs[s][0]: i for s, i in idx.items()}
    for nid in d.order:
        nd = d.nodes[nid]
        if nd.op == "input":
            masks[nid] = 1 << sel_node[nid] if nid in sel_node else 0
            continue
        m = 0
        for a in operands(nd.op, nd.args):
            m |= masks[a]
            readers.setdefault(a, []).append(nid)
            if a in sel_node:
                fanout[sel_node[a]] += 1
        masks[nid] = m

    sink_nodes: dict[str, int] = {f"next:{s}": v for s, v in d.next.items()}
    k = 0
    for b in d.bad:
        for leaf in split_bad(d, abs(b), masks):
            sink_nodes[f"out:{k}"] = leaf
            k += 1
    names = list(sink_nodes)
    sink_masks = [masks[abs(sink_nodes[s])] for s in names]

    # Cone sizes per select: nodes with one support share one count, which is what
    # makes this affordable on miters with thousands of selects.
    cone = np.zeros(n, dtype=np.int64)
    mux = np.zeros(n, dtype=np.int64)
    arith = np.zeros(n, dtype=np.int64)
    cmp_ = np.zeros(n, dtype=np.int64)
    groups: dict[int, list[int]] = {}
    for nid in d.order:
        m = masks[nid]
        if m and nid not in sel_node:
            g = groups.setdefault(m, [0, 0, 0, 0])
            op = d.nodes[nid].op
            g[0] += 1
            g[1] += op == "ite"
            g[2] += op in ARITH_OPS
            g[3] += op in CMP_OPS
    for m, (c0, c1, c2, c3) in groups.items():
        where = bits(m)
        cone[where] += c0
        mux[where] += c1
        arith[where] += c2
        cmp_[where] += c3

    # Distance to the nearest sink: one backward pass over the DAG.
    dist: dict[int, int] = {abs(s): 0 for s in sink_nodes.values()}
    for nid in reversed(d.order):
        best = dist.get(nid)
        for r in readers.get(nid, ()):
            if r in dist and (best is None or dist[r] + 1 < best):
                best = dist[r] + 1
        if best is not None:
            dist[nid] = best
    sink_dist = [dist.get(d.inputs[s][0], -1) for s in selects]

    constraint_mask = 0
    for c in d.constraints:
        constraint_mask |= masks[abs(c)]
    return Cones(selects=list(selects), sink_names=names, sink_masks=sink_masks, node_masks=masks, fanout=fanout,
                 cone_nodes=cone.tolist(), cone_mux=mux.tolist(), cone_arith=arith.tolist(), cone_cmp=cmp_.tolist(),
                 sink_dist=sink_dist, n_nodes=len(d.order), constraint_mask=constraint_mask)


def components(cones: Cones, exclusive: list[tuple[str, str]] = ()) -> list[list[int]]:
    """Selects grouped so that no sink and no exclusive pair crosses a group. Largest first."""
    parent = list(range(len(cones.selects)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for m in dict.fromkeys(cones.sink_masks):
        members = bits(m)
        for a in members[1:]:
            parent[find(a)] = find(members[0])
    idx = {s: i for i, s in enumerate(cones.selects)}
    for a, b in exclusive:
        parent[find(idx[a])] = find(idx[b])
    groups: dict[int, list[int]] = {}
    for i in range(len(parent)):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: (-len(g), g[0]))


def decomposable(cones: Cones, two_frames: bool) -> bool:
    """Whether the module docstring's independence holds on this design."""
    return not two_frames and cones.constraint_mask == 0


def module_depth(man: Manifest) -> dict[str, int]:
    """Modules above each module: every ancestor lists a descendant's cuts as forwarded."""
    mods = man.data["modules"]
    below: dict[str, set[str]] = {m: set() for m in mods}
    for parent, m in mods.items():
        for f in m["forwarded"]:
            below[f["module"]].add(parent)
    return {m: len(p) for m, p in below.items()}


@dataclass
class SelectTable:
    """Per-select features, one row per select, in `selects` order."""

    selects: list[str]
    columns: list[str]
    X: np.ndarray
    sink_lists: list[np.ndarray]  # per sink, the selects it reads (sinks reading fewer than two are dropped)
    comp_of: np.ndarray
    n_components: int
    decomposable: bool


def select_table(cones: Cones, man: Manifest, exclusive: list[tuple[str, str]], two_frames: bool) -> SelectTable:
    n = len(cones.selects)
    comps = components(cones, exclusive)
    comp_of = np.zeros(n, dtype=np.int64)
    for gi, g in enumerate(comps):
        comp_of[g] = gi
    comp_size = np.array([len(comps[comp_of[i]]) for i in range(n)], dtype=float)
    depth = module_depth(man)
    cuts = [man.cut_of(s) for s in cones.selects]
    per_module: dict[str, int] = {}
    for c in cuts:
        per_module[c.module] = per_module.get(c.module, 0) + 1
    max_line = {}
    for c in cuts:
        max_line[c.module] = max(max_line.get(c.module, 1), c.line)
    excl = {x for pair in exclusive for x in pair}

    sinks = np.zeros(n)
    sinks_out = np.zeros(n)
    sinks_next = np.zeros(n)
    for name, m in zip(cones.sink_names, cones.sink_masks):
        where = bits(m)
        sinks[where] += 1
        (sinks_out if name.startswith("out:") else sinks_next)[where] += 1

    cols: dict[str, np.ndarray] = {
        "fanout": np.array(cones.fanout, dtype=float),
        "cone_nodes": np.array(cones.cone_nodes, dtype=float),
        "cone_frac": np.array(cones.cone_nodes, dtype=float) / max(cones.n_nodes, 1),
        "cone_mux": np.array(cones.cone_mux, dtype=float),
        "cone_arith": np.array(cones.cone_arith, dtype=float),
        "cone_cmp": np.array(cones.cone_cmp, dtype=float),
        "sinks": sinks,
        "sinks_frac": sinks / max(len(cones.sink_names), 1),
        "sinks_output": sinks_out,
        "sinks_next": sinks_next,
        "sink_dist": np.array(cones.sink_dist, dtype=float),
        "component": comp_size,
        "module_depth": np.array([depth[c.module] for c in cuts], dtype=float),
        "module_cuts": np.array([per_module[c.module] for c in cuts], dtype=float),
        "line_frac": np.array([c.line / max_line[c.module] for c in cuts], dtype=float),
        "exclusive": np.array([s in excl for s in cones.selects], dtype=float),
    }
    for fam in FAMILIES:
        cols[f"family_{fam}"] = np.array([c.family == fam for c in cuts], dtype=float)
    unknown = sorted({c.family for c in cuts} - set(FAMILIES))
    if unknown:
        raise ValueError(f"cut families with no feature column: {unknown}")
    for v in VARIANTS:
        cols[f"variant_{v}"] = np.array([f"({v})" in c.type for c in cuts], dtype=float)
    sink_lists = [np.array(bits(m), dtype=np.int64) for m in dict.fromkeys(cones.sink_masks) if m & (m - 1)]
    return SelectTable(selects=list(cones.selects), columns=list(cols), X=np.column_stack(list(cols.values())) if n
                       else np.zeros((0, len(cols))), sink_lists=sink_lists, comp_of=comp_of,
                       n_components=len(comps), decomposable=decomposable(cones, two_frames))


class History:
    """What the loop knows just before a candidate: only cores learned earlier, so
    these features are available at prediction time and leak nothing from the label."""

    def __init__(self, n: int):
        self.on_count = np.zeros(n)
        self.off_count = np.zeros(n)
        self.cores: list[tuple[np.ndarray, np.ndarray]] = []  # per core: selects on, selects off

    def add(self, core: list) -> None:
        on = np.array([i for i, v in core if v], dtype=np.int64)
        off = np.array([i for i, v in core if not v], dtype=np.int64)
        self.on_count[on] += 1
        self.off_count[off] += 1
        self.cores.append((on, off))


SCALAR_STATS = ("fanout", "cone_nodes", "cone_frac", "cone_mux", "cone_arith", "sinks", "sinks_frac",
                "sink_dist", "component", "module_depth")


def set_features(t: SelectTable, on: np.ndarray, h: History, prev_on: np.ndarray | None) -> dict[str, float]:
    """Features of one candidate. `on` is a boolean array over the selects; `prev_on` is
    the previous candidate's, or None on the first."""
    n = len(t.selects)
    k = int(on.sum())
    f: dict[str, float] = {"n_selects": n, "n_on": k, "frac_on": k / n if n else 0.0}

    col = {c: i for i, c in enumerate(t.columns)}
    for name in SCALAR_STATS:
        v = t.X[on, col[name]]
        f[f"{name}_mean"] = float(v.mean()) if k else 0.0
        f[f"{name}_max"] = float(v.max()) if k else 0.0
    for name in t.columns:
        if name.startswith(("family_", "variant_")):
            f[f"frac_{name}"] = float(t.X[on, col[name]].sum() / k) if k else 0.0

    # interaction: how crowded the sinks are with this candidate's selects
    crowd = [int(on[s].sum()) for s in t.sink_lists]
    shared = [c for c in crowd if c >= 2]
    f["sinks_shared"] = len(shared)
    f["sink_crowd_max"] = max(crowd, default=0)
    f["sink_crowd_mean"] = float(np.mean(shared)) if shared else 0.0
    f["sink_pairs"] = sum(c * (c - 1) // 2 for c in shared)
    touched = np.bincount(t.comp_of[on], minlength=t.n_components) if k else np.zeros(t.n_components)
    f["components_touched"] = int((touched > 0).sum())
    f["n_components"] = t.n_components
    f["component_crowd_max"] = float(touched.max()) if t.n_components else 0.0

    # conflict history
    f["n_cores"] = len(h.cores)
    v_on, v_off = h.on_count[on], h.off_count[~on]
    f["core_on_sum"] = float(v_on.sum())
    f["core_on_max"] = float(v_on.max()) if k else 0.0
    f["frac_on_seen_in_core"] = float((v_on > 0).sum() / k) if k else 0.0
    f["core_off_sum"] = float(v_off.sum())
    # A candidate never contains a whole learned core, but how close it comes is informative.
    best, near = 0.0, 0
    for c_on, c_off in h.cores:
        size = len(c_on) + len(c_off)
        hit = int(on[c_on].sum()) + int((~on[c_off]).sum())
        best = max(best, hit / size)
        near += hit == size - 1
    f["core_closest"] = best
    f["core_near_misses"] = near
    f["hamming_prev"] = int((on != prev_on).sum()) if prev_on is not None else -1
    return f
