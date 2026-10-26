# General TODOs:
# - Change from using CST to AST for rewrites?

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import fnmatch
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from pyslang.syntax import SyntaxTree

from papercuts import chipper
from papercuts.backends import discover_backends, get_backend
from papercuts.deadcode import prune_dead_source
from papercuts.elaborator import (
    ElaborationError,
    EmitError,
    build_compilation_from,
    elaborate_design,
    gate_map_script,
    make_parse_env,
)
from papercuts.manifest import build_manifest, check_manifest, write_manifest
from papercuts.pypercuts import (
    Papercutter,
    get_instantiated_modules,
    insert_muxes_report,
    rename_instance_types,
    rename_module,
    wire_mux_hierarchy,
)
from papercuts.status import StatusWriter
from papercuts.utils import Run, print_tree, set_verbose, status

CUT_FAMILIES = ("bitshrink", "ternary", "if", "case", "binop", "force-const")


def cut_family(ctype: str) -> str:
    """Family name for a cut type string, e.g. 'binop(and,keep-left)' -> 'binop'."""
    return ctype.split("(", 1)[0]


# MARK: Module context
@dataclass
class ModuleCuts:
    """All enumerated cuts for a single (concretized) module."""

    name: str                  # module name
    tree: SyntaxTree           # concretized source tree (pre-cut)
    pc: Papercutter | None     # cutter used to generate each cut on demand + consolidate (None if excluded)
    is_top: bool
    cut_infos: list            # per-index (type, line) from pc.cut_info()
    cur_dir: str
    shrink_widths: list = field(default_factory=list)  # per-index dim width (0 if not a bitshrink cut)
    baseline: str = ""         # serialized pre-cut source; a cut equal to it is a no-op
    runs: list[Run] = field(default_factory=list)
    excluded: bool = False     # kept in the golden source but never cut
    noops: list[int] = field(default_factory=list)  # cut indices identical to source (never FVed)


# MARK: papercuts.log
RESULTS_FILENAME = "papercuts.results.jsonl"

def append_result(path: str, rec: dict) -> None:
    """Append one result record as a JSON line and flush it to disk."""
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
        f.flush()

def write_papercuts_log(
    log_path: str,
    modules: list[ModuleCuts],
    checked: bool,
    final_runs: list[tuple[ModuleCuts, Run]] | None = None,
    fv_gate: str | None = None,
    prune_stats=None,
    cegis: list[str] | None = None,
    joint: Run | None = None,
) -> None:
    """Write a text summary of every papercut that was tried.

    One row per cut: module, index, type, source line, and valid (Y/N). When
    equivalence checking was not requested, validity is unknown and shown as '-'.

    When ``final_runs`` is provided (after consolidation), a second section
    reports each module's consolidated run: how many valid cuts were merged and
    whether the merged source verified (PROVEN/FAILED).

    Under --method cegis-comb, ``cegis`` holds the run's header lines, rows gain
    a `set` column (Y: in the selected set), valid is the cut's validity on its
    own under the combinational spec ('-' not checked, 'U' untested: its select
    drives nothing), and ``joint`` is the one check of the whole consolidated
    design.
    """
    total = sum(len(m.runs) for m in modules)
    valid = sum(1 for m in modules for r in m.runs if r.valid)
    noop = sum(len(m.noops) for m in modules)
    in_set = sum(1 for m in modules for r in m.runs if r.in_set)

    rows = []
    for m in modules:
        if m.excluded:
            # Kept in the golden source but never cut
            rows.append((m.name, "-", "excluded", "-", "X", "-"))
            continue
        for run in m.runs:
            ctype, line = m.cut_infos[run.index]
            if not checked or run.verdict == "not-checked":
                v = "-"
            elif run.verdict == "untested":
                v = "U"
            else:
                v = "Y" if run.valid else "N"
                # Report number of bits removed
                if run.valid and ctype.startswith("bitshrink") and run.shrink_amount > 1:
                    v = f"Y(-{run.shrink_amount}b)"
            rows.append((m.name, run.index, ctype, line, v, "Y" if run.in_set else "N"))
        # Cuts that are byte-identical to elaborated source should not occur, and are errored accordingly
        for idx in m.noops:
            ctype, line = m.cut_infos[idx]
            rows.append((m.name, idx, ctype, line, "ERR", "-"))

    # Column widths for aligned output (account for both sections' module names).
    mod_names = [r[0] for r in rows] + (
        [m.name for m, _ in final_runs] if final_runs else []
    )
    w_mod = max([len("module")] + [len(n) for n in mod_names], default=len("module"))
    w_type = max([len("type")] + [len(r[2]) for r in rows], default=len("type"))

    with open(log_path, "w") as f:
        f.write(
            f"# papercuts summary: {total} cuts tried, {valid} valid, "
            f"{noop} no-op errors\n"
        )
        if fv_gate is not None:
            f.write(f"# elaboration-vs-original FV gate: {fv_gate}\n")
        if prune_stats is not None:
            f.write(
                f"# dead-code prune: removed {prune_stats.signals} signal(s), "
                f"{prune_stats.bits} dead bit(s), {prune_stats.decls} decl(s) "
                f"in {prune_stats.iters} iteration(s)\n"
            )
        for line in cegis or []:
            f.write(f"# {line}\n")
        set_head = "  set" if cegis is not None else ""
        f.write(
            f"# {'module':<{w_mod}}  {'idx':>4}  {'type':<{w_type}}  {'line':>6}  valid{set_head}\n"
        )
        for mod, idx, ctype, line, v, s in rows:
            set_col = f"  {s:>5}" if cegis is not None else ""
            f.write(
                f"  {mod:<{w_mod}}  {idx:>4}  {ctype:<{w_type}}  {line:>6}  {v:<5}{set_col}\n".rstrip() + "\n"
            )

        # Per-type success breakdown (only meaningful once checks have run). This
        # is the final, authoritative version of the live papercuts.stats.log:
        # planned/proven/failed per cut sub-type, then rolled up per family, with
        # a failure-verdict tally so a disproven cut reads differently from an
        # errored/timed-out one.
        if checked:
            by_type: dict[str, dict] = {}
            for m in modules:
                if m.excluded:
                    continue
                for run in m.runs:
                    ct = m.cut_infos[run.index][0]
                    a = by_type.setdefault(
                        ct, {"planned": 0, "proven": 0, "failed": 0, "in_set": 0, "verdicts": {}}
                    )
                    a["planned"] += 1
                    a["in_set"] += bool(run.in_set)
                    if run.valid:
                        a["proven"] += 1
                    else:
                        a["failed"] += 1
                        vd = run.verdict or "unknown"
                        a["verdicts"][vd] = a["verdicts"].get(vd, 0) + 1

            if by_type:
                fam: dict[str, dict] = {}
                verdicts: dict[str, int] = {}
                for ct, a in by_type.items():
                    fkey = ct.split("(", 1)[0]
                    fa = fam.setdefault(fkey, {"planned": 0, "proven": 0, "failed": 0, "in_set": 0})
                    fa["planned"] += a["planned"]
                    fa["proven"] += a["proven"]
                    fa["failed"] += a["failed"]
                    fa["in_set"] += a["in_set"]
                    for k, v in a["verdicts"].items():
                        verdicts[k] = verdicts.get(k, 0) + v

                wt = max([len("type")] + [len(t) for t in by_type])
                wf = max([len("family")] + [len(t) for t in fam])
                # Under cegis-comb, proven/failed are each cut on its own and
                # `in set` is membership of the selected set.
                set_h = f"  {'in set':>6}" if cegis is not None else ""

                def set_c(a):
                    return f"  {a['in_set']:>6}" if cegis is not None else ""

                f.write("\n# success by type\n")
                f.write(
                    f"# {'type':<{wt}}  {'planned':>7}  {'proven':>6}  {'failed':>6}{set_h}\n"
                )
                for ct, a in sorted(by_type.items()):
                    f.write(
                        f"  {ct:<{wt}}  {a['planned']:>7}  {a['proven']:>6}  "
                        f"{a['failed']:>6}{set_c(a)}\n"
                    )
                f.write(f"# {'family':<{wf}}  {'planned':>7}  {'proven':>6}  {'failed':>6}{set_h}\n")
                for fkey, a in sorted(fam.items()):
                    f.write(
                        f"  {fkey:<{wf}}  {a['planned']:>7}  {a['proven']:>6}  "
                        f"{a['failed']:>6}{set_c(a)}\n"
                    )
                if verdicts:
                    f.write(
                        "# failed by verdict: "
                        + ", ".join(f"{k} {v}" for k, v in sorted(verdicts.items()))
                        + "\n"
                    )

        if final_runs is not None:
            n_proven = sum(1 for _, fr in final_runs if fr.valid)
            f.write("\n")
            f.write(
                f"# consolidated runs: {n_proven}/{len(final_runs)} verified\n"
            )
            f.write(f"# {'module':<{w_mod}}  {'applied':>7}  result\n")
            for m, fr in final_runs:
                applied = sum(1 for r in m.runs if r.valid)
                result = "PROVEN" if fr.valid else "FAILED"
                f.write(f"  {m.name:<{w_mod}}  {applied:>7}  {result}\n")

        if joint is not None:
            result = "PROVEN" if joint.valid else f"FAILED ({joint.verdict})"
            f.write(f"\n# joint check of the consolidated design ({in_set} cuts applied): {result}\n")


