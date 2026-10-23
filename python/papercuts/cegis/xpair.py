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
from collections import defaultdict
from dataclasses import dataclass, field

from pyosys import libyosys as ys

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


X = "x"  # a constant-X bit in a snapshot


def _snap(b):
    """A SigBit as plain data: (wire, offset), X, or None for any other constant."""
    if b.wire is not None:
        return (b.wire.name.str(), b.offset)
    return X if b.data == ys.State.Sx else None


def pair_x(design, prefixes: tuple[str, str] = ("orig", "mod")) -> XReport:
    rep = XReport()
    m = design.top_module()
    _rewrite_divs(m, rep)

    # ---- snapshot the netlist once; everything below reads plain Python data
    cells = []  # (cell, type, {port: [bit]}, {port: [bit]})
    for cell in m.cells_.values():
        ins, outs = {}, {}
        for port, sig in cell.connections_.items():
            (ins if cell.input(port) else outs)[port.str()] = [_snap(b) for b in _bits(sig)]
        cells.append((cell, cell.type.str(), ins, outs))
    conns = [([_snap(b) for b in _bits(lhs)], [_snap(b) for b in _bits(rhs)]) for lhs, rhs in m.connections_]
    wires = [(w.name.str(), w.width) for w in m.wires_.values()]
    inputs = {w.name.str() for w in m.wires_.values() if w.port_input}

    parent: dict[tuple, tuple] = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    occ = []  # ("cell", cell_idx, port, i) | ("conn", conn_idx, i)
    start = {}  # occurrence -> the bits it drives first
    for ci, (lhs, rhs) in enumerate(conns):
        for i, (lb, rb) in enumerate(zip(lhs, rhs)):
            if isinstance(rb, tuple):
                parent[find(lb)] = find(rb)
            elif rb is X:
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

    readers = defaultdict(list)  # alias root -> [(cell_idx, port, i)]
    for idx, (_, _, ins, _) in enumerate(cells):
        for port, bits in ins.items():
            for i, b in enumerate(bits):
                if isinstance(b, tuple):
                    readers[find(b)].append((idx, port, i))
                elif b is X:
                    occ.append(("cell", idx, port, i))
                    start[occ[-1]] = outputs(idx, port, i)

    # Undriven wire bits: no cell drives them, no connection gives them a value,
    # and they are not an input port. One occurrence per alias class, starting
    # at the class itself so a signal both copies have keys it by name.
    driven = set()
    for _, _, _, outs in cells:
        driven.update(find(b) for bits in outs.values() for b in bits if isinstance(b, tuple))
    for lhs, rhs in conns:
        driven.update(find(lb) for lb, rb in zip(lhs, rhs) if not isinstance(rb, tuple))
    for name, width in wires:
        if name in inputs:
            driven.update(find((name, i)) for i in range(width))
    undriven = {}
    for name, width in wires:
        for i in range(width):
            r = find((name, i))
            if r not in driven:
                undriven.setdefault(r, (name, i))
    for r, bit in undriven.items():
        occ.append(("undriven", bit))
        start[occ[-1]] = [bit]
    rep.undriven = len(undriven)

    copies = defaultdict(set)  # name without the instance prefix -> copies that have it
    for name, _ in wires:
        head, _, rest = name[1:].partition(".")
        if name.startswith("\\") and head in prefixes and rest:
            copies[rest].add(head)
    shared_names = {n for n, c in copies.items() if len(c) == len(prefixes)}
    keyed = defaultdict(list)  # alias root -> [(copy, name, offset)] of signals both copies have
    members = defaultdict(list)  # alias root -> copies any public name of it belongs to
    for name, width in wires:
        if not name.startswith("\\"):
            continue
        head, _, rest = name[1:].partition(".")
        if head not in prefixes or not rest:
            continue
        for i in range(width):
            r = find((name, i))
            members[r].append(head)
            if rest in shared_names:
                keyed[r].append((head, rest, i))

    def reach(bits):
        """(first keyed bits on every path, every copy the X reaches at all).
        The copies are counted past the first keyed bits, so an X that
        opt_merge already shared between orig and mod is recognised as shared."""
        seen, found, sides = set(), set(), set()
        todo = [(find(b), True) for b in bits if isinstance(b, tuple)]
        while todo:
            r, first = todo.pop()
            if (r, first) in seen:
                continue
            seen.add((r, first))
            sides.update(members.get(r, ()))
            hits = keyed.get(r)
            if hits and first:
                found.update(hits)
            for idx, port, i in readers.get(r, ()):
                todo.extend((find(b), first and not hits) for b in outputs(idx, port, i) if isinstance(b, tuple))
        return found, sides

    groups = defaultdict(lambda: defaultdict(list))  # key -> copy | "both" | "none" -> [occurrence]
    for o in occ:
        hits, sides = reach(start[o])
        k = frozenset((h[1], h[2]) for h in hits)
        side = "both" if len(sides) > 1 else (next(iter(sides)) if sides else "none")
        groups[k]["none" if not k and side != "both" else side].append(o)
    rep.bits = len(occ)

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
            name, off = o[1]
        elif o[0] == "conn":
            name, off = start[o][0] if isinstance(start[o][0], tuple) else (None, None)
        else:
            name, off = f"{cells[o[1]][0].name.str()}{o[2]}", o[3]
        if name is None:
            return None
        for pre in prefixes:
            name = name.replace(f"\\{pre}.", "\\").replace(f"${pre}.", "$")
        return (re.sub(r"\$\d+$", "", name), off)

    a_name, b_name = prefixes
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
            if left:
                rep.ambiguous.append(f"{describe(k)}: {len(left)} of {len(a) + len(b)} X left free")
            for o in left:
                subst[o] = new_input()
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
            drive.append((o[1][0], o[1][1], nb))
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
                   if c.input(p) and any(_snap(b) is X for b in _bits(s))})
    left += [f"connection {ci}" for ci, (_, rhs) in enumerate(m.connections_)
             if any(_snap(b) is X for b in _bits(rhs))]
    if left:
        raise XPairingError(f"constant X left after pairing, in: {left[:10]}")
    return rep
