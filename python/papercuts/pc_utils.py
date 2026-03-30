from typing import Callable, Union
from pyslang.syntax import SyntaxNode, SyntaxRewriter, SyntaxTree, SyntaxPrinter
from pyslang.parsing import Token

def rewrite_wrapper(f, *args, **kwargs) -> Callable[[SyntaxNode, SyntaxRewriter], None]:
    return lambda node, rewriter: f(node, rewriter, *args, **kwargs)

def visitor_wrapper(
    f, *args, **kwargs
) -> Callable[[Union[Token, SyntaxNode]], None]:
    return lambda node: f(node, *args, **kwargs)

def print_tree(tree: SyntaxTree) -> str:
    """Serialize a SyntaxTree, falling back when source-loc metadata is invalid."""
    try:
        return SyntaxPrinter.printFile(tree)
    except RuntimeError:
        # Some rewritten trees can carry invalid source buffer ids in pyslang.
        # `str(tree.root)` prints from CST text without querying those locations.
        return str(tree.root)