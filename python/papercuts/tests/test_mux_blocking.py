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
    # a function-local variable gets no companion: its `assign` would land inside
    # the function (opencores aes_cipher mix_col, video_systems_qnr assign_s)
    fsrc = """
module f(input logic [7:0] a, output logic [7:0] y);
    function automatic logic [7:0] g(input logic [7:0] x);
        reg [7:0] t;
        t = x ^ 8'h0F;
        g = t;
    endfunction
    assign y = g(a);
endmodule
"""
    from papercuts.elaborator import build_compilation_from, report_diagnostics
    ftree, _ = insert_muxes_report(SyntaxTree.fromText(fsrc), True, True, True, caseMux=True,
                                   binopMux=True, constForceMux=True)
    ftext = print_tree(ftree)
    assert "t_papercuts" not in ftext, ftext
    assert not report_diagnostics(build_compilation_from([SyntaxTree.fromText(ftext)])), ftext
    print("test_mux_blocking: OK (blocking-written signal inert, nonblocking one muxed, function local inert)")


if __name__ == "__main__":
    run()
