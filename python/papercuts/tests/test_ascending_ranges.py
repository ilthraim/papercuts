"""Bit-shrink companions of ascending ranges ([1:48], [0:3]).

The left index is the MSB whichever way a range runs. The intermediate-wire
builder took the numerically highest index for the MSB, so for `[1:48]` it wrote
`{(sel ? 1'b0 : a[48]), a[1:47]}`: the wrong bit dropped and the rest rotated,
wrong even with the select off (opencores des: MiterMismatch with every cut off).
"""

from pyslang.syntax import SyntaxTree

from papercuts import insert_muxes
from papercuts.utils import print_tree

SRC = """
module m(input logic [1:48] a, input logic [7:0] b, input logic signed [0:3] c, input logic [0:1][3:0] e,
         output logic [1:48] x, output logic [7:0] y, output logic signed [0:3] z, output logic [0:1][3:0] f);
    wire logic [1:48] aw = a;
    wire logic [7:0] bw = b;
    wire logic signed [0:3] cw = c;
    wire logic [0:1][3:0] ew = e;
    assign x = aw;
    assign y = bw;
    assign z = cw;
    assign f = ew;
endmodule
"""

EXPECT = [
    "assign aw_papercuts = {(pc_sel0 ? 1'b0 : aw[1]), aw[2:48]};",
    "assign bw_papercuts = {(pc_sel1 ? 1'b0 : bw[7]), bw[6:0]};",
    "assign cw_papercuts = {(pc_sel2 ? cw[1] : cw[0]), cw[1:3]};",
    "assign ew_papercuts = {(pc_sel3 ? {4{1'b0}} : {(pc_sel4 ? 1'b0 : ew[0][3]), ew[0][2:0]}), "
    "{(pc_sel4 ? 1'b0 : ew[1][3]), ew[1][2:0]}};",
]


def run():
    text = print_tree(insert_muxes(SyntaxTree.fromText(SRC), bitMux=True, shrinkWithIntermediate=True))
    got = [l.strip() for l in text.splitlines() if "_papercuts =" in l]
    assert got == EXPECT, got
    return "MSB is the left index: ascending ranges drop index left, keep order"
