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


def register_paths(sv_files: list[str], top: str, workdir: str, tag: str = "") -> tuple[Registers, list[str]]:
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
    script = (f"plugin -i slang; read_slang {' '.join(sv_files)} --top {top}; "
              f"prep -top {top} -flatten; memory_map; write_json {out}")
    r = subprocess.run(["yosys", "-q", "-p", script], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"yosys failed listing registers of {top}:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    with open(out) as f:
        j = json.load(f)
    m = j["modules"][top]
    owner: dict[int, str] = {}
    by_bits: dict[tuple, list[str]] = {}  # identical bit vectors: one net, several names
    for name, w in m["netnames"].items():
        if w.get("hide_name"):
            continue
        for b in w["bits"]:
            owner.setdefault(b, name)
        by_bits.setdefault(tuple(w["bits"]), []).append(name)

    regs = Registers()
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
    dropped += [f"{n} (muxed only)" for n in sorted(set(muxed) - used)]
    return pairs, dropped


# ---------------------------------------------------------------- BTOR2 side
@dataclass(frozen=True)
class RegPair:
    """One register of the golden design and its twin in the muxed copy, however
    the BTOR2 happens to name them.

    `kind` is "state" when both sides are state elements (a synchronous design:
    the states are literally `mod.q` and `orig.q`) and "symbol" when they are
    named nodes instead. `async2sync` replaces each asynchronous flip-flop with
    an auxiliary register of its own naming, so on such a design nothing at the
    state level relates to the RTL -- but the wire the register drives keeps its
    name, and `write_btor -s` labels that node. Reading the symbol gives the
    register's value in whichever frame is asked."""

    name: str  # the register, without the mod./orig. prefix
    kind: str  # "state" | "symbol"
    mod: str
    orig: str

    def value(self, fr: Frame, design: Design) -> tuple[BV, BV]:
        if self.kind == "state":
            return fr.states[self.mod], fr.states[self.orig]
        return fr.values[design.symbols[self.mod]], fr.values[design.symbols[self.orig]]


def _resolve(design: Design, name: str) -> str | None:
    """The one state a register name stands for: itself if it is a state, else
    the single state in the cone of the node it labels, else None."""
    if name in design.states:
        return name
    if name in design.symbols:
        cone = design.cone_states(design.symbols[name])
        if len(cone) == 1:
            return next(iter(cone))
    return None


def match_registers(design: Design, registers: list[str] | None = None,
                    prefixes: tuple[str, str] = ("mod.", "orig.")) -> tuple[list[RegPair], list[str]]:
    """Pair each register of the two copies. Returns (pairs, unmatched).

    `registers` is the RTL register list (paths relative to either copy, from
    register_paths); with it, symbols are searched as well as states, which is
    what makes this work on an asynchronous-reset design. Given nothing, only
    the state names are used, which is the synchronous case.
    """
    a, b = prefixes
    names = list(registers) if registers is not None else sorted(
        {n[len(a):] for n in design.states if n.startswith(a)}
        | {n[len(b):] for n in design.states if n.startswith(b)})
    pairs, unmatched = [], []
    for name in names:
        m, o = f"{a}{name}", f"{b}{name}"
        if m in design.states and o in design.states and design.width(m) == design.width(o):
            pairs.append(RegPair(name, "state", m, o))
            continue
        # One side a state, the other a name reading only that state: yosys merged
        # the two copies' identical registers into one. Equal by construction.
        ms_, os__ = _resolve(design, m), _resolve(design, o)
        if ms_ is not None and ms_ == os__ and design.width(m) == design.width(o):
            pairs.append(RegPair(name, "state", ms_, os__))
            continue
        if m in design.symbols and o in design.symbols and design.width(m) == design.width(o):
            # Prefer the state the register actually lives in. Its read value is
            # a function of exactly one state, and pairing THAT is what makes R
            # inductive: the read passes through the asynchronous reset mux, so
            # two registers holding different values can still read alike while
            # reset is asserted, and equality of the reads alone is not preserved
            # by a step.
            ms, os_ = design.cone_states(design.symbols[m]), design.cone_states(design.symbols[o])
            if len(ms) == 1 and len(os_) == 1:
                ms, os_ = ms.pop(), os_.pop()
                if design.width(ms) == design.width(os_):
                    pairs.append(RegPair(name, "state", ms, os_))
                    continue
            pairs.append(RegPair(name, "symbol", m, o))
            continue
        unmatched.append(name)
    return pairs, unmatched


def correspondence(fr: Frame, pairs: list[RegPair], design: Design) -> z3.BoolRef:
    """R: every matched register holds the same value in this frame."""
    out = []
    for p in pairs:
        x, y = p.value(fr, design)
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
            x, y = p.value(fr, design)
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


def power_up(design: Design, registers: list[tuple[str, int]] | None = None) -> PowerUp:
    """The PowerUp for this miter. `registers` is the RTL (path, width) list
    (matched_registers); without it the BTOR2 state names are the paths, the
    synchronous case."""
    consts = rom_values(design)
    pairs, _ = match_registers(design, [p for p, _ in registers] if registers else None)

    def states_of(p: RegPair) -> set[str]:
        if p.kind == "state":
            return {p.mod, p.orig}
        return design.cone_states(design.symbols[p.mod]) | design.cone_states(design.symbols[p.orig])

    return PowerUp(consts, [p for p in pairs if not (states_of(p) & set(consts))])
