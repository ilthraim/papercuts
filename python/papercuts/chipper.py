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

from papercuts.utils import rewrite_wrapper, print_tree, vprint


def collect_modules_ast(comp: Compilation) -> dict[str, ast.DefinitionSymbol]:
    """Collects all module instances from the given compilation and returns a dictionary mapping their hierarchical paths (string) to their definitions."""
    modules = {}

    def _module_collector(obj: Union[Token, SyntaxNode]) -> None:
        if isinstance(obj, ast.InstanceSymbol):
            vprint("Found module instance: ", obj.name)
            modules[obj.hierarchicalPath] = obj.definition

    comp.getRoot().visit(_module_collector)

    return modules


def concretized_definition_names(comp: Compilation) -> dict[str, str]:
    """Map each concretized module name to its original definition name.

    ``eval_modules`` names each concretized tree after the *instance path*
    (hierarchical path with ``.`` replaced by ``_``), so a module defined as
    ``mul`` and instantiated as ``top.u_mul`` becomes ``top_u_mul``.
    This returns ``{concretized_name: definition_name}`` so callers can act on
    a module by its definition (e.g. skip every instance of a library
    primitive) rather than by each instance-path name.
    """
    return {
        path.replace(".", "_"): defn.name
        for path, defn in collect_modules_ast(comp).items()
    }


def collect_modules_cst(comp: Compilation) -> dict[str, SyntaxTree]:
    """Collects all module instances from the given compilation and returns a dictionary mapping their hierarchical paths (string) to their syntax trees."""
    modules = {}
    name_list = []

    def _module_collector(obj: Union[Token, SyntaxNode]) -> None:
        if isinstance(obj, syntax.ModuleDeclarationSyntax):
            vprint("Found module declaration: ", obj.header.name.valueText)
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


def eval_modules(
    comp: Compilation, excluded_defs: "set[str] | None" = None
) -> list[tuple[SyntaxTree, str]]:
    """Evaluates all expressions in the given compilation and returns a list of concretized syntax trees for each module.
    Each instance of a submodule is replaced with a separate syntax tree so optimizations can be performed on them individually.

    Modules whose *definition* name is in ``excluded_defs`` are left untouched:
    they are NOT concretized or individualized per instance. Instead the original
    (parameterized) source is emitted once under its original name, and parents
    that instantiate them keep the original name + ``#(...)`` overrides (via
    ``rename_submodules``'s ``excluded`` list). All instances of an excluded
    definition collapse to that single shared definition. An excluded module must
    not instantiate a non-excluded submodule (its verbatim source would reference
    an individualized child by a name that no longer exists) -- that raises.
    """
    excluded_defs = set(excluded_defs or ())

    # NARROW EDIT: skip parameter concretization and per-instance module
    # renaming. Emit each module DEFINITION once, verbatim, under its original
    # name. Cuts still run on these original (parameterized) trees downstream.
    # The concretization + rename body below is left as dead code for easy revert.
    return [(tree, name) for name, tree in collect_modules_cst(comp).items()]

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
            if not obj.type.isPredefinedInteger:
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
                    vprint(new_type_str)
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
                        f"Can't handle symbol {obj} of type {obj.type}.")
        if isinstance(obj, Expression) and not isinstance(obj, ast.IntegerLiteral):
            ev = obj.eval(ecx)
            if ev.value is not None:
                vprint("Evaluating: ", obj.syntax)

                ref = obj.getSymbolReference()

                if ref is None:
                    ref = _attempt_getSymbolReference(obj)

                if ref is not None:
                    path = ref.hierarchicalPath.rsplit(".", 1)[0]
                    vprint("Path:", path)
                    vprint("Value:", ev.value)
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
                vprint("Could not evaluate: ", obj.syntax)
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

    # Guard: an excluded module is emitted verbatim (referencing its children by
    # their original names) while non-excluded modules are individualized/renamed
    # per instance. If an excluded module instantiated a non-excluded submodule the
    # reference would dangle, so reject that configuration up front.
    if excluded_defs:
        excluded_paths = {
            path: defn.name
            for path, defn in mod_dict.items()
            if defn.name in excluded_defs
        }
        for path, defn in mod_dict.items():
            if defn.name in excluded_defs:
                continue
            for epath, ename in excluded_paths.items():
                if path.startswith(epath + "."):
                    raise Exception(
                        f"Excluded module '{ename}' (instance '{epath}') instantiates "
                        f"non-excluded submodule '{defn.name}' (instance '{path}'). "
                        f"Excluded modules must not contain non-excluded submodules."
                    )

    excluded_list = list(excluded_defs)
    emitted_excluded: set[str] = set()

    for mod_name, mod_path in mod_dict.items():
        def_name = mod_path.name
        if def_name in excluded_defs:
            # Emit the original (parameterized) source once per excluded definition,
            # untouched: no concretization, no rename. Every instance collapses to it.
            if def_name not in emitted_excluded:
                emitted_excluded.add(def_name)
                conc_trees.append((tree_dict[def_name], def_name))
            continue
        vprint(f"Module: {mod_name}, Path: {mod_path}")
        conc_trees.append(
            (
                syntax.rewrite(
                    tree_dict[def_name],
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
        # Excluded modules keep their original name and are handed over verbatim.
        if tree_name in excluded_defs:
            vprint(f"Keeping excluded module {tree_name} untouched")
            renamed_conc_trees.append((tree, tree_name))
            continue
        # Replace dots in tree names with underscores to avoid issues with naming in SV
        new_name = tree_name.replace(".", "_")
        vprint(f"Renaming tree {tree_name} to {new_name}")
        # Rename all submodules in the tree to match the new names of the concretized
        # trees, but leave instantiations of excluded modules pointing at the original.
        renamed_conc_trees.append(
            (rename_submodules(rename_module(tree, new_name), excluded_list), new_name)
        )

    return renamed_conc_trees

    # DO REWRITES OVER DEFINITIONS FROM COMPILATION


def split_tree(tree: SyntaxTree) -> list[tuple[str, SyntaxTree]]:

    modules = []
    raw_modules = []
    info_trees = []

    # A lone top-level module parses with the ModuleDeclaration as the root
    # itself; multiple members parse under a CompilationUnit whose [0] is the
    # member list. Normalize to a member iterable either way.
    root = tree.root
    members = [root] if isinstance(root, syntax.ModuleDeclarationSyntax) else root[0]

    for st in members:
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