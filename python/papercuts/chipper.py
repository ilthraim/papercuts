from __future__ import annotations
import os
import shutil
from typing import Union
import sys

import pyslang
from pyslang import syntax, ast
from pyslang.syntax import SyntaxNode, SyntaxRewriter, SyntaxTree
from pyslang.ast import Symbol, Compilation, Expression
from pyslang.parsing import Token, TokenKind
from pyslang.driver import Driver
from papercuts.pypercuts import rename_module, get_module_name, rename_submodules

from papercuts.utils import rewrite_wrapper, print_tree


def collect_modules_ast(comp: Compilation) -> dict[str, ast.DefinitionSymbol]:
    """Collects all module instances from the given compilation and returns a dictionary mapping their hierarchical paths (string) to their definitions."""
    modules = {}

    def _module_collector(obj: Union[Token, SyntaxNode]) -> None:
        if isinstance(obj, ast.InstanceSymbol):
            print("Found module instance: ", obj.name)
            modules[obj.hierarchicalPath] = obj.definition

    comp.getRoot().visit(_module_collector)

    return modules


def collect_modules_cst(comp: Compilation) -> dict[str, SyntaxTree]:
    """Collects all module instances from the given compilation and returns a dictionary mapping their hierarchical paths (string) to their syntax trees."""
    modules = {}
    name_list = []

    def _module_collector(obj: Union[Token, SyntaxNode]) -> None:
        if isinstance(obj, syntax.ModuleDeclarationSyntax):
            print("Found module declaration: ", obj.header.name.valueText)
            name_list.append(obj.header.name.valueText)

    for tree in comp.getSyntaxTrees():
        tree.root.visit(_module_collector)
        if len(name_list) > 1:
            raise Exception(
                "Multiple modules in a single file not supported. Inputs should be run through split_tree first."
            )
        modules[name_list[0]] = tree
        name_list.clear()
    return modules


def eval_modules(comp: Compilation) -> list[tuple[SyntaxTree, str]]:
    """Evaluates all expressions in the given compilation and returns a list of concretized syntax trees for each module.
    Each instance of a submodule is replaced with a separate syntax tree so optimizations can be performed on them individually.
    """
    # The main point of this is to concretize any parameters that we can evaluate at compile time.

    cx = ast.ASTContext(comp.getRoot(), ast.LookupLocation.max)
    ecx = ast.EvalContext(cx)
    local_replacement_syntax_pairs = {}
    global_replacement_syntax_pairs = []

    def _attempt_getSymbolReference(obj) -> Union[Symbol, None]:
        g_ref = None

        def _get_ref_from_children(obj: Union[Token, SyntaxNode]) -> None:
            if isinstance(obj, Expression):
                ref = obj.getSymbolReference()
                if ref is not None:
                    nonlocal g_ref  # TODO Fix this
                    g_ref = ref
                    return

        obj.visit(_get_ref_from_children)
        return g_ref

    def _eval_visitor(obj: Union[Token, SyntaxNode]) -> None:
        if isinstance(obj, ast.VariableSymbol):
            if obj.type.isAggregate:
                raise NotImplementedError("Papercuts cannot operate on packed arrays")
            if isinstance(obj.syntax.parent, syntax.ImplicitAnsiPortSyntax):
                new_type_str = " " + str(obj.type)
                path = obj.hierarchicalPath.rsplit(".", 1)[0]
                if path in local_replacement_syntax_pairs:
                    local_replacement_syntax_pairs[path].append(
                        (obj.syntax.parent.header.dataType, SyntaxTree.fromText(new_type_str).root.type)
                    )
                else:
                    local_replacement_syntax_pairs[path] = [
                        (obj.syntax.parent.header.dataType, SyntaxTree.fromText(new_type_str).root.type)
                    ]
            elif isinstance(obj.syntax.parent, syntax.DataDeclarationSyntax):
                old_trivia = obj.syntax.parent.type.getFirstToken().trivia
                new_type_str = "".join(triv.getRawText() for triv in old_trivia) + str(obj.type)
                print(new_type_str)
                path = obj.hierarchicalPath.rsplit(".", 1)[0]
                if path in local_replacement_syntax_pairs:
                    local_replacement_syntax_pairs[path].append(
                        (obj.syntax.parent.type, SyntaxTree.fromText(new_type_str).root.type)
                    )
                else:
                    local_replacement_syntax_pairs[path] = [
                        (obj.syntax.parent.type, SyntaxTree.fromText(new_type_str).root.type)
                    ]
            else:
                raise NotImplementedError(
                    "Don't know how to handle variable declarations that aren't ports or data declarations yet.")
        if isinstance(obj, Expression) and not isinstance(obj, ast.IntegerLiteral):
            ev = obj.eval(ecx)
            if ev.value is not None:
                print("Evaluating: ", obj.syntax)

                ref = obj.getSymbolReference()

                if ref is None:
                    ref = _attempt_getSymbolReference(obj)

                if ref is not None:
                    path = ref.hierarchicalPath.rsplit(".", 1)[0]
                    print("Path:", path)
                    print("Value:", ev.value)
                    if path in local_replacement_syntax_pairs:
                        local_replacement_syntax_pairs[path].append(
                            (obj.syntax, SyntaxTree.fromText(str(ev.value)).root)
                        )
                    else:
                        local_replacement_syntax_pairs[path] = [
                            (obj.syntax, SyntaxTree.fromText(str(ev.value)).root)
                        ]
                else:
                    # print(ev.value)
                    global_replacement_syntax_pairs.append(
                        (obj.syntax, SyntaxTree.fromText(str(ev.value)).root)
                    )
            else:
                print("Could not evaluate: ", obj.syntax)
    comp.getRoot().visit(_eval_visitor)

    def _apply_all_replacements(node, rewriter: SyntaxRewriter, rewrite_list) -> None:
        for syntax_node, replacement in rewrite_list:
            if node == syntax_node:
                #nr = rewriter.deepClone(replacement)
                rewriter.replace(node, replacement)
                return

    conc_trees = []
    # get module definitions from AST to get the hierarchical paths for the local replacements
    mod_dict = collect_modules_ast(comp)
    # Get module syntax trees from CST to perform the rewrites on every instance of the modules
    # These syntax trees are of the pre-concretized modules, so we concretize them below. Still need
    # to rewrite every supermodule to to change names of the submodules, and rename the submodule
    # trees
    tree_dict = collect_modules_cst(comp)
    for mod_name, mod_path in mod_dict.items():
        print(f"Module: {mod_name}, Path: {mod_path}")
        conc_trees.append(
            (
                syntax.rewrite(
                    tree_dict[mod_path.name],
                    rewrite_wrapper(
                        _apply_all_replacements,
                        local_replacement_syntax_pairs[mod_name]
                        if mod_name in local_replacement_syntax_pairs
                        else [] + global_replacement_syntax_pairs,
                    ),
                ),
                mod_name,  # mod_name is path e.g. top.inst1.inst2 . Names are actual instance names
            )
        )

    renamed_conc_trees = []

    for tree, tree_name in conc_trees:
        # Replace dots in tree names with underscores to avoid issues with naming in SV
        new_name = tree_name.replace(".", "_")
        print(f"Renaming tree {tree_name} to {new_name}")
        # Rename all submodules in the tree to match the new names of the concretized trees
        renamed_conc_trees.append(
            (rename_submodules(rename_module(tree, new_name)), new_name)
        )

    return renamed_conc_trees

    # DO REWRITES OVER DEFINITIONS FROM COMPILATION


