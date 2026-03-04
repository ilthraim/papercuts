from __future__ import annotations
import sys
from typing import Callable, Union, List, Any
import argparse
import os
import shutil
import asyncio
from pathlib import Path
from dataclasses import dataclass

from pyslang import syntax, driver, ast
from pyslang.syntax import SyntaxTree, SyntaxNode, SyntaxKind, SyntaxPrinter, SyntaxRewriter
from pyslang.parsing import Token
from pyslang.driver import Driver

from chipper import eval_modules, split_tree
from pc_utils import rename_module, visitor_wrapper, get_module_name
import ec

#MARK: Rewrites and Runs
@dataclass
class Rewrite:
    """A single text replacement that can be applied to source."""
    start_offset: int
    end_offset: int
    replacement_text: str
    matcher: Callable[[Any], bool]
    get_replacement: Callable[[Any], Any]
    description: str = ""  # optional metadata
    
    def apply(self, tree) -> SyntaxTree:
        """Apply this single rewrite to source."""
        def handler(node, rewriter, r=self):
            if r.matcher(node):
                replacement = r.get_replacement(node)
                rewriter.replace(node, replacement)

        return syntax.rewrite(tree, handler)
    
@dataclass 
class RewriteSet:
    """A collection of rewrites to be applied together."""
    rewrites: List[Rewrite]
    
    def apply(self, tree) -> SyntaxTree:
        """Apply all rewrites to tree using pyslang.rewrite()."""

        #TODO: check for overlapping rewrites

        current_tree = tree
        
        def handler(node, rewriter: SyntaxRewriter, r=self):
            matching_rewrites = [rw for rw in r.rewrites if rw.matcher(node)]

            if len(matching_rewrites) > 1:
                print(f"Warning: multiple rewrites match node at offsets {node.sourceRange.start.offset}-{node.sourceRange.end.offset}")
                for rw in matching_rewrites:
                    print(f" - Rewrite: {rw.description}")

                replacement = matching_rewrites[0].get_replacement(node)
                rewriter.replace(node, replacement)
            else:
                for rw in matching_rewrites:
                    replacement = rw.get_replacement(node)
                    rewriter.replace(node, replacement)
            
        new_tree = syntax.rewrite(current_tree, handler)
        
        return new_tree
    
    def merge(self, other: 'RewriteSet') -> 'RewriteSet':
        """Combine with another RewriteSet."""
        return RewriteSet(rewrites=self.rewrites + other.rewrites)

@dataclass
class Run:
    """A single test run with input and expected output."""
    canonical_fname: str
    mod_fname: str
    input_tree: SyntaxTree
    output_tree: SyntaxTree
    rewrite_set: RewriteSet
    wrapper_fname: str = ""
    valid: bool = False
    output: str = ""

    def run(self):
        """Run JasperGold on the wrapper file and capture output."""
        pass  # Implementation would go here

#MARK: Module Refactoring

def split_modules(tree: SyntaxTree) -> List[SyntaxTree]:
    """Split a SyntaxTree with multiple modules into a list of single-module SyntaxTrees."""
    modules = []

    def _collect_modules(obj: Union[Token, SyntaxNode], modules) -> None:
        if obj.kind == SyntaxKind.ModuleDeclaration:
            modules.append(obj)

    tree.root.visit(visitor_wrapper(_collect_modules, modules))
    print(f"Found {len(modules)} ModuleDeclaration nodes.")

    module_trees = []

    for module in modules:
        start_offset = module.sourceRange.start.offset
        end_offset = module.sourceRange.end.offset
        module_text = SyntaxPrinter.printFile(tree)[start_offset:end_offset]
        module_tree = SyntaxTree.fromText(module_text)
        module_trees.append(module_tree)

    return module_trees

