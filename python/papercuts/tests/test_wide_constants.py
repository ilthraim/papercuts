"""Wide constants are emitted in full.

slang's str() of a wide decimal abbreviates it (`195'd2510840...e21`), which
is not a literal: opencores ecg's elaborated source failed to parse in Jasper.
Unknown bits stay exact (binary), signed negatives keep their sign.
"""

import os
import re
import tempfile

from papercuts.elaborator import elaborate

DESIGN = """
module top(input logic clk, input logic reset, output logic [194:0] i, output logic [7:0] u,
           output logic signed [7:0] n);
    localparam logic [194:0] BIG = 195'd25108406941546723055343157692830665664123456789012345678;
    localparam logic [7:0] UNK = 8'b10x1z011;
    localparam logic signed [7:0] NEG = -8'sd5;
    always @(posedge clk) i <= reset ? BIG : (i >> 1);
    assign u = UNK;
    assign n = NEG;
endmodule
"""


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.sv")
        with open(path, "w") as f:
            f.write(DESIGN)
        out = elaborate([path])
    assert "..." not in out, out
    m = re.search(r"BIG = 195'h([0-9a-fA-F]+);", out)
    assert m and int(m.group(1), 16) == 25108406941546723055343157692830665664123456789012345678, out
    assert "8'b10x1z011" in out and "-8'sd5" in out, out
    return "wide decimal in full, unknowns in binary, negatives signed"
