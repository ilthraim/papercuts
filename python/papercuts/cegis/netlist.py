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

# memory_map: btor2z3 has no array sorts. async2sync + dffunmap: every flip-flop
# becomes a plain synchronous state, which BTOR2's state/next/init model needs.
# opt runs before dffunmap so it cannot re-merge enables.
FRONT_END = ("plugin -i slang; read_slang {files} --top {top}; prep -top {top} -flatten; memory_map; "
             "chformal -lower; async2sync; opt -nodffe -nosdff; dffunmap; write_rtlil {out}")


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


def module_info(sv_files: list[str], top: str, workdir: str) -> ModuleInfo:
    """Ports of `top` and the formal cells anywhere below it, via yosys JSON."""
    out = os.path.join(workdir, f"{top}.json")
    log = os.path.join(workdir, f"{top}.yosys.log")
    script = f"plugin -i slang; read_slang {' '.join(sv_files)} --top {top}; prep -top {top}; write_json {out}"
    r = subprocess.run(["yosys", "-q", "-l", log, "-p", script], capture_output=True, text=True)
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
    """Problems that make the two copies untrustworthy to compare: conflicting
    drivers on either side, and wires the muxed copy reads but no longer drives
    although the golden design drives them (a driver lost to muxing). A wire
    undriven in the golden design too is ordinary X and stays allowed."""
    def golden_name(w: str) -> str:
        mod, _, wire = w.partition(".")
        return (mod[: -len(suffix)] if suffix and mod.endswith(suffix) else mod) + "." + wire

    problems = [f"golden design: multiple conflicting drivers for {w}" for w in orig.conflicts]
    problems += [f"muxed design: multiple conflicting drivers for {w}" for w in muxed.conflicts]
    golden_undriven = set(orig.undriven)
    problems += [f"muxed design: {w} is read but has no driver (driven in the golden design)"
                 for w in muxed.undriven if golden_name(w) not in golden_undriven]
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

    def decl(p):
        w = shared[p][1]
        return f"input logic [{w - 1}:0] {p}" if w > 1 else f"input logic {p}"

    lines = [f"// generated by papercuts.cegis.netlist.miter_text: {top} (orig) vs {muxed_top} (mod)",
             f"module {miter_name}(",
             "    " + ",\n    ".join([decl(p) for p in ins] + [f"input logic {s}" for s in selects] + ["output logic eq"]),
             ");"]
    for p in outs:
        w = shared[p][1]
        rng = f"[{w - 1}:0] " if w > 1 else ""
        lines.append(f"    logic {rng}{p}__orig, {p}__mod;")
    conn_in = ", ".join(f".{p}({p})" for p in ins)
    lines.append(f"    {top} orig({conn_in}{', ' if ins else ''}" + ", ".join(f".{p}({p}__orig)" for p in outs) + ");")
    conn_sel = ", ".join(f".{s}({s})" for s in selects)
    lines.append(f"    {muxed_top} mod({conn_in}{', ' if ins else ''}" + ", ".join(f".{p}({p}__mod)" for p in outs)
                 + (", " + conn_sel if selects else "") + ");")
    lines.append("    assign eq = " + " & ".join(f"({p}__orig == {p}__mod)" for p in outs) + ";")
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


def build_btor(rtl: list[str], top: str, out_base: str) -> XReport:
    """golden + muxed + miter sources -> `<out_base>.btor` and `.info` (clock list).

    Also left behind: `<out_base>.il` (the front end's RTLIL), `<out_base>_x.il`
    (after X pairing), `<out_base>.xpair.json` (the XReport) and
    `<out_base>.pyosys.log`."""
    r = subprocess.run(["yosys", "-q", "-p", FRONT_END.format(files=" ".join(rtl), top=top, out=f"{out_base}.il")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"yosys front end failed on {top}:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    d = ys.Design()
    with _stdout_to(f"{out_base}.pyosys.log"):
        ys.run_pass(f"read_rtlil {out_base}.il", d)
        rep = pair_x(d)
        ys.run_pass(f"write_rtlil {out_base}_x.il", d)
        ys.run_pass(f"write_btor -s -x -i {out_base}.info {out_base}.btor", d)
    with open(f"{out_base}.xpair.json", "w") as f:
        json.dump(asdict(rep), f, indent=1)
    return rep
