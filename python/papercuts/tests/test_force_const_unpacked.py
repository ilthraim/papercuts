"""A 1-bit unpacked array is not a force-const target.

`wire logic w[8:0]` has no packed dimension, so the collector took it for a
scalar and built `w_papercuts = sel ? 1'b1 : (sel ? 1'b0 : w)` for an array,
which does not compile (opencores pid_controller).
"""

from pyslang.syntax import SyntaxTree

from papercuts import insert_muxes
from papercuts.utils import print_tree

SRC = """
module m(input logic clk, input logic a, output logic y);
    wire logic w[8:0];
    wire logic s;
    assign w[0] = a;
    assign s = a;
    assign y = w[0] ^ s;
endmodule
"""


def run():
    text = print_tree(insert_muxes(SyntaxTree.fromText(SRC), constForceMux=True))
    assert "w_papercuts" not in text, text
    assert "s_papercuts" in text, text  # the scalar is still a target
    tree = SyntaxTree.fromText(text)
    assert not any(d.isError() for d in tree.diagnostics), [str(d.code) for d in tree.diagnostics]
    return "unpacked arrays are skipped, scalars kept"
