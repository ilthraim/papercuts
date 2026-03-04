import time

from pc_core import make_identifier

from pyslang import syntax
from pyslang.syntax import SyntaxTree, SyntaxPrinter, SyntaxRewriter, SyntaxNode, SyntaxKind
from pyslang.parsing import TokenKind

tree = SyntaxTree.fromText("module bit_shrink_ex(input logic [32:0] a);\nendmodule")

def handler(node, r: SyntaxRewriter):
    if isinstance(node, syntax.ModuleHeaderSyntax):
        new_token = r.makeId("TEST")
        node.name = r.makeId("TEST")

nt = syntax.rewrite(tree, handler)

time.sleep(5)

print(SyntaxPrinter.printFile(nt))