#MARK: Shrink Bits
def shrink_bits(tree: SyntaxTree) -> RewriteSet:
    """Generate new SyntaxTrees with one less bit in each SimpleRangeSelect node."""
    nodes = []

    def _count_range_handle(obj: Union[Token, SyntaxNode], nodes) -> None:
        if obj.kind == SyntaxKind.SimpleRangeSelect and isinstance(obj, syntax.RangeSelectSyntax):
            nodes.append(obj[0])

    tree.root.visit(visitor_wrapper(_count_range_handle, nodes))
    print(f"Found {len(nodes)} SimpleRangeSelect nodes.")

    rewrites = []

    for index in range(len(nodes)):
        def make_matcher(target):
            def matcher(node):
                return node == target
            return matcher
        
        def get_replacement(target=nodes[index]):
            dim = int(target.getFirstToken().rawText)
            if dim > 0:
                new_dim = dim - 1
                new_node = SyntaxTree.fromText(f"{new_dim}").root
                return new_node
            else:
                return target

        dim = int(nodes[index].getFirstToken().rawText) - 1
        new_dim = max(dim, 0)

        print(dim)
        if dim > -1:
            rewrites.append(Rewrite(
                start_offset=nodes[index].sourceRange.start.offset,
                end_offset=nodes[index].sourceRange.end.offset,
                replacement_text=f"{new_dim}",
                matcher=make_matcher(nodes[index]),
                get_replacement=get_replacement,
                description=f"Shrink bit width from {dim + 1} to {new_dim}"
            ))

    return RewriteSet(rewrites=rewrites)

#MARK: Cases
def case_branch_deletion(tree: SyntaxTree) -> RewriteSet:
    """Generate new SyntaxTrees each with one StandardCaseItem node removed."""
    nodes = []

    def _count_switch_branches(obj: Union[Token, SyntaxNode], nodes) -> None:
        if obj.kind == SyntaxKind.StandardCaseItem:
            nodes.append(obj)

    tree.root.visit(visitor_wrapper(_count_switch_branches, nodes))
    print(f"Found {len(nodes)} StandardCaseItem nodes.")

    # def _case_branch_deletion_handler(node: pyslang.SyntaxNode, rewriter: pyslang.SyntaxRewriter, index, nodes) -> None:
    #     if node in nodes and nodes.index(node) == index:
    #         rewriter.remove(node)

    rewrites = []

    for index in range(len(nodes)):
        def make_matcher(target):
            def matcher(node):
                return node == target
            return matcher
        
        rewrites.append(Rewrite(
            start_offset=nodes[index].sourceRange.start.offset,
            end_offset=nodes[index].sourceRange.end.offset,
            replacement_text="",
            matcher=make_matcher(nodes[index]),
            get_replacement=lambda node: SyntaxTree.fromText("").root,
            description=f"Delete case branch at index {index}"
        ))

    return RewriteSet(rewrites=rewrites)


#MARK: Ifs
def remove_if_conditionals(tree: SyntaxTree) -> RewriteSet:
    """Generate new SyntaxTrees each with one IfGenerate node removed."""
    nodes = []

    def _count_conditionals_handle(obj: Union[Token, SyntaxNode], nodes) -> None:
        if isinstance(obj, syntax.ConditionalStatementSyntax):
            nodes.append(obj)

    tree.root.visit(visitor_wrapper(_count_conditionals_handle, nodes))
    print(f"Found {len(nodes)} IfGenerate nodes.")

    rewrites = []

    for index in range(len(nodes)):
        def make_matcher(target):
            def matcher(node):
                return node == target
            return matcher
        
        def make_replacement(use_else, target):
            def get_replacement(node):
                if use_else:
                    if node.elseClause is not None:
                        old_trivia = ""
                        for t in node.getFirstToken().trivia:
                            old_trivia += t.getRawText()
                        new_string = old_trivia + str(node.elseClause.clause)
                        new_node = SyntaxTree.fromText(new_string).root
                        return new_node
                    else:
                        old_trivia = ""
                        for t in node.getFirstToken().trivia:
                            old_trivia += t.getRawText()
                        new_node = SyntaxTree.fromText(old_trivia).root
                        return new_node
                else:
                    old_trivia = ""
                    for t in node.getFirstToken().trivia:
                        old_trivia += t.getRawText()
                    new_string = old_trivia + str(node.statement)
                    new_node = SyntaxTree.fromText(new_string).root
                    return new_node
            return get_replacement
        
        #TODO: add replacement text
        rewrites.append(Rewrite(
            start_offset=nodes[index].sourceRange.start.offset,
            end_offset=nodes[index].sourceRange.end.offset,
            replacement_text="",
            matcher=make_matcher(nodes[index]),
            get_replacement=make_replacement(False, nodes[index]),
            description=f"Remove if conditional at index {index} using 'then' branch"
        ))

        rewrites.append(Rewrite(
            start_offset=nodes[index].sourceRange.start.offset,
            end_offset=nodes[index].sourceRange.end.offset,
            replacement_text="",
            matcher=make_matcher(nodes[index]),
            get_replacement=make_replacement(True, nodes[index]),
            description=f"Remove if conditional at index {index} using 'else' branch"
        ))

    return RewriteSet(rewrites=rewrites)

