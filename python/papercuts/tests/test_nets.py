"""Bit-shrink collector coverage for data decls and nets.

Exercises the widened bit-shrink collector: logic/reg/bit data decls and wire
nets (single + multi-declarator, signed), plus delay/strength nets that must be
skipped. All signals here are single-packed-dimension; see test_arrays.py for
multi-dimensional vectors.
"""

from papercuts import Papercutter
from pyslang.syntax import SyntaxTree

SRC = """
module nets #(parameter W=8) (
    input  logic [7:0] a,
    output logic [7:0] y
    );
    logic  [7:0] l0;
    reg    [7:0] r0;
    bit    [7:0] b0;
    logic  [7:0] lm0, lm1;
    wire   [7:0] w0;
    wire   [7:0] wm0, wm1;
    wire signed [7:0] ws;
    wire [7:0] #3 wdly = a;
    wire (strong0, strong1) [7:0] wstr = a;

    assign w0  = a;
    assign wm0 = a;
    assign wm1 = a;
    assign ws  = a;

    always_comb begin
        l0 = a;
        r0 = a;
        b0 = a;
        lm0 = a; lm1 = a;
        y  = l0 ^ r0 ^ b0 ^ w0 ^ wm0 ^ wm1 ^ ws ^ wdly ^ wstr;
    end
endmodule
"""


def _decl_lines(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith(("logic", "reg", "bit", "wire"))]


def run():
    tree = SyntaxTree.fromText(SRC)
    pc = Papercutter(tree)
    info = pc.cut_info()
    shrink_idxs = [i for i, (t, _) in enumerate(info) if t == "bitshrink"]

    # Nine shrinkable signals: l0, r0, b0, lm0, lm1, w0, wm0, wm1, ws. The two
    # ports and the delay/strength nets (wdly, wstr) are not collected.
    assert len(shrink_idxs) == 9, f"expected 9 bitshrink cuts, got {len(shrink_idxs)}"

    # Each cut narrows its one packed dimension [7:0] -> [6:0]. Spot-check a reg,
    # a plain wire, and a signed wire (signed decls are narrow-mode only).
    produced = set()
    for idx in shrink_idxs:
        out = pc.cut_index([idx])
        produced.update(_decl_lines(out.root.__str__()))

    for want in ("reg [6:0] r0;", "wire [6:0] w0;", "wire signed [6:0] ws;"):
        assert want in produced, f"missing narrowed decl: {want!r}"

    # The delay/strength nets must never be narrowed.
    for bad in produced:
        assert "wdly" not in bad or "[7:0]" in bad, f"wdly should not be narrowed: {bad!r}"
        assert "wstr" not in bad or "[7:0]" in bad, f"wstr should not be narrowed: {bad!r}"

    print(f"test_nets: OK ({len(shrink_idxs)} bitshrink cuts)")


if __name__ == "__main__":
    run()
