"""
Pair the X sources of a flattened golden/muxed miter the way Jasper SEC does.

Jasper treats every X source (an 'x literal, a case default 'x, an out-of-range
select, a division by zero) as a free value, and `check_sec -setup` auto-maps
the spec's X sources onto the imp's by the signal they are assigned to: a
mapped pair is one shared value, an X with no partner is free on its own. So
golden == golden, and replacing an X with any constant is a counterexample
(measured 2026-10-23 with papercuts' own pcjg.tcl). yosys instead gives every
constant X bit its own anonymous BTOR2 input, so golden != golden whenever an
X is observable, and models division by zero as all-ones, with no X at all.

`pair_x`, run on the flattened miter just before write_btor, makes the netlist
say what Jasper means:

  1. every $div/$mod/$divfloor/$modfloor becomes `B == 0 ? 'x : result`
  2. every constant-X bit, and every wire bit nothing drives (Jasper's
     "undriven" X sources, auto-mapped by name like the rest), is keyed by the
     first bits it reaches, through the logic, of a signal BOTH copies have
     (instance prefix stripped); an undriven bit of such a signal is its own key
  3. one X in `orig` and one in `mod` with the same key share one new input;
     an X with no partner, or one of several with the same key, gets an input
     of its own; an X that opt_merge had already shared between the copies
     stays shared

Keys skip names only one copy carries. The muxed copy's `<sig>_papercuts`
companions are plain aliases of `<sig>` on their untouched bits, so keying on
them would leave every X near a bit-mux unpaired; Jasper keys on the signal the
X is assigned to, which a cut never renames. The cost: an X re-assigned to a
renamed signal pairs here and not in Jasper -- which a golden/muxed pair cannot
contain, the two being one source. Ambiguous keys never pair (each X goes free),
which can only make the loop reject more, never accept what Jasper rejects.

After the pass no constant X is left for write_btor to turn into an anonymous
input; one that is left is an error.
"""

from __future__ import annotations

import re
from array import array
from collections import defaultdict
from dataclasses import dataclass, field

from pyosys import libyosys as ys

from papercuts.cegis.flat import X, Flat

DIV_CELLS = {"$div", "$mod", "$divfloor", "$modfloor"}
# Cells where input bit i reaches only output bit i (when the widths agree).
BITWISE = {"$mux": ("A", "B"), "$bwmux": ("A", "B"), "$and": ("A", "B"), "$or": ("A", "B"),
           "$xor": ("A", "B"), "$xnor": ("A", "B"), "$not": ("A",), "$pos": ("A",),
           # flip-flops: D[i] -> Q[i] (build_btor's dffunmap leaves $ff / $dff)
           "$ff": ("D",), "$dff": ("D",), "$dffe": ("D",), "$sdff": ("D",), "$sdffe": ("D",), "$sdffce": ("D",)}
X_INPUT = "__x"  # prefix of the inputs the pass adds


class XPairingError(RuntimeError):
    pass


@dataclass
class XReport:
    divs: int = 0  # division/remainder cells given an X on a zero divisor
    bits: int = 0  # constant-X bits found
    paired: int = 0  # orig/mod pairs given one shared input
    merged: int = 0  # X bits opt_merge had already shared between the copies
    unpaired: list[str] = field(default_factory=list)  # "<copy>: <key>", free on their own
    ambiguous: list[str] = field(default_factory=list)  # keys with several X in a copy
    unkeyed: int = 0  # reach no signal both copies have
    undriven: int = 0  # of `bits`: wire bits nothing drives
    inputs: int = 0  # inputs added
    # set by netlist.build_btor, not by pair_x: top inputs reaching a clock,
    # asynchronous control or latch enable (netlist.control_inputs)
    control_inputs: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.bits} X bits ({self.undriven} undriven): {self.paired} paired, {self.merged} already shared, "
                f"{len(self.unpaired)} unpaired, {len(self.ambiguous)} ambiguous keys, "
                f"{self.unkeyed} unkeyed; {self.divs} divisions given an X on zero")


