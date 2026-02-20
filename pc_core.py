# General TODOs:
# - Change from using CST to AST for rewrites?

from __future__ import annotations
from typing import Callable, Union, List, Any
import pyslang
from pyslang import syntax, parsing, ast
import argparse
import os
import shutil
import asyncio
from pathlib import Path
from dataclasses import dataclass, field

import concretizer
import ec


# MARK: Modules
@dataclass
class Module:
    """A SystemVerilog module with its name and syntax tree."""

    name: str
    tree: syntax.SyntaxTree
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

    start_offset: int
    end_offset: int
    replacement_text: str
    matcher: Callable[[Any], bool]
    mux_matcher: Callable[[Any], bool]
    get_replacement: Callable[[syntax.SyntaxNode, bool], Any]
    get_mux_replacement: Callable[[syntax.SyntaxNode, int], Any]
    num_selections: int
    start_index: int = 0

    def apply(self, tree, index) -> syntax.SyntaxTree:
        """Apply this single rewrite to source."""

        def handler(node, rewriter, r=self):
            if r.matcher(node):
                if index == 0:
                    replacement = node
                elif index == 1:
                    replacement = r.get_replacement(node, False)
                else:
                    replacement = r.get_replacement(node, True)
                rewriter.replace(node, replacement)

        return syntax.rewrite(tree, handler)

    def apply_mux(self, tree) -> syntax.SyntaxTree:
        """Apply this rewrite to a tree  with a select input"""

        def handler(node, rewriter, r=self):
            if r.mux_matcher(node):
                replacement = r.get_mux_replacement(node, r.start_index)
                rewriter.replace(node, replacement)

        return syntax.rewrite(tree, handler)


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

    # def apply(self, tree) -> syntax.SyntaxTree:
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

    def apply_muxes(self, tree) -> syntax.SyntaxTree:
        """Apply all mux rewrites to tree using pyslang.syntax.rewrite()."""

        current_tree = tree

        def handler(node, rewriter, r=self):
            matching_rewrites = [rw for rw in r.rewrites if rw.mux_matcher(node)]

            # if len(matching_rewrites) > 1:
            #     print(f"Warning: multiple rewrites match node at offsets {node.sourceRange.start.offset}-{node.sourceRange.end.offset}")
            #     for rw in matching_rewrites:
            #         print(f" - Rewrite: {rw.description}")

            #     replacement = matching_rewrites[0].get_mux_replacement(node, matching_rewrites[0].start_index)
            #     rewriter.replace(node, replacement)
            # else:
            #     for rw in matching_rewrites:
            #         replacement = rw.get_mux_replacement(node, rw.start_index)
            #         rewriter.replace(node, replacement)

            for rw in matching_rewrites:
                replacement = rw.get_mux_replacement(node, rw.start_index)
                rewriter.replace(node, replacement)

        new_tree = syntax.rewrite(current_tree, handler)

        return new_tree

    def merge(self, other: "RewriteSet") -> "RewriteSet":
        """Combine with another RewriteSet."""
        return RewriteSet(rewrites=self.rewrites + other.rewrites)


@dataclass
class Run:
    """A single test run with input and expected output."""

    canonical_fname: str
    mod_fname: str
    input_tree: syntax.SyntaxTree
    output_tree: syntax.SyntaxTree
    rewrite: Rewrite
    index: int = 0
    wrapper_fname: str = ""
    valid: bool = False
    output: str = ""

    def run(self):
        """Run JasperGold on the wrapper file and capture output."""
        pass  # Implementation would go here


