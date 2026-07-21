# General TODOs:
# - Change from using CST to AST for rewrites?

from __future__ import annotations
import sys
from dataclasses import dataclass, field
from pyslang.syntax import SyntaxTree

import argparse
import fnmatch
import os
import shutil
import asyncio

import papercuts.chipper as chipper
from papercuts.elaborator import elaborate_design, ElaborationError, EmitError
from papercuts.utils import print_tree, status, set_verbose, Run
from papercuts.ec import generate_jasper_tcl_script
from papercuts.backends import discover_backends, get_backend
from papercuts.pypercuts import Papercutter, insert_muxes
from papercuts.status import StatusWriter


# MARK: Module context
@dataclass
class ModuleCuts:
    """All enumerated cuts for a single (concretized) module."""

    name: str
    tree: SyntaxTree           # concretized source tree (pre-cut)
    pc: Papercutter | None     # cutter for later cut_index() consolidation (None if excluded)
    is_top: bool
    cut_infos: list            # per-index (type, line) from pc.cut_info()
    cur_dir: str
    runs: list[Run] = field(default_factory=list)
    excluded: bool = False     # kept in the golden source but never cut
    noops: list[int] = field(default_factory=list)  # cut indices identical to source (never FVed)


# MARK: papercuts.log
def write_papercuts_log(
    log_path: str,
    modules: list[ModuleCuts],
    checked: bool,
    final_runs: "list[tuple[ModuleCuts, Run]] | None" = None,
    fv_gate: "str | None" = None,
) -> None:
    """Write a text summary of every papercut that was tried.

    One row per cut: module, index, type, source line, and valid (Y/N). When
    equivalence checking was not requested, validity is unknown and shown as '-'.

    When ``final_runs`` is provided (after consolidation), a second section
    reports each module's consolidated run: how many valid cuts were merged and
    whether the merged source verified (PROVEN/FAILED).
    """
    total = sum(len(m.runs) for m in modules)
    valid = sum(1 for m in modules for r in m.runs if r.valid)
    noop = sum(len(m.noops) for m in modules)

    rows = []
    for m in modules:
        if m.excluded:
            # Kept in the golden source but never cut; no runs to report.
            rows.append((m.name, "-", "excluded", "-", "X"))
            continue
        for run in m.runs:
            ctype, line = m.cut_infos[run.index]
            if not checked:
                v = "-"
            else:
                v = "Y" if run.valid else "N"
            rows.append((m.name, run.index, ctype, line, v))
        # No-op cuts: generated but byte-identical to the elaborated source, so
        # never sent to FV. Flagged as errors here (a cut that changes nothing is
        # a cut-generation bug) rather than silently dropped.
        for idx in m.noops:
            ctype, line = m.cut_infos[idx]
            rows.append((m.name, idx, ctype, line, "ERR"))

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
        f.write(
            f"# {'module':<{w_mod}}  {'idx':>4}  {'type':<{w_type}}  {'line':>6}  valid\n"
        )
        for mod, idx, ctype, line, v in rows:
            f.write(
                f"  {mod:<{w_mod}}  {idx:>4}  {ctype:<{w_type}}  {line:>6}  {v}\n"
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


# MARK: cut plan
def write_cut_plan(
    plan_path: str, modules: list[ModuleCuts], blackboxed: "set[str] | None" = None
) -> None:
    """Write the planned equivalence tests (one row per cut to be checked).

    Emitted after enumeration but before any FV run, so the full test plan --
    every cut and its type -- is visible up front, independent of whether -e is
    used. No-op cuts (byte-identical to the elaborated source) are never checked,
    so they are excluded from the rows and only counted in the header.

    ``blackboxed`` (modules with no definition in the inputs, under
    --allow-missing-modules) never appear as cut rows -- they have no body to cut
    -- so they are recorded in a header comment for the permanent record.
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
        if blackboxed:
            f.write(
                f"# black-boxed modules (no definition in inputs): "
                f"{', '.join(sorted(blackboxed))}\n"
            )
        f.write(f"# {'module':<{w_mod}}  {'idx':>4}  {'type':<{w_type}}  {'line':>6}\n")
        for mod, idx, ctype, line in planned:
            f.write(f"  {mod:<{w_mod}}  {idx:>4}  {ctype:<{w_type}}  {line:>6}\n")


# MARK: Main
async def main():
    parser = argparse.ArgumentParser(description="Process a SystemVerilog file.")

    parser.add_argument("input_files", help="The input SystemVerilog files to process", nargs="+")
    parser.add_argument("-e", "--check-equivalence", action="store_true")
    parser.add_argument("-m", "--mux-rewrites", action="store_true")
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
        "(e.g. 'logic [7:0] x;' -> 'logic [6:0] x;').",
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
        "--no-default-excludes",
        action="store_true",
        help="Ignore the exclusions the selected backend recommends by default.",
    )
    parser.add_argument(
        "--allow-missing-modules",
        action="store_true",
        help="Accept an incomplete file list: instantiations of modules with no "
        "definition in the inputs become black boxes (opaque FV boundaries) "
        "instead of aborting. The original golden and elaborated sides are both "
        "built from the same inputs, so the missing module is absent from both "
        "and the equivalence gate compares like-for-like. Verification is then "
        "modulo those boundaries. NOTE: this suppresses undefined-module errors "
        "design-wide, so a typo'd or forgotten module silently becomes a black "
        "box -- check the reported black-box list.",
    )
    parser.add_argument(
        "--fold-constants",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resolve fully-constant subexpressions in the elaborated source "
        "(e.g. generate-loop junk like '0 * 10') to their folded value "
        "(default: on; use --no-fold-constants to emit the raw arithmetic).",
    )

    # Two-phase parse: resolve the backend, let it add its own args, then parse.
    args, _ = parser.parse_known_args()
    backend_cls = get_backend(args.backend)
    backend_cls.add_cli_args(parser)
    args = parser.parse_args()

    set_verbose(args.verbose)

    backend = backend_cls.from_args(args) if args.check_equivalence else None

    # Modules to keep in the golden source but never cut. Union of the user's
    # --exclude-module / --exclude-modules-file with the backend's recommended
    # defaults (unless --no-default-excludes). Matching is fnmatch, so bare
    # names match exactly and globs (e.g. "lib_*") match families. These
    # patterns feed the elaborator's `ignore` (excluded modules are emitted
    # verbatim under their original names) and are matched again below, via
    # is_excluded, against those same names to skip cutting them.
    exclude_patterns: set[str] = set(args.exclude_module or [])
    if args.exclude_modules_file:
        with open(args.exclude_modules_file) as f:
            for raw in f:
                s = raw.split("#", 1)[0].strip()
                if s:
                    exclude_patterns.add(s)
    if backend is not None and not args.no_default_excludes:
        exclude_patterns |= backend.default_excluded_modules()

    # TODO: change this to based on the source file directory
    output_dir = "./outputs"

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    os.makedirs(output_dir, exist_ok=True)

    # Live status for an external viewer (`python -m papercuts.status --watch`).
    # Records each equivalence check's lifecycle to outputs/status.json so the
    # user can watch which FV checks are running, and for how long, from a second
    # terminal. Only meaningful when checks actually run (a backend is selected).
    tracker = StatusWriter(output_dir) if backend is not None else None

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
                                allow_missing=args.allow_missing_modules)
    except (ElaborationError, EmitError) as e:
        hint = ""
        if not args.allow_missing_modules:
            hint = (" If the file list is intentionally incomplete, re-run with "
                    "--allow-missing-modules to black-box the absent modules.")
        raise SystemExit(f"FATAL: elaboration failed: {e}{hint}")

    if len(elab.tops) != 1:
        raise SystemExit(
            f"FATAL: expected exactly one top-level instance, got {elab.tops or 'none'}."
        )
    top_name = elab.top
    status(f"Elaborated top: {top_name}")

    # Surface black-boxed (missing-definition) modules. These are opaque FV
    # boundaries on both sides of every check; listing them lets the user catch a
    # typo'd/forgotten module that silently became a black box (see the
    # --allow-missing-modules footgun note).
    if elab.blackboxed:
        status(
            f"black-boxed {len(elab.blackboxed)} module(s) with no definition "
            f"in the inputs: {', '.join(sorted(elab.blackboxed))}"
        )

    # The elaborated whole-design source is a single self-contained blob (all
    # specialized submodules + verbatim boundaries in one file). Keep it in its
    # own dir so it never pollutes the original-source library used by the gate.
    elab_dir = f"{output_dir}/elab"
    os.makedirs(elab_dir, exist_ok=True)
    elab_blob_path = f"{elab_dir}/{top_name}_elaborated.sv"
    with open(elab_blob_path, "w") as f:
        f.write(elab.source)

    # The original design, split one module per file, is the golden ("spec") side
    # of the elaboration-equivalence gate below.
    orig_dir = f"{output_dir}/orig"
    os.makedirs(orig_dir, exist_ok=True)
    for raw in (SyntaxTree.fromFile(f) for f in args.input_files):
        for name, tree in chipper.split_tree(raw):
            with open(f"{orig_dir}/{name}.sv", "w") as f:
                f.write(print_tree(tree))

    # Canonical per-module sources = the elaborated blob, split one module per
    # file. This replaces the old concretized-tree list and becomes the cut spec
    # lib. split_tree yields (name, tree); the pipeline consumes (tree, name).
    blob_tree = SyntaxTree.fromText(elab.source)
    conc_trees = [(tree, name) for name, tree in chipper.split_tree(blob_tree)]

    def is_excluded(module_name: str) -> bool:
        # The elaborator emits an excluded module AND its whole subtree verbatim
        # (an opaque boundary), so the uncut set is exactly what it emitted
        # verbatim -- not just the pattern-matched names. Skip cutting all of it.
        return module_name in elab.verbatim

    ctree_dir = f"{output_dir}/concrete_sources"
    os.makedirs(ctree_dir, exist_ok=True)

    # Write the canonical (elaborated) per-module trees; this is the spec lib.
    for tree, name in conc_trees:
        with open(f"{ctree_dir}/{name}.sv", "w") as f:
            f.write(print_tree(tree))

    status("Elaboration complete.")

    # Make a directory for the final output sources
    consolidated_dir = f"{output_dir}/consolidated_sources"
    os.makedirs(consolidated_dir, exist_ok=True)

    # Collects each individually-proven ("working") cut's source, populated after
    # equivalence checking (only ever filled on an -e run, where validity is known).
    working_dir = f"{output_dir}/working_cuts"
    os.makedirs(working_dir, exist_ok=True)

    # write our tcl script for JasperGold equivalence checking
    with open("pcjg.tcl", "w") as f:
        f.write(generate_jasper_tcl_script())

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
    # elaboration would silently invalidate the entire run.
    if backend is not None:
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
        if not gate_run.valid:
            verdict = getattr(gate_run, "verdict", "not-proven")
            raise SystemExit(
                f"FATAL: elaborated design is not equivalent to the original "
                f"(verdict={verdict}). The elaboration cannot be trusted as "
                f"canonical; aborting. See {gate_dir} for the run artifacts."
            )
        fv_gate_result = (getattr(gate_run, "verdict", None) or "proven").upper()
        status("FV gate passed: elaborated design == original.")
    else:
        status("FV gate skipped (no -e/backend); elaborated source is unverified.")

    if args.mux_rewrites:
        status("Performing mux rewrites...")
        mux_dir = f"{output_dir}/muxed_sources"
        os.makedirs(mux_dir, exist_ok=True)
        for tree, name in conc_trees:
            rewrite = insert_muxes(SyntaxTree.fromText(print_tree(tree)), True, True, True)
            with open(f"{mux_dir}/{name}.sv", "w") as f:
                f.write(print_tree(rewrite))
        status("Mux rewrites complete.")

    # MARK: Phase 1 -- enumerate every cut across every module up front.
    # Every candidate rewrite for every module is written to disk and collected
    # into a single flat list, so the equivalence checks can later run in
    # parallel across module boundaries (up to --max-jobs) instead of one module
    # at a time.
    status("Enumerating cuts...")
    modules: list[ModuleCuts] = []
    all_runs: list[Run] = []
    for tree, name in conc_trees:
        cur_dir = f"{output_dir}/{name}"
        os.makedirs(cur_dir, exist_ok=True)
        is_top = name == top_name

        if is_excluded(name):
            # Left uncut: it stays in the golden source (already written to
            # ctree_dir) so cuts on modules that instantiate it still see the
            # real logic, but we enumerate and check no cuts on it.
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
        pc = Papercutter(ntree, shrink_with_intermediate=args.shrink_with_intermediate)
        rewrites = pc.cut_all()
        cut_infos = list(pc.cut_info())  # (type, line) aligned 1:1 with rewrites

        mod = ModuleCuts(
            name=name,
            tree=tree,
            pc=pc,
            is_top=is_top,
            cut_infos=cut_infos,
            cur_dir=cur_dir,
        )
        # Baseline every cut is compared against: the elaborated per-module source
        # (the same text written to ctree_dir and used as the FV spec). A cut whose
        # serialized form is byte-identical to this changed nothing -- a
        # cut-generation bug -- and must never reach FV, where it would trivially
        # "prove" and masquerade as a valid, removable cut. Skip it and flag it as
        # an error in the log instead.
        baseline = print_tree(ntree)
        for idx, rewrite in enumerate(rewrites):
            if print_tree(rewrite) == baseline:
                ctype, line = cut_infos[idx]
                mod.noops.append(idx)
                status(
                    f"WARNING: {name} idx={idx} {ctype} L{line} is a NO-OP "
                    f"(identical to elaborated source); excluded from FV"
                )
                continue
            run = Run(
                top_module_path=f"{ctree_dir}/{top_name}.sv",
                spec_lib_path=ctree_dir,
                impl_module_path=f"{cur_dir}/{name}_pc{idx}.sv",
                impl_module_folder=cur_dir,
                is_top=is_top,
                index=idx,
            )
            with open(run.impl_module_path, "w") as f:
                f.write(print_tree(rewrite))
            mod.runs.append(run)
            all_runs.append(run)
        modules.append(mod)

    status(f"{len(all_runs)} cuts across {len(modules)} modules")

    log_path = f"{output_dir}/papercuts.log"

    n_noop = sum(len(m.noops) for m in modules)
    if n_noop:
        status(
            f"WARNING: {n_noop} no-op cut(s) detected (identical to elaborated "
            f"source); excluded from FV. See {log_path}"
        )

    # Pre-cut test plan: every planned cut and its type, before any FV runs.
    plan_path = f"{output_dir}/papercuts.plan.log"
    write_cut_plan(plan_path, modules, blackboxed=elab.blackboxed)
    status(f"Cut plan ({len(all_runs)} planned tests) written to {plan_path}")

    if not args.check_equivalence:
        write_papercuts_log(log_path, modules, checked=False, fv_gate=fv_gate_result)
        status(f"Enumeration-only (no -e). Cut summary written to {log_path}")
        return

    # Label lookup for progress lines, keyed by run identity.
    labels = {}
    for mod in modules:
        for run in mod.runs:
            ctype, line = mod.cut_infos[run.index]
            labels[id(run)] = (mod.name, ctype, line)

    # Register every cut as pending so the status viewer shows the full backlog
    # before the checks start dispatching.
    tracker.set_phase("cuts")
    for mod in modules:
        for run in mod.runs:
            ctype = mod.cut_infos[run.index][0]
            tracker.register(id(run), "cuts", f"{mod.name}_pc{run.index}", ctype)

    # MARK: Phase 2 -- check every cut in parallel under one global limit.
    status(f"Checking equivalence ({len(all_runs)} cuts, max_jobs={args.max_jobs})...")
    semaphore = asyncio.Semaphore(args.max_jobs)
    total = len(all_runs)
    done = 0

    async def run_with_limit(run: Run):
        nonlocal done
        async with semaphore:
            tracker.start(id(run))
            try:
                await backend.check(run)
            finally:
                tracker.finish(id(run), getattr(run, "verdict", None), run.valid)
            done += 1
            mname, ctype, line = labels[id(run)]
            verdict = "PROVEN" if run.valid else "failed"
            status(f"EC {done}/{total}  {mname} idx={run.index} {ctype} L{line}  {verdict}")
            return run

    await asyncio.gather(*(run_with_limit(run) for run in all_runs))

    n_valid = sum(1 for run in all_runs if run.valid)
    status(f"done: {n_valid}/{total} cuts valid")

    # Persist the per-cut summary before consolidation.
    write_papercuts_log(log_path, modules, checked=True, fv_gate=fv_gate_result)
    status(f"Cut summary written to {log_path}")

    # Collect every individually-proven cut's source into working_cuts/ for easy
    # access, alongside concrete_sources/ and consolidated_sources/. Each file is
    # the module's source with exactly one valid cut applied (<module>_pc<idx>.sv,
    # already written at enumeration); filenames are module-prefixed so a single
    # flat directory never collides across modules.
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
        with open(out_path, "w") as f:
            if working:
                f.write(print_tree(mod.pc.cut_index(working)))
            else:
                f.write(print_tree(mod.tree))

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

    async def check_final(final_run: Run):
        async with semaphore:
            tracker.start(id(final_run))
            try:
                return await backend.check(final_run)
            finally:
                tracker.finish(
                    id(final_run),
                    getattr(final_run, "verdict", None),
                    final_run.valid,
                )

    status(f"Verifying {len(final_runs)} consolidated module(s)...")
    await asyncio.gather(*(check_final(fr) for _, fr in final_runs))

    for mod, fr in final_runs:
        status(f"consolidated {mod.name}: {'PROVEN' if fr.valid else 'FAILED'}")

    # Rewrite the log now that consolidation verdicts are known, so the final
    # papercuts.log includes both the per-cut table and the consolidated results.
    write_papercuts_log(
        log_path, modules, checked=True, final_runs=final_runs, fv_gate=fv_gate_result
    )
    status(f"Consolidated results written to {log_path}")

    tracker.set_phase("done")


if __name__ == "__main__":
    asyncio.run(main())
