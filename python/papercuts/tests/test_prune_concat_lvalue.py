"""A dead signal named in a concatenation target keeps its declaration.

The emitter drops an assignment only when its sole target is pruned, so
`assign {op, rd, ra} = ir;` stayed while rd's declaration went. rd then became
an implicit 1-bit net, the concatenation got shorter, and op and ra read the
wrong bits of ir (opencores aemb edk32 failed the FV gate on this). In a
procedural assignment the undeclared name is a compile error instead, which
threw away the whole prune iteration.
"""

import os
import tempfile

from papercuts.deadcode import _compile, prune_dead_source
from papercuts.elaborator import elaborate

DESIGN = """
module top(input logic clk, input logic [15:0] ir,
           output logic [3:0] y, output logic [3:0] z, output logic [1:0] w);
    wire [3:0] op;
    wire [7:0] rd;
    wire [3:0] ra;
    wire [3:0] gone;
    assign {op, rd, ra} = ir;
    assign gone = ir[3:0];
    logic [1:0] r;
    logic [1:0] dead;
    always_ff @(posedge clk) {r, dead} <= ir[3:0];
    assign y = op;
    assign z = ra;
    assign w = r;
endmodule
"""


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.sv")
        with open(path, "w") as f:
            f.write(DESIGN)
        out, stats = prune_dead_source(elaborate([path]), "top")
    lines = out.splitlines()
    assert any("rd;" in l for l in lines), out
    assert any("dead;" in l for l in lines), out
    # The prune still ran: the signal outside any concatenation went.
    assert not any("gone" in l for l in lines), out
    assert stats.signals >= 1, stats
    comp, _ = _compile(out, "top")
    codes = [d.code.name if hasattr(d.code, "name") else str(d.code)
             for d in comp.getAllDiagnostics()]
    assert not any("ImplicitNet" in c for c in codes), codes
    return "concatenation-target dead signals keep their declarations"
