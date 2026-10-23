"""Companion muxes leave blocking-written signals inert.

A companion `x_papercuts` follows the register. A read placed after a blocking
write in the same block (`x = x - 1; if (!x)`) sees the new value in the
original and the old one through the companion, so the muxed design differed
from the original with every select off (opencores osdvu). Bit-shrink and
const-force selects for such a signal stay reserved but drive nothing; a signal
written only with `<=` is still muxed.
"""

from papercuts import Papercutter
from papercuts.pypercuts import insert_muxes_report
from papercuts.utils import print_tree
from pyslang.syntax import SyntaxTree

SRC = """
module m(input logic clk, input logic [3:0] d, output logic z, output logic [3:0] q);
    reg [3:0] cnt;
    reg [3:0] r;
    always @(posedge clk) begin
        cnt = cnt - 1;
        if (!cnt) z <= 1'b1;
        r <= d;
    end
    assign q = r;
endmodule
"""


def run():
    cuts = list(Papercutter(SyntaxTree.fromText(SRC)).cut_info())
    tree, inserted = insert_muxes_report(SyntaxTree.fromText(SRC), True, True, True, caseMux=True,
                                         binopMux=True, constForceMux=True)
    text = print_tree(tree)
    assert "cnt_papercuts" not in text, text
    assert "r_papercuts" in text, text
    reserved = [i for i, (t, _) in enumerate(cuts) if t.startswith(("bitshrink", "force-const"))]
    assert set(reserved) - set(inserted), (reserved, sorted(inserted))  # cnt's selects are inert
    print("test_mux_blocking: OK (blocking-written signal inert, nonblocking one muxed)")


if __name__ == "__main__":
    run()
