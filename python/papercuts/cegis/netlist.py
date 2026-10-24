"""
The yosys side of the combinational loop: elaborate each copy, write the miter,
and turn golden + muxed + miter into BTOR2 with Jasper's X semantics.

    info = module_info(files, top, workdir)        # ports, own assertions, driver problems
    problems = check_drivers(orig_info, muxed_info, suffix)
    sv = miter_text(top, muxed_top, orig_info, muxed_info, selects, "top_miter")
    rep = build_btor(rtl_files, "top_miter", f"{workdir}/top_miter")   # .btor, .info

Two yosys builds take part. The front end -- yosys-slang's read_slang through
dffunmap -- runs in the `yosys` binary (oss-cad-suite), because the slang
plugin is built against it. It writes RTLIL, which pyosys reads, rewrites
(xpair.pair_x) and writes as BTOR2. RTLIL is yosys's own format, read the same
by both.

module_info, check_drivers and miter_text are copied from smt_ec's
src/certify_cuts.py (rewriting repo 12a5a7f + working tree, 2026-10-23); the
two copies are maintained separately.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field

from pyosys import libyosys as ys

from papercuts.cegis.xpair import XReport, pair_x
from papercuts.elaborator import ident

# memory_map: btor2z3 has no array sorts. async2sync + dffunmap: every flip-flop
# becomes a plain synchronous state, which BTOR2's state/next/init model needs.
# opt runs before dffunmap so it cannot re-merge enables.
# {pre} is written before async2sync/dffunmap erase clocks: control_inputs reads it.
# The netlist sees power-up as Jasper does. Jasper ignores initial values
# ("'initial' construct is ignored") and starts every flip-flop free, mapped to
# its twin. yosys honours them, and with a known power-up value it folds a
# flip-flop whose D is that constant (wreduce, opt_dff) and merges flip-flops
# that start and stay alike (opt_merge). A cut that only matters while the
# register holds something else then looked valid: hls nussinov, `initial
# ap_done_reg = 0` and never set, folded in the golden copy only, joint check
# cex in 2 cycles; hls bicg, two submodules' ap_done_reg merged in the golden
# copy only (ISSUES.md 64). So `init` is dropped right after `proc` (memory
# contents, $meminit, are not touched: ROMs keep theirs), every opt runs with
# -keepdc (an undefined init then blocks merging and folding), `prep` is spelled
# out without wreduce, and the last opt skips opt_dff (-noff; its dffe/sdff
# merging was already off).
PREP = ("hierarchy -check -top {top}; proc; setattr -unset init; flatten; future; opt_expr -keepdc; "
        "opt_clean; check; opt -noff -keepdc; opt_clean; memory_collect; opt -noff -keepdc -fast; check")
FRONT_END = ("plugin -i slang; read_slang {files} --top {top} {slang_args}; " + PREP + "; memory_map; "
             "chformal -lower; write_rtlil {pre}; async2sync; opt -noff -keepdc; dffunmap; write_rtlil {out}")

# Flip-flop and latch pins that decide WHEN a cell changes, not what it stores.
_CONTROL_PORTS = {"CLK", "ARST", "SET", "CLR", "ALOAD"}
_LATCHES = {"$dlatch", "$adlatch", "$dlatchsr"}


def control_inputs(design) -> list[str]:
    """Top-level inputs that reach a flip-flop's clock, asynchronous set/reset or
    load, or a latch's enable, through any logic, registers included.

    The BTOR2 model steps every register once per step (async2sync + dffunmap),
    so logic that only decides when a register changes is invisible to it: a cut
    there looks free and is not. opencores ptc: three cuts rewrote clock-select
    muxes (`cntr_clk = rptc_ctrl[1] ? eclk_gate : wb_clk_i` -> `eclk_gate`); the
    loop accepted them and the joint Jasper check found the difference. A select
    in this set must not be searched."""
    m = design.top_module()
    parent: dict[tuple, tuple] = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def bits(sig):
        return [(b.wire.name.str(), b.offset) for b in sig.to_sigbit_vector() if b.wire is not None]

    for lhs, rhs in m.connections_:
        for lb, rb in zip(lhs.to_sigbit_vector(), rhs.to_sigbit_vector()):
            if lb.wire is not None and rb.wire is not None:
                parent[find((lb.wire.name.str(), lb.offset))] = find((rb.wire.name.str(), rb.offset))
    driver: dict[tuple, list] = {}  # alias root -> input bits of the cell driving it
    starts = []
    for cell in m.cells_.values():
        ins, outs = [], []
        for port, sig in cell.connections_.items():
            (ins if cell.input(port) else outs).extend(bits(sig))
            name = port.str().lstrip("\\")
            if cell.input(port) and (name in _CONTROL_PORTS or (name == "EN" and cell.type.str() in _LATCHES)):
                starts.extend(bits(sig))
        for b in outs:
            driver.setdefault(find(b), []).extend(ins)
    inputs = {}
    for w in m.wires_.values():
        if w.port_input:
            for i in range(w.width):
                inputs[find((w.name.str(), i))] = w.name.str().lstrip("\\")
    seen, found, todo = set(), set(), [find(b) for b in starts]
    while todo:
        r = todo.pop()
        if r in seen:
            continue
        seen.add(r)
        if r in inputs:
            found.add(inputs[r])
        todo.extend(find(b) for b in driver.get(r, ()))
    return sorted(found)


def script_file(script: str, path: str) -> str:
    """Write a yosys script (`;`-separated commands) to `path`, one per line. A
    design elaborated into hundreds of module files does not fit on a command
    line (opencores tiny_aes_128: "Argument list too long: 'yosys'")."""
    with open(path, "w") as f:
        f.write("\n".join(c.strip() for c in script.split(";")) + "\n")
    return path


class DesignAssertsPresent(ValueError):
    pass


class DesignMalformed(ValueError):
    """yosys reports wires with conflicting drivers, or wires the muxed copy
    reads but no longer drives. Every checker would resolve such a netlist its
    own way (yosys here, Jasper in papercuts' gates and final check), so their
    answers would stop describing the same design."""


@dataclass
class ModuleInfo:
    name: str
    ports: dict[str, tuple[str, int]]  # name -> (direction, width)
    asserts: list[str]  # "<module>: <cell> at <loc>" for every $check/$assert below the top
    assumes: int = 0
    conflicts: list[str] = field(default_factory=list)  # "<module>.<wire>" with multiple conflicting drivers
    undriven: list[str] = field(default_factory=list)  # "<module>.<wire>" used but driven by nothing


def module_info(sv_files: list[str], top: str, workdir: str, slang_args: tuple[str, ...] = ()) -> ModuleInfo:
    """Ports of `top` and the formal cells anywhere below it, via yosys JSON."""
    out = os.path.join(workdir, f"{top}.json")
    log = os.path.join(workdir, f"{top}.yosys.log")
    script = (f"plugin -i slang; read_slang {' '.join(sv_files)} --top {top} {' '.join(slang_args)}; "
              f"prep -top {top}; write_json {out}")
    r = subprocess.run(["yosys", "-q", "-l", log, "-s", script_file(script, os.path.join(workdir, f"{top}.ys"))],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"yosys failed elaborating {top} from {sv_files}:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    with open(out) as f:
        j = json.load(f)
    if top not in j["modules"]:
        raise RuntimeError(f"module {top} not found after elaboration; modules: {sorted(j['modules'])}")
    ports = {p: (d["direction"], len(d["bits"])) for p, d in j["modules"][top]["ports"].items()}
    asserts, assumes = [], 0
    for mname, m in j["modules"].items():
        for c in m["cells"].values():
            t = c["type"]
            if t in ("$check", "$assert"):
                flavor = c.get("parameters", {}).get("FLAVOR", "")
                if t == "$check" and flavor and flavor != "assert":
                    if flavor == "assume":
                        assumes += 1
                    continue  # cover / live / fair
                asserts.append(f"{mname}: {t}{(' ' + flavor) if flavor else ''} at {c.get('attributes', {}).get('src', '?')}")
            elif t == "$assume":
                assumes += 1
    conflicts, undriven = driver_problems(log)
    return ModuleInfo(top, ports, asserts, assumes, conflicts, undriven)


_CONFLICT = re.compile(r"^Warning: multiple conflicting drivers for (\S+?)\.\\(\S+?):?$")
_UNDRIVEN = re.compile(r"^Warning: Wire (\S+?)\.\\(\S+) is used but has no driver\.$")


def driver_problems(log_path: str) -> tuple[list[str], list[str]]:
    """The driver warnings of yosys's `check` (which `prep` runs), as
    "<module>.<wire>", de-duplicated in order of appearance."""
    conflicts, undriven = {}, {}
    with open(log_path) as f:
        for line in f:
            line = line.rstrip()
            m = _CONFLICT.match(line)
            if m:
                conflicts[f"{m.group(1)}.{m.group(2)}"] = None
            m = _UNDRIVEN.match(line)
            if m:
                undriven[f"{m.group(1)}.{m.group(2)}"] = None
    return list(conflicts), list(undriven)


def check_drivers(orig: ModuleInfo, muxed: ModuleInfo, suffix: str) -> list[str]:
    """Conflicting drivers on either side: every checker would resolve them its
    own way, so the copies cannot be compared.

    Undriven wires are not a problem here. They are X sources, paired across the
    copies like the rest (xpair.pair_x, the way Jasper maps "undriven" signals),
    and a driver lost to muxing makes the copies differ with every cut off, which
    the loop reports as MiterMismatch. Checking them by name gave false positives:
    yosys names an undriven net by whichever alias it prefers, and the two copies
    can prefer different ones (opencores rtfsimpleuart: `baud8x` left unconnected
    in both, reported as `isX8` only in the muxed copy, where a companion reads it)."""
    problems = [f"golden design: multiple conflicting drivers for {w}" for w in orig.conflicts]
    problems += [f"muxed design: multiple conflicting drivers for {w}" for w in muxed.conflicts]
    return problems


def miter_text(top: str, muxed_top: str, orig: ModuleInfo, muxed: ModuleInfo, selects: list[str],
               miter_name: str) -> str:
    """Wrapper instantiating the golden design as `orig` and the muxed copy as `mod`.
    `selects` is the manifest's port list, in manifest order."""
    shared = dict(orig.ports)
    for p, (d, w) in shared.items():
        if p not in muxed.ports or muxed.ports[p] != (d, w):
            raise ValueError(f"port {p} ({d}, {w} bits) of {top} has no matching port on {muxed_top}")
    extra = [p for p in muxed.ports if p not in shared and p not in selects]
    if extra:
        raise ValueError(f"{muxed_top} has ports that are neither {top}'s nor manifest selects: {extra}")
    ins = [p for p, (d, _) in shared.items() if d == "input"]
    outs = [p for p, (d, _) in shared.items() if d == "output"]
    inouts = [p for p, (d, _) in shared.items() if d == "inout"]
    if inouts:
        raise NotImplementedError(f"inout ports not supported in the miter: {inouts}")
    if not outs:
        raise ValueError(f"{top} has no outputs to compare")

    # Port names are re-escaped where needed (`\\opcode[0] `, EPFL netlists), and
    # so are the per-copy wires named after them.
    def decl(p):
        w = shared[p][1]
        return f"input logic [{w - 1}:0] {ident(p)}" if w > 1 else f"input logic {ident(p)}"

    def orig(p):
        return ident(f"{p}__orig")

    def mod(p):
        return ident(f"{p}__mod")

    lines = [f"// generated by papercuts.cegis.netlist.miter_text: {top} (orig) vs {muxed_top} (mod)",
             f"module {miter_name}(",
             "    " + ",\n    ".join([decl(p) for p in ins] + [f"input logic {ident(s)}" for s in selects]
                                     + ["output logic eq"]),
             ");"]
    for p in outs:
        w = shared[p][1]
        rng = f"[{w - 1}:0] " if w > 1 else ""
        lines.append(f"    logic {rng}{orig(p)}, {mod(p)};")
    conn_in = ", ".join(f".{ident(p)}({ident(p)})" for p in ins)
    lines.append(f"    {ident(top)} orig({conn_in}{', ' if ins else ''}"
                 + ", ".join(f".{ident(p)}({orig(p)})" for p in outs) + ");")
    conn_sel = ", ".join(f".{ident(s)}({ident(s)})" for s in selects)
    lines.append(f"    {ident(muxed_top)} mod({conn_in}{', ' if ins else ''}"
                 + ", ".join(f".{ident(p)}({mod(p)})" for p in outs) + (", " + conn_sel if selects else "") + ");")
    lines.append("    assign eq = " + " & ".join(f"({orig(p)} == {mod(p)})" for p in outs) + ";")
    lines.append("    always @(*) assert(eq);")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


@contextlib.contextmanager
def _stdout_to(path: str):
    """Send file descriptor 1 to `path`: pyosys logs straight to it, past sys.stdout."""
    sys.stdout.flush()
    saved = os.dup(1)
    with open(path, "a") as f:
        os.dup2(f.fileno(), 1)
        try:
            yield
        finally:
            sys.stdout.flush()
            os.dup2(saved, 1)
            os.close(saved)


def build_btor(rtl: list[str], top: str, out_base: str, slang_args: tuple[str, ...] = ()) -> XReport:
    """golden + muxed + miter sources -> `<out_base>.btor` and `.info` (clock list).

    Also left behind: `<out_base>.il` (the front end's RTLIL), `<out_base>_x.il`
    (after X pairing), `<out_base>.xpair.json` (the XReport) and
    `<out_base>.pyosys.log`."""
    script = FRONT_END.format(files=" ".join(rtl), top=top, slang_args=" ".join(slang_args),
                              pre=f"{out_base}.pre.il", out=f"{out_base}.il")
    r = subprocess.run(["yosys", "-q", "-s", script_file(script, f"{out_base}.front.ys")], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"yosys front end failed on {top}:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    pre = ys.Design()
    with _stdout_to(f"{out_base}.pyosys.log"):
        ys.run_pass(f"read_rtlil {out_base}.pre.il", pre)
        control = control_inputs(pre)
    d = ys.Design()
    with _stdout_to(f"{out_base}.pyosys.log"):
        ys.run_pass(f"read_rtlil {out_base}.il", d)
        rep = pair_x(d)
        rep.control_inputs = control
        ys.run_pass(f"write_rtlil {out_base}_x.il", d)
        ys.run_pass(f"write_btor -s -x -i {out_base}.info {out_base}.btor", d)
    with open(f"{out_base}.xpair.json", "w") as f:
        json.dump(asdict(rep), f, indent=1)
    return rep
