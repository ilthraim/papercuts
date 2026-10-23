"""defparam and specify blocks in the elaborated front end (vtr ch_intrinsics,
opencores sbd_sqrt_fp). A defparam's override lands in the specialized module's
parameter values; a specify block is timing only. Both are dropped, and the
override must still take effect."""

import os
import tempfile

from papercuts.elaborator import elaborate_design

SRC = """
module leaf #(parameter W = 2) (input logic [7:0] a, output logic [7:0] y);
    assign y = a + W;
    specify
        (a => y) = 1;
    endspecify
endmodule
module top(input logic [7:0] a, output logic [7:0] y);
    leaf u(.a(a), .y(y));
    defparam u.W = 5;
endmodule
"""


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "d.v")
        with open(path, "w") as f:
            f.write(SRC)
        src = elaborate_design([path]).source
        assert "defparam" not in src and "specify" not in src, src
        assert "5" in src.split("module top_u")[1] if "module top_u" in src else "W = 5" in src, src
    print("test_defparam_specify: OK (defparam applied and dropped; specify dropped)")


if __name__ == "__main__":
    run()
