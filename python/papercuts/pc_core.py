# General TODOs:
# - Change from using CST to AST for rewrites?

from __future__ import annotations
from typing import Callable, Union, List, Any
from papercuts.pypercuts import Papercutter, rename_module
from pyslang.syntax import SyntaxNode, SyntaxTree, SyntaxRewriter, SyntaxPrinter, SyntaxFactory, SyntaxKind
from pyslang.parsing import Token, TokenKind
from pyslang import syntax, parsing, ast

import argparse
import os
import shutil
import asyncio
from pathlib import Path
from dataclasses import dataclass, field

from papercuts import concretizer
from papercuts import ec


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


# MARK: Rewrites and Runs
@dataclass
class Rewrite:
    """A single text replacement that can be applied to source."""

    matcher: Callable[[SyntaxNode], bool]
    do_replacement: Callable[[SyntaxNode, bool, SyntaxRewriter]]
    num_selections: int
    start_index: int = 0

    def apply(self, tree, index) -> SyntaxTree:
        """Apply this single rewrite to source."""
        def handler(node, rewriter, r=self):
            if r.matcher(node):
                if index == 0:
                    pass  # Do not do the rewrite
                elif index == 1:
                    r.do_replacement(node, False, rewriter)
                else:
                    r.do_replacement(node, True, rewriter)

        try:
            return syntax.rewrite(tree, handler)
        except Exception as e:
            print(f"Error applying rewrite: {e}")

        return tree  # Return original tree if rewrite fails for some reason


@dataclass
class RewriteSet:
    """A collection of rewrites to be applied together."""

    rewrites: List[Rewrite] = field(default_factory=list)
    current_index: int = 0

    def add_rewrite(self, rewrite: Rewrite) -> None:
        """Add a rewrite to the set."""
        rewrite.start_index = self.current_index
        self.rewrites.append(rewrite)
        self.current_index += rewrite.num_selections

    # def apply(self, tree) -> SyntaxTree:
    #     """Apply all rewrites to tree using syntax.rewrite()."""

    #     #TODO: check for overlapping rewrites

    #     current_tree = tree

    #     def handler(node, rewriter, r=self):
    #         matching_rewrites = [rw for rw in r.rewrites if rw.matcher(node)]

    #         if len(matching_rewrites) > 1:
    #             print(f"Warning: multiple rewrites match node at offsets {node.sourceRange.start.offset}-{node.sourceRange.end.offset}")
    #             for rw in matching_rewrites:
    #                 print(f" - Rewrite: {rw.description}")

    #             replacement = matching_rewrites[0].get_replacement(node)
    #             rewriter.replace(node, replacement)
    #         else:
    #             for rw in matching_rewrites:
    #                 replacement = rw.get_replacement(node)
    #                 rewriter.replace(node, replacement)

    #     new_tree = pyslang.rewrite(current_tree, handler)

    #     return new_tree

    def merge(self, other: "RewriteSet") -> "RewriteSet":
        """Combine with another RewriteSet."""
        return RewriteSet(rewrites=self.rewrites + other.rewrites)


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


# def consolidate_runs(tree: SyntaxTree, runs: List[Run]) -> SyntaxTree:
#     """Consolidate multiple runs into a single SyntaxTree with all rewrites applied."""

#     def handler(node, rewriter):
#         matching_rewrites = [rw for rw in runs if rw.rewrite.matcher(node)]

#         if len(matching_rewrites) > 1:
#             print(
#                 f"Warning: multiple rewrites match node at offsets {node.sourceRange.start.offset}-{node.sourceRange.end.offset}"
#             )

#             matching_rewrites[0].rewrite.do_replacement(node, True, rewriter)
#         else:
#             for rw in matching_rewrites:
#                 branch = False if rw.index == 1 else False
#                 rw.rewrite.do_replacement(node, branch, rewriter)

#     new_tree = syntax.rewrite(tree, handler)

#     return new_tree


# MARK: Helpers and Papercuts
def rewrite_wrapper(f, *args, **kwargs) -> Callable[[syntax.SyntaxNode, SyntaxRewriter], None]:
    return lambda node, rewriter: f(node, rewriter, *args, **kwargs)


