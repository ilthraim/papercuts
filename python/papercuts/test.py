from papercuts import cut, insert_muxes
from pyslang.syntax import SyntaxPrinter, SyntaxTree
import sys

tree = SyntaxTree.fromFile(sys.argv[1])

newTrees = cut(tree, True, True, True)

# for ntree in newTrees:
#     print(SyntaxPrinter.printFile(ntree))

muxTree = insert_muxes(tree, True, True, True)
print(SyntaxPrinter.printFile(muxTree))