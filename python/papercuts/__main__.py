# General TODOs:
# - Change from using CST to AST for rewrites?

from __future__ import annotations
import sys
from pyslang.syntax import SyntaxTree
from pyslang.driver import Driver, CommandLineOptions
from pyslang.ast import RootSymbol

import argparse
import os
import shutil
import asyncio

import papercuts.chipper as chipper
from papercuts.utils import print_tree, Run
from papercuts.ec import generate_jasper_tcl_script, run_jasper
from papercuts.pypercuts import Papercutter, insert_muxes


# MARK: Main
async def main():
    parser = argparse.ArgumentParser(description="Process a SystemVerilog file.")

    parser.add_argument("input_files", help="The input SystemVerilog files to process", nargs="+")
    parser.add_argument("-e", "--check-equivalence", action="store_true")
    parser.add_argument("-m", "--mux-rewrites", action="store_true")

    args = parser.parse_args()

    raw_trees = [SyntaxTree.fromFile(f) for f in args.input_files]

    print("Splitting modules...")

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
    print(f"Compilation root has {len(comp_root.topInstances)} top-level instances.")
    print("Top-level instances:", [top.name for top in comp_root.topInstances])
    assert len(comp_root.topInstances) == 1, "Expected exactly one top-level instance."
    top_name = comp_root.topInstances[0].name

    # Attempt concretization
    print("Extracting parameters and concretizing...")
    conc_trees = chipper.eval_modules(compilation)

    ctree_dir = f"{output_dir}/concrete_sources"
    os.makedirs(ctree_dir, exist_ok=True)

    # Write concretized trees to output directory (this will be out spec lib)
    for tree, name in conc_trees:
        with open(f"{ctree_dir}/{name}.sv", "w") as f:
            f.write(print_tree(tree))

    print("Concretization complete.")

    # Make a directory for the final output sources
    consolidated_dir = f"{output_dir}/consolidated_sources"
    os.makedirs(consolidated_dir, exist_ok=True)

    # write our tcl script for JasperGold equivalence checking
    with open("pcjg.tcl", "w") as f:
        f.write(generate_jasper_tcl_script())

    if args.mux_rewrites:
        print("Performing mux rewrites...")
        mux_dir = f"{output_dir}/muxed_sources"
        os.makedirs(mux_dir, exist_ok=True)
        for tree, name in conc_trees:
            rewrite = insert_muxes(SyntaxTree.fromText(print_tree(tree)), True, True, True)
            with open(f"{mux_dir}/{name}.sv", "w") as f:
                f.write(print_tree(rewrite))
        print("Mux rewrites complete.")

    # Now cut all of our trees
    for tree, name in conc_trees:
        runs: list[Run] = []
        cur_dir = f"{output_dir}/{name}"
        os.makedirs(cur_dir, exist_ok=True)
        is_top = name == top_name

        ntree = SyntaxTree.fromText(print_tree(tree))
        pc = Papercutter(ntree)
        rewrites = pc.cut_all()
        for idx, rewrite in enumerate(rewrites):
            runs.append(
                Run(
                    top_module_path=f"{ctree_dir}/{top_name}.sv",
                    spec_lib_path=ctree_dir,
                    impl_module_path=f"{cur_dir}/{name}_pc{idx}.sv",
                    impl_module_folder=cur_dir,
                    is_top=is_top,
                    index=idx,
                )
            )
            with open(f"{cur_dir}/{name}_pc{idx}.sv", "w") as f:
                f.write(print_tree(rewrite))

        # We've added all of our rewritten modules to the runs list, now check all of them for equivalence

        if args.check_equivalence:

            async def run_with_limit(semaphore, run):
                async with semaphore:
                    return await run_jasper(run, False)

            semaphore = asyncio.Semaphore(32)  # Limit to 5 concurrent tasks

            tasks = [run_with_limit(semaphore, run) for run in runs]
            await asyncio.gather(*tasks)

            print("JasperGold runs complete. Processing results...")

            for run in runs:
                print(
                    f"JasperGold run for {run.impl_module_path} completed with return code {run.valid}"
                )

            working_rewrites = [run.index for run in runs if run.valid]
            if working_rewrites:
                with open(f"{consolidated_dir}/{name}.sv", "w") as f:
                    f.write(print_tree(pc.cut_index(working_rewrites)))
            else:
                with open(f"{consolidated_dir}/{name}.sv", "w") as f:
                    f.write(print_tree(tree))

            final_run = Run(
                top_module_path=f"{ctree_dir}/{top_name}.sv",
                spec_lib_path=ctree_dir,
                impl_module_path=f"{consolidated_dir}/{name}.sv",
                impl_module_folder=consolidated_dir,
                is_top=is_top,
                index=-1,
            )

            print("Running JasperGold on final consolidated source...")
            await run_jasper(final_run, True)

            print(
                f"Final JasperGold run for {final_run.impl_module_path} completed with return code {final_run.valid}"
            )


if __name__ == "__main__":
    asyncio.run(main())