def split_tree(tree: SyntaxTree) -> list[tuple[str, SyntaxTree]]:

    modules = []
    raw_modules = []
    info_trees = []

    for st in tree.root[0]:
        if isinstance(st, syntax.ModuleDeclarationSyntax):
            raw_modules.append((st.header.name.valueText, st.__str__()))
        else:
            info_trees.append(st.__str__())

    for module in raw_modules:
        modules.append(
            (module[0], SyntaxTree.fromText("\n".join(info_trees + [module[1]])))
        )

    return modules


def main():
    print("Splitting modules...")

    output_dir = "./outputs"

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    os.makedirs(output_dir, exist_ok=True)

    trees = []
    for file in sys.argv[1:]:
        tree = SyntaxTree.fromFile(file)
        trees.append(tree)

    src_list = []

    for tree in trees:
        split_trees = split_tree(tree)
        for tree in split_trees:
            src_list.append(f"{output_dir}/{tree[0]}.sv")
            with open(f"{output_dir}/{tree[0]}.sv", "w") as f:
                f.write(print_tree(tree[1]))

    d = Driver()
    d.addStandardArgs()

    # Parse command line arguments
    srcs = " ".join([sys.argv[0]] + src_list)
    if not d.parseCommandLine(srcs, pyslang.driver.CommandLineOptions()):
        return

    # Process options and parse all provided sources
    if not d.processOptions() or not d.parseAllSources():
        return

    # Perform elaboration and report all diagnostics
    compilation = d.createCompilation()
    d.reportCompilation(compilation, False)

    comp_root: ast.RootSymbol = compilation.getRoot()
    print(comp_root.topInstances)

    conc_trees = eval_modules(compilation)

    for tree, name in conc_trees:
        with open(f"{output_dir}/{name}_concretized.sv", "w") as f:
            f.write(print_tree(tree))

    print()
    print()
    print()
    print(len(conc_trees), "concretized trees")


if __name__ == "__main__":
    main()