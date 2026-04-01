from papercuts import insert_muxes, Papercutter, rename_module
from pyslang.syntax import SyntaxPrinter, SyntaxTree, SyntaxNode
import pyslang.syntax as syntax
import pyslang
import sys

tree = SyntaxTree.fromText("""
module poly #(
    parameter WIDTH=8)(
    inputlogic[7:0] x,
    outputlogic[16:0] y
    );logic[16:0] z;logic[16:0] N;logic[16:0] K;
    localparam N_val =256;
    localparam K_val =65535;
  
	assign N =256;
    assign K =65535;
    // polynomial (2^n - x) * x
    always_comb begin
        z = (N - x) * x; 
        y = (z > K) ? K : z; // This is unnecessary
    end
endmodule
""")

pc = Papercutter(tree)