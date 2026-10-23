"""Bit-shrink muxing on an unpacked array leaves the select inert.

The bit-shrink cut narrows `reg [31:0] mem [0:63]` to `[30:0]`, which is fine as
a source cut. A mux would need a companion per element; the packed-only
companion read `mem[31]` as a bit of one vector, which is an element select of
the array, and the muxed source failed to compile (opencores cr_div). The select
must stay reserved (indices aligned, mux invariant 8) but drive nothing, and the
muxed source must compile.
"""

import re

from papercuts import Papercutter
from papercuts.elaborator import build_compilation_from, report_diagnostics
from papercuts.pypercuts import insert_muxes_report
from papercuts.utils import print_tree
from pyslang.syntax import SyntaxTree

SRC = """
module m(input logic clk, input logic [5:0] a, input logic [31:0] d, output logic [31:0] q, output logic [7:0] r);
    reg [31:0] mem [0:63];
    logic [7:0] v;
    always @(posedge clk) mem[a] <= d;
    assign q = mem[a];
    assign v = d[7:0];
    assign r = v;
endmodule
"""


def run():
    cuts = list(Papercutter(SyntaxTree.fromText(SRC)).cut_info())
    bitshrink = [i for i, (t, _) in enumerate(cuts) if t.startswith("bitshrink")]
    tree, inserted = insert_muxes_report(SyntaxTree.fromText(SRC), True, True, True, caseMux=True,
                                         binopMux=True, constForceMux=True)
    text = print_tree(tree)
    assert "mem_papercuts" not in text, text
    assert "v_papercuts" in text, text  # a plain vector is still muxed
    inert = [i for i in bitshrink if i not in set(inserted)]
    assert len(inert) == 1, (bitshrink, sorted(inserted))
    assert re.search(rf"\bpc_sel{inert[0]}\b", text.split(");", 1)[0]), "reserved select must still be a port"
    comp = build_compilation_from([SyntaxTree.fromText(text)])
    assert not report_diagnostics(comp), text
    print("test_mux_unpacked: OK (array bit-shrink select reserved and inert; muxed source compiles)")


if __name__ == "__main__":
    run()
