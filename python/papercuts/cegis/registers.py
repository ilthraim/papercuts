"""
Registers of the two copies in the miter: which RTL registers there are, how
they pair up across golden and muxed, and what they hold at power-up.

    regs, skipped = register_paths(files, top, workdir)      # RTL side, via yosys
    paired, dropped = matched_registers(golden_regs, muxed_regs)
    pairs, unmatched = match_registers(design, [p for p, _ in paired])   # BTOR2 side
    correspondence(frame, pairs, design)                     # R: every pair equal
    power_up(design, paired).constraints(frame, design)      # ROMs fixed, pairs equal

Copied from smt_ec (src/portfolio.py: RegPair, match_registers, PowerUp,
rom_values, power_up, correspondence; src/certify_cuts.py: register_paths,
matched_registers), rewriting repo 12a5a7f + working tree, 2026-10-23, and
trimmed to what the combinational loop uses: no reset analysis (the loop never
asserts a reset) and no RTL rendering for sby or Jasper. The two copies are
maintained separately.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

import z3

from papercuts.cegis.btor2z3 import BV, Design, Frame, _conj


# ---------------------------------------------------------------- RTL side
# Yosys flip-flop cells, enumerated rather than matched on a substring so a new
# cell type shows up as a missing register (reported) instead of a silent match.
FF_CELL_TYPES = frozenset(
    "$dff $dffe $adff $adffe $aldff $aldffe $sdff $sdffe $sdffce $dffsr $dffsre "
    "$dlatch $adlatch $dlatchsr $ff".split()
)


class Registers(dict):
    """{name: width} of a design's registers, plus `aliases`: every public name
    the register's output net carries (the same bits), for pairing across the
    two copies (matched_registers)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.aliases: dict[str, frozenset[str]] = {}
        self.nets: dict[str, int] = {}  # every public net: name -> width