# MARK: cut plan
def write_cut_plan(plan_path: str, modules: list[ModuleCuts]) -> None:
    """Write the planned equivalence tests (one row per cut to be checked).

    Emitted after enumeration but before any FV run, so the full test plan --
    every cut and its type -- is visible up front, independent of whether -e is
    used. No-op cuts (byte-identical to the elaborated source) are never checked,
    so they are excluded from the rows and only counted in the header.
    """
    planned = [
        (m.name, run.index, m.cut_infos[run.index][0], m.cut_infos[run.index][1])
        for m in modules
        for run in m.runs
    ]
    n_noop = sum(len(m.noops) for m in modules)
    excluded = [m.name for m in modules if m.excluded]

    # Per-type tally in first-seen order.
    tally: dict[str, int] = {}
    for _, _, ctype, _ in planned:
        tally[ctype] = tally.get(ctype, 0) + 1

    w_mod = max([len("module")] + [len(n) for n, *_ in planned], default=len("module"))
    w_type = max([len("type")] + [len(t) for *_, t, _ in planned], default=len("type"))

    with open(plan_path, "w") as f:
        header = (
            f"# papercuts cut plan: {len(planned)} planned tests across "
            f"{len(modules)} modules"
        )
        if n_noop:
            header += f" ({n_noop} no-op cut(s) excluded)"
        f.write(header + "\n")
        if tally:
            f.write("# by type: " + ", ".join(f"{t} {c}" for t, c in tally.items()) + "\n")
        if excluded:
            f.write(f"# excluded modules (never cut): {', '.join(excluded)}\n")
        f.write(f"# {'module':<{w_mod}}  {'idx':>4}  {'type':<{w_type}}  {'line':>6}\n")
        for mod, idx, ctype, line in planned:
            f.write(f"  {mod:<{w_mod}}  {idx:>4}  {ctype:<{w_type}}  {line:>6}\n")


# MARK: CEGIS method
async def cegis_phase(args, modules: list[ModuleCuts], manifest_path: str, *, backend, tracker,
                      ctree_dir: str, consolidated_dir: str, top_name: str, output_dir: str,
                      results_path: str, log_path: str, fv_gate, prune_stats) -> None:
    """--method cegis-comb, in place of checking each cut and consolidating per module.

    1. no-ops: a cut whose source is byte-identical to the baseline is a cutter
       bug, as under fv; its select is held off, since applying the cut would
       not reproduce what its mux did
    2. the CEGIS loop on the muxed design picks the set (papercuts.cegis), and
       each cut is also checked on its own for the statistics
    3. every module gets its share of the set applied (and is re-pruned), and
       the backend checks the WHOLE consolidated design against the golden one
       once. Per-module checks would be wrong here: the set is valid jointly, and
       one module's share of it need not be valid alone. The check starts from
       every shared power-up state with no reset (ECBackend.check_from_power_up):
       that is what the spec proves, and a reset-based check can fail on the
       backend's own reset analysis instead.

    A failed joint check is fatal. The set was proven on the muxed design, so a
    consolidated design that is not equivalent means the cut and mux paths
    disagree, or the BTOR2 and the backend read the design differently.
    """
    from papercuts.cegis.comb import AnonymousX, MiterMismatch
    from papercuts.cegis.netlist import DesignAssertsPresent, DesignMalformed
    from papercuts.cegis.selection import select_cuts
    from papercuts.cegis.xpair import XPairingError
    from papercuts.manifest import Manifest, ManifestError

    man = Manifest.load(manifest_path)
    by_id: dict[str, tuple[ModuleCuts, Run]] = {}
    for mod in modules:
        for run in mod.runs:
            by_id[f"{mod.name}#{run.index}"] = (mod, run)
    testable = {c.id for c in man.testable}
    stray = sorted(testable - set(by_id))
    if stray:
        raise SystemExit(f"FATAL: the manifest marks cuts testable that the pipeline did not schedule: "
                         f"{stray[:10]}")

    # 1. no-ops
    noop_ids = []
    for cid, (mod, run) in by_id.items():
        if mod.pc.cut_index_text([run.index]) == mod.baseline:
            ctype, line = mod.cut_infos[run.index]
            run.noop = True
            mod.noops.append(run.index)
            noop_ids.append(cid)
            append_result(results_path, {
                "ts": time.time(), "phase": "cuts", "method": "cegis-comb", "module": mod.name,
                "idx": run.index, "type": ctype, "line": line, "valid": False, "verdict": "noop",
                "in_set": False,
            })
            status(f"WARNING: {mod.name} idx={run.index} {ctype} L{line} is a NO-OP (identical to "
                   f"the source); its select is held off")
    for mod in modules:
        mod.runs = [r for r in mod.runs if not getattr(r, "noop", False)]
    by_id = {cid: v for cid, v in by_id.items() if cid not in set(noop_ids)}

    # 2. select
    tracker.set_phase("cuts")
    for cid, (mod, run) in by_id.items():
        tracker.register(id(run), "cuts", f"{mod.name}_pc{run.index}", mod.cut_infos[run.index][0], flush=False)
    tracker.flush()
    n_individual = [0]

    def on_event(kind, **f):
        if kind == "stage":
            extra = ", ".join(f"{k}={v}" for k, v in f.items() if k != "name")
            status(f"CEGIS {f['name']}" + (f" ({extra})" if extra else ""))
        elif kind == "iteration" and f["it"] % 10 == 0:
            status(f"CEGIS iteration {f['it']}: {f['on']} cuts on")
        elif kind == "done":
            status(f"CEGIS converged: {f['on']} cuts in the set, {f['total_s']}s")
        elif kind == "individual":
            n_individual[0] += 1
            if n_individual[0] % 100 == 0:
                status(f"CEGIS individual checks: {n_individual[0]} done")

    sel_task = object()
    tracker.start(id(sel_task), "cegis", "select")
    t0 = time.time()
    try:
        sel = await asyncio.to_thread(
            select_cuts, man, f"{output_dir}/cegis", solver=args.cegis_solver, timeout=args.cegis_timeout,
            individual=args.cegis_individual, pin_off=[c for c in noop_ids if c in testable],
            verbose=args.verbose, on_event=on_event,
            slang_args=("--ignore-initial",) if args.cegis_ignore_initial else ())
    except (DesignAssertsPresent, DesignMalformed, MiterMismatch, ManifestError, AnonymousX,
            XPairingError) as e:
        tracker.finish(id(sel_task), "error", False)
        raise SystemExit(f"FATAL: CEGIS cut selection: {type(e).__name__}: {e}")
    tracker.finish(id(sel_task), "proven", True)
    sel_s = time.time() - t0

    on = set(sel.ids)
    control_ids = {c.id for c in sel.control_net}
    for cid, (mod, run) in by_id.items():
        ctype, line = mod.cut_infos[run.index]
        run.in_set = cid in on
        if cid not in testable or cid in control_ids:
            # control-net cuts were never searched: the model cannot see them
            run.valid, run.verdict = False, "untested"
        elif sel.individual is None:
            run.valid, run.verdict = False, "not-checked"
        else:
            run.valid = sel.individual[cid]
            run.verdict = "proven" if run.valid else "cex"
        tracker.finish(id(run), run.verdict, run.valid)
        append_result(results_path, {
            "ts": time.time(), "phase": "cuts", "method": "cegis-comb", "module": mod.name,
            "idx": run.index, "type": ctype, "line": line, "valid": run.valid, "verdict": run.verdict,
            "in_set": run.in_set, "how": (sel.individual_how or {}).get(cid),
        })
    untested = sum(1 for cid in by_id if cid not in testable)
    summary = [
        f"method cegis-comb: {len(on)} cuts in the selected set, of {len(by_id) - untested - len(control_ids)} "
        f"searched ({untested} untested: their select drives nothing; {len(control_ids)} untested: they reach "
        f"a clock or asynchronous control, which the model cannot see); selection {sel_s:.1f}s",
        "valid = the cut on its own under the combinational spec (registers equal => outputs and "
        "next states equal)" + ("" if sel.individual is not None else "; not checked (--no-cegis-individual)"),
        f"power-up: {'holds' if sel.base_ok else 'DOES NOT HOLD'} ({sel.base_evidence})",
        f"registers compared: {len(sel.registers)}"
        + (f"; not compared: {', '.join(sel.registers_dropped)}" if sel.registers_dropped else ""),
        f"X sources: {sel.xpair.summary()}",
    ]
    for line in summary:
        status(line)

    # Machine-readable twin of the header above, for batch reports. Rewritten
    # after the joint check, so a run that dies there still leaves the rest.
    with open(sel.btor) as f:
        ops = [ln.split()[1] for ln in f if ln[:1].isdigit()]
    by_family: dict[str, dict] = {}
    for cid, (mod, run) in by_id.items():
        fam = by_family.setdefault(cut_family(mod.cut_infos[run.index][0]),
                                   {"planned": 0, "valid": 0, "in_set": 0, "untested": 0})
        fam["planned"] += 1
        fam["valid"] += bool(run.valid)
        fam["in_set"] += bool(run.in_set)
        fam["untested"] += run.verdict == "untested"
    record = {
        "top": top_name, "mode": "in-situ" if args.in_situ else "elaborated",
        "cuts": len(by_id) + len(noop_ids), "noops": len(noop_ids), "untested": untested,
        "control_net": len(control_ids), "searched": len(by_id) - untested - len(control_ids), "in_set": len(on),
        "individually_valid": None if sel.individual is None else sum(sel.individual.values()),
        "by_family": by_family,
        "iterations": len(sel.stats), "loop_s": sel.total_s, "individual_s": sel.individual_s,
        "selection_s": sel_s, "base_ok": sel.base_ok,
        "registers": len(sel.registers), "registers_dropped": sel.registers_dropped,
        "btor": {"nodes": len(ops), "inputs": ops.count("input"), "states": ops.count("state")},
        "xpair": dataclasses.asdict(sel.xpair), "joint": None,
    }

    def write_record():
        with open(f"{output_dir}/cegis/summary.json", "w") as f:
            json.dump(record, f, indent=1)

    write_record()
    write_papercuts_log(log_path, modules, checked=True, fv_gate=fv_gate, prune_stats=prune_stats,
                        cegis=summary)

    # 3. consolidate the set and check the whole design
    status(f"Consolidating the selected set ({len(on)} cuts)...")
    texts = []
    for mod in modules:
        if mod.excluded:
            text = print_tree(mod.tree)
        else:
            working = sorted(r.index for r in mod.runs if r.in_set)
            text = print_tree(mod.pc.cut_index(working)) if working else print_tree(mod.tree)
            if args.prune_dead and not args.in_situ:
                text, _ = prune_dead_source(text, mod.name, fold_constants=args.fold_constants)
        with open(f"{consolidated_dir}/{mod.name}.sv", "w") as f:
            f.write(text)
        texts.append(text)
    joint_dir = f"{consolidated_dir}/joint"
    os.makedirs(joint_dir, exist_ok=True)
    joint_path = f"{joint_dir}/{top_name}_consolidated.sv"
    with open(joint_path, "w") as f:
        f.write("\n".join(texts))
    joint = Run(top_module_path=f"{ctree_dir}/{top_name}.sv", spec_lib_path=ctree_dir,
                impl_module_path=joint_path, impl_module_folder=joint_dir, is_top=True, index=-1)

    status("Verifying the whole consolidated design (from power-up, no reset)...")
    tracker.set_phase("consolidate")
    tracker.start(id(joint), "consolidate", "joint")
    t0 = time.time()
    try:
        await backend.check_from_power_up(joint)
    finally:
        tracker.finish(id(joint), joint.verdict, joint.valid)
        append_result(results_path, {
            "ts": time.time(), "phase": "consolidate", "method": "cegis-comb", "module": top_name,
            "idx": -1, "type": "joint", "line": None, "valid": joint.valid, "verdict": joint.verdict,
            "applied": len(on), "elapsed": round(time.time() - t0, 3),
        })
    write_papercuts_log(log_path, modules, checked=True, fv_gate=fv_gate, prune_stats=prune_stats,
                        cegis=summary, joint=joint)
    record["joint"] = {"valid": joint.valid, "verdict": joint.verdict, "s": round(time.time() - t0, 3)}
    write_record()
    if not joint.valid:
        raise SystemExit(
            f"FATAL: the consolidated design {_failed_as(joint.verdict)} the golden one (verdict={joint.verdict}), "
            f"although the selected set was proven on the muxed design. The cut and mux paths disagree, "
            f"or the CEGIS model and the backend read the design differently. See {joint_dir}.")
    status(f"Consolidated design PROVEN ({len(on)} cuts); results written to {log_path}")