def _rewrite_divs(m, rep: XReport) -> None:
    for cell in list(m.cells_.values()):
        if cell.type.str() not in DIV_CELLS:
            continue
        rep.divs += 1
        tag = f"{rep.divs}_{cell.name.str().lstrip(chr(92) + '$').replace('.', '_')}"
        y = cell.getPort(ys.IdString("\\Y"))
        raw = m.addWire(ys.IdString(f"$xdiv_raw_{tag}"), y.size())
        zero = m.addWire(ys.IdString(f"$xdiv_zero_{tag}"), 1)
        cell.setPort(ys.IdString("\\Y"), ys.SigSpec(raw))
        m.addLogicNot(ys.IdString(f"$xdiv_not_{tag}"), cell.getPort(ys.IdString("\\B")), ys.SigSpec(zero))
        xs = ys.SigSpec([ys.SigBit(ys.State.Sx)] * y.size())
        m.addMux(ys.IdString(f"$xdiv_mux_{tag}"), ys.SigSpec(raw), xs, ys.SigSpec(zero), y)


def _bits(sig) -> list:
    return list(sig.to_sigbit_vector())


def _is_x(b) -> bool:
    return b.wire is None and b.data == ys.State.Sx


def _fold(starts, succ, own, join, val: dict) -> None:
    """Fill `val[v]` for every node reachable from `starts`: own(v) joined with
    the value of every successor, around cycles too (registers make them).

    Tarjan's strongly connected components, iterative: a component closes only
    after every component it reaches, so its members share one value, built from
    their own values and the values across their edges out of it. A node already
    in `val` is not entered again, so one graph walk serves every X source. A walk
    per X source cost X sources x netlist (koios tpu: over an hour, still going;
    runs ISSUES.md 119)."""
    index, low, acc, stack = {}, {}, {}, []
    count = 0
    for s in starts:
        if s in val:
            continue
        index[s] = low[s] = count
        count += 1
        acc[s] = own(s)
        stack.append(s)
        work = [(s, iter(succ(s)))]
        while work:
            v, it = work[-1]
            for w in it:
                if w in val:
                    acc[v] = join(acc[v], val[w])
                elif w not in index:
                    index[w] = low[w] = count
                    count += 1
                    acc[w] = own(w)
                    stack.append(w)
                    work.append((w, iter(succ(w))))
                    break
                elif index[w] < low[v]:  # on the stack: same component
                    low[v] = index[w]
            else:
                work.pop()
                if low[v] < index[v]:
                    u = work[-1][0]
                    low[u] = min(low[u], low[v])
                    continue
                members, total = [], None
                while True:
                    w = stack.pop()
                    members.append(w)
                    total = acc.pop(w) if total is None else join(total, acc.pop(w))
                    del index[w], low[w]
                    if w == v:
                        break
                for w in members:
                    val[w] = total
                if work:
                    u = work[-1][0]
                    acc[u] = join(acc[u], total)


_EMPTY = frozenset()


def _union(a: frozenset, b: frozenset) -> frozenset:
    if not b or b <= a:
        return a
    if not a or a <= b:
        return b
    return a | b


