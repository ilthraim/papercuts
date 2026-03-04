from typing import Callable, Union

from pyslang import syntax
from pyslang.syntax import SyntaxTree, SyntaxPrinter, SyntaxKind, SyntaxNode, SyntaxRewriter
from pyslang.parsing import Token

def rename_module(tree: SyntaxTree, new_name: str) -> SyntaxTree:
    """Rename the module in a SystemVerilog source string."""
    root = tree.root
    source = SyntaxPrinter.printFile(tree)

    module_decl =  find_module_decl(root)
    
    name_token = module_decl.header.name
    token_range = name_token.range
    
    start = token_range.start.offset
    end = token_range.end.offset
    
    return SyntaxTree.fromText(source[:start] + new_name + source[end:])

def rewrite_wrapper(f, *args, **kwargs) -> Callable[[SyntaxNode, SyntaxRewriter], None]:
    return lambda node, rewriter: f(node, rewriter, *args, **kwargs)

def visitor_wrapper(f, *args, **kwargs) -> Callable[[Union[Token, SyntaxNode]], None]:
    return lambda node: f(node, *args, **kwargs)

def find_module_decl(node) -> syntax.ModuleDeclarationSyntax:
    """Recursively find ModuleDeclarationSyntax."""
    if node.kind == SyntaxKind.ModuleDeclaration:
        return node
    for child in node:
        result = find_module_decl(child)
        if result:
            return result
    raise ValueError("ModuleDeclarationSyntax not found")

def get_module_name(tree: SyntaxTree) -> str:
    """Get the module name from a SystemVerilog source."""
    
    module_decl: syntax.ModuleDeclarationSyntax = find_module_decl(tree.root)
    return module_decl.header.name.valueText