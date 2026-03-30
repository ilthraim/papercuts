# General TODOs:
# - Change from using CST to AST for rewrites?

from __future__ import annotations
import sys
from typing import Callable, Union, List
from papercuts.pypercuts import Papercutter, rename_module, get_module_name
from pyslang.syntax import (
    SyntaxNode,
    SyntaxTree,
    SyntaxRewriter,
    SyntaxFactory,
    SyntaxKind,
)
from pyslang.parsing import Token, TokenKind
from pyslang.driver import Driver
from pyslang import syntax, parsing, ast, driver

import argparse
import os
import shutil
import asyncio
from pathlib import Path
from dataclasses import dataclass, field

from papercuts import ec
import papercuts.chipper as chipper
from papercuts.pc_utils import print_tree


# MARK: Modules
@dataclass
class Module:
    """A SystemVerilog module with its name and syntax tree."""

    name: str
    tree: SyntaxTree
    submodules: list[ModuleInfo]
    is_top: bool = False


@dataclass
class ModuleInfo:
    name: str
    m_type: str
    params: dict


# MARK: Run
@dataclass
class Run:
    """A single test run with input and expected output."""

    canonical_fname: str
    mod_fname: str
    input_tree: SyntaxTree
    output_tree: SyntaxTree
    index: int = 0
    wrapper_fname: str = ""
    valid: bool = False
    output: str = ""

    def run(self):
        """Run JasperGold on the wrapper file and capture output."""
        pass  # Implementation would go here


# MARK: Main
async def main():
    parser = argparse.ArgumentParser(description="Process a SystemVerilog file.")

    parser.add_argument("input_files", help="The input SystemVerilog files to process", nargs="+")
    parser.add_argument("-e", "--check-equivalence", action="store_true")

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
    if not d.parseCommandLine(srcs, driver.CommandLineOptions()):
        print("Error parsing command line arguments.")
        return

    if not d.processOptions() or not d.parseAllSources():
        print("Error processing options or parsing sources.")
        return

    # Perform elaboration and report all diagnostics
    compilation = d.createCompilation()
    d.reportCompilation(compilation, False)

    comp_root: ast.RootSymbol = compilation.getRoot()
    print(f"Compilation root has {len(comp_root.topInstances)} top-level instances.")
    print("Top-level instances:", [top.name for top in comp_root.topInstances])
    assert len(comp_root.topInstances) == 1, "Expected exactly one top-level instance."

    # Attempt concretization
    print("Extracting parameters and concretizing...")
    conc_trees = chipper.eval_modules(compilation)


    ctree_dir = f"{output_dir}/concrete_sources"
    os.makedirs(ctree_dir, exist_ok=True)

    for tree, name in conc_trees:
        with open(f"{ctree_dir}/{name}_concretized.sv", "w") as f:
            f.write(print_tree(tree))

    print("Concretization complete.")

    for tree, name in conc_trees:
        cur_dir = f"{output_dir}/{name}"
        os.makedirs(cur_dir, exist_ok=True)
        # Make sure we have all relevant trees for equivalence testing
        for itree, iname in conc_trees:
            if iname != name:
                with open(f"{cur_dir}/{iname}.sv", "w") as f:
                    f.write(print_tree(itree))

        


    # runs: list[Run] = []

    # pcutter = Papercutter(sw)

    # rewrites = pcutter.cut_all()

    # fname = get_module_name(sw)

    # for idx, rewrite in enumerate(rewrites):
    #     runs.append(
    #         Run(
    #             canonical_fname=fname,
    #             mod_fname=f"{fname}_pc{idx}",
    #             input_tree=sw,
    #             output_tree=rename_module(rewrite, f"{fname}_pc{idx}"),
    #             index=idx,
    #         )
    #     )

    # try:
    #     with open(f"{output_dir}/{fname}_concretized.sv", "w") as fout:
    #         fout.write(SyntaxPrinter.printFile(sw))
    # except Exception as e:
    #     print(f"Error writing original file: {e}")

    # for run in runs:
    #     try:
    #         with open(f"{output_dir}/{run.mod_fname}.sv", "w") as fout:
    #             fout.write(SyntaxPrinter.printFile(run.output_tree))
    #     except Exception as e:
    #         print(f"Error writing output files: {e}")

    # if args.check_equivalence:
    #     for run in runs:
    #         ec.generate_jasper_files(run, output_dir=output_dir)

    #     directory = Path(output_dir)

    #     for item in directory.glob("*_jgproject"):
    #         if item.is_dir():
    #             shutil.rmtree(item)

    #     if runs:
    #         shutil.copy(args.input_file, output_dir)
    #         os.chdir(output_dir)

    #         async def run_with_limit(semaphore, run):
    #             async with semaphore:
    #                 return await ec.run_jasper(run, True)

    #         semaphore = asyncio.Semaphore(32)  # Limit to 32 concurrent tasks

    #         tasks = [run_with_limit(semaphore, run) for run in runs]
    #         await asyncio.gather(*tasks)

    #         print("JasperGold runs complete. Processing results...")

    #         successes = ""

    #         for run in runs:
    #             with open(f"{run.wrapper_fname}_output.log", "w") as fout:
    #                 fout.write(run.output)

    #             print(
    #                 f"JasperGold run for {run.wrapper_fname} completed with return code {run.valid}"
    #             )
    #             successes += f"{run.wrapper_fname}: {'PASS' if run.valid else 'FAIL'}\n"

    #         with open("../equivalence_results.txt", "w") as fout:
    #             fout.write(successes)

    #         print("Initial equivalence checks complete. Attempting consolidation...")

    #         working_rewrites = [run.index for run in runs if run.valid]

    #         # with open(f"{fname}_consolidated.sv", "w") as fout:
    #         #     fout.write(SyntaxPrinter.printFile(pcutter.cut_index(working_rewrites)))

    #         # # ec.generate_jasper_files(consolidated_run, output_dir=".")

    #         # # result = await ec.run_jasper(consolidated_run, True)

    #         # # print(f"JasperGold run for {consolidated_run.wrapper_fname} completed with return code {consolidated_run.valid}")

    #         # os.chdir("..")
    #         # directory = Path(output_dir)


if __name__ == "__main__":
    asyncio.run(main())