def visitor_wrapper(
    f, *args, **kwargs
) -> Callable[[Union[parsing.Token, syntax.SyntaxNode]], None]:
    return lambda node: f(node, *args, **kwargs)


def _copy_tree(tree: SyntaxTree) -> SyntaxTree:
    """Return a copy of the given SyntaxTree."""
    return SyntaxTree.fromText(SyntaxPrinter.printFile(tree))


# TODO: add width 1, add multidimensions
def change_int_dimensions(
    w: int,
    f: SyntaxFactory,
    r: SyntaxRewriter,
    n: syntax.IntegerTypeSyntax,
) -> syntax.IntegerTypeSyntax:
    return f.integerType(
        n.kind,
        n.keyword,
        n.signing,
        r.makeList(
            [
                f.variableDimension(
                    r.makeToken(TokenKind.OpenBracket),
                    f.rangeDimensionSpecifier(
                        f.rangeSelect(
                            SyntaxKind.SimpleRangeSelect,
                            f.literalExpression(
                                SyntaxKind.IntegerLiteralExpression,
                                r.makeToken(TokenKind.IntegerLiteral, str(w - 1)),
                            ),
                            r.makeToken(TokenKind.Colon),
                            f.literalExpression(
                                SyntaxKind.IntegerLiteralExpression,
                                r.makeToken(TokenKind.IntegerLiteral, "0"),
                            ),
                        )
                    ),
                    r.makeToken(TokenKind.CloseBracket),
                )
            ]
        ),
    )


def make_space(r: SyntaxRewriter) -> parsing.Trivia:
    return r.makeTrivia(parsing.TriviaKind.Whitespace, " ")


def make_int_literal(r: SyntaxRewriter, value: int) -> syntax.LiteralExpressionSyntax:
    return r.factory.literalExpression(
        kind=SyntaxKind.IntegerLiteralExpression,
        literal=r.makeToken(kind=TokenKind.IntegerLiteral, text=str(value)),
    )


def make_identifier(
    r: SyntaxRewriter, name: str, trivia: List[parsing.Trivia] = []
) -> syntax.IdentifierNameSyntax:
    return r.factory.identifierName(r.makeToken(TokenKind.Identifier, name, trivia=trivia))

def rename_select(
    r: SyntaxRewriter, name: str, selectors
) -> syntax.IdentifierSelectNameSyntax:
    return r.factory.identifierSelectName(r.makeId(name), selectors)

def make_select(r: SyntaxRewriter, left: int, right: int) -> syntax.ElementSelectSyntax:
    f = r.factory

    return f.elementSelect(
        openBracket=r.makeToken(TokenKind.OpenBracket),
        selector=f.rangeSelect(
            kind=SyntaxKind.SimpleRangeSelect,
            left=make_int_literal(r, left),
            range=r.makeToken(TokenKind.Colon),
            right=make_int_literal(r, right),
        ),
        closeBracket=r.makeToken(TokenKind.CloseBracket),
    )


def make_int_vector(
    r: SyntaxRewriter, width: int, base: str, value: str
) -> syntax.IntegerVectorExpressionSyntax:
    """Creates an IntegerVectorExpressionSyntax with the given value, base, and width."""

    return r.factory.integerVectorExpression(
        size=r.makeToken(TokenKind.IntegerLiteral, str(width)),
        base=r.makeToken(TokenKind.IntegerBase, base),
        value=r.makeToken(TokenKind.IntegerLiteral, value),
    )


# MARK: Module Refactoring

def find_module_decl(tree: SyntaxTree) -> syntax.ModuleDeclarationSyntax:
    """Find the first ModuleDeclarationSyntax node in the tree."""

    decl = []

    def handler(obj: Union[parsing.Token, syntax.SyntaxNode]) -> None:
        if isinstance(obj, syntax.ModuleDeclarationSyntax):
            decl.append(obj)
            return ast.VisitAction.Interrupt

    tree.root.visit(visitor_wrapper(handler))

    if not decl:
        raise ValueError("No module declaration found in the syntax tree.")

    return decl[0]


