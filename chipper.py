import os
import shutil
from typing import Union
import sys
import pyslang
from dataclasses import dataclass

import pc_core
import concretizer



def analyze_modules(comp: pyslang.Compilation) -> list[pc_core.Module]:

    trees = comp.getSyntaxTrees()

    modules = []

    for mod in modules:
        comp.addSyntaxTree(mod.tree)

    analysisManager = pyslang.AnalysisManager()
    analysisManager.analyze(comp)
    
    analysisManager.getAnalyzedScope
    print("Is done", comp.isElaborated)


    return modules

def collect_modules_ast(comp: pyslang.Compilation):
    modules = {}

    def _module_collector(obj: Union[pyslang.Token, pyslang.SyntaxNode]) -> None:
        if isinstance(obj, pyslang.InstanceSymbol):
            print("Found module instance: ", obj.name)
            modules[obj.hierarchicalPath] = obj.definition

    comp.getRoot().visit(_module_collector)

    return modules

def collect_modules_cst(comp: pyslang.Compilation):
    modules = {}
    name_list = []

    def _module_collector(obj: Union[pyslang.Token, pyslang.SyntaxNode]) -> None:
        if isinstance(obj, pyslang.ModuleDeclarationSyntax):
            print("Found module declaration: ", obj.header.name.valueText)
            name_list.append(obj.header.name.valueText)

    
    for tree in comp.getSyntaxTrees():
        tree.root.visit(_module_collector)
        if len(name_list) > 1:
            raise Exception("Multiple modules in a single file not supported")
        modules[name_list[0]] = tree
        name_list.clear()

    return modules

def eval_modules(comp: pyslang.Compilation):

    cx = pyslang.ASTContext(comp.getRoot(), pyslang.LookupLocation.max)
    ecx = pyslang.EvalContext(cx)
    local_replacement_syntax_pairs = {}
    global_replacement_syntax_pairs = []

    def _attempt_getSymbolReference(obj: pyslang.Expression) -> Union[pyslang.Symbol, None]:
        g_ref = None

        def _get_ref_from_children(obj: Union[pyslang.Token, pyslang.SyntaxNode]) -> None:
            if isinstance(obj, pyslang.Expression):
                ref = obj.getSymbolReference()
                if ref is not None:
                    nonlocal g_ref #TODO Fix this
                    g_ref = ref
                    return
                
        obj.visit(_get_ref_from_children)
        return g_ref


    def _eval_visitor(obj: Union[pyslang.Token, pyslang.SyntaxNode]) -> None:
        if isinstance(obj, pyslang.Expression):
            ev = obj.eval(ecx)
            if ev.value is not None:
                print("Evaluating: ", obj.syntax)
                
                ref = obj.getSymbolReference()

                if ref is None:
                    ref = _attempt_getSymbolReference(obj)

                if ref is not None:
                    path = ref.hierarchicalPath.rsplit(".", 1)[0]
                    print("Path:", path)
                    if path in local_replacement_syntax_pairs:
                        local_replacement_syntax_pairs[path].append((obj.syntax, pyslang.SyntaxTree.fromText(str(ev.value)).root))
                    else:
                        local_replacement_syntax_pairs[path] = [(obj.syntax, pyslang.SyntaxTree.fromText(str(ev.value)).root)]
                else:
                    global_replacement_syntax_pairs.append((obj.syntax, pyslang.SyntaxTree.fromText(str(ev.value)).root))

    comp.getRoot().visit(_eval_visitor)

    def _apply_all_replacements(node, rewriter, rewrite_list) -> None:
        for syntax, replacement in rewrite_list:
            if node == syntax:
                rewriter.replace(node, replacement)
                return

    conc_trees = []
    mod_dict = collect_modules_ast(comp)
    tree_dict = collect_modules_cst(comp)
    for mod_name, mod_path in mod_dict.items():
        print(f"Module: {mod_name}, Path: {mod_path}")
        conc_trees.append((pyslang.rewrite(tree_dict[mod_path.name], pc_core.rewrite_wrapper(_apply_all_replacements, local_replacement_syntax_pairs[mod_name] + global_replacement_syntax_pairs)), mod_name))

    return conc_trees

    # DO REWRITES OVER DEFINITIONS FROM COMPILATION
            

def get_submodules(tree: pyslang.SyntaxTree) -> list[pc_core.ModuleInfo]:
    """Extracts the names, types, and params of all submodules instantiated within the given syntax tree.

    Args:
        tree: The input syntax tree.
    Returns:
        A list of ModuleInfo objects representing submodules.
    """
    submodules = []

    #TODO: Non-named params
    def _submodule_collector(obj: Union[pyslang.Token, pyslang.SyntaxNode], submodules: list[pc_core.ModuleInfo]) -> None:
        if isinstance(obj, pyslang.HierarchyInstantiationSyntax):
            for inst in obj.instances:
                if isinstance(inst, pyslang.HierarchicalInstanceSyntax):
                    params = {}
                    for param in obj.parameters.parameters if obj.parameters else []:
                        if isinstance(param, pyslang.OrderedParamAssignmentSyntax):
                            params[param.expr.left.identifier.valueText] = param.expr.right.literal.valueText #type: ignore
                    
                    print(type(inst))

                    submodules.append(pc_core.ModuleInfo(name=inst.decl.name.valueText, m_type=obj.type.valueText, params=params))

    tree.root.visit(pc_core.visitor_wrapper(_submodule_collector, submodules))

    return submodules

def split_tree (tree: pyslang.SyntaxTree) -> list[tuple[str, pyslang.SyntaxTree]]:

    modules = []
    raw_modules = []
    info_trees = []

    for st in tree.root[0]:
        if isinstance(st, pyslang.ModuleDeclarationSyntax):
            raw_modules.append((st.header.name.valueText, st.__str__()))
        else:
            info_trees.append(st.__str__())

    for module in raw_modules:
        modules.append((module[0], pyslang.SyntaxTree.fromText("\n".join(info_trees + [module[1]]))))

    return modules

def main():
    print("Splitting modules...")

    output_dir = "./outputs"

    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)

    os.makedirs(output_dir, exist_ok=True)

    trees = []
    for file in sys.argv[1:]:
        tree = pyslang.SyntaxTree.fromFile(file)
        trees.append(tree)
    
    src_list = []

    for tree in trees:
        split_trees = split_tree(tree)
        for tree in split_trees:
            src_list.append(f"{output_dir}/{tree[0]}.sv")
            with open (f"{output_dir}/{tree[0]}.sv", "w") as f:
                f.write(pyslang.SyntaxPrinter.printFile(tree[1]))

    driver = pyslang.Driver()
    driver.addStandardArgs()

    # Parse command line arguments
    srcs = " ".join([sys.argv[0]] + src_list)
    if not driver.parseCommandLine(srcs, pyslang.CommandLineOptions()):
        return

    # Process options and parse all provided sources
    if not driver.processOptions() or not driver.parseAllSources():
        return

    # Perform elaboration and report all diagnostics
    compilation = driver.createCompilation()
    driver.reportCompilation(compilation, False)

    pyslang.Compilation.getParseDiagnostics(compilation)

    conc_trees = eval_modules(compilation)

    for tree, name in conc_trees:
        with open (f"{output_dir}/{name}_concretized.sv", "w") as f:
            f.write(pyslang.SyntaxPrinter.printFile(tree))

    print()
    print()
    print()
    print(len(conc_trees), "concretized trees:")


if __name__ == "__main__":
    main()