#MARK: Ternary
def remove_ternary_conditionals(tree: SyntaxTree) -> RewriteSet:
    """Generate new SyntaxTrees each with one TernaryExpression node removed."""
    nodes = []

    def _count_ternary_conditionals(obj: Union[Token, SyntaxNode], nodes) -> None:
        if isinstance(obj, syntax.ConditionalExpressionSyntax):
            nodes.append(obj)

    tree.root.visit(visitor_wrapper(_count_ternary_conditionals, nodes))
    print(f"Found {len(nodes)} ConditionalExpression nodes.")

    if not nodes:
        return RewriteSet(rewrites=[])

    rewrites = []

    for index in range(len(nodes)):
        def make_matcher(target):
            def matcher(node):
                return node == target
            return matcher
        
        def make_replacement(use_left):
            def get_replacement(node):
                return node.left if use_left else node.right
            return get_replacement
        
        rewrites.append(Rewrite(
            start_offset=nodes[index].sourceRange.start.offset,
            end_offset=nodes[index].sourceRange.end.offset,
            replacement_text=nodes[index].left.getFirstToken().rawText,
            matcher=make_matcher(nodes[index]),
            get_replacement=make_replacement(True),
            description=f"Remove ternary conditional at index {index} using 'true' branch"
        ))

        rewrites.append(Rewrite(
            start_offset=nodes[index].sourceRange.start.offset,
            end_offset=nodes[index].sourceRange.end.offset,
            replacement_text=nodes[index].right.getFirstToken().rawText,
            matcher=make_matcher(nodes[index]),
            get_replacement=make_replacement(False),
            description=f"Remove ternary conditional at index {index} using 'false' branch"
        ))

    return RewriteSet(rewrites=rewrites)
    