def get_module_name(tree: SyntaxTree) -> str:
    """Get the module name from a SystemVerilog source."""

    module_decl: syntax.ModuleDeclarationSyntax = find_module_decl(tree)
    return module_decl.header.name.valueText


def add_select_inputs(tree: SyntaxTree, num_inputs: int) -> SyntaxTree:
    """Add select inputs to the module declaration."""
    source = SyntaxPrinter.printFile(tree)

    module_decl = find_module_decl(tree)

    port_node = module_decl.header.ports
    ports = module_decl.header.ports[1]  # Exclude the parentheses
    port_str = str(ports) if ports is not None else ""

    sel_str = "input logic " + ", ".join([f"pc_sel{i + 1}" for i in range(num_inputs)])

    return SyntaxTree.fromText(
        source[: port_node.sourceRange.start.offset + 1]
        + sel_str
        + (", " if port_str else "")
        + source[port_node.sourceRange.start.offset + 1 :]
    )


# MARK: Shrink Bits
def shrink_bits(tree: SyntaxTree, rewrites: RewriteSet, start_index: int) -> tuple[SyntaxTree, int]:
    def get_dimensions(node: syntax.DataDeclarationSyntax) -> int:
        if isinstance(node.type, syntax.IntegerTypeSyntax) and node.type.dimensions:
            return (
                int(node.type.dimensions[0].specifier.getFirstToken().rawText)
                - int(node.type.dimensions[0].specifier.getLastToken().rawText)
                + 1
            )
        return 1

    def get_decls(node: syntax.DataDeclarationSyntax) -> list[str]:
        decls = []
        for decl in node.declarators:
            if isinstance(decl, syntax.DeclaratorSyntax):
                decls.append(decl.name.valueText)
        return decls

    old_decl_set = set()

    def collect_old_decls(node: SyntaxNode):
        if isinstance(node, syntax.DataDeclarationSyntax):
            decls = get_decls(node)
            for decl in decls:
                old_decl_set.add(decl)

    tree.root.visit(visitor_wrapper(collect_old_decls))

    cur_index = [start_index]

    def add_decl_handler(node, r: SyntaxRewriter):
        if isinstance(node, syntax.DataDeclarationSyntax):
            width = get_dimensions(node)
            # Right now, only handle shrinking bits with width > 1, but will need to extend this
            # to handle width 1 and multidimensional arrays for more complex designs
            if width > 1:
                new_width = width - 1

                old_decls = get_decls(node)
                new_decls = [f"{decl}_papercut" for decl in old_decls]

                # Add the new rewrites to the RewriteSet for the destructive rewrites, we will
                # apply the muxed rewrites here
                def make_matcher(target_decl: str):
                    def matcher(node: SyntaxNode) -> bool:
                        return (
                            (
                                (
                                    isinstance(node, syntax.IdentifierNameSyntax)
                                    and node.identifier.valueText == target_decl
                                )
                                or (
                                    isinstance(node, syntax.IdentifierSelectNameSyntax)
                                    and node.identifier.valueText == target_decl
                                )
                                # This is to prevent overwriting the assignment of the original declaration
                            )
                            and not (
                                node.parent is not None
                                and isinstance(node.parent, syntax.BinaryExpressionSyntax)
                                and node.isEquivalentTo(node.parent.left)
                            )
                            or (
                                # Check if module body and add matcher so we can insert the new declaration
                                isinstance(node, SyntaxNode)
                                and node.kind == SyntaxKind.SyntaxList
                                and node.parent is not None
                                and isinstance(node.parent, syntax.ModuleDeclarationSyntax)
                                and node == node.parent.members
                            )
                        )

                    return matcher

                f = r.factory

                new_decl_list = []
                for d in new_decls:
                    new_decl_list.append(
                        f.declarator(
                            r.makeToken(TokenKind.Identifier, d),
                            r.makeList([]),
                        )
                    )
                    if d != new_decls[-1]:
                        new_decl_list.append(r.makeComma())

                if isinstance(node.type, syntax.IntegerTypeSyntax):
                    n_type = node.type
                else:
                    raise NotImplementedError(
                        "Only IntegerTypeSyntax is supported for shrinking bits in this implementation."
                    )

                # Copy attributes and modifiers from the original declaration
                new_decl_node = f.dataDeclaration(
                    attributes=node.attributes,
                    modifiers=node.modifiers,
                    type=n_type,
                    declarators=r.makeSeparatedList(new_decl_list),
                    semi=r.makeToken(TokenKind.Semicolon, []),
                )
                r.insertAfter(node, new_decl_node)

                for index in range(len(old_decls)):
                    # Assign our cut logic to be the value of the original declaration (e.g. assign x_papercut = x;)

                    new_assign_node_str = f"assign {new_decls[index]} = (pc_sel{cur_index[0] + 1} ? {{(pc_sel{cur_index[0]} ? 1'b0 : 1'b1), {old_decls[index]}[{new_width - 1}:0]}} : {old_decls[index]});"

                    new_assign_node = SyntaxTree.fromText(new_assign_node_str).root

                    def do_replacement_insertion(node, u: bool, rewriter: SyntaxRewriter):
                        # If this is one of the instances of the old declaration, then we replace it
                        # with the new wire
                        if isinstance(node, syntax.IdentifierNameSyntax):
                            pass
                            # rewriter.replace(
                            #     node,
                            #     make_identifier(
                            #         rewriter,
                            #         node.identifier.valueText + "_papercut"
                            #     ),
                            # )
                        elif isinstance(node, syntax.IdentifierSelectNameSyntax):
                            print("renaming select")
                            # rewriter.replace(
                            #     node,
                            #     rename_select(rewriter,"papercut", node.selectors)
                            # )
                        # Otherwise we assume this is the module body and we assert the new declaration
                        # at the front
                        else:
                            # Assert is here to make sure we are not matching into random nodes
                            assert node.kind == SyntaxKind.SyntaxList
                            insert_declaration = rewriter.factory.dataDeclaration(
                                attributes=rewriter.makeList([]),
                                modifiers=rewriter.makeTokenList([]),
                                type=n_type,
                                declarators=rewriter.makeSeparatedList(
                                    [
                                        rewriter.factory.declarator(
                                            rewriter.makeToken(
                                                TokenKind.Identifier, new_decls[index]
                                            ),
                                            rewriter.makeList([]),
                                        )
                                    ]
                                ),
                                semi=rewriter.makeToken(TokenKind.Semicolon, []),
                            )

                            if u:
                                insert_assignment = SyntaxTree.fromText(
                                    f"assign {new_decls[index]} = {{1'b0, {old_decls[index]}[{new_width - 1}:0]}};"
                                ).root
                            else:
                                insert_assignment = SyntaxTree.fromText(
                                    f"assign {new_decls[index]} = {{1'b1, {old_decls[index]}[{new_width - 1}:0]}};"
                                ).root
                                

                            #rewriter.insertAtFront(node, insert_assignment)

                            #rewriter.insertAtFront(node, insert_declaration)

                    rewrites.add_rewrite(
                        Rewrite(
                            matcher=make_matcher(old_decls[index]),
                            do_replacement=do_replacement_insertion,
                            num_selections=2,
                        )
                    )

                    cur_index[0] += 2

                    r.insertAfter(node, new_assign_node)

    def replace_identifiers_handler(node, r: SyntaxRewriter):
        # If this node is the left side of an assignment, skip it to avoid replacing it with the muxed identifier
        if (
            isinstance(node, SyntaxNode)
            and node.parent
            and node.parent.kind == SyntaxKind.AssignmentExpression
        ):
            assert isinstance(node.parent, syntax.BinaryExpressionSyntax)
            if node.isEquivalentTo(node.parent.left):
                return ast.VisitAction.Skip

        if (
            isinstance(node, syntax.IdentifierNameSyntax)
            and node.identifier.valueText in old_decl_set
        ):
            new_node = make_identifier(r, node.identifier.valueText + "_papercut")
            r.replace(node, new_node)
        elif (
            isinstance(node, syntax.IdentifierSelectNameSyntax)
            and node.identifier.valueText in old_decl_set
        ):
            f = r.factory
            new_node = f.identifierSelectName(
                identifier=r.makeToken(
                    TokenKind.Identifier, node.identifier.valueText + "_papercut"
                ),
                selectors=node.selectors,
            )
            r.replace(node, new_node)

    return (
        syntax.rewrite(syntax.rewrite(tree, replace_identifiers_handler), add_decl_handler),
        cur_index[0],
    )