def _failed_as(verdict) -> str:
    """How a failed check relates the two designs. Only a cex shows them unequal;
    an `error` verdict means the backend never got to a proof (an elaborated
    file Jasper could not compile was reported as "not equivalent")."""
    if verdict == "cex":
        return "is not equivalent to"
    if verdict == "error":
        return "could not be checked (backend error, no proof ran) against"
    return "was not proven equal to"


# MARK: Main
async def main():
    parser = argparse.ArgumentParser(description="Process a SystemVerilog file.")

    parser.add_argument("input_files", help="The input SystemVerilog files to process", nargs="+")
    parser.add_argument("-e", "--check-equivalence", action="store_true")
    parser.add_argument("-m", "--mux-rewrites", action="store_true")
    parser.add_argument(
        "--mux-suffix",
        default="_muxed",
        help="Suffix appended to every muxed module's name (default: _muxed), so the "
        "muxed copy can be elaborated alongside the original -- which is what a miter "
        "needs. Pass an empty string to emit the muxed copy under the original names.",
    )
    parser.add_argument(
        "-j",
        "--max-jobs",
        type=int,
        default=32,
        help="Maximum number of equivalence-check jobs to run in parallel (default: 32)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print concretization/cut debug output (off by default)",
    )
    parser.add_argument(
        "--shrink-with-intermediate",
        action="store_true",
        help="Use the legacy bit-shrink strategy: introduce an intermediate "
        "'<signal>_papercuts' wire with its MSB forced to 0 and redirect reads "
        "to it. The default instead narrows the declaration in place "
        "(e.g. 'logic [7:0] x;' -> 'logic [6:0] x;'). Always on under --method "
        "cegis-comb, whose bit-shrink muxes model exactly this form.",
    )
    parser.add_argument(
        "--binops-in-conditions-only",
        action="store_true",
        help="Restrict the binop cut family to binops inside the condition of "
        "an 'if' statement or a ternary ('?:') -- e.g. the 'x | y' in "
        "'if (x | y)' or the 'x | z' in 'x | z ? b : c'. Binops elsewhere "
        "(branch bodies, assignment RHSs, etc.) are not cut. Other cut "
        "families are unaffected.",
    )
    parser.add_argument(
        "--iterative-bitshrink",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="After a bit-shrink cut is proven equivalent, keep removing bits "
        "from that same signal/dimension one at a time until a shrink fails, "
        "keeping the last passing width (greedy maximal shrink). On by default "
        "under --method fv; pass --no-iterative-bitshrink to disable. Only has "
        "an effect with -e. Costs up to (width-1) extra checks per shrinkable "
        "dimension; the consolidated design reflects each signal's maximal proven "
        "width. Not available under --method cegis-comb, whose muxes remove one bit.",
    )
    parser.add_argument(
        "--max-bitshrink-bits",
        type=int,
        default=0,
        metavar="N",
        help="With --iterative-bitshrink, cap how many bits may be removed from "
        "any one dimension (default 0 = unlimited, bounded only by the "
        "dimension's width-1).",
    )
    parser.add_argument(
        "--backend",
        default="jg",
        choices=sorted(discover_backends()),
        help="Equivalence-checking backend (default: jg)",
    )
    parser.add_argument(
        "--exclude-module",
        action="append",
        metavar="NAME",
        help="Module definition name (or fnmatch glob) to leave uncut: keep it "
        "in the golden source but skip enumerating/checking any cuts on it. "
        "Repeatable. Unions with the backend's recommended defaults.",
    )
    parser.add_argument(
        "--exclude-modules-file",
        default=None,
        help="File listing one module name/glob per line to exclude "
        "('#' comments and blank lines ignored).",
    )
    parser.add_argument(
        "--cut-only",
        action="append",
        metavar="NAME",
        help="Enumerate/check cuts ONLY on modules whose name matches one of "
        "these fnmatch globs; every other module is still elaborated and kept in "
        "the golden source (so it provides real context) but is never cut. "
        "Repeatable. The inverse of --exclude-module; use it to reduce one target "
        "while keeping surrounding logic as context.",
    )
    parser.add_argument(
        "-I",
        "--incdir",
        action="append",
        default=[],
        metavar="DIR",
        help="Add an include-search directory (like +incdir) so inputs that "
        "'`include' headers can be parsed. Repeatable.",
    )
    parser.add_argument(
        "-D",
        "--define",
        action="append",
        default=[],
        metavar="NAME[=VALUE]",
        help="Predefine a macro before preprocessing (like +define). Repeatable.",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="Ignore the exclusions the selected backend recommends by default.",
    )
    parser.add_argument(
        "--in-situ",
        action="store_true",
        help="Cut the original source in place: skip elaboration entirely, so "
        "parameters are not concretized, generate loops are not unrolled, and a "
        "module instantiated N times stays ONE definition instead of becoming N "
        "specialized copies. Each cut therefore has to hold for every instance of "
        "the definition it edits (the equivalence check enforces this for free, "
        "since it elaborates at the design top), which yields fewer but "
        "source-actionable cuts. A read-only elaboration is still used to find the "
        "top and to learn signal widths for parameterized bit-shrinks; pass --top "
        "to skip even that.",
    )
    parser.add_argument(
        "--top",
        default=None,
        metavar="NAME",
        help="Name of the design top, for sources that leave more than one module "
        "uninstantiated (unused helpers): only its hierarchy is elaborated and cut. "
        "Under --in-situ it also makes the read-only elaboration optional: a design "
        "that cannot be elaborated is still cuttable (bit-shrinks on parameterized "
        "ranges are then skipped, since their real widths are unknown).",
    )
    parser.add_argument(
        "--fold-constants",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resolve fully-constant subexpressions in the elaborated source "
        "(e.g. generate-loop junk like '0 * 10') to their folded value "
        "(default: on; use --no-fold-constants to emit the raw arithmetic).",
    )
    parser.add_argument(
        "--prune-dead",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Before enumerating cuts, remove dead code from the elaborated "
        "source -- signals that are never read or never driven, the logic that "
        "solely drives them, and declarations (localparams/typedefs) that become "
        "unused -- iterating to a fixpoint. Also re-prunes each consolidated "
        "module after cutting. Cuts on dead logic would just burn FV checks "
        "proving that deleting something no output reads changes nothing, so "
        "this cuts both enumeration and FV time; pruning only removes "
        "unobservable logic, and the elaboration-vs-original FV gate backstops "
        "it. On by default; --no-prune-dead disables. No effect under --in-situ "
        "(no elaboration/emit step).",
    )
    parser.add_argument(
        "--only-families",
        metavar="LIST",
        default=None,
        help="Comma-separated list of cut families to enumerate/check; all "
        "others are skipped entirely (not scheduled, not consolidated). Valid "
        f"families: {', '.join(CUT_FAMILIES)}. E.g. '--only-families bitshrink' "
        "does only (iterative) bit-shrinks; '--only-families bitshrink,binop' "
        "does both. Default: all families.",
    )
    parser.add_argument(
        "--method",
        choices=("fv", "cegis-comb"),
        default="fv",
        help="How the cuts that pass are chosen (both need -e). 'fv' (default): "
        "check every cut with the backend, consolidate each module's winners, and "
        "check each consolidated module. 'cegis-comb': mux every cut in (implies -m) "
        "and let a CEGIS loop find the largest set of cuts that, together, leave "
        "the logic between the registers unchanged (sound, but stricter than SEC: "
        "cuts valid only through reachability or cancellation are left out); "
        "consolidate that set and check the whole consolidated design with the "
        "backend once. The backend still runs both gates.",
    )
    parser.add_argument(
        "--cegis-solver",
        default="auto",
        metavar="STRATEGY",
        help="--method cegis-comb: SMT strategy for the loop's queries: auto "
        "(yices-smt2, else bitwuzla, else a z3 tactic pipeline), ext:BINARY, "
        "tactic:a,b,c, or qfbv.",
    )
    parser.add_argument(
        "--cegis-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="--method cegis-comb: limit per solver call. A call that runs out "
        "stops the run with an error; default: no limit.",
    )
    parser.add_argument(
        "--jasper-bbox-array",
        type=int,
        default=None,
        metavar="BITS",
        help="Jasper: black-box only arrays larger than BITS (elaborate -bbox_a). "
        "Jasper's default black-boxes much smaller arrays and then compares every "
        "write into them, read or not, which can fail a check on a valid cut "
        "(hls covariance). Raising it models the arrays instead, at a large memory "
        "cost on array-heavy designs; off by default.",
    )
    parser.add_argument(
        "--jasper-engines",
        type=int,
        default=0,
        metavar="N",
        help="Jasper: proof-engine jobs one check may run at once "
        "(set_proofgrid_max_local_jobs; one job per engine). A check runs its whole "
        "engine portfolio in parallel, ~28 engines and up to ~50 processes, so the "
        "machine load is about -j times that. 0 (default) leaves the portfolio "
        "whole; the proof strategy needs at least 4, and a cap drops engines, which "
        "can make a hard check far slower (a deep cex: 81 s uncapped, >15 min at 16).",
    )
    parser.add_argument(
        "--cegis-ignore-initial",
        action="store_true",
        help="--method cegis-comb: build the CEGIS netlists with read_slang "
        "--ignore-initial, for designs whose initial blocks it rejects (e.g. "
        "`mem[i] <= $random`). Jasper ignores initial constructs too; ROM contents "
        "loaded in an initial block are then free in the CEGIS model as well.",
    )
    parser.add_argument(
        "--cegis-individual",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="--method cegis-comb: also check each cut on its own (one solver call "
        "per cut, fewer where a learned conflict core already answers), so the "
        "per-type statistics count valid cuts, not only the selected set. On by "
        "default.",
    )

    # Two-phase parse: resolve the backend, let it add its own args, then parse.
    # Phase 1 runs on a throwaway parser that knows only --backend and has no
    # -h/--help of its own: parsing with the real parser here would make --help
    # exit before the backend has registered its arguments, so they would never
    # appear in the help text.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--backend", default="jg")
    pre_args, _ = pre.parse_known_args()
    backend_cls = get_backend(pre_args.backend)
    backend_cls.add_cli_args(parser)
    args = parser.parse_args()

    set_verbose(args.verbose)

    # Resolve --only-families into a set (None = all families). Validate names up
    # front so a typo fails fast instead of silently selecting zero cuts.
    only_families: set[str] | None = None
    if args.only_families is not None:
        requested = [f.strip() for f in args.only_families.split(",") if f.strip()]
        unknown = [f for f in requested if f not in CUT_FAMILIES]
        if unknown:
            parser.error(
                f"--only-families: unknown family {unknown} "
                f"(valid: {', '.join(CUT_FAMILIES)})"
            )
        only_families = set(requested)

    if args.method == "cegis-comb":
        if not args.check_equivalence:
            parser.error("--method cegis-comb needs -e: the backend runs both gates and "
                         "checks the consolidated design")
        if args.iterative_bitshrink:
            parser.error("--iterative-bitshrink is not available under --method cegis-comb: "
                         "a bit-shrink mux removes exactly one bit")
        args.iterative_bitshrink = False
        args.mux_rewrites = True  # the loop works on the muxed design
        # The set is proven on the muxes, and a bit-shrink mux is a companion wire
        # whose MSB reads as 0 while the signal keeps its width and its writes.
        # Consolidation must apply that same form. Narrowing the declaration in
        # place is a different rewrite: a constant-index write to the dropped bit
        # stops compiling (vtr__multiclock_reader_writer), and a variable-index
        # read of it becomes an out-of-range X instead of 0.
        args.shrink_with_intermediate = True
    elif args.iterative_bitshrink is None:
        args.iterative_bitshrink = True


    backend = backend_cls.from_args(args) if args.check_equivalence else None

    # Modules to keep in the golden source but never cut. Union of the user's
    # --exclude-module / --exclude-modules-file with the backend's recommended
    # defaults (unless --no-default-excludes). Matching is fnmatch, so bare
    # names match exactly and globs (e.g. "lib_*") match families. These
    # patterns feed the elaborator's `ignore` (excluded modules are emitted
    # verbatim under their original names) and are matched again below, via
    # is_excluded, against those same names to skip cutting them. Under
    # --in-situ nothing is emitted or renamed, so they are only matched there.
    exclude_patterns: set[str] = set(args.exclude_module or [])
    if args.exclude_modules_file:
        with open(args.exclude_modules_file) as f:
            for raw in f:
                s = raw.split("#", 1)[0].strip()
                if s:
                    exclude_patterns.add(s)
    if backend is not None and not args.no_default_excludes:
        exclude_patterns |= backend.default_excluded_modules()

    # --cut-only: fnmatch globs; only matching modules are cut. Empty => cut all
    # (minus excludes). Include-dir / macro passthrough for parsing inputs that
    # are not self-contained.
    cut_only_patterns: set[str] = set(args.cut_only or [])
    incdirs = list(args.incdir or [])
    defines = list(args.define or [])

    # TODO: change this to based on the source file directory
    output_dir = "./outputs"

    if os.path.exists(output_dir):
        # A blocking shutil.rmtree here can cost minutes on NFS: a previous run
        # leaves one folder per module, 10k+ cut sources, and -- after an -e run
        # -- a jgproject tree per cut, each unlink a network round-trip. Rename
        # the stale tree aside (one metadata op) and delete it in a detached
        # background process so enumeration starts immediately. start_new_session
        # detaches the rm so it outlives this (possibly short) run.
        stale = f"{output_dir}.stale.{os.getpid()}"
        os.rename(output_dir, stale)
        subprocess.Popen(["rm", "-rf", stale], start_new_session=True)

    os.makedirs(output_dir, exist_ok=True)

    # Live status for an external viewer (`python -m papercuts.status --watch`).
    # Records each equivalence check's lifecycle to outputs/status.json so the
    # user can watch which FV checks are running, and for how long, from a second
    # terminal. Only meaningful when checks actually run (a backend is selected).
    tracker = StatusWriter(output_dir) if backend is not None else None

    # Append-only durable record of every finished check (see append_result). The
    # output dir was just recreated, so this always starts empty.
    results_path = f"{output_dir}/{RESULTS_FILENAME}"

    # The original design, split one module per file: the golden ("spec") side of
    # the elaboration-equivalence gate below, and -- under --in-situ -- the cut
    # spec library itself. Every consumer resolves modules by file stem, so one
    # definition per name is required.
    orig_dir = f"{output_dir}/orig"
    os.makedirs(orig_dir, exist_ok=True)
    orig_sm, orig_opts = make_parse_env(incdirs, defines)
    raw_trees = [SyntaxTree.fromFile(f, orig_sm, orig_opts) for f in args.input_files]
    orig_trees: list[tuple[SyntaxTree, str]] = []
    defined_in: dict[str, str] = {}
    for src, raw in zip(args.input_files, raw_trees):
        for name, tree in chipper.split_tree(raw):
            if name in defined_in:
                raise SystemExit(
                    f"FATAL: module '{name}' is defined in both {defined_in[name]} "
                    f"and {src}. Papercuts addresses modules by name; pass only one "
                    f"definition of each (or exclude the duplicate)."
                )
            defined_in[name] = src
            orig_trees.append((tree, name))
            with open(f"{orig_dir}/{name}.sv", "w") as f:
                f.write(print_tree(tree))

    # `symbolic_ranges[def][signal]` = the (left, right) each of that signal's packed
    # dimensions evaluates to (outermost first), so bit-shrink can narrow
    # parameterized ranges (`logic [WIDTH-1:0] x;`) -- every dimension, including
    # those of multi-packed-dim vectors. Empty => those stay uncut.
    symbolic_ranges: dict[str, dict[str, list[tuple[int, int]]]] = {}
    elab_blob_path = None
    gate_map_path = None  # check_sec commands pairing what elaboration renamed
    # Dead-code prune summary for the elaborated blob (None => pruning not run,
    # e.g. --no-prune-dead or --in-situ). Recorded into papercuts.log.
    prune_stats = None

    if args.in_situ:
        # MARK: In-situ front end -- no elaboration, no rewriting.
        # The original parameterized definitions ARE the canonical source, so
        # orig_dir doubles as the cut spec lib. A definition is shared by all its
        # instances, so a cut must hold for every one of them -- which the checks
        # below enforce for free, since each elaborates at the design top.
        conc_trees, ctree_dir = orig_trees, orig_dir
        top_name = args.top
        try:
            comp = build_compilation_from(raw_trees)
            symbolic_ranges = chipper.definition_signal_ranges(comp)
            tops = [t.name for t in comp.getRoot().topInstances]
        except Exception as e:
            if top_name is None:
                raise SystemExit(
                    f"FATAL: could not elaborate to discover the top module ({e}). "
                    f"Pass --top NAME to cut without elaborating at all."
                )
            status(f"WARNING: read-only elaboration failed ({e}); bit-shrink cuts "
                   f"on parameterized ranges will be skipped.")
        else:
            if top_name is None:
                if len(tops) != 1:
                    raise SystemExit(
                        f"FATAL: expected exactly one top-level instance, got "
                        f"{tops or 'none'}. Pass --top NAME to choose one."
                    )
                top_name = tops[0]
        if top_name not in defined_in:
            raise SystemExit(f"FATAL: top module '{top_name}' is not among the inputs.")
        # No renaming happens in situ, so a module can be left uncut on its own
        # without dragging its subtree along (unlike the elaborated path below).
        verbatim = {
            n for _, n in conc_trees
            if any(fnmatch.fnmatch(n, p) for p in exclude_patterns)
        }
        status(f"In-situ top: {top_name} ({len(conc_trees)} module definitions, "
               f"not elaborated)")
        if args.prune_dead:
            status("note: --prune-dead has no effect under --in-situ "
                   "(no elaboration/emit step to prune); original source is cut as-is")
    else:
        # MARK: Elaboration -- the canonical front end.
        # Unroll generates, resolve parameters, and flatten hierarchy up front by
        # re-emitting the whole design from a live slang elaboration. Excluded
        # modules are emitted verbatim (opaque boundaries) so they reach the formal
        # tool as their original source and parents instantiate them by their
        # original name. Everything downstream (the FV gate and the cut pipeline)
        # treats the elaborated source as canonical -- chipper.eval_modules
        # concretization is no longer part of the flow.
        status("Elaborating design (unroll + flatten + concretize)...")
        try:
            elab = elaborate_design(args.input_files, flatten=True, ignore=exclude_patterns,
                                    fold_constants=args.fold_constants,
                                    incdirs=incdirs, defines=defines, top=args.top)
        except (ElaborationError, EmitError) as e:
            raise SystemExit(f"FATAL: elaboration failed: {e}")

        if len(elab.tops) != 1:
            raise SystemExit(
                f"FATAL: expected exactly one top-level instance, got {elab.tops or 'none'}. "
                f"Pass --top NAME to choose one."
            )
        top_name = elab.top
        status(f"Elaborated top: {top_name}")

        # Dead-code prune (on by default): drop signals nothing reads or drives,
        # the logic that solely drives them, and now-unused localparams/typedefs,
        # so no cut or FV check is ever spent on logic no output can observe. Only
        # unobservable logic is removed, and the Phase-0 gate below re-proves the
        # pruned blob == original, so an over-prune fails loudly rather than
        # silently. Verbatim/excluded modules are protected (left untouched).
        elab_source = elab.source
        if args.prune_dead:
            status("Pruning dead code (unread/undriven signals)...")
            elab_source, prune_stats = prune_dead_source(
                elab_source, top_name,
                protect_modules=elab.verbatim,
                fold_constants=args.fold_constants,
            )
            status(
                f"Dead-code prune: removed {prune_stats.signals} signal(s), "
                f"{prune_stats.bits} dead bit(s), and {prune_stats.decls} decl(s) "
                f"over {prune_stats.iters} iteration(s)"
            )

        # The elaborated whole-design source is a single self-contained blob (all
        # specialized submodules + verbatim boundaries in one file). Keep it in its
        # own dir so it never pollutes the original-source library used by the gate.
        elab_dir = f"{output_dir}/elab"
        os.makedirs(elab_dir, exist_ok=True)
        elab_blob_path = f"{elab_dir}/{top_name}_elaborated.sv"
        with open(elab_blob_path, "w") as f:
            f.write(elab_source)

        # Canonical per-module sources = the elaborated blob, split one module per
        # file. This replaces the old concretized-tree list and becomes the cut spec
        # lib. split_tree yields (name, tree); the pipeline consumes (tree, name).
        blob_tree = SyntaxTree.fromText(elab_source)
        conc_trees = [(tree, name) for name, tree in chipper.split_tree(blob_tree)]

        # Jasper pairs uninitialized flops, blackboxes and X sources by name, and
        # flattening a generate scope renames them (ISSUES 96): the gate gets the map.
        # The is_top script reads impl_module_path (this blob) as Jasper's spec.
        map_text = gate_map_script(elab.renames, build_compilation_from([blob_tree]), spec_is_original=False)
        if map_text:
            gate_map_path = os.path.abspath(f"{elab_dir}/gate_map.tcl")
            with open(gate_map_path, "w") as f:
                f.write(map_text)

        # The elaborator emits an excluded module AND its whole subtree verbatim
        # (an opaque boundary), so the uncut set is exactly what it emitted
        # verbatim -- not just the pattern-matched names.
        verbatim = elab.verbatim

        ctree_dir = f"{output_dir}/concrete_sources"
        os.makedirs(ctree_dir, exist_ok=True)
        for tree, name in conc_trees:
            with open(f"{ctree_dir}/{name}.sv", "w") as f:
                f.write(print_tree(tree))

        status("Elaboration complete.")

    # Which instance connections are WRITES: a signal connected to a child's output
    # port is driven there, and neither a cut nor a mux may treat it as a read.
    # The same map goes to the cutter and the muxer, like the symbolic ranges.
    try:
        port_directions = chipper.definition_port_directions(
            build_compilation_from([t for t, _ in conc_trees]))
    except Exception as e:
        port_directions = {}
        status(f"WARNING: could not read port directions ({e}); no cut or mux will touch "
               f"a signal inside an instance's port connection.")

    def is_excluded(module_name: str) -> bool:
        return module_name in verbatim

    def is_cut_target(module_name: str) -> bool:
        # --cut-only: when set, enumerate cuts only on modules whose name matches
        # one of the fnmatch globs. Non-matching modules stay in the golden source
        # (real context) but are never cut. Empty => cut all.
        if not cut_only_patterns:
            return True
        return any(fnmatch.fnmatch(module_name, p) for p in cut_only_patterns)

    # Make a directory for the final output sources
    consolidated_dir = f"{output_dir}/consolidated_sources"
    os.makedirs(consolidated_dir, exist_ok=True)

    # Collects each individually-proven ("working") cut's source, populated after
    # equivalence checking (only ever filled on an -e run, where validity is known).
    working_dir = f"{output_dir}/working_cuts"
    os.makedirs(working_dir, exist_ok=True)

    # Let the backend write whatever tool scripts it needs (for jg, the SEC
    # TCL, with any --clock/--reset declarations baked in). Only runs when a
    # backend was selected, i.e. on an -e run.
    if backend is not None:
        backend.prepare()

    # The elaboration-vs-original FV verdict, recorded into papercuts.log below.
    fv_gate_result = None

    # MARK: Phase -1 -- FV environment self-check (LOAD-BEARING).
    # Prove the original design equivalent to itself before trusting the formal
    # tool for anything real. A design is trivially equivalent to itself, so the
    # ONLY way this fails is a broken FV setup (tool not on PATH, no license, or
    # a misconfigured backend). Fail here with a clear, environment-focused
    # message rather than letting the elaboration gate
    # or the per-cut checks below misreport a setup problem as a design or
    # elaboration failure. spec == impl == the original design top, so the verdict
    # depends only on the formal environment, not on the design or elaboration.
    if backend is not None:
        status("FV self-check: verifying original design == itself...")
        selfcheck_dir = f"{output_dir}/fv_selfcheck"
        os.makedirs(selfcheck_dir, exist_ok=True)
        selfcheck_run = Run(
            top_module_path=f"{orig_dir}/{top_name}.sv",
            spec_lib_path=orig_dir,
            impl_module_path=f"{orig_dir}/{top_name}.sv",  # same source both sides
            impl_module_folder=selfcheck_dir,
            is_top=True,
            index=0,
        )
        tracker.set_phase("selfcheck")
        tracker.start(id(selfcheck_run), "selfcheck", "self-check")
        try:
            await backend.check(selfcheck_run)
        finally:
            tracker.finish(
                id(selfcheck_run),
                getattr(selfcheck_run, "verdict", None),
                selfcheck_run.valid,
            )
            append_result(results_path, {
                "ts": time.time(), "phase": "selfcheck", "module": top_name,
                "idx": None, "type": "self-check", "line": None,
                "valid": selfcheck_run.valid,
                "verdict": getattr(selfcheck_run, "verdict", None),
            })
        if not selfcheck_run.valid:
            raise SystemExit("Formal verification setup failed, check FV environment.")
        status("FV self-check passed: formal environment is working.")

    # MARK: Phase 0 -- elaboration-equivalence gate (LOAD-BEARING).
    # Prove the elaborated whole design is equivalent to the original before it is
    # trusted as canonical. Reuses the existing SEC infrastructure: a single
    # is_top check with spec = original design (orig_dir), impl = elaborated blob.
    # Both sides elaborate at the design top; the blob is self-contained so the
    # -y orig_dir search path is harmless. A non-proven verdict is fatal -- every
    # downstream cut is checked against the elaborated golden, so an unfaithful
    # elaboration would silently invalidate the entire run. Vacuous under
    # --in-situ, where the canonical source IS the original (the self-check above
    # already covers that comparison), so it is skipped there.
    if backend is not None and elab_blob_path is not None:
        status("FV gate: verifying elaborated design == original...")
        gate_dir = f"{elab_dir}/fv"
        os.makedirs(gate_dir, exist_ok=True)
        gate_run = Run(
            top_module_path=f"{orig_dir}/{top_name}.sv",
            spec_lib_path=orig_dir,
            impl_module_path=elab_blob_path,
            impl_module_folder=gate_dir,
            is_top=True,
            index=0,
            map_script=gate_map_path,
        )
        tracker.set_phase("gate")
        tracker.start(id(gate_run), "gate", "elab-gate")
        try:
            await backend.check(gate_run)
        finally:
            tracker.finish(
                id(gate_run),
                getattr(gate_run, "verdict", None),
                gate_run.valid,
            )
            append_result(results_path, {
                "ts": time.time(), "phase": "gate", "module": top_name,
                "idx": None, "type": "elab-gate", "line": None,
                "valid": gate_run.valid,
                "verdict": getattr(gate_run, "verdict", None),
            })
        if not gate_run.valid:
            verdict = getattr(gate_run, "verdict", "not-proven")
            raise SystemExit(
                f"FATAL: elaborated design {_failed_as(verdict)} the original "
                f"(verdict={verdict}). The elaboration cannot be trusted as "
                f"canonical; aborting. See {gate_dir} for the run artifacts."
            )
        fv_gate_result = (getattr(gate_run, "verdict", None) or "proven").upper()
        status("FV gate passed: elaborated design == original.")
    elif args.in_situ:
        fv_gate_result = "N/A (in-situ)"
    else:
        status("FV gate skipped (no -e/backend); elaborated source is unverified.")

    if args.mux_rewrites:
        # Bit muxes are sized from the same literal packed ranges bit-shrink uses,
        # so under --in-situ they are only inserted for non-parameterized signals.
        #
        # The per-family flags follow --only-families, so a select is inserted only
        # where a cut would actually be scheduled. Families left out still RESERVE
        # their select numbers inside insert_muxes -- --only-families does not change
        # what cut_info() enumerates, so cut indices are unchanged and pc_sel<N> has
        # to keep meaning cut N. Those reserved selects become ports that drive
        # nothing.
        fams = only_families if only_families is not None else set(CUT_FAMILIES)
        status("Performing mux rewrites"
               + (f" ({', '.join(sorted(fams))})" if only_families is not None else "")
               + "...")
        mux_dir = f"{output_dir}/muxed_sources"
        os.makedirs(mux_dir, exist_ok=True)
        muxed: dict[str, SyntaxTree] = {}
        # Selects a control was actually emitted for, per module. The rest of
        # 0..N-1 are reserved ports that drive nothing; the manifest reports them
        # as `inserted: false` so no consumer mistakes one for a testable cut.
        mux_inserted: dict[str, set[int]] = {}
        n_muxed = 0
        for tree, name in conc_trees:
            # An excluded or non-target module is never cut, so it has no selects to
            # insert -- muxing it would only burn time rebuilding a module that is
            # kept verbatim.
            if is_excluded(name) or not is_cut_target(name):
                continue
            rewrite, inserted = insert_muxes_report(
                SyntaxTree.fromText(print_tree(tree)),
                "bitshrink" in fams, "ternary" in fams, "if" in fams,
                caseMux="case" in fams,
                binopMux="binop" in fams,
                constForceMux="force-const" in fams,
                binopsInConditionsOnly=args.binops_in_conditions_only,
                # Must be exactly what this module's Papercutter gets below:
                # insert_muxes builds one internally to take the cut bands from,
                # so a difference here is a difference in what pc_sel<N> means.
                symbolicRanges=symbolic_ranges.get(name, {}),
                shrinkWithIntermediate=args.shrink_with_intermediate,
                portDirections=port_directions,
            )
            muxed[name] = rewrite
            mux_inserted[name] = set(inserted)
            n_muxed += 1

        # Wire the selects up the hierarchy. A cut belongs to a module DEFINITION,
        # so every instance of a module shares one set of selects: a parent forwards
        # one signal per (module, cut index), not one per instance. Each module
        # therefore gains an input port for every select of every module beneath it,
        # named pc_sel_<module>_<i>, and passes it down; a module's own selects keep
        # the plain pc_sel<i> names. At the top that leaves one flat, uniquely named
        # port per (module, cut) in the whole design.
        def mux_select_count(t) -> int:
            # Ports are always created for 0..N-1, including reserved ones, so the
            # highest index that appears is the count.
            nums = [int(m) for m in re.findall(r"\bpc_sel(\d+)\b", print_tree(t))]
            return max(nums) + 1 if nums else 0

        own = {n: mux_select_count(t) for n, t in muxed.items()}
        kids = {n: [c for c in get_instantiated_modules(t) if c in muxed]
                for n, t in muxed.items()}

        fwd: dict[str, list[tuple[str, int]]] = {}

        def forwarded(name: str, stack: tuple[str, ...] = ()) -> list[tuple[str, int]]:
            """Every (module, select) this module must carry for the modules below
            it, deepest-last and deduplicated."""
            if name in fwd:
                return fwd[name]
            if name in stack:
                raise SystemExit(f"module instantiation cycle: {' -> '.join(stack + (name,))}")
            out: list[tuple[str, int]] = []
            for child in kids.get(name, []):
                for entry in [(child, i) for i in range(own.get(child, 0))] + \
                             forwarded(child, stack + (name,)):
                    if entry not in out:
                        out.append(entry)
            fwd[name] = out
            return out

        for name in muxed:
            forwarded(name)

        # Rename the muxed copy so it can be elaborated next to the original: a
        # miter instantiates both, and two definitions of one name collide. Only
        # MUXED modules are renamed. A child that was never muxed (excluded or not
        # a cut target) keeps its name and is shared by both hierarchies, which is
        # correct -- it holds no cuts, so the two copies of it are identical.
        suffix = args.mux_suffix
        mux_files: dict[str, str] = {}
        renames = {n: f"{n}{suffix}" for n in muxed} if suffix else {}
        clashes = sorted(set(renames.values()) & {n for _, n in conc_trees})
        if clashes:
            raise SystemExit(
                f"--mux-suffix {suffix!r} collides with existing module(s): {', '.join(clashes)}. "
                "Choose another suffix."
            )

        for name, tree_ in muxed.items():
            extra = [f"pc_sel_{m}_{i}" for m, i in fwd[name]]
            conns = {}
            for child in kids.get(name, []):
                # The child's own selects bind to this module's forwarded signals;
                # anything the child forwards in turn passes straight through.
                pairs = [(f"pc_sel{i}", f"pc_sel_{child}_{i}") for i in range(own.get(child, 0))]
                pairs += [(f"pc_sel_{m}_{i}", f"pc_sel_{m}_{i}") for m, i in fwd[child]]
                if pairs:
                    conns[child] = pairs
            if extra or conns:
                # Wiring is keyed by the ORIGINAL child names, so it has to happen
                # before the rename.
                tree_ = wire_mux_hierarchy(tree_, extra, conns)
            if renames:
                child_renames = {c: renames[c] for c in kids.get(name, []) if c in renames}
                tree_ = rename_module(tree_, renames[name])
                if child_renames:
                    tree_ = rename_instance_types(tree_, child_renames)
            muxed_path = f"{mux_dir}/{renames.get(name, name)}.sv"
            with open(muxed_path, "w") as f:
                f.write(print_tree(tree_))
            mux_files[name] = muxed_path

        n_wired = sum(1 for n in muxed if fwd[n])
        status(f"Mux rewrites complete ({n_muxed}/{len(conc_trees)} modules, "
               f"{n_wired} wired through hierarchy"
               + (f", renamed with suffix '{suffix}'" if renames else "") + ").")

    # MARK: Phase 1 -- enumerate every cut across every module.
    # Enumeration only reads the cut PLAN (pc.cut_info(): type + line per cut,
    # cheap) and records one Run per candidate cut. It does NOT generate cut
    # sources, write files, or create folders here. Generating a cut is the
    # expensive step (a full CST clone + serialize, O(module_size) each), so
    # doing it up front for every cut is what made this phase slow and I/O-heavy.
    # Instead each source is produced just-in-time: in the -e path by the check
    # task that consumes it (so generation overlaps jg latency and only checked
    # cuts ever hit disk); in the no-e path by the writer loop below (the cut
    # sources are that path's deliverable).
    #
    # A cut whose serialized form is byte-identical to the baseline changed
    # nothing -- a cut-generation bug -- and must never reach FV, where it would
    # trivially "prove". Detecting a no-op requires generating the cut, so no-ops
    # are discovered at generation time (in-task for -e, in the writer loop for
    # no-e), not here. Consequently the pre-run plan below is a superset that
    # includes any would-be no-ops; the authoritative no-op accounting lands in
    # papercuts.log (and results.jsonl) once generation has happened.
    status("Enumerating cuts...")
    modules: list[ModuleCuts] = []
    all_runs: list[Run] = []
    for tree, name in conc_trees:
        cur_dir = f"{output_dir}/{name}"  # created lazily, only when a cut is written
        is_top = name == top_name

        if is_excluded(name) or not is_cut_target(name):
            # Left uncut: it stays in the golden source (already written to
            # ctree_dir) so cuts on modules that instantiate it still see the
            # real logic, but we enumerate and check no cuts on it. Reached either
            # via --exclude-module (verbatim boundary) or --cut-only (non-target).
            modules.append(
                ModuleCuts(
                    name=name,
                    tree=tree,
                    pc=None,
                    is_top=is_top,
                    cut_infos=[],
                    cur_dir=cur_dir,
                    excluded=True,
                )
            )
            status(f"excluding {name} from cuts")
            continue

        ntree = SyntaxTree.fromText(print_tree(tree))
        pc = Papercutter(
            ntree,
            shrink_with_intermediate=args.shrink_with_intermediate,
            binops_in_conditions_only=args.binops_in_conditions_only,
            symbolic_ranges=symbolic_ranges.get(name, {}),
            port_directions=port_directions,
        )
        cut_infos = list(pc.cut_info())  # (type, line) aligned 1:1 with cut indices

        mod = ModuleCuts(
            name=name,
            tree=tree,
            pc=pc,
            is_top=is_top,
            cut_infos=cut_infos,
            cur_dir=cur_dir,
            shrink_widths=list(pc.cut_shrink_widths()),  # per-index dim width (0 if not bitshrink)
            baseline=print_tree(ntree),
        )
        for idx in range(len(cut_infos)):
            # --only-families: skip cuts outside the selected families. They are
            # never scheduled, logged, or consolidated (everything downstream is
            # driven by mod.runs).
            if only_families is not None and cut_family(cut_infos[idx][0]) not in only_families:
                continue
            run = Run(
                top_module_path=f"{ctree_dir}/{top_name}.sv",
                spec_lib_path=ctree_dir,
                impl_module_path=f"{cur_dir}/{name}_pc{idx}.sv",
                impl_module_folder=cur_dir,
                is_top=is_top,
                index=idx,
                )
            mod.runs.append(run)
            all_runs.append(run)
        modules.append(mod)

    status(f"{len(all_runs)} candidate cuts across {len(modules)} modules")

    # MARK: Mux manifest -- the contract for whoever consumes the muxed design.
    # Written here, after enumeration, because it joins what the muxer inserted
    # (mux_inserted, captured above) with what the cutter enumerated (cut_infos and
    # cut_pairs, the same objects the log is written from). Anything derived by
    # convention instead -- select name to cut index, a module's `_muxed` name, two
    # cuts on one line being a pair -- has been wrong at least once.
    if args.mux_rewrites:
        golden_files = {mod.name: f"{ctree_dir}/{mod.name}.sv" for mod in modules}
        manifest = build_manifest(
            mode="in-situ" if args.in_situ else "elaborated",
            top=top_name,
            suffix=args.mux_suffix,
            modules=modules,
            muxed_names=renames,
            muxed_files=mux_files,
            golden_files=golden_files,
            inserted=mux_inserted,
            forwarded=fwd,
            base_dir=output_dir,
            # the backend registers these; a backend without them leaves both unset
            clock=getattr(args, "clock", None),
            reset=getattr(args, "reset", None),
        )
        problems = check_manifest(manifest, muxed_text={
            name: open(path).read() for name, path in mux_files.items()})
        if problems:
            raise SystemExit("mux manifest is inconsistent:\n  " + "\n  ".join(problems))
        manifest_path = write_manifest(manifest, output_dir)
        n_inert = sum(1 for m in manifest["modules"].values() for c in m["cuts"] if not c["inserted"])
        status(f"Mux manifest written to {manifest_path}"
               + (f" ({n_inert} reserved select(s) drive nothing)" if n_inert else ""))

    log_path = f"{output_dir}/papercuts.log"
    plan_path = f"{output_dir}/papercuts.plan.log"

    if not args.check_equivalence:
        # Enumeration-only: the cut sources ARE the deliverable, so generate and
        # write them now (folders created lazily). No-ops are detected here, so
        # this path's plan is exact.
        status("Writing cut sources...")
        for mod in modules:
            if mod.excluded:
                continue
            made_dir = False
            kept: list[Run] = []
            for run in mod.runs:
                cut_src = mod.pc.cut_index_text([run.index])
                if cut_src == mod.baseline:
                    mod.noops.append(run.index)
                    ctype, line = mod.cut_infos[run.index]
                    status(
                        f"WARNING: {mod.name} idx={run.index} {ctype} L{line} is a "
                        f"NO-OP (identical to elaborated source); excluded from FV"
                    )
                    continue
                if not made_dir:
                    os.makedirs(mod.cur_dir, exist_ok=True)
                    made_dir = True
                with open(run.impl_module_path, "w") as f:
                    f.write(cut_src)
                kept.append(run)
            mod.runs = kept

        n_noop = sum(len(m.noops) for m in modules)
        if n_noop:
            status(
                f"WARNING: {n_noop} no-op cut(s) detected (identical to elaborated "
                f"source); excluded from FV. See {log_path}"
            )
        write_cut_plan(plan_path, modules)
        write_papercuts_log(log_path, modules, checked=False, fv_gate=fv_gate_result,
                            prune_stats=prune_stats)
        status(f"Enumeration-only (no -e). Cut summary written to {log_path}")
        return

    # -e path: the plan is written before any FV run, so it is a pre-run superset
    # (no-ops are only discovered as each cut is generated during checking).
    write_cut_plan(plan_path, modules)
    status(
        f"Cut plan ({len(all_runs)} planned tests; no-ops detected during "
        f"checking) written to {plan_path}"
    )

    if args.method == "cegis-comb":
        await cegis_phase(
            args, modules, manifest_path, backend=backend, tracker=tracker,
            ctree_dir=ctree_dir, consolidated_dir=consolidated_dir, top_name=top_name,
            output_dir=output_dir, results_path=results_path, log_path=log_path,
            fv_gate=fv_gate_result, prune_stats=prune_stats,
        )
        tracker.set_phase("done")
        return

    # Label lookup + owning module for progress lines and just-in-time cut
    # generation, keyed by run identity.
    labels = {}
    owner: dict[int, ModuleCuts] = {}
    for mod in modules:
        for run in mod.runs:
            ctype, line = mod.cut_infos[run.index]
            labels[id(run)] = (mod.name, ctype, line)
            owner[id(run)] = mod

    # Register every cut as pending so the status viewer shows the full backlog
    # before the checks start dispatching. Batch: register without flushing, then
    # publish the whole backlog in one write -- flushing per cut re-serializes the
    # growing task list each time (O(n^2)), which stalled startup for minutes on
    # an 11k-cut design.
    tracker.set_phase("cuts")
    for mod in modules:
        for run in mod.runs:
            ctype = mod.cut_infos[run.index][0]
            tracker.register(
                id(run), "cuts", f"{mod.name}_pc{run.index}", ctype, flush=False
            )
    tracker.flush()

    # MARK: Phase 2 -- check every cut in parallel under one global limit.
    # Each task generates its own cut source, writes its file (creating the
    # module folder on demand), then runs the check -- all inside the semaphore,
    # so generation overlaps other checks' jg latency and at most --max-jobs cut
    # sources are ever live at once (keeps the memory bound). A cut that
    # serializes to the baseline is a no-op: recorded and skipped, never sent to
    # FV.
    status(f"Checking equivalence ({len(all_runs)} cuts, max_jobs={args.max_jobs})...")
    semaphore = asyncio.Semaphore(args.max_jobs)
    total = len(all_runs)
    done = 0

    async def run_with_limit(run: Run):
        nonlocal done
        async with semaphore:
            mod = owner[id(run)]
            mname, ctype, line = labels[id(run)]

            # Generate this cut just-in-time (CPU-bound C++, runs to completion
            # before the loop yields -- safe to call on the shared Papercutter).
            cut_src = mod.pc.cut_index_text([run.index])
            if cut_src == mod.baseline:
                run.noop = True
                mod.noops.append(run.index)
                tracker.finish(id(run), "noop", False)
                append_result(results_path, {
                    "ts": time.time(), "phase": "cuts", "module": mname,
                    "idx": run.index, "type": ctype, "line": line,
                    "valid": False, "verdict": "noop", "elapsed": 0.0,
                })
                done += 1
                status(f"EC {done}/{total}  {mname} idx={run.index} {ctype} L{line}  NO-OP")
                return run

            os.makedirs(mod.cur_dir, exist_ok=True)
            with open(run.impl_module_path, "w") as f:
                f.write(cut_src)

            tracker.start(id(run))
            t0 = time.time()
            try:
                await backend.check(run)
                # Iterative bit-shrink: once one bit is proven removable, greedily
                # remove more from the same dimension (2, 3, ...) until a check
                # fails, keeping the last passing width. Each probe re-verifies the
                # k-bit-narrower source against the golden original, so it is a
                # standalone proof (probes do not chain). Sequential within this
                # run (reuses its jgproject dir); other runs still parallel.
                if (
                    args.iterative_bitshrink
                    and run.valid
                    and ctype.startswith("bitshrink")
                ):
                    cap = mod.shrink_widths[run.index] - 1  # keep >= 1 bit
                    if args.max_bitshrink_bits > 0:
                        cap = min(cap, args.max_bitshrink_bits)
                    best = 1
                    k = 2
                    while k <= cap:
                        cand = mod.pc.cut_index_text([run.index], {run.index: k})
                        with open(run.impl_module_path, "w") as f:
                            f.write(cand)
                        await backend.check(run)
                        if run.valid:
                            best = k
                            k += 1
                        else:
                            break
                    # Restore the maximal proven shrink as this run's result.
                    run.shrink_amount = best
                    run.valid = True
                    run.verdict = "proven"
                    with open(run.impl_module_path, "w") as f:
                        f.write(mod.pc.cut_index_text([run.index], {run.index: best}))
            finally:
                tracker.finish(id(run), getattr(run, "verdict", None), run.valid)
                # Durable per-cut record, appended the moment the check finishes.
                append_result(results_path, {
                    "ts": time.time(), "phase": "cuts", "module": mname,
                    "idx": run.index, "type": ctype, "line": line,
                    "valid": run.valid, "verdict": getattr(run, "verdict", None),
                    "shrink_amount": run.shrink_amount,
                    "elapsed": round(time.time() - t0, 3),
                })
            done += 1
            verdict = "PROVEN" if run.valid else "failed"
            bits = f" -{run.shrink_amount}bit" if ctype.startswith("bitshrink") and run.valid else ""
            status(f"EC {done}/{total}  {mname} idx={run.index} {ctype} L{line}  {verdict}{bits}")
            return run

    await asyncio.gather(*(run_with_limit(run) for run in all_runs))

    # Relocate no-ops discovered during checking out of runs so the log and stats
    # treat them as no-ops (ERR rows), not as failed checks.
    for mod in modules:
        if any(getattr(r, "noop", False) for r in mod.runs):
            mod.runs = [r for r in mod.runs if not getattr(r, "noop", False)]

    n_noop = sum(len(m.noops) for m in modules)
    if n_noop:
        status(
            f"WARNING: {n_noop} no-op cut(s) detected (identical to elaborated "
            f"source); excluded from FV. See {log_path}"
        )
    n_valid = sum(1 for run in all_runs if run.valid)
    status(f"done: {n_valid}/{total - n_noop} cuts valid")

    # Persist the per-cut summary before consolidation.
    write_papercuts_log(log_path, modules, checked=True, fv_gate=fv_gate_result,
                        prune_stats=prune_stats)
    status(f"Cut summary written to {log_path}")

    # Collect every individually-proven cut's source into working_cuts/ for easy
    # access, alongside concrete_sources/ and consolidated_sources/. Each file is
    # the module's source with exactly one valid cut applied (<module>_pc<idx>.sv,
    # written by its check task); filenames are module-prefixed so a single flat
    # directory never collides across modules.
    n_working = 0
    for mod in modules:
        for run in mod.runs:
            if run.valid:
                shutil.copy2(run.impl_module_path, working_dir)
                n_working += 1
    status(f"{n_working} working cut source(s) copied to {working_dir}")

    # MARK: Phase 3 -- consolidate each module's valid cuts, then verify the
    # merged result. Each final run gets its own working dir so the checks can
    # also run in parallel without colliding on jgproject/run directories.
    status("Consolidating valid cuts...")
    final_runs: list[tuple[ModuleCuts, Run]] = []
    for mod in modules:
        out_path = f"{consolidated_dir}/{mod.name}.sv"

        if mod.excluded:
            # Emit the untouched module so the consolidated design is complete,
            # but don't re-verify it -- nothing was cut.
            with open(out_path, "w") as f:
                f.write(print_tree(mod.tree))
            continue

        working = [run.index for run in mod.runs if run.valid]
        # Per-index shrink amounts so a bitshrink cut that was greedily narrowed to
        # N bits merges at N bits (not 1). cut_index ignores amounts for non-bitshrink
        # indices, so passing every valid run's amount is safe.
        amounts = {run.index: run.shrink_amount for run in mod.runs if run.valid}
        consolidated_text = (
            print_tree(mod.pc.cut_index(working, amounts)) if working
            else print_tree(mod.tree)
        )
        # Re-prune after cutting: a cut can leave logic that now feeds nothing.
        # Skipped under --in-situ (pruning re-emits, which would defeat in-situ's
        # cut-original-source premise). The consolidated FV check below verifies
        # this pruned source against the golden, so the prune is proven too.
        if args.prune_dead and not args.in_situ:
            consolidated_text, _ = prune_dead_source(
                consolidated_text, mod.name, fold_constants=args.fold_constants
            )
        with open(out_path, "w") as f:
            f.write(consolidated_text)

        work_dir = f"{consolidated_dir}/{mod.name}"
        os.makedirs(work_dir, exist_ok=True)
        final_runs.append(
            (
                mod,
                Run(
                    top_module_path=f"{ctree_dir}/{top_name}.sv",
                    spec_lib_path=ctree_dir,
                    impl_module_path=out_path,
                    impl_module_folder=work_dir,
                    is_top=mod.is_top,
                    index=-1,
                        ),
            )
        )

    # Register consolidated runs as pending, then check them (in parallel).
    tracker.set_phase("consolidate")
    for m, fr in final_runs:
        tracker.register(id(fr), "consolidate", m.name)

    async def check_final(mod: ModuleCuts, final_run: Run):
        async with semaphore:
            tracker.start(id(final_run))
            t0 = time.time()
            try:
                return await backend.check(final_run)
            finally:
                tracker.finish(
                    id(final_run),
                    getattr(final_run, "verdict", None),
                    final_run.valid,
                )
                append_result(results_path, {
                    "ts": time.time(), "phase": "consolidate", "module": mod.name,
                    "idx": -1, "type": "consolidated", "line": None,
                    "valid": final_run.valid,
                    "verdict": getattr(final_run, "verdict", None),
                    "applied": sum(1 for r in mod.runs if r.valid),
                    "elapsed": round(time.time() - t0, 3),
                })

    status(f"Verifying {len(final_runs)} consolidated module(s)...")
    await asyncio.gather(*(check_final(m, fr) for m, fr in final_runs))

    for mod, fr in final_runs:
        status(f"consolidated {mod.name}: {'PROVEN' if fr.valid else 'FAILED'}")

    # Rewrite the log now that consolidation verdicts are known, so the final
    # papercuts.log includes both the per-cut table and the consolidated results.
    write_papercuts_log(
        log_path, modules, checked=True, final_runs=final_runs, fv_gate=fv_gate_result,
        prune_stats=prune_stats,
    )
    status(f"Consolidated results written to {log_path}")

    tracker.set_phase("done")


if __name__ == "__main__":
    asyncio.run(main())
