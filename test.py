import pc_core

from pyslang import syntax
t = syntax.SyntaxTree.fromFile("../verilog_examples/random/param_submodule/binary2bcd.sv")
nt = pc_core.shrink_bits_mux(t)

print(syntax.SyntaxPrinter.printFile(nt))