def pair_x(design, prefixes: tuple[str, str] = ("orig", "mod")) -> XReport:
    rep = XReport()
    m = design.top_module()
    _rewrite_divs(m, rep)

    # ---- snapshot the netlist once; everything below reads integers (flat.Flat)
    f = Flat(m)
    root, cells, conns, n = f.root, f.cells, f.conns, f.n

    occ = []  # ("cell", cell_idx, port, i) | ("conn", conn_idx, i) | ("undriven", bit)
    start = {}  # occurrence -> the bits it drives first
    for ci, (lhs, rhs) in enumerate(conns):
        for i, (lb, rb) in enumerate(zip(lhs, rhs)):
            if rb == X:
                occ.append(("conn", ci, i))
                start[occ[-1]] = [lb]

    def outputs(idx, port, i):
        _, t, ins, outs = cells[idx]
        if t in BITWISE and port.lstrip("\\") in BITWISE[t]:
            y = next(iter(outs.values()))
            if len(ins[port]) == len(y):
                return [y[i]]
        if t == "$pmux" and port in ("\\A", "\\B"):
            y = outs["\\Y"]
            return [y[i % len(y)]]
        return [b for bits in outs.values() for b in bits]

    # The graph X sources are traced through. Nodes: alias classes (0..n-1) and
    # cells (n + cell index). An input bit is an edge from its class to the one
    # output bit it reaches (`outputs`), or else to its cell, which fans out to
    # all the cell's outputs: wide cells cost inputs + outputs edges, not their
    # product. Edges are stored by class in two arrays (`off`, `to`).
    def edge(idx, port, i):
        _, t, ins, outs = cells[idx]
        if t in BITWISE and port.lstrip("\\") in BITWISE[t]:
            y = next(iter(outs.values()))
            if len(ins[port]) == len(y):
                return root[y[i]] if y[i] >= 0 else None
        if t == "$pmux" and port in ("\\A", "\\B"):
            y = outs["\\Y"]
            b = y[i % len(y)]
            return root[b] if b >= 0 else None
        return n + idx

    e_from, e_to = array("q"), array("q")
    for idx, (_, _, ins, _) in enumerate(cells):
        for port, bits in ins.items():
            for i, b in enumerate(bits):
                if b >= 0:
                    t = edge(idx, port, i)
                    if t is not None:
                        e_from.append(root[b])
                        e_to.append(t)
                elif b == X:
                    occ.append(("cell", idx, port, i))
                    start[occ[-1]] = outputs(idx, port, i)
    off = array("q", bytes(8 * (n + 1)))
    for r in e_from:
        off[r + 1] += 1
    for r in range(n):
        off[r + 1] += off[r]
    to, at = array("q", bytes(8 * len(e_to))), array("q", off)
    for r, t in zip(e_from, e_to):
        to[at[r]] = t
        at[r] += 1
    del e_from, e_to, at

    def succ(v):
        if v < n:
            return to[off[v]:off[v + 1]]
        return [root[b] for bits in cells[v - n][3].values() for b in bits if b >= 0]

    # Undriven wire bits: no cell drives them, no connection gives them a value,
    # and they are not an input port. One occurrence per alias class, starting
    # at the class itself so a signal both copies have keys it by name.
    driven = bytearray(n)
    for _, _, _, outs in cells:
        for bits in outs.values():
            for b in bits:
                if b >= 0:
                    driven[root[b]] = 1
    for lhs, rhs in conns:
        for lb, rb in zip(lhs, rhs):
            if rb < 0:
                driven[root[lb]] = 1
    for _, first, width in f.inputs:
        for i in range(width):
            driven[root[first + i]] = 1
    undriven = {}
    for b in range(n):
        r = root[b]
        if not driven[r]:
            undriven[r] = b
            driven[r] = 1
    for b in undriven.values():
        occ.append(("undriven", b))
        start[occ[-1]] = [b]
    rep.undriven = len(undriven)

    copies = defaultdict(set)  # name without the instance prefix -> copies that have it
    for name in f.names:
        head, _, rest = name[1:].partition(".")
        if name.startswith("\\") and head in prefixes and rest:
            copies[rest].add(head)
    shared = [r for r, c in copies.items() if len(c) == len(prefixes)]
    sid = {r: k for k, r in enumerate(shared)}
    # By alias class: the copies any public name of it belongs to (bit 1: the
    # first prefix, 2: the second), and the (shared name, offset) keys it carries,
    # one in `key` (-1: none), any further ones in `more`. A key is an int,
    # shared-name index << 32 | offset.
    a_name, b_name = prefixes
    copy_bit = {a_name: 1, b_name: 2}
    members = bytearray(n + len(cells))
    key = array("q", [-1]) * n
    more = defaultdict(set)
    for k, name in enumerate(f.names):
        if not name.startswith("\\"):
            continue
        head, _, rest = name[1:].partition(".")
        if head not in prefixes or not rest:
            continue
        s, c, first = sid.get(rest), copy_bit[head], f.base[k]
        for i in range(f.widths[k]):
            r = root[first + i]
            members[r] |= c
            if s is not None:
                kv = s << 32 | i
                if key[r] < 0:
                    key[r] = kv
                elif key[r] != kv:
                    more[r].add(kv)

    def keys(v):
        if v >= n or key[v] < 0:
            return _EMPTY
        return frozenset((key[v], *more[v])) if v in more else frozenset((key[v],))

    def succ_to_key(v):
        return () if v < n and key[v] >= 0 else succ(v)

    # Per class, what an X arriving there reaches: the copies, counted past the
    # first keyed bits so an X opt_merge already shared between orig and mod is
    # recognised as shared; and the first keyed bits on every path.
    starts = [root[b] for o in occ for b in start[o] if b >= 0]
    sides_at, keys_at = {}, {}
    _fold(starts, succ, members.__getitem__, int.__or__, sides_at)
    _fold(starts, succ_to_key, keys, _union, keys_at)

    groups = defaultdict(lambda: defaultdict(list))  # key -> copy | "both" | "none" -> [occurrence]
    for o in occ:
        hits, sides = _EMPTY, 0
        for b in start[o]:
            if b >= 0:
                hits = _union(hits, keys_at[root[b]])
                sides |= sides_at[root[b]]
        k = frozenset((shared[kv >> 32], kv & 0xFFFFFFFF) for kv in hits)
        side = "both" if sides == 3 else a_name if sides == 1 else b_name if sides == 2 else "none"
        groups[k]["none" if not k and side != "both" else side].append(o)
    rep.bits = len(occ)

    # A key is the set of shared signals an X reaches first, and a copy can carry
    # an alias the other lacks: muxing moves a read onto a companion wire, so the
    # muxed copy's X reaches `{u0.K_sub[0], u1.K_sub[0]}` where the golden one
    # reaches `{K_sub[0], u0.K_sub[0], u1.K_sub[0]}` (opencores des; hls seidel_2d
    # A_address1). A group present in one copy only is the same X sources as the
    # groups its key overlaps: each connected component of the overlap around
    # such a group (one to one, or several on a side, opencores nextz80) becomes
    # one group, paired or tied below like any other. Groups that already have
    # both copies are only drawn in by a one-copy group overlapping them.
    one = [(k, c) for k, by in groups.items() for c in (a_name, b_name)
           if k and by.get(c) and not by.get("both") and not by.get(b_name if c == a_name else a_name)]
    lone = {k for k, _ in one}
    two = [k for k, by in groups.items() if k and k not in lone and by.get(a_name) and by.get(b_name)]
    parent = {k: k for k in list(lone) + two}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for ka, ca in one:
        for kb, cb in one:
            if ca == a_name and cb == b_name and ka & kb:
                parent[root(ka)] = root(kb)
        for kb in two:
            if ka & kb:
                parent[root(ka)] = root(kb)
    comps = defaultdict(list)
    for k in parent:
        comps[root(k)].append(k)
    for keys in comps.values():
        if len(keys) < 2 or not any(k in lone for k in keys):
            continue
        merged = defaultdict(list)
        for k in keys:
            for c, os_ in groups.pop(k).items():
                merged[c].extend(os_)
        groups[frozenset().union(*keys)] = merged

    # ---- one input per X source
    subst = {}  # occurrence -> SigBit of its input

    def new_input():
        w = m.addWire(ys.IdString(f"\\{X_INPUT}{rep.inputs}"), 1)
        w.port_input = True
        rep.inputs += 1
        return ys.SigBit(w, 0)

    def describe(k):
        return ", ".join(f"{n}[{i}]" for n, i in sorted(k)) or "(no signal both copies have)"

    def structural_name(o):
        """The X's own name with the copy and yosys's running counter removed, so
        the same construct in both copies matches: undriven bits of memory_map's
        out-of-range words (opencores wb_lcd: 61 per output bit) are
        `$memory\\orig.lcd.ram$rdmux[0][1][0]$b$2344` and the same with
        `mod.` and another counter."""
        if o[0] == "undriven":
            name, off = f.name_of(o[1])
        elif o[0] == "conn":
            name, off = f.name_of(start[o][0]) if start[o][0] >= 0 else (None, None)
        else:
            name, off = f"{cells[o[1]][0].name.str()}{o[2]}", o[3]
        if name is None:
            return None
        for pre in prefixes:
            name = name.replace(f"\\{pre}.", "\\").replace(f"${pre}.", "$")
        return (re.sub(r"\$\d+$", "", name), off)

    for k, by in groups.items():
        for o in by.get("both", []):
            subst[o] = new_input()
            rep.merged += 1
        for o in by.get("none", []):
            subst[o] = new_input()
            rep.unkeyed += 1
        a, b = by.get(a_name, []), by.get(b_name, [])
        if len(a) == 1 and len(b) == 1:
            subst[a[0]] = subst[b[0]] = new_input()
            rep.paired += 1
            continue
        if a and b:
            # Several X per copy behind one key: pair those whose structural
            # names match one to one (structural_name), leave the rest free.
            ka, kb = defaultdict(list), defaultdict(list)
            for o in a:
                ka[structural_name(o)].append(o)
            for o in b:
                kb[structural_name(o)].append(o)
            left = []
            for sk in set(ka) | set(kb):
                if sk is not None and len(ka.get(sk, [])) == 1 and len(kb.get(sk, [])) == 1:
                    subst[ka[sk][0]] = subst[kb[sk][0]] = new_input()
                    rep.paired += 1
                else:
                    left += ka.get(sk, []) + kb.get(sk, [])
            # The rest share ONE value. A copy may have merged X sources the other
            # keeps apart: slang's front end folds a case's `'x` arm into the
            # `'x` default (one X), while the muxed copy, its case an if-chain,
            # keeps both (hls floyd_warshall, nussinov, trisolv, seidel: every
            # cut off differed). Golden then equals the muxed copy only if all of
            # them are one value. Jasper may keep them apart; anything that leans
            # on the tie fails the joint check, loudly.
            if left:
                rep.ambiguous.append(f"{describe(k)}: {len(left)} of {len(a) + len(b)} X tied to one value")
                shared = new_input()
                for o in left:
                    subst[o] = shared
            continue
        else:
            rep.unpaired += [f"{a_name if a else b_name}: {describe(k)}"] * len(a + b)
        for o in a + b:
            subst[o] = new_input()

    # ---- rewrite the netlist
    by_port = defaultdict(dict)  # (cell_idx, port) -> {i: SigBit}
    by_conn = defaultdict(dict)  # conn_idx -> {i: SigBit}
    drive = []  # (wire name, offset, SigBit): undriven bits given their input
    for o, nb in subst.items():
        if o[0] == "cell":
            by_port[(o[1], o[2])][o[3]] = nb
        elif o[0] == "conn":
            by_conn[o[1]][o[2]] = nb
        else:
            drive.append((*f.name_of(o[1]), nb))
    for (idx, port), repl in by_port.items():
        cell = cells[idx][0]
        pid = ys.IdString(port)
        bits = _bits(cell.getPort(pid))
        cell.setPort(pid, ys.SigSpec([repl.get(i, b) for i, b in enumerate(bits)]))
    if by_conn:
        new = []
        for ci, (lhs, rhs) in enumerate(m.connections_):
            if ci in by_conn:
                rhs = ys.SigSpec([by_conn[ci].get(i, b) for i, b in enumerate(_bits(rhs))])
            new.append((lhs, rhs))
        m.new_connections(new)
    for name, off, nb in drive:
        m.connect(ys.SigSpec(m.wire(ys.IdString(name)), off, 1), ys.SigSpec([nb]))
    m.fixup_ports()

    left = sorted({c.name.str() for c in m.cells_.values() for p, s in c.connections_.items()
                   if c.input(p) and any(_is_x(b) for b in _bits(s))})
    left += [f"connection {ci}" for ci, (_, rhs) in enumerate(m.connections_)
             if any(_is_x(b) for b in _bits(rhs))]
    if left:
        raise XPairingError(f"constant X left after pairing, in: {left[:10]}")
    return rep
