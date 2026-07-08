# General TODOs:
# - Change from using CST to AST for rewrites?

from __future__ import annotations
import sys
from dataclasses import dataclass, field
from pyslang.syntax import SyntaxTree
from pyslang.driver import Driver, CommandLineOptions
from pyslang.ast import RootSymbol

import argparse
import os
import shutil
import asyncio

import papercuts.chipper as chipper
from papercuts.utils import print_tree, status, set_verbose, Run
from papercuts.ec import generate_jasper_tcl_script
from papercuts.backends import discover_backends, get_backend
from papercuts.pypercuts import Papercutter, insert_muxes


# MARK: Module context
@dataclass
class ModuleCuts:
    """All enumerated cuts for a single (concretized) module."""

    name: str
    tree: SyntaxTree           # concretized source tree (pre-cut)
    pc: Papercutter            # cutter, retained for later cut_index() consolidation
    is_top: bool
    cut_infos: list            # per-index (type, line) from pc.cut_info()
    cur_dir: str
    runs: list[Run] = field(default_factory=list)


# MARK: papercuts.log
def write_papercuts_log(log_path: str, modules: list[ModuleCuts], checked: bool) -> None:
    """Write a text summary of every papercut that was tried.

    One row per cut: module, index, type, source line, and valid (Y/N). When
    equivalence checking was not requested, validity is unknown and shown as '-'.
    """
    total = sum(len(m.runs) for m in modules)
    valid = sum(1 for m in modules for r in m.runs if r.valid)

    rows = []
    for m in modules:
        for run in m.runs:
            ctype, line = m.cut_infos[run.index]
            if not checked:
                v = "-"
            else:
                v = "Y" if run.valid else "N"
            rows.append((m.name, run.index, ctype, line, v))

    # Column widths for aligned output.
    w_mod = max([len("module")] + [len(r[0]) for r in rows], default=len("module"))
    w_type = max([len("type")] + [len(r[2]) for r in rows], default=len("type"))

    with open(log_path, "w") as f:
        f.write(f"# papercuts summary: {total} cuts tried, {valid} valid\n")
        f.write(
            f"# {'module':<{w_mod}}  {'idx':>4}  {'type':<{w_type}}  {'line':>6}  valid\n"
        )
        for mod, idx, ctype, line, v in rows:
            f.write(
                f"  {mod:<{w_mod}}  {idx:>4}  {ctype:<{w_type}}  {line:>6}  {v}\n"
            )


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
        "--backend",
        default="jg",
        choices=sorted(discover_backends()),
        help="Equivalence-checking backend (default: jg)",
    )

    # Two-phase parse: resolve the backend, let it add its own args, then parse.
    args, _ = parser.parse_known_args()
    backend_cls = get_backend(args.backend)
    backend_cls.add_cli_args(parser)
    args = parser.parse_args()

    set_verbose(args.verbose)

    backend = backend_cls.from_args(args) if args.check_equivalence else None

    raw_trees = [SyntaxTree.fromFile(f) for f in args.input_files]

    status("Splitting modules...")

    # TODO: change this to based on the source file directory
    output_dir = "./outputs"

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    os.makedirs(output_dir, exist_ok=True)

    # Create a SyntaxTree for each module and write to output directory
    src_list = []
    for tree in raw_trees:
        split_trees = chipper.split_tree(tree)
        for tree in split_trees:
            src_list.append(f"{output_dir}/{tree[0]}.sv")
            with open(f"{output_dir}/{tree[0]}.sv", "w") as f:
                f.write(print_tree(tree[1]))

    # Parse our newly split trees and compile with pyslang to get elaborated ASTs
    d = Driver()
    d.addStandardArgs()
    srcs = " ".join([sys.argv[0]] + src_list)
    if not d.parseCommandLine(srcs, CommandLineOptions()):
        print("Error parsing command line arguments.")
        return

    if not d.processOptions() or not d.parseAllSources():
        print("Error processing options or parsing sources.")
        return

    # Perform elaboration and report all diagnostics
    compilation = d.createCompilation()
    d.reportCompilation(compilation, False)

    comp_root: RootSymbol = compilation.getRoot()
    status(f"Compilation root has {len(comp_root.topInstances)} top-level instance(s).")
    assert len(comp_root.topInstances) == 1, "Expected exactly one top-level instance."
    top_name = comp_root.topInstances[0].name

    # Attempt concretization
    status("Extracting parameters and concretizing...")
    conc_trees = chipper.eval_modules(compilation)

    ctree_dir = f"{output_dir}/concrete_sources"
    os.makedirs(ctree_dir, exist_ok=True)

    # Write concretized trees to output directory (this will be out spec lib)
    for tree, name in conc_trees:
        with open(f"{ctree_dir}/{name}.sv", "w") as f:
            f.write(print_tree(tree))

    status("Concretization complete.")

    # Make a directory for the final output sources
    consolidated_dir = f"{output_dir}/consolidated_sources"
    os.makedirs(consolidated_dir, exist_ok=True)

    # write our tcl script for JasperGold equivalence checking
    with open("pcjg.tcl", "w") as f:
        f.write(generate_jasper_tcl_script())

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

        ntree = SyntaxTree.fromText(print_tree(tree))
        pc = Papercutter(ntree)
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
        for idx, rewrite in enumerate(rewrites):
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

    if not args.check_equivalence:
        write_papercuts_log(log_path, modules, checked=False)
        status(f"Enumeration-only (no -e). Cut summary written to {log_path}")
        return

    # Label lookup for progress lines, keyed by run identity.
    labels = {}
    for mod in modules:
        for run in mod.runs:
            ctype, line = mod.cut_infos[run.index]
            labels[id(run)] = (mod.name, ctype, line)

    # MARK: Phase 2 -- check every cut in parallel under one global limit.
    status(f"Checking equivalence ({len(all_runs)} cuts, max_jobs={args.max_jobs})...")
    semaphore = asyncio.Semaphore(args.max_jobs)
    total = len(all_runs)
    done = 0

    async def run_with_limit(run: Run):
        nonlocal done
        async with semaphore:
            await backend.check(run)
            done += 1
            mname, ctype, line = labels[id(run)]
            verdict = "PROVEN" if run.valid else "failed"
            status(f"EC {done}/{total}  {mname} idx={run.index} {ctype} L{line}  {verdict}")
            return run

    await asyncio.gather(*(run_with_limit(run) for run in all_runs))

    n_valid = sum(1 for run in all_runs if run.valid)
    status(f"done: {n_valid}/{total} cuts valid")

    # Persist the per-cut summary before consolidation.
    write_papercuts_log(log_path, modules, checked=True)
    status(f"Cut summary written to {log_path}")

    # MARK: Phase 3 -- consolidate each module's valid cuts, then verify the
    # merged result. Each final run gets its own working dir so the checks can
    # also run in parallel without colliding on jgproject/run directories.
    status("Consolidating valid cuts...")
    final_runs: list[tuple[ModuleCuts, Run]] = []
    for mod in modules:
        working = [run.index for run in mod.runs if run.valid]
        out_path = f"{consolidated_dir}/{mod.name}.sv"
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

    async def check_final(final_run: Run):
        async with semaphore:
            return await backend.check(final_run)

    status(f"Verifying {len(final_runs)} consolidated module(s)...")
    await asyncio.gather(*(check_final(fr) for _, fr in final_runs))

    for mod, fr in final_runs:
        status(f"consolidated {mod.name}: {'PROVEN' if fr.valid else 'FAILED'}")


if __name__ == "__main__":
    asyncio.run(main())