# MARK: Cases
def case_branch_deletion(tree: SyntaxTree, rewrites: RewriteSet) -> None:
    """Generate new SyntaxTrees each with one StandardCaseItem node removed."""
    nodes = []

    def _count_switch_branches(obj: Union[parsing.Token, syntax.SyntaxNode], nodes) -> None:
        if obj.kind == SyntaxKind.StandardCaseItem:
            nodes.append(obj)

    tree.root.visit(visitor_wrapper(_count_switch_branches, nodes))
    print(f"Found {len(nodes)} StandardCaseItem nodes.")

    for index in range(len(nodes)):

        def make_matcher(target):
            def matcher(node):
                return node == target

            return matcher

        # TODO: figure this out
        def get_mux_replacement(node, sel_index):
            pass

        # rewrites.append(Rewrite(
        #     start_offset=nodes[index].sourceRange.start.offset,
        #     end_offset=nodes[index].sourceRange.end.offset,
        #     replacement_text="",
        #     matcher=make_matcher(nodes[index]),
        #     get_replacement=lambda node: pyslang.SyntaxTree.fromText("").root,
        #     get_mux_replacement=get_mux_replacement,
        #     num_selections = 1
        # ))


# MARK: Ifs
def remove_if_conditionals(
    tree: SyntaxTree, rewrites: RewriteSet, start_index: int
) -> tuple[SyntaxTree, int]:
    """Generate new SyntaxTrees each with one IfGenerate node removed."""
    nodes = []

    def _count_conditionals_handle(obj: Union[parsing.Token, syntax.SyntaxNode], nodes) -> None:
        if isinstance(obj, syntax.ConditionalStatementSyntax):
            nodes.append(obj)

    tree.root.visit(visitor_wrapper(_count_conditionals_handle, nodes))
    print(f"Found {len(nodes)} IfGenerate nodes.")

    for index in range(len(nodes)):

        def make_matcher(target):
            def matcher(node):
                return node == target

            return matcher

        def do_replacement(node, use_else, r: SyntaxRewriter):
            if use_else:
                if node.elseClause is not None:
                    old_trivia = ""
                    for t in node.getFirstToken().trivia:
                        old_trivia += t.getRawText()
                    new_string = old_trivia + str(node.elseClause.clause)
                    new_node = SyntaxTree.fromText(new_string).root
                    r.replace(node, new_node)
                    return
                else:
                    old_trivia = ""
                    for t in node.getFirstToken().trivia:
                        old_trivia += t.getRawText()
                    new_node = SyntaxTree.fromText(old_trivia).root
                    r.replace(node, new_node)
                    return
            else:
                old_trivia = ""
                for t in node.getFirstToken().trivia:
                    old_trivia += t.getRawText()
                new_string = old_trivia + str(node.statement)
                new_node = SyntaxTree.fromText(new_string).root
                r.replace(node, new_node)

        # TODO: add replacement text
        rewrites.add_rewrite(
            Rewrite(
                matcher=make_matcher(nodes[index]),
                do_replacement=do_replacement,
                num_selections=2,
            )
        )

    return insert_if_muxes(tree, start_index)


# TODO: do this with slang factory methods
def insert_if_muxes(tree: SyntaxTree, start_index: int) -> tuple[SyntaxTree, int]:
    """Insert select inputs for the ifs in the tree based on the starting index"""

    cur_index = [start_index]

    def insert_mux_handler(node, rewriter: SyntaxRewriter):
        if isinstance(node, syntax.ConditionalStatementSyntax):
            new_pred = (
                f"(pc_sel{cur_index[0] + 1} | (!pc_sel{cur_index[0]} & ("
                + str(node.predicate)
                + ")))"
            )
            new_node = SyntaxTree.fromText(new_pred).root
            cur_index[0] += 2
            rewriter.replace(node.predicate, new_node)

    return (syntax.rewrite(tree, insert_mux_handler), cur_index[0])


# MARK: Ternary
def remove_ternary_conditionals(
    tree: SyntaxTree, rewrites: RewriteSet, start_index: int
) -> tuple[SyntaxTree, int]:
    """Generate new SyntaxTrees each with one TernaryExpression node removed."""
    nodes = []

    def _count_ternary_conditionals(obj: Union[parsing.Token, syntax.SyntaxNode], nodes) -> None:
        if isinstance(obj, syntax.ConditionalExpressionSyntax):
            nodes.append(obj)

    tree.root.visit(visitor_wrapper(_count_ternary_conditionals, nodes))
    print(f"Found {len(nodes)} ConditionalExpression nodes.")

    for index in range(len(nodes)):

        def make_matcher(target):
            def matcher(node):
                return node == target

            return matcher

        def do_replacement(node, use_left, r: SyntaxRewriter):
            replacement = node.left if use_left else node.right
            r.replace(node, replacement)

        rewrites.add_rewrite(
            Rewrite(
                matcher=make_matcher(nodes[index]),
                do_replacement=do_replacement,
                num_selections=2,
            )
        )

    return insert_ternary_muxes(tree, start_index)


def insert_ternary_muxes(tree: SyntaxTree, start_index: int) -> tuple[SyntaxTree, int]:
    """Insert select inputs for the ternary conditionals in the tree based on the starting index"""

    cur_index = [start_index]

    def insert_mux_handler(node, rewriter: SyntaxRewriter):
        if isinstance(node, syntax.ConditionalExpressionSyntax):
            new_pred = (
                f"(pc_sel{cur_index[0] + 1} | (!pc_sel{cur_index[0]} & ("
                + str(node.predicate)
                + ")))"
            )
            new_node = SyntaxTree.fromText(new_pred).root
            cur_index[0] += 2
            rewriter.replace(node.predicate, new_node)

    return (syntax.rewrite(tree, insert_mux_handler), cur_index[0])


# MARK: Main
async def main():

    parser = argparse.ArgumentParser(description="Process a SystemVerilog file.")

    parser.add_argument("input_file", help="The input SystemVerilog files to process.")
    parser.add_argument("-e", "--check-equivalence", action="store_true")

    args = parser.parse_args()

    raw_tree = SyntaxTree.fromFile(args.input_file)

    params = concretizer.extract_params(raw_tree)
    concretized_tree = concretizer.concretize_params(raw_tree, params)
    sw = concretizer.reduce_expressions(concretized_tree)

    print("Concretization complete.")

    runs: list[Run] = []

    pcutter = Papercutter(sw)

    rewrites = pcutter.cut_all()

    fname = get_module_name(sw)

    for idx, rewrite in enumerate(rewrites):
        runs.append(
            Run(
                canonical_fname=fname,
                mod_fname=f"{fname}_pc{idx}",
                input_tree=sw,
                output_tree=rename_module(
                    rewrite, f"{fname}_pc{idx}"
                ),
                index=idx
            )
        )

        
    # TODO: change this to based on the source file directory
    output_dir = "./outputs"

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    os.makedirs(output_dir, exist_ok=True)

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

    if args.check_equivalence:
        for run in runs:
            ec.generate_jasper_files(run, output_dir=output_dir)

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

                print(
                    f"JasperGold run for {run.wrapper_fname} completed with return code {run.valid}"
                )
                successes += f"{run.wrapper_fname}: {'PASS' if run.valid else 'FAIL'}\n"

            with open("../equivalence_results.txt", "w") as fout:
                fout.write(successes)

            print("Initial equivalence checks complete. Attempting consolidation...")

            working_rewrites = [run.index for run in runs if run.valid]


            # with open(f"{fname}_consolidated.sv", "w") as fout:
            #     fout.write(SyntaxPrinter.printFile(pcutter.cut_index(working_rewrites)))

            # # ec.generate_jasper_files(consolidated_run, output_dir=".")

            # # result = await ec.run_jasper(consolidated_run, True)

            # # print(f"JasperGold run for {consolidated_run.wrapper_fname} completed with return code {consolidated_run.valid}")

            # os.chdir("..")
            # directory = Path(output_dir)


if __name__ == "__main__":
    asyncio.run(main())