def consolidate_runs(tree: syntax.SyntaxTree, runs: List[Run]) -> syntax.SyntaxTree:
    """Consolidate multiple runs into a single SyntaxTree with all rewrites applied."""

    def handler(node, rewriter):
        matching_rewrites = [rw for rw in runs if rw.rewrite.matcher(node)]

        if len(matching_rewrites) > 1:
            print(
                f"Warning: multiple rewrites match node at offsets {node.sourceRange.start.offset}-{node.sourceRange.end.offset}"
            )

            replacement = matching_rewrites[0].rewrite.get_replacement(node, True)
            rewriter.replace(node, replacement)
        else:
            for rw in matching_rewrites:
                branch = False if rw.index == 1 else False
                replacement = rw.rewrite.get_replacement(node, branch)
                rewriter.replace(node, replacement)

    new_tree = syntax.rewrite(tree, handler)

    return new_tree


# MARK: Helpers and Papercuts
def rewrite_wrapper(
    f, *args, **kwargs
) -> Callable[[syntax.SyntaxNode, syntax.SyntaxRewriter], None]:
    return lambda node, rewriter: f(node, rewriter, *args, **kwargs)


def visitor_wrapper(
    f, *args, **kwargs
) -> Callable[[Union[parsing.Token, syntax.SyntaxNode]], None]:
    return lambda node: f(node, *args, **kwargs)


def _is_parent(parent: syntax.SyntaxNode, child: syntax.SyntaxNode) -> bool:
    """Check if parent is the parent of child."""
    return child in parent


def _get_sibling_node(node: syntax.SyntaxNode) -> syntax.SyntaxNode:
    """Get the sibling node of a given SyntaxNode."""
    parent = node.parent
    siblings = list(parent)
    if node in siblings:
        index = siblings.index(node)
        return siblings[index + 1]
    else:
        for sibling in siblings:
            if _is_parent(sibling, node):
                if isinstance(siblings[siblings.index(sibling) + 1], syntax.SyntaxNode):
                    return siblings[siblings.index(sibling) + 1]
                else:
                    return parent

    return syntax.SyntaxNode()


def _move_trivia_to_sibling(node: syntax.SyntaxNode) -> None:
    """Move trivia from node to its sibling."""
    sibling = _get_sibling_node(node)
    if isinstance(sibling, syntax.SyntaxNode):
        node_trivia = node.getFirstToken().trivia
        sibling.getFirstToken().trivia.append(node_trivia)


def _get_attr_name(parent_obj, child_obj) -> str:
    """Get the attribute name of child_obj in parent_obj."""
    for attr_name, attr_value in parent_obj.__dict__.items():
        if attr_value is child_obj:  # Use 'is' for identity check
            return attr_name
    return ""


def _copy_tree(tree: syntax.SyntaxTree) -> syntax.SyntaxTree:
    """Return a copy of the given SyntaxTree."""
    return syntax.SyntaxTree.fromText(syntax.SyntaxPrinter.printFile(tree))


# TODO: add width 1, add multidimensions
def change_int_dimensions(
    w: int,
    f: syntax.SyntaxFactory,
    r: syntax.SyntaxRewriter,
    n: syntax.IntegerTypeSyntax,
) -> syntax.IntegerTypeSyntax:
    return f.integerType(
        n.kind,
        n.keyword,
        n.signing,
        r.makeList(
            [
                f.variableDimension(
                    r.makeToken(parsing.TokenKind.OpenBracket),
                    f.rangeDimensionSpecifier(
                        f.rangeSelect(
                            syntax.SyntaxKind.SimpleRangeSelect,
                            f.literalExpression(
                                syntax.SyntaxKind.IntegerLiteralExpression,
                                r.makeToken(parsing.TokenKind.IntegerLiteral, str(w - 1)),
                            ),
                            r.makeToken(parsing.TokenKind.Colon),
                            f.literalExpression(
                                syntax.SyntaxKind.IntegerLiteralExpression,
                                r.makeToken(parsing.TokenKind.IntegerLiteral, "0"),
                            ),
                        )
                    ),
                    r.makeToken(parsing.TokenKind.CloseBracket),
                )
            ]
        ),
    )


def make_space(r: syntax.SyntaxRewriter) -> parsing.Trivia:
    return r.makeTrivia(parsing.TriviaKind.Whitespace, " ")