#MARK: Main
async def main():

    parser = argparse.ArgumentParser(description="Process a SystemVerilog file.")

    parser.add_argument("input_files", help="The input SystemVerilog files to process", nargs="+")
    parser.add_argument("-s", "--shrink-bits", action="store_true")
    parser.add_argument("-c", "--delete-case-branch", action="store_true")
    parser.add_argument("-i", "--remove-if-conditionals", action="store_true")
    parser.add_argument("-t", "--remove-ternary-conditionals", action="store_true")
    parser.add_argument("-e", "--check-equivalence", action="store_true")
    parser.add_argument("--all", action="store_true", help="Apply all papercuts and check equivalence")
    parser.add_argument("--all-no-ec", action="store_true", help="Apply all papercuts without checking equivalence")

    args = parser.parse_args()
    print(f"Processing files: {args.input_files}")

    raw_trees = [SyntaxTree.fromFile(f) for f in args.input_files]

    print("Splitting modules...")

    # Create output directory
    output_dir = "./outputs"

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    os.makedirs(output_dir, exist_ok=True)

    # Create a SyntaxTree for each module and write to output directory
    src_list = []
    for tree in raw_trees:
        split_trees = split_tree(tree)
        for tree in split_trees:
            src_list.append(f"{output_dir}/{tree[0]}.sv")
            with open(f"{output_dir}/{tree[0]}.sv", "w") as f:
                f.write(SyntaxPrinter.printFile(tree[1]))

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
    conc_trees = eval_modules(compilation)

    for tree, name in conc_trees:
        with open(f"{output_dir}/{name}_concretized.sv", "w") as f:
            f.write(SyntaxPrinter.printFile(tree))

    sw = None

    print("Concretization complete.")

    runs = []

    for ctree, ctree_name in conc_trees:
        fname = ctree_name
        sw = ctree

        if args.shrink_bits or args.all or args.all_no_ec:
            print("Applying shrink bits papercut...")
            sb_trees = shrink_bits(sw)
            for i, rewrite in enumerate(sb_trees.rewrites):
                runs.append(Run(
                    canonical_fname=fname,
                    mod_fname=f"{fname}_sb{i}",
                    input_tree=sw,
                    output_tree=rename_module(rewrite.apply(sw), f"{fname}_sb{i}"),
                    rewrite_set=RewriteSet(rewrites=[rewrite])
                ))

        if args.delete_case_branch or args.all or args.all_no_ec:
            cbd_trees = case_branch_deletion(sw)
            for i, rewrite in enumerate(cbd_trees.rewrites):
                runs.append(Run(
                    canonical_fname=fname,
                    mod_fname=f"{fname}_cbd{i}",
                    input_tree=sw,
                    output_tree=rename_module(rewrite.apply(sw), f"{fname}_cbd{i}"),
                    rewrite_set=RewriteSet(rewrites=[rewrite])
                ))

        if args.remove_if_conditionals or args.all or args.all_no_ec:
            ric_trees = remove_if_conditionals(sw)
            for i, rewrite in enumerate(ric_trees.rewrites):
                runs.append(Run(
                    canonical_fname=fname,
                    mod_fname=f"{fname}_ric{i}",
                    input_tree=sw,
                    output_tree=rename_module(rewrite.apply(sw), f"{fname}_ric{i}"),
                    rewrite_set=RewriteSet(rewrites=[rewrite])
                ))

        if args.remove_ternary_conditionals or args.all or args.all_no_ec:
            rtc_trees = remove_ternary_conditionals(sw)
            for i, rewrite in enumerate(rtc_trees.rewrites):
                runs.append(Run(
                    canonical_fname=fname,
                    mod_fname=f"{fname}_rtc{i}",
                    input_tree=sw,
                    output_tree=rename_module(rewrite.apply(sw), f"{fname}_rtc{i}"),
                    rewrite_set=RewriteSet(rewrites=[rewrite])
                ))
                
        

        try:
            with open(f"{output_dir}/{fname}_concretized.sv", "w") as fout:
                fout.write(SyntaxPrinter.printFile(sw))
        except Exception as e:
            print(f"Error writing original file: {e}")

    for run in runs:
        try:
            with open(f"{output_dir}/{run.mod_fname}.sv", "w") as fout:
                fout.write(SyntaxPrinter.printFile(run.output_tree))
        except Exception as e:
            print(f"Error writing output files: {e}")

    if args.check_equivalence or args.all:
        for run in runs:
            ec.generate_ec_files(run, output_dir=output_dir)

        directory = Path(output_dir)

        for item in directory.glob("*_jgproject"):
            if item.is_dir():
                shutil.rmtree(item)
        
        if runs:
            shutil.copy(args.input_file, output_dir)
            os.chdir(output_dir)

            async def run_with_limit(semaphore, run):
                async with semaphore:
                    return await ec.run_jasper(run, True)
                
            semaphore = asyncio.Semaphore(32)  # Limit to 32 concurrent tasks

            tasks = [run_with_limit(semaphore, run) for run in runs]
            await asyncio.gather(*tasks)

            print("JasperGold runs complete. Processing results...")

            successes = ""

            for run in runs:
                with open(f"{run.wrapper_fname}_output.log", "w") as fout:
                    fout.write(run.output)

                print(f"JasperGold run for {run.wrapper_fname} completed with return code {run.valid}")
                successes += f"{run.wrapper_fname}: {'PASS' if run.valid else 'FAIL'}\n"

            with open("../equivalence_results.txt", "w") as fout:
                fout.write(successes)

            print("Initial equivalence checks complete. Attempting consolidation...")

            consolidated_rewrites = [run.rewrite_set.rewrites for run in runs if run.valid]
            consolidated_rewrites = [rw for sublist in consolidated_rewrites for rw in sublist]

            consolidated_set = RewriteSet(rewrites=consolidated_rewrites)
            consolidated_tree = consolidated_set.apply(sw)

            consolidated_run = Run(
                canonical_fname=fname,
                mod_fname=f"{fname}_consolidated",
                input_tree=sw,
                output_tree=rename_module(consolidated_tree, f"{fname}_consolidated"),
                rewrite_set=consolidated_set
            )

            with open(f"{fname}_consolidated.sv", "w") as fout:
                fout.write(SyntaxPrinter.printFile(consolidated_run.output_tree))

            ec.generate_ec_files(consolidated_run, output_dir=".")

            result = await ec.run_jasper(consolidated_run, True)

            print(f"JasperGold run for {consolidated_run.wrapper_fname} completed with return code {consolidated_run.valid}")

            os.chdir("..")
            directory = Path(output_dir)


if __name__ == "__main__":
    asyncio.run(main())