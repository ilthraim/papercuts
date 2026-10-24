"""Deeply nested binops do not blow up the muxed source.

A binop mux repeats both operands, so muxing every level of a left-nested chain
doubles the text per level: a 25-deep `|` chain in an HLS if-condition grew past
300 GB (hls gemm, jacobi_1d; opencores ae18). Targets with more than
kBinopNestLimit (10) other targets inside them are left unmuxed (reserved,
inert); the inner ones keep their muxes.
"""

from pyslang.syntax import SyntaxTree

from papercuts.pypercuts import insert_muxes_report
from papercuts.utils import print_tree

DEPTH = 25
CHAIN = "a0"
for i in range(1, DEPTH + 1):
    CHAIN = f"({CHAIN} | a{i})"
SRC = f"""
module m(input logic [{DEPTH}:0] a, output logic y);
    wire logic {", ".join(f"a{i}" for i in range(DEPTH + 1))};
    assign {{{", ".join(f"a{i}" for i in reversed(range(DEPTH + 1)))}}} = a;
    assign y = {CHAIN};
endmodule
"""


def run():
    out, inserted = insert_muxes_report(SyntaxTree.fromText(SRC), binopMux=True)
    text = print_tree(out)
    assert len(text) < 2_000_000, len(text)
    # the 11 innermost `|` (10 or fewer targets below) are muxed, 2 selects each
    assert len(inserted) == 2 * 11, (len(inserted), inserted)
    return f"25-deep chain: {len(inserted)} selects inserted, {len(text)} chars"
