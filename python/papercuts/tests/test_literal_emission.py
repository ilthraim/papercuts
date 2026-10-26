"""Integer literals keep their meaning when re-emitted.

- An unsized literal with x/z bits stays unsized: its x top bit extends to the
  width of the context (IEEE 1800 5.7.1, as Jasper reads it). The elaborator
  wrote slang's 32-bit value, `'bx` into 34 bits became `32'dx`, two bits 0
  where the original had X; Jasper found two unpaired X sources (hls jacobi).
  A constant fold over one would bake in the same zero-extension, so none folds.
- A wide literal copied from the source is spelled out in full; str()
  abbreviated it (`196'd2510...e21`, opencores ecg, pairing, ps2), which Jasper
  could not parse.
"""

import os
import tempfile

from papercuts.elaborator import elaborate

DESIGN = """
module t(input logic c, output logic [33:0] y, z, u, q, output logic [255:0] w);
    localparam P = 1;
    assign y = 'bx;
    assign z = 'b1x;
    assign u = 'x;
    assign q = P ? 'hx : 34'd5;
    assign w = 256'h41165887904040835767391004200235198184abcdef0123456789abcdef01;
endmodule
"""


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.sv")
        with open(path, "w") as f:
            f.write(DESIGN)
        out = elaborate([path])
        # stable: the emitted design elaborates to itself
        again = os.path.join(tmp, "again.sv")
        with open(again, "w") as f:
            f.write(out)
        assert elaborate([again]) == out, out
    lines = {l.split("=")[0].split()[-1]: l.strip() for l in out.splitlines() if l.strip().startswith("assign")}
    assert lines["y"] == "assign y = 'b" + "x" * 32 + ";", lines["y"]
    assert lines["z"] == "assign z = 'b1x;", lines["z"]
    assert lines["u"] == "assign u = 34'dx;", lines["u"]  # unbased: already the full width
    assert "'b" + "x" * 32 in lines["q"] and "32'd" not in lines["q"], lines["q"]
    assert "..." not in out and "256'h41165887904040835767391004200235198184abcdef0123456789abcdef01" in out, out
    return "unsized x stays unsized (and unfolded); wide source literals in full"
