from papercuts import insert_muxes, Papercutter, rename_module
from pyslang.syntax import SyntaxPrinter, SyntaxTree
import sys

tree = SyntaxTree.fromFile(sys.argv[1])

pc = Papercutter(tree)
newTrees = pc.cut_all()

for ntree in newTrees:
    print(SyntaxPrinter.printFile(ntree))

muxTree = insert_muxes(tree, True, True, True)

renameTree = rename_module(tree, "NewModuleName")

print(SyntaxPrinter.printFile(muxTree))
print(SyntaxPrinter.printFile(renameTree))