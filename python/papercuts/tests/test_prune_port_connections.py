"""A dead signal named in an instance's port connection keeps its declaration.

The emitter re-emits port connections as they are, so pruning such a signal
left `.q({used, unused})` naming an undeclared wire (opencores mesi_isc). slang
accepts that as an implicit net; Jasper does not parse it.
"""

import os
import tempfile

from papercuts.deadcode import prune_dead_source
from papercuts.elaborator import elaborate

DESIGN = """
module fifo(input logic clk, input logic [3:0] d, output logic [3:0] q);
    always @(posedge clk) q <= d;
endmodule
module top(input logic clk, input logic [3:0] d, output logic [1:0] y);
    wire [1:0] used;
    wire [1:0] unused;
    fifo f (.clk(clk), .d(d), .q({used, unused}));
    assign y = used;
endmodule
"""


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.sv")
        with open(path, "w") as f:
            f.write(DESIGN)
        out, _ = prune_dead_source(elaborate([path]), "top")
    assert any("unused;" in l for l in out.splitlines()), out
    return "port-connected dead wire keeps its declaration"
