"""Bit-shrink must not narrow the redeclaration of a non-ANSI port.

A non-ANSI port's `reg [7:0] y;` / `wire [7:0] z;` is an ordinary data/net
declaration, so the collector used to shrink it. Two failure shapes:

- the port line has a range (`output [7:0] y;`): the cut left the port line at
  the old width -- illegal source ("inconsistent dimensions") that Jasper
  accepted and checked as if nothing were shrunk, so the cut came back proven
  (sha_core_512 `text_o`, 31 bits).
- the port line has no range (`output w;`): the cut was legal source but
  narrowed the port itself.

Ports are not bit-shrink targets (ANSI ports never reach the collector), so
both are skipped; an internal reg in the same module still is one.
"""

import pyslang
from papercuts import Papercutter
from pyslang.syntax import SyntaxTree

SRC = """
module m (clk, a, y, z, w, q);
  input clk;
  input [7:0] a;
  output [7:0] y;
  output [7:0] z;
  output w;
  output [7:0] q;
  reg [7:0] y;
  wire [7:0] z;
  reg [7:0] w;
  reg [7:0] inner;
  always @(posedge clk) begin y <= a; w <= a; inner <= a; end
  assign z = a;
  assign q = inner;
endmodule
"""


def _errors(text):
    comp = pyslang.ast.Compilation()
    comp.addSyntaxTree(SyntaxTree.fromText(text))
    return [d for d in comp.getAllDiagnostics() if d.isError()]


def run():
    pc = Papercutter(SyntaxTree.fromText(SRC))
    shrink = [i for i, (t, _) in enumerate(pc.cut_info()) if t == "bitshrink"]
    lines = [ln for i, (t, ln) in enumerate(pc.cut_info()) if t == "bitshrink"]

    # Only `inner` (line 12); y, z, w are non-ANSI port redeclarations.
    assert lines == [12], f"expected one bitshrink cut on line 12, got lines {lines}"

    text = pyslang.syntax.SyntaxPrinter.printFile(pc.cut_index(shrink))
    assert "reg [6:0] inner;" in text, text
    for decl in ("output [7:0] y;", "reg [7:0] y;", "wire [7:0] z;", "output w;", "reg [7:0] w;"):
        assert decl in text, f"{decl!r} changed:\n{text}"
    errs = _errors(text)
    assert not errs, f"cut source does not compile: {errs}"
