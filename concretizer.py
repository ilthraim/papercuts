from typing import TYPE_CHECKING, Callable, Union
import pyslang
import re

import pc_core

def extract_params(tree: pyslang.SyntaxTree) -> dict:

    param_dict = {}

    def extract_var_names(expr: str) -> list[str]:
        pattern = r'[a-zA-Z_$][a-zA-Z0-9_$]*'
        return re.findall(pattern, expr)

    def _param_decl_handler(obj: Union[pyslang.Token, pyslang.SyntaxNode], param_dict) -> None:
        if isinstance(obj, pyslang.ParameterDeclarationSyntax):
            decl: pyslang.DeclaratorSyntax
            for decl in obj.declarators:
                param_name = decl.name.valueText
                try:
                    init = decl.initializer
                    expr_str = init.expr.__str__()
                    var_names = extract_var_names(expr_str)
                    for var in var_names:
                        if var in param_dict:
                            print(f"Parameter {param_name} depends on another parameter {var}, substituting {param_dict[var]}.")
                            expr_str = expr_str.replace(var, str(param_dict[var]))
                    default = int(eval(expr_str))  # TODO: Safer eval
                    print(f"Extracted parameter {param_name} with default value {default}")
                    
                    param_dict[param_name] = default
                except ValueError:
                    pass

    tree.root.visit(pc_core.visitor_wrapper(_param_decl_handler, param_dict))

    return param_dict

def concretize_params(tree: pyslang.SyntaxTree, param_dict: dict) -> pyslang.SyntaxTree:
    def _param_concretizer(node: pyslang.SyntaxNode, rewriter: pyslang.SyntaxRewriter, param_dict: dict) -> None:
        if isinstance(node, pyslang.IdentifierNameSyntax):
            if node.identifier.valueText in param_dict:
                value = param_dict[node.identifier.valueText]
                print(f"Concretizing parameter {node.identifier.valueText} to value {value}")
                old_trivia = "".join(t.getRawText() for t in node.getFirstToken().trivia)
                new_node = pyslang.SyntaxTree.fromText(old_trivia + str(value)).root
                rewriter.replace(node, new_node)
            

    return pyslang.rewrite(tree, pc_core.rewrite_wrapper(_param_concretizer, param_dict))

def reduce_expressions(tree: pyslang.SyntaxTree) -> pyslang.SyntaxTree:

    def _expr_reducer(node: pyslang.SyntaxNode, rewriter: pyslang.SyntaxRewriter) -> None:

        if isinstance(node, pyslang.BinaryExpressionSyntax):
            try:
                result = eval(node.__str__()) #TODO: Safer eval
            except Exception:
                return

            print(f"Reducing expression {node} to value {result}")
            old_trivia = "".join(t.getRawText() for t in node.getFirstToken().trivia)
            new_node = pyslang.SyntaxTree.fromText(old_trivia + str(result)).root
            rewriter.replace(node, new_node)
    try:
        new_tree = pyslang.rewrite(tree, _expr_reducer)
        return new_tree
    except Exception:
        return tree
    
    