def make_identifier(
    r: syntax.SyntaxRewriter, name: str, trivia: List[parsing.Trivia] = []
) -> syntax.IdentifierNameSyntax:
    return r.factory.identifierName(r.makeToken(parsing.TokenKind.Identifier, name, trivia=trivia))


# MARK: Module Refactoring
def find_module_decl(node) -> syntax.ModuleDeclarationSyntax:
    """Recursively find ModuleDeclarationSyntax."""
    if node.kind == syntax.SyntaxKind.ModuleDeclaration:
        return node
    for child in node:
        result = find_module_decl(child)
        if result:
            return result
    raise ValueError("ModuleDeclarationSyntax not found")


def rename_module(tree: syntax.SyntaxTree, new_name: str) -> syntax.SyntaxTree:
    """Rename the module in a SystemVerilog source string."""
    root = tree.root
    source = syntax.SyntaxPrinter.printFile(tree)

    module_decl = find_module_decl(root)

    name_token = module_decl.header.name
    token_range = name_token.range

    start = token_range.start.offset
    end = token_range.end.offset

    return syntax.SyntaxTree.fromText(source[:start] + new_name + source[end:])


def get_module_name(tree: syntax.SyntaxTree) -> str:
    """Get the module name from a SystemVerilog source."""

    module_decl: syntax.ModuleDeclarationSyntax = find_module_decl(tree.root)
    return module_decl.header.name.valueText


def add_select_inputs(tree: syntax.SyntaxTree, num_inputs: int) -> syntax.SyntaxTree:
    """Add select inputs to the module declaration."""
    source = syntax.SyntaxPrinter.printFile(tree)
    root = tree.root

    module_decl = find_module_decl(root)

    port_node = module_decl.header.ports
    ports = module_decl.header.ports[1]  # Exclude the parentheses
    port_str = str(ports) if ports is not None else ""

    sel_str = "input logic " + ", ".join([f"pc_sel{i + 1}" for i in range(num_inputs)])

    return syntax.SyntaxTree.fromText(
        source[: port_node.sourceRange.start.offset + 1]
        + sel_str
        + (", " if port_str else "")
        + source[port_node.sourceRange.start.offset + 1 :]
    )


# MARK: Shrink Bits
def shrink_bits(tree: syntax.SyntaxTree) -> RewriteSet:
    """Generate new SyntaxTrees with one less bit in each SimpleRangeSelect node."""
    nodes = []

    def _count_range_handle(obj: Union[parsing.Token, syntax.SyntaxNode], nodes) -> None:
        if obj.kind == syntax.SyntaxKind.SimpleRangeSelect and isinstance(
            obj, syntax.RangeSelectSyntax
        ):
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
                new_node = syntax.SyntaxTree.fromText(f"{new_dim}").root
                return new_node
            else:
                return target

        dim = int(nodes[index].getFirstToken().rawText) - 1
        new_dim = max(dim, 1)

        # rewrites.append(Rewrite(
        #     start_offset=nodes[index].sourceRange.start.offset,
        #     end_offset=nodes[index].sourceRange.end.offset,
        #     replacement_text=f"{new_dim}",
        #     matcher=make_matcher(nodes[index]),
        #     get_replacement=get_replacement,
        #     description=f"Shrink bit width from {dim + 1} to {new_dim}"
        # ))

    return RewriteSet(rewrites=rewrites)


def shrink_bits_mux(tree: syntax.SyntaxTree) -> syntax.SyntaxTree:
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

    new_decl_set = set()
    old_decl_set = set()

    def add_decl_handler(node, rewriter: syntax.SyntaxRewriter):
        if isinstance(node, syntax.DataDeclarationSyntax):
            width = get_dimensions(node)
            if width > 1:
                new_width = width - 1

                old_decls = get_decls(node)
                new_decls = [f"{decl}_papercut" for decl in old_decls]
                new_decl_set.update(new_decls)
                old_decl_set.update(old_decls)

                f: syntax.SyntaxFactory = rewriter.factory

                new_decl_list = []
                for d in new_decls:
                    new_decl_list.append(
                        f.declarator(
                            rewriter.makeToken(parsing.TokenKind.Identifier, d),
                            rewriter.makeList([]),
                        )
                    )
                    if d != new_decls[-1]:
                        new_decl_list.append(rewriter.makeComma())

                if isinstance(node.type, syntax.IntegerTypeSyntax):
                    type = change_int_dimensions(new_width, f, rewriter, node.type)
                else:
                    raise NotImplementedError(
                        "Only IntegerTypeSyntax is supported for shrinking bits in this implementation."
                    )

                # Copy attributes and modifiers from the original declaration
                new_decl_node = f.dataDeclaration(
                    attributes=node.attributes,
                    modifiers=node.modifiers,
                    type=type,
                    declarators=rewriter.makeSeparatedList(new_decl_list),
                    semi=rewriter.makeToken(parsing.TokenKind.Semicolon, []),
                )
                rewriter.insertAfter(node, new_decl_node)

                # Assign our cut logic to be the value of the original declaration (e.g. assign x_papercut = x;)
                new_assign_node = f.continuousAssign(
                    attributes=rewriter.makeList([]),
                    assign=rewriter.makeToken(
                        parsing.TokenKind.AssignKeyword, node.getFirstToken().trivia
                    ),
                    assignments=rewriter.makeSeparatedList(
                        [
                            f.binaryExpression(
                                kind=syntax.SyntaxKind.AssignmentExpression,
                                left=make_identifier(
                                    rewriter, new_decls[0], [make_space(rewriter)]
                                ),
                                operatorToken=rewriter.makeToken(
                                    parsing.TokenKind.Equals,
                                    [make_space(rewriter)],
                                ),
                                attributes=rewriter.makeList([]),
                                right=make_identifier(
                                    rewriter, old_decls[0], [make_space(rewriter)]
                                ),
                            )
                        ]
                    ),
                    semi=rewriter.makeToken(parsing.TokenKind.Semicolon),
                )
                rewriter.insertAfter(node, new_assign_node)

    def add_mux_handler(node, rewriter: syntax.SyntaxRewriter):

        # If this node is the left side of an assignment, skip it to avoid replacing it with a mux
        if (
            isinstance(node, syntax.SyntaxNode)
            and node.parent
            and node.parent.kind == syntax.SyntaxKind.AssignmentExpression
        ):
            assert isinstance(node.parent, syntax.BinaryExpressionSyntax)
            if node.isEquivalentTo(node.parent.left):
                print(f"Node: {node}, Parent: {node.parent}")
                return pyslang.ast.VisitAction.Skip

        # if we find an identifier that is in our list of cut declarations, insert a mux between it and the cut declaration
        if (
            isinstance(node, syntax.IdentifierNameSyntax)
            and node.identifier.valueText in old_decl_set
        ):
            f: syntax.SyntaxFactory = rewriter.factory
            new_node = f.parenthesizedExpression(
                openParen=rewriter.makeToken(
                    parsing.TokenKind.OpenParenthesis,
                    [rewriter.makeTrivia(parsing.TriviaKind.Whitespace, " ")],
                ),
                expression=f.conditionalExpression(
                    predicate=f.conditionalPredicate(
                        rewriter.makeSeparatedList(
                            [
                                f.conditionalPattern(
                                    make_identifier(
                                        rewriter,
                                        f"{node.identifier.valueText}_papercut",
                                    ),
                                )
                            ]
                        )
                    ),
                    question=rewriter.makeToken(parsing.TokenKind.Question),
                    attributes=rewriter.makeList([]),
                    left=make_identifier(rewriter, f"{node.identifier.valueText}_papercut"),
                    colon=rewriter.makeToken(parsing.TokenKind.Colon),
                    right=make_identifier(rewriter, node.identifier.valueText),
                ),
                closeParen=rewriter.makeToken(parsing.TokenKind.CloseParenthesis),
            )

            rewriter.replace(node, new_node)

    return syntax.rewrite(syntax.rewrite(tree, add_decl_handler), add_mux_handler)


# MARK: Cases
def case_branch_deletion(tree: syntax.SyntaxTree, rewrites: RewriteSet) -> None:
    """Generate new SyntaxTrees each with one StandardCaseItem node removed."""
    nodes = []

    def _count_switch_branches(obj: Union[parsing.Token, syntax.SyntaxNode], nodes) -> None:
        if obj.kind == syntax.SyntaxKind.StandardCaseItem:
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
def remove_if_conditionals(tree: syntax.SyntaxTree, rewrites: RewriteSet) -> None:
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

        def make_mux_matcher(target):
            def matcher(node):
                return node == target.predicate

            return matcher

        def get_replacement(node, use_else):
            if use_else:
                if node.elseClause is not None:
                    old_trivia = ""
                    for t in node.getFirstToken().trivia:
                        old_trivia += t.getRawText()
                    new_string = old_trivia + str(node.elseClause.clause)
                    new_node = syntax.SyntaxTree.fromText(new_string).root
                    return new_node
                else:
                    old_trivia = ""
                    for t in node.getFirstToken().trivia:
                        old_trivia += t.getRawText()
                    new_node = syntax.SyntaxTree.fromText(old_trivia).root
                    return new_node
            else:
                old_trivia = ""
                for t in node.getFirstToken().trivia:
                    old_trivia += t.getRawText()
                new_string = old_trivia + str(node.statement)
                new_node = syntax.SyntaxTree.fromText(new_string).root
                return new_node

        def get_mux_replacement(node, sel_index):
            new_pred = f"(pc_sel{sel_index + 1} | (!pc_sel{sel_index} & (" + str(node) + ")))"
            new_node = syntax.SyntaxTree.fromText(new_pred).root
            return new_node

        # TODO: add replacement text
        rewrites.add_rewrite(
            Rewrite(
                start_offset=nodes[index].sourceRange.start.offset,
                end_offset=nodes[index].sourceRange.end.offset,
                replacement_text="",
                matcher=make_matcher(nodes[index]),
                mux_matcher=make_mux_matcher(nodes[index]),
                get_replacement=get_replacement,
                get_mux_replacement=get_mux_replacement,
                num_selections=2,
            )
        )


# MARK: Ternary
def remove_ternary_conditionals(tree: syntax.SyntaxTree, rewrites: RewriteSet) -> None:
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

        def make_mux_matcher(target):
            def matcher(node):
                return node == target.predicate

            return matcher

        def get_replacement(node, use_left):
            return node.left if use_left else node.right

        def get_mux_replacement(node, sel_index):
            new_pred = (
                f"(pc_sel{sel_index + 1} | (!pc_sel{sel_index} & (" + str(node.predicate) + ")))"
            )
            new_node = syntax.SyntaxTree.fromText(new_pred).root
            return new_node

        rewrites.add_rewrite(
            Rewrite(
                start_offset=nodes[index].sourceRange.start.offset,
                end_offset=nodes[index].sourceRange.end.offset,
                replacement_text=nodes[index].left.getFirstToken().rawText,
                matcher=make_matcher(nodes[index]),
                mux_matcher=make_mux_matcher(nodes[index]),
                get_replacement=get_replacement,
                get_mux_replacement=get_mux_replacement,
                num_selections=2,
            )
        )


# MARK: Main
async def main():

    parser = argparse.ArgumentParser(description="Process a SystemVerilog file.")

    parser.add_argument("input_file", help="The input SystemVerilog files to process.")
    parser.add_argument("-s", "--shrink-bits", action="store_true")
    parser.add_argument("-c", "--delete-case-branch", action="store_true")
    parser.add_argument("-i", "--remove-if-conditionals", action="store_true")
    parser.add_argument("-t", "--remove-ternary-conditionals", action="store_true")
    parser.add_argument("-e", "--check-equivalence", action="store_true")

    args = parser.parse_args()

    compilation = ast.Compilation()

    # for input_file in args.input_files:
    #     print(f"Processing file: {input_file}")

    #     compilation.addSyntaxTree(pyslang.SyntaxTree.fromFile(input_file))

    raw_tree = syntax.SyntaxTree.fromFile(args.input_file)

    params = concretizer.extract_params(raw_tree)
    concretized_tree = concretizer.concretize_params(raw_tree, params)
    sw = concretizer.reduce_expressions(concretized_tree)

    print("Concretization complete.")

    runs = []
    rewrites = RewriteSet()

    fname = get_module_name(sw)

    if args.shrink_bits:
        pass
        # sb_trees = shrink_bits(sw)

    if args.delete_case_branch:
        pass
        # cbd_trees = case_branch_deletion(sw)

    if args.remove_if_conditionals:
        remove_if_conditionals(sw, rewrites)

    if args.remove_ternary_conditionals:
        remove_ternary_conditionals(sw, rewrites)

    muxed_tree = rewrites.apply_muxes(sw)
    muxed_tree = rename_module(muxed_tree, f"{fname}_muxed")
    muxed_tree = add_select_inputs(muxed_tree, sum(rw.num_selections for rw in rewrites.rewrites))

    with open(f"{fname}_muxed.sv", "w") as fout:
        fout.write(syntax.SyntaxPrinter.printFile(muxed_tree))

    for rewrite in rewrites.rewrites:
        for i in range(rewrite.num_selections):
            runs.append(
                Run(
                    canonical_fname=fname,
                    mod_fname=f"{fname}_pc{rewrite.start_index + i}",
                    input_tree=sw,
                    output_tree=rename_module(
                        rewrite.apply(sw, i + 1), f"{fname}_pc{rewrite.start_index + i}"
                    ),
                    rewrite=rewrite,
                    index=i,
                )
            )

    # TODO: change this to based on the source file directory
    output_dir = "./outputs"

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    os.makedirs(output_dir, exist_ok=True)

    try:
        with open(f"{output_dir}/{fname}_concretized.sv", "w") as fout:
            fout.write(syntax.SyntaxPrinter.printFile(sw))
    except Exception as e:
        print(f"Error writing original file: {e}")

    for run in runs:
        try:
            with open(f"{output_dir}/{run.mod_fname}.sv", "w") as fout:
                fout.write(syntax.SyntaxPrinter.printFile(run.output_tree))
        except Exception as e:
            print(f"Error writing output files: {e}")

    if args.check_equivalence:
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

                print(
                    f"JasperGold run for {run.wrapper_fname} completed with return code {run.valid}"
                )
                successes += f"{run.wrapper_fname}: {'PASS' if run.valid else 'FAIL'}\n"

            with open("../equivalence_results.txt", "w") as fout:
                fout.write(successes)

            print("Initial equivalence checks complete. Attempting consolidation...")

            consolidated_rewrites = [run.rewrite for run in runs if run.valid]

            consolidated_set = RewriteSet(rewrites=consolidated_rewrites)
            # consolidated_tree = consolidated_set.apply(sw)

            # consolidated_run = Run(
            #     canonical_fname=fname,
            #     mod_fname=f"{fname}_consolidated",
            #     input_tree=sw,
            #     output_tree=rename_module(consolidated_tree, f"{fname}_consolidated"),
            #     rewrite_set=consolidated_set
            # )

            with open(f"{fname}_consolidated.sv", "w") as fout:
                fout.write(syntax.SyntaxPrinter.printFile(consolidate_runs(sw, runs)))

            # ec.generate_ec_files(consolidated_run, output_dir=".")

            # result = await ec.run_jasper(consolidated_run, True)

            # print(f"JasperGold run for {consolidated_run.wrapper_fname} completed with return code {consolidated_run.valid}")

            os.chdir("..")
            directory = Path(output_dir)


if __name__ == "__main__":
    asyncio.run(main())