def register_paths(sv_files: list[str], top: str, workdir: str, tag: str = "",
                   slang_args: tuple[str, ...] = ()) -> tuple[Registers, list[str]]:
    """Every register in `top`'s flattened hierarchy, as {path: width}, plus the
    ones that had to be skipped.

    Taken from the RTL rather than from BTOR2 state names. `async2sync` replaces
    each asynchronous flip-flop with an auxiliary register of its own naming, so
    after that pass nothing in the BTOR2 relates to `mod.x` / `orig.x` -- on xtea
    it leaves 24 states and zero matchable names. The RTL names survive, and the
    golden and muxed copies share them by construction, being rewrites of one
    source.

    Memories are mapped to registers first (`memory_map`, as the BTOR2 front end
    does), so each word is a register named `<mem>[<i>]`, the name its BTOR2
    state carries. Without that the words were never paired and started apart
    in the two copies (opencores cr_div: MiterMismatch with every cut off). Names
    are only looked up in the BTOR2, never written into SystemVerilog, so any
    public name is usable.

    One net can carry several names once the hierarchy is flattened: a child's
    output port, the parent wire it drives and the next child's input port are
    one net. Which of those the register is listed under is arbitrary, and the
    golden and muxed copies can differ in which names a net carries at all
    (muxing moves a child's input onto a companion wire), so every alias is
    kept in `Registers.aliases` for matched_registers.
    """
    out = os.path.join(workdir, f"regs_{tag or top}.json")
    from papercuts.cegis.netlist import PREP, script_file
    # the miter's own `prep` (netlist.PREP): the same flip-flops survive here
    script = (f"plugin -i slang; read_slang {' '.join(sv_files)} --top {top} {' '.join(slang_args)}; "
              f"{PREP.format(top=top)}; memory_map; write_json {out}")
    r = subprocess.run(["yosys", "-q", "-s", script_file(script, os.path.join(workdir, f"regs_{tag or top}.ys"))],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"yosys failed listing registers of {top}:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    with open(out) as f:
        j = json.load(f)
    m = j["modules"][top]
    owner: dict[int, str] = {}
    by_bits: dict[tuple, list[str]] = {}  # identical bit vectors: one net, several names
    regs = Registers()
    for name, w in m["netnames"].items():
        if w.get("hide_name"):
            continue
        regs.nets[name] = len(w["bits"])
        for b in w["bits"]:
            owner.setdefault(b, name)
        by_bits.setdefault(tuple(w["bits"]), []).append(name)

    skipped: list[str] = []
    for cell in m["cells"].values():
        if cell["type"] not in FF_CELL_TYPES:
            continue
        q = cell["connections"].get("Q", [])
        # Name the register by the nets that ARE its output, bit for bit. Only when
        # no net matches Q exactly (several cells write one wire) is each bit named
        # by its first owner. Owners alone mislead when another wire shares most of
        # the bits: in opencores osdvu `assign rx_byte = rx_data` made the golden
        # register "rx_byte", while in the muxed copy rx_byte reads the companion
        # and differs at bit 7, so the golden register was paired with a wire and
        # the muxed rx_data started free (MiterMismatch with every cut off).
        # One register per cell, named canonically; its other names are aliases.
        exact = by_bits.get(tuple(q))
        if exact:
            names = {min(exact, key=lambda n: (n.endswith("_papercuts"), len(n), n))}
        else:
            names = {owner.get(b) for b in q}
        names.discard(None)
        if not names:
            skipped.append(f"{cell['type']} drives no public wire")
            continue
        for n in names:
            # The whole wire is the register; comparing all of it is what R means,
            # even when a cell drives only part of it.
            width = len(m["netnames"][n]["bits"])
            if regs.get(n, width) != width:
                skipped.append(f"{n} (conflicting widths)")
                continue
            regs[n] = width
            regs.aliases[n] = frozenset(by_bits.get(tuple(m["netnames"][n]["bits"]), [n]))
    return regs, sorted(set(skipped))


def matched_registers(golden: dict[str, int], muxed: dict[str, int]) -> tuple[list[tuple[str, int]], list[str]]:
    """Registers present on both sides with the same width, and what was dropped.

    A golden and a muxed register are the same register when their output nets
    share a name (`Registers.aliases`; a plain dict has one name per register).
    The pair is named by a name both sides carry -- the golden one if the muxed
    net carries it too -- so `orig.<name>` and `mod.<name>` both exist. Matching
    on the single listed name failed on scalable_arbiter__demo_filters: the
    golden net of `msec_pulser.out` also carries `debouncer.enable` and was
    listed under it, while muxing had moved `debouncer.enable` onto a companion
    wire, so the muxed net was listed as `msec_pulser.out`; the unpaired
    registers then started independent in the two copies."""
    def aliases(regs, n):
        return getattr(regs, "aliases", {}).get(n, frozenset([n]))

    owner_m = {}  # muxed alias -> muxed register
    for n in muxed:
        for a in aliases(muxed, n):
            owner_m.setdefault(a, n)
    pairs, dropped, used = [], [], set()
    for g in sorted(golden):
        ga = aliases(golden, g)
        cands = sorted({owner_m[a] for a in ga if a in owner_m} - used)
        if not cands:
            dropped.append(f"{g} (golden only)")
            continue
        mname = next((c for c in cands if muxed[c] == golden[g]), None)
        if mname is None:
            dropped.append(f"{g} ({golden[g]} bits golden, {muxed[cands[0]]} muxed)")
            used.add(cands[0])
            continue
        used.add(mname)
        common = ga & aliases(muxed, mname)
        name = g if g in common else (mname if mname in common else sorted(common)[0])
        pairs.append((name, golden[g]))
    # A register in one copy only, where the other copy has a public net of that
    # name and width: the other copy's flip-flop may sit behind that net under no
    # name of its own (opencores rtfsimpleuart: golden rxdsum's state is
    # anonymous). The name is put forward; match_registers pairs it only where
    # flip-flops stand behind it in both copies and reports it unmatched
    # otherwise.
    paired = {n for n, _ in pairs}
    for n in sorted(set(muxed) - used):
        if getattr(golden, "nets", {}).get(n) == muxed[n] and n not in paired:
            pairs.append((n, muxed[n]))
        else:
            dropped.append(f"{n} (muxed only)")
    rescued = [d for d in dropped if d.endswith("(golden only)")
               and getattr(muxed, "nets", {}).get(d.split(" ")[0]) == golden.get(d.split(" ")[0])]
    for d in rescued:
        dropped.remove(d)
        pairs.append((d.split(" ")[0], golden[d.split(" ")[0]]))
    return pairs, dropped


# ---------------------------------------------------------------- BTOR2 side
@dataclass(frozen=True)
class Side:
    """One copy's half of a register pair: bits [lo, lo+width) of `state`."""

    state: str
    lo: int
    width: int

    def _bits(self, v: BV) -> BV:
        return v if self.lo == 0 and v.size() == self.width else z3.Extract(self.lo + self.width - 1, self.lo, v)

    def value(self, fr: Frame) -> BV:
        return self._bits(fr.states[self.state])

    def next_value(self, fr: Frame) -> BV:
        return self._bits(fr.next[self.state])


@dataclass(frozen=True)
class RegPair:
    """Flip-flop bits of one RTL register in the golden copy and the same bits in
    the muxed copy: what Jasper maps to each other at power-up, by name.

    R may equate nothing else. A register's BTOR2 name labels its output net,
    and that net is not always a plain read of its flip-flops: yosys can turn an
    array into one wide wire with some words combinational (opencores
    video_systems qnr: `s_pipe[0] = z`). Equating such a net equated the logic in
    front of it, cuts included, so R assumed what the cuts change: a bit-shrink
    of `iz`, which feeds `s_pipe[0]`, became provable, and the joint Jasper check
    failed from power-up. So pairs are made bit by bit from the states behind
    the net (`_flop_bits`), and a bit that is not a flip-flop in both copies is
    left out of R.

    There is no flip-flop-to-constant pair. One copy's flip-flop facing a
    constant in the other means yosys picked that flip-flop's power-up value
    (opt_dff, now kept off by `opt -keepdc`); R holding the other copy's
    flip-flop to the constant assumed that value, and Jasper starts both free
    (hls nussinov: a 2-cycle cex through ap_done_reg; ISSUES.md 64). Such a bit
    is reported unmatched instead."""

    name: str  # the register, without the mod./orig. prefix; [hi:lo] when partial
    mod: Side
    orig: Side

    @property
    def kind(self) -> str:
        return "state"

    def states(self) -> set[str]:
        return {self.mod.state, self.orig.state}

    def value(self, fr: Frame, design: Design | None = None) -> tuple[BV, BV]:
        return self.mod.value(fr), self.orig.value(fr)

    def next_value(self, fr: Frame) -> tuple[BV, BV]:
        return self.mod.next_value(fr), self.orig.next_value(fr)


def _bare(name: str, prefixes: tuple[str, str]) -> str:
    for p in prefixes:
        if name.startswith(p):
            return name[len(p):]
    return name


def _flop_bits(design: Design, nid: int, own: str, others: set[str], selects: set[str],
               prefixes: tuple[str, str]) -> list:
    """Per bit of node `nid`, LSB first: (state, bit) when the bit is a plain read
    of a flip-flop of register `own`, an int when it is a constant, else None.

    Peeled: concat, slice, zero extension, and a mux no select steers, where
    exactly one arm is a flip-flop bit (async2sync puts one in front of every
    asynchronous flip-flop, `arst ? value : q`, and every latch, `en ? d : q`).
    A select-steered mux is a cut and is not peeled, so whatever is reached is
    the same flip-flop in both copies -- except async2sync's own mux, an arm of
    which IS the state async2sync made: it is that flip-flop's storage whatever
    steers the mux, and cuts on a reset expression do steer it (opencores cop,
    ptc: every async register was lost before this exception). A state named as another RTL register
    (`others`) is that register's flip-flop, read combinationally, not one of
    `own`'s: it is paired under its own name."""
    memo: dict[int, list] = {}

    def unsteered(c: int) -> bool:  # no select in the condition's cone (within the frame)
        seen, stack = set(), [c]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            nd = design.nodes[n]
            if nd.op == "input" and nd.sym in selects:
                return False
            if nd.op in ("input", "const", "state"):
                continue
            stack.extend(nd.args[:1] if nd.op in ("slice", "uext", "sext") else nd.args)
        return True

    def walk(n: int) -> list:
        if n in memo:
            return memo[n]
        nd = design.nodes[n]
        w = nd.sort
        if nd.op == "state":
            bare = _bare(nd.sym, prefixes)
            out = [None] * w if bare != own and bare in others else [(nd.sym, i) for i in range(w)]
        elif nd.op == "const":
            out = [(nd.args[0] >> i) & 1 for i in range(w)]
        elif nd.op == "concat":
            out = walk(nd.args[1]) + walk(nd.args[0])
        elif nd.op == "slice":
            out = walk(nd.args[0])[nd.args[2]:nd.args[1] + 1]
        elif nd.op == "uext":
            out = walk(nd.args[0]) + [0] * nd.args[1]
        elif nd.op == "ite" and any(_async2sync_state(design, x) for x in nd.args[1:]):
            # async2sync's own mux: the register is the arm that IS its state, even
            # when the other arm reads flip-flops too (a latch's data input)
            out = walk(next(x for x in nd.args[1:] if _async2sync_state(design, x)))
        elif nd.op == "ite" and unsteered(nd.args[0]):
            out = []
            for a, b in zip(walk(nd.args[1]), walk(nd.args[2])):
                flops = {x for x in (a, b) if isinstance(x, tuple)}
                out.append(flops.pop() if len(flops) == 1 else None)
        else:
            out = [None] * w
        memo[n] = out
        return out

    return walk(nid)


def _async2sync_state(design: Design, nid: int) -> bool:
    nd = design.nodes[nid]
    return nd.op == "state" and "$async2sync" in (nd.sym or "")


def _node(design: Design, name: str) -> int | None:
    if name in design.states:
        return design.states[name][0]
    return design.symbols.get(name)


def _runs(name: str, mb: list, ob: list) -> tuple[list[RegPair], list[int]]:
    """Group a register's per-bit classification (`_flop_bits` of each copy) into
    RegPairs of contiguous bits, flip-flop against flip-flop only (RegPair says
    why). Returns (pairs, bits that are a flip-flop in one copy only and so stay
    out of R)."""
    def cls(m, o):
        if isinstance(m, tuple) and isinstance(o, tuple):
            return ("ff", m[0], m[1], o[0], o[1])
        return None

    def follows(c, prev):  # c continues the run ending with prev
        return (c is not None and prev is not None and c[1] == prev[1] and c[3] == prev[3]
                and c[2] == prev[2] + 1 and c[4] == prev[4] + 1)

    classes = [cls(m, o) for m, o in zip(mb, ob)]
    pairs, lost, k, w = [], [], 0, len(mb)
    while k < w:
        c = classes[k]
        if c is None:
            if isinstance(mb[k], tuple) or isinstance(ob[k], tuple):
                lost.append(k)
            k += 1
            continue
        j = k + 1
        while j < w and follows(classes[j], classes[j - 1]):
            j += 1
        label = name if (k, j) == (0, w) else f"{name}[{j - 1}:{k}]"
        pairs.append(RegPair(label, Side(c[1], c[2], j - k), Side(c[3], c[4], j - k)))
        k = j
    return pairs, lost


def match_registers(design: Design, registers: list[str] | None = None,
                    prefixes: tuple[str, str] = ("mod.", "orig."),
                    selects: set[str] | frozenset[str] = frozenset()) -> tuple[list[RegPair], list[str]]:
    """Pair each register of the two copies. Returns (pairs, unmatched).

    `registers` is the RTL register list (paths relative to either copy, from
    register_paths); without it the BTOR2 state names are the list, the
    synchronous case. Each name is looked up as a state or a named node in both
    copies and paired bit by bit through `_flop_bits` (RegPair says why).
    `selects` are the cut selects: a mux they steer is a cut, never peeled.
    `unmatched` names registers, or bits of them, that are a flip-flop in one
    copy and nothing pairable in the other: they start free in both copies.
    """
    a, b = prefixes
    names = list(registers) if registers is not None else sorted(
        {n[len(a):] for n in design.states if n.startswith(a)}
        | {n[len(b):] for n in design.states if n.startswith(b)})
    others = set(names)
    pairs, unmatched, partial = [], [], []
    for name in names:
        m, o = f"{a}{name}", f"{b}{name}"
        mn, on = _node(design, m), _node(design, o)
        if mn is None or on is None or design.nodes[mn].sort != design.nodes[on].sort:
            unmatched.append(name)
            continue
        mb = _flop_bits(design, mn, name, others, selects, prefixes)
        ob = _flop_bits(design, on, name, others, selects, prefixes)
        got, lost = _runs(name, mb, ob)
        pairs += got
        if lost:
            partial.append((name, lost, mb, ob))

    # A flip-flop is one cell: once some of its bits pair with the same bits of
    # an equally wide flip-flop in the other copy, the two are the same register,
    # and its other bits pair too. Needed where a bit is reachable through the
    # register's name in one copy only (hls nussinov: `sub_ln21` is
    # {q[9:0], 2'b00}, and the muxed net reads q[9] through a bit-shrink's
    # select). Only an unambiguous, aligned correspondence counts.
    seen: dict[str, set] = {}
    for p in pairs:
        if p.kind == "state":
            seen.setdefault(p.mod.state, set()).add((p.orig.state, p.mod.lo - p.orig.lo))
    same = {ms: next(iter(v))[0] for ms, v in seen.items()
            if len(v) == 1 and next(iter(v))[1] == 0 and design.width(ms) == design.width(next(iter(v))[0])}
    if len(set(same.values())) != len(same):  # an orig state claimed twice: trust neither
        dup = {os for os in same.values() if list(same.values()).count(os) > 1}
        same = {ms: os for ms, os in same.items() if os not in dup}
    whole = {(p.mod.state, p.orig.state) for p in pairs
             if p.kind == "state" and p.mod.lo == 0 and p.mod.width == design.width(p.mod.state)}
    for ms, os in sorted(same.items()):
        if (ms, os) not in whole:
            pairs.append(RegPair(f"{_bare(ms, prefixes)}~{_bare(os, prefixes)}",
                                 Side(ms, 0, design.width(ms)), Side(os, 0, design.width(os))))
    rev = {os: ms for ms, os in same.items()}
    for name, lost, mb, ob in partial:
        left = [k for k in lost if not ((isinstance(mb[k], tuple) and mb[k][0] in same)
                                        or (isinstance(ob[k], tuple) and ob[k][0] in rev))]
        if left:
            unmatched.append(name if len(left) == len(mb) else f"{name} bits {left}")
    return pairs, unmatched


def correspondence(fr: Frame, pairs: list[RegPair], design: Design | None = None) -> z3.BoolRef:
    """R: every matched register holds the same value in this frame."""
    out = []
    for p in pairs:
        x, y = p.value(fr)
        out.append(x == y)
    return _conj(out)


# ---------------------------------------------------------------- power-up
@dataclass
class PowerUp:
    """What frame 0 of the base case stands for: power-up, as Jasper's
    `check_sec -auto_map_reset_x_values on` defines it.

      * `consts`: registers never written that carry an `init` value (ROM
        contents, real on an ASIC)
      * `equal`: every other matched pair, at an arbitrary value shared by both
        copies (Jasper's auto-mapping of reset-X values)

    `init` values of registers that are written are ignored: an ASIC has no
    `initial`."""

    consts: dict[str, int]
    equal: list[RegPair]

    def constraints(self, fr: Frame, design: Design) -> list[z3.BoolRef]:
        out = [fr.states[s] == z3.BitVecVal(v, design.width(s)) for s, v in self.consts.items() if s in fr.states]
        for p in self.equal:
            x, y = p.value(fr)
            out.append(x == y)
        return out

    def describe(self) -> str:
        return f"{len(self.consts)} states fixed, {len(self.equal)} register pairs equal"


def rom_values(design: Design) -> dict[str, int]:
    """Registers never written (their next state is themselves) that carry a
    constant `init`: ROM contents and constants, which exist on an ASIC too."""
    out = {}
    for st, nid in design.init.items():
        sid = design.states[st][0]
        nd = design.nodes.get(nid)
        if design.next.get(st) == sid and nd is not None and nd.op == "const":
            out[st] = nd.args[0]
    return out


def power_up(design: Design, registers: list[tuple[str, int]] | None = None,
             selects: set[str] | frozenset[str] = frozenset()) -> PowerUp:
    """The PowerUp for this miter. `registers` is the RTL (path, width) list
    (matched_registers); without it the BTOR2 state names are the paths, the
    synchronous case."""
    consts = rom_values(design)
    pairs, _ = match_registers(design, [p for p, _ in registers] if registers else None, selects=selects)
    # every pair is flip-flop against flip-flop, "arbitrary but equal" at power-up
    # (Jasper's auto-mapping), except ROM contents, which are fixed
    return PowerUp(consts, [p for p in pairs if not (p.states() & set(consts))])
