"""
From a mux manifest, the largest set of cuts that can all be on together and
leave the logic between the registers unchanged (comb.CombCegis).

    from papercuts.cegis.selection import select_cuts
    r = select_cuts(Manifest.load("outputs/mux_manifest.json"), workdir="outputs/cegis")
    r.on_cuts, r.off_cuts, r.untested      # manifest Cuts
    r.base_ok                              # the set holds from power-up, not only step by step
    r.individual                           # {cut id: valid on its own}, unless individual=False

Steps, all in `workdir`:
  1. elaborate each copy with yosys: the top's ports, a check that neither copy
     carries an assertion of its own, a check for conflicting or lost drivers,
     and a cross-check of the manifest against the muxed top's actual ports
  2. list and pair the registers of the two copies
  3. write the miter (golden as `orig`, muxed as `mod`, every output compared)
     and build its BTOR2 with the X sources paired (netlist.build_btor)
  4. run the loop; inert selects, and those the caller pins off, are pinned to 0
  5. unless individual=False, each searched cut on alone (CombCegis.individually_valid)

The manifest's clock and reset play no part (comb module docstring).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from papercuts.cegis.btor2z3 import Design
from papercuts.cegis.comb import CombCegis, MiterMismatch
from papercuts.cegis.netlist import (DesignAssertsPresent, DesignMalformed, build_btor, check_drivers,
                                     miter_text, module_info)
from papercuts.cegis.registers import matched_registers, register_paths
from papercuts.cegis.xpair import XReport
from papercuts.manifest import Cut, Manifest, ManifestError


@dataclass
class Selection:
    """Cuts are manifest cuts throughout: (module, index) with a stable id."""

    on_cuts: list[Cut]
    off_cuts: list[Cut]
    untested: list[Cut]  # reserved-but-inert selects, or not reachable at the top: never exercised
    pinned_off: list[Cut]  # testable, but held off at the caller's request (pin_off)
    manifest: Manifest
    base_ok: bool  # R holds at power-up: the set is equivalent for all time, not only step by step
    base_evidence: str
    miter_sv: str
    btor: str
    xpair: XReport
    registers: list[tuple[str, int]] = field(default_factory=list)  # compared, as (path, width)
    registers_dropped: list[str] = field(default_factory=list)  # and why each other one was not
    stats: list[dict] = field(default_factory=list)  # per loop iteration
    individual: dict[str, bool] | None = None  # cut id -> valid on its own (searched cuts only)
    individual_how: dict[str, str] | None = None  # cut id -> "core" | "verify"
    individual_s: float | None = None
    total_s: float | None = None
    workdir: str = ""

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.on_cuts]

    def by_module(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for c in self.on_cuts:
            out.setdefault(c.module, []).append(c.index)
        return {m: sorted(v) for m, v in sorted(out.items())}


def select_cuts(man: Manifest, workdir: str, solver: str = "auto", timeout: float | None = None,
                individual: bool = True, pin_off: list[str] = (), verbose: bool = False,
                on_event: Callable[..., None] | None = None) -> Selection:
    """`pin_off`: ids of testable cuts to hold off rather than search (the
    pipeline's no-ops, whose cut source would not match what the mux does)."""
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)
    emit = on_event or (lambda kind, **fields: None)

    # ---- 1. elaborate and check
    emit("stage", name="elaborate", top=man.top)
    orig = module_info(man.golden_files, man.top, workdir)
    muxed = module_info(man.muxed_files + man.golden_files, man.muxed_top, workdir)
    asserts = list(dict.fromkeys(orig.asserts + muxed.asserts))
    if asserts:
        raise DesignAssertsPresent(
            "the design carries assertions of its own; remove them before selecting cuts, a failing one "
            "would look like an equivalence failure:\n  " + "\n  ".join(asserts))
    problems = check_drivers(orig, muxed, man.suffix)
    if problems:
        raise DesignMalformed(
            "yosys reports driver problems; each checker would resolve them its own way, so the two "
            "copies cannot be compared:\n  " + "\n  ".join(problems))
    problems = man.check_against(muxed.ports)
    if problems:
        raise ManifestError(f"{man.path} does not describe {man.muxed_top}:\n  " + "\n  ".join(problems))

    # ---- 2. registers
    golden_regs, gold_skipped = register_paths(man.golden_files, man.top, workdir, "golden")
    muxed_regs, muxed_skipped = register_paths(man.muxed_files + man.golden_files, man.muxed_top,
                                               workdir, "muxed")
    registers, dropped_regs = matched_registers(golden_regs, muxed_regs)
    unpaired = dropped_regs + gold_skipped + muxed_skipped
    emit("stage", name="registers", matched=len(registers), dropped=len(unpaired))

    # ---- 3. miter and BTOR2
    selects = [c.port for c in sorted(man.cuts, key=lambda c: (c.module, c.index)) if c.port]
    miter_name = f"{man.top}_miter"
    miter_sv = os.path.join(workdir, f"{miter_name}.sv")
    with open(miter_sv, "w") as f:
        f.write(miter_text(man.top, man.muxed_top, orig, muxed, selects, miter_name))
    base = os.path.join(workdir, miter_name)
    xrep = build_btor(man.golden_files + man.muxed_files + [miter_sv], miter_name, base)
    emit("stage", name="btor2", xpair=xrep.summary())
    design = Design.from_file(base + ".btor", base + ".info")

    # ---- 4. the loop
    unknown = sorted(set(pin_off) - {c.id for c in man.testable})
    if unknown:
        raise ManifestError(f"pin_off names cuts that are not testable: {unknown[:10]}")
    testable = [c for c in man.testable if c.id not in set(pin_off)]
    pinned = [c for c in man.testable if c.id in set(pin_off)]
    missing = [c.port for c in testable + pinned if c.port not in design.inputs]
    if missing:
        raise ManifestError(f"select port(s) the manifest marks inserted are not inputs of the miter's "
                            f"BTOR2: {missing[:10]}")
    inert = [c.port for c in man.untested if c.port and c.port in design.inputs] + [c.port for c in pinned]
    loop = CombCegis(design, verbose=verbose, solver=solver, exclusive=man.exclusive_ports,
                     selects=[c.port for c in testable], pin_zero=inert, registers=registers,
                     on_event=on_event, timeout=timeout)
    emit("stage", name="loop", selects=len(testable))
    try:
        res = loop.run()
    except MiterMismatch as e:
        if unpaired:  # the likeliest cause: registers never compared, free in each copy
            raise MiterMismatch(e.cex, f"{e.args[0].split(':')[0]}; registers not paired between the "
                                       f"copies: {unpaired}") from None
        raise
    if res is None:
        raise RuntimeError("no assignment survived, not even all cuts off")

    ind = how = ind_s = None
    if individual:
        emit("stage", name="individual", selects=len(testable))
        t0 = time.time()
        got = loop.individually_valid([c.port for c in testable])
        ind_s = time.time() - t0
        ind = {man.cut_of(p).id: v for p, (v, _) in got.items()}
        how = {man.cut_of(p).id: h for p, (_, h) in got.items()}

    key = lambda c: (c.module, c.index)  # noqa: E731
    return Selection(
        on_cuts=sorted((man.cut_of(p) for p, v in res.items() if v), key=key),
        off_cuts=sorted((man.cut_of(p) for p, v in res.items() if not v), key=key),
        untested=sorted(man.untested, key=key), pinned_off=sorted(pinned, key=key), manifest=man,
        base_ok=bool(loop.base_ok), base_evidence=loop.base_evidence,
        miter_sv=miter_sv, btor=base + ".btor", xpair=xrep, registers=registers,
        registers_dropped=unpaired, stats=loop.stats, total_s=loop.total_s, workdir=workdir,
        individual=ind, individual_how=how, individual_s=ind_s)
