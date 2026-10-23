"""Constant operands that fold into invalid SV: a parameter selected with a
variable index, and a zero-count replication inside a concatenation.

Folding turns a parameter reference into its literal; selecting from a literal
(`6'b100101[i]`) is not SystemVerilog, and Jasper rejected the elaborated
opencores configurable_crc_core for it. A select that does not fold must name
the parameter, which the emitted module declares with its specialized value; a
select with a constant index still folds to a literal.
"""

import os
import re
import tempfile

from papercuts.elaborator import elaborate_design

SRC = """
module m #(parameter [5:0] C = 6'b100101, parameter W = 16, parameter init = "rom.hex")
         (input logic [2:0] i, input logic [15:0] a, output logic y, output logic z, output logic [W-1:0] w);
    reg [7:0] mem [0:3];
    initial $readmemh(init, mem, 0, 3);  // opencores dpsfmnce
    assign y = C[i + 1];
    assign z = C[2];
    assign w = { {{W-16}{1'b0}}, a };  // zero-count replication (opencores wb_mcs51)
endmodule
"""


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "m.sv")
        with open(path, "w") as f:
            f.write(SRC)
        src = elaborate_design([path]).source
        assert not re.search(r"'[bBhHdDoO][0-9a-fA-FxXzZ_]+\[", src), src  # no select on a literal
        assert "assign y = C[(i + 1)];" in src, src
        assert "assign z = 1'b1;" in src, src  # constant index still folds
        assert "null" not in src and "assign w = {a};" in src, src
        assert '$readmemh("rom.hex", mem, 0, 3);' in src, src
    print("test_param_select: OK (variable index names the parameter; constant index folds; "
          "zero-count replication dropped; $readmemh file name and memory argument)")


if __name__ == "__main__":
    run()
