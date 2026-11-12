"""Narrow-mode bit-shrink pads a narrowed signal connected to an input port.

Narrowing edits only the declaration, so `.in2(x)` with `x` now `[0:0]` and the
port still `[4:0]` relies on the connection extending `x` implicitly. That is
legal SystemVerilog and the formal tool accepts it, but Fusion Compiler folds a
`[0:0]` net driven only by a constant into a scalar and then will not link it
to a bus port (LNK-042, then DES-220): opencores rs_decoder's FV result had 31
such connections. The cutter writes the extension out instead, padding the
signal back to the width it was declared with. Output connections, connections
through an expression, inner-dimension narrowing and unresolvable modules are
left as written.
"""

import os
import re
import shutil
import subprocess
import tempfile

from papercuts import Papercutter, chipper
from papercuts.elaborator import build_compilation_from
from pyslang.syntax import SyntaxTree
from pyslang import ast

CHILD = """
module child (input logic [4:0] a, input logic signed [3:0] s, input logic [5:0] k,
              output logic [4:0] y);
    assign y = a ^ s ^ k;
endmodule

module unknown (input logic [4:0] a);
endmodule
"""

# n: named input; c: constant-driven (the rs_decoder shape); r: positional input;
# o: driven by an output; e: read through an expression; sg: signed;
# pk: packed 2-D (outer and inner narrowing); x: input of an unknown module.
PARENT = """
module p (input logic [4:0] in, input logic b, output logic [4:0] out1, output logic [4:0] out2,
          output logic [4:0] out3);
    logic [4:0] n;
    logic [4:0] c;
    logic [4:0] r;
    logic [4:0] o;
    logic [4:0] e;
    logic signed [3:0] sg;
    logic [2:0][1:0] pk;
    logic [4:0] x;
    assign n = in;
    assign c = 5'b0;
    assign r = in;
    assign e = in;
    assign sg = in[3:0];
    assign pk = {in, b};
    assign x = in;
    child u1 (.a(n), .s(sg), .k(pk), .y(o));
    child u2 (r, sg, pk, out2);
    child u3 (.a(c), .s(sg), .k(pk), .y(out3));
    child u4 (.a(e & {5{b}}), .s(sg), .k(pk), .y());
    unknown u5 (.a(x));
    assign out1 = o;
endmodule
"""

DECL = {ln.split()[-1].rstrip(";"): ln.strip() for ln in PARENT.splitlines()
        if ln.strip().startswith("logic ")}


def _dirs():
    comp = build_compilation_from([SyntaxTree.fromText(CHILD), SyntaxTree.fromText(PARENT)])
    dirs = chipper.definition_port_directions(comp)
    # `unknown` is left out, as a module the pipeline could not resolve would be
    return {k: v for k, v in dirs.items() if k == "child"}


def _flat(text):
    return " ".join(text.split())


def _cuts(pc, name, amount=1):
    """{dimIndex: cut text} for every bitshrink cut on `name`'s declaration."""
    out = []
    for i, (kind, _) in enumerate(pc.cut_info()):
        if kind != "bitshrink":
            continue
        text = pc.cut_index_text([i], {i: amount})
        decl = next((ln.strip() for ln in text.splitlines() if re.search(rf"\b{name};$", ln.strip())
                     and ln.strip().startswith("logic")), None)
        if decl is not None and decl != DECL[name]:
            out.append((decl, text))
    assert out, f"no bitshrink cut narrows {name}"
    return out


def _errors(text):
    comp = ast.Compilation()
    comp.addSyntaxTree(SyntaxTree.fromText(CHILD))
    comp.addSyntaxTree(SyntaxTree.fromText(text))
    return [str(d.code) for d in comp.getAllDiagnostics() if d.isError()]


def _equivalent(padded: str, bare: str) -> bool:
    """yosys proves the padded cut equal to the same cut with the padding removed."""
    tmp = tempfile.mkdtemp()
    files = []
    for tag, text in (("pad", padded), ("bare", bare)):
        text = text.replace("module p ", f"module p_{tag} ")
        files.append(os.path.join(tmp, f"{tag}.sv"))
        with open(files[-1], "w") as f:
            f.write(text)
    child = os.path.join(tmp, "child.sv")
    with open(child, "w") as f:
        f.write(CHILD)
    script = (f"plugin -i slang; read_slang {child} {' '.join(files)} --top p_pad --top p_bare; "
              "proc; opt_clean; miter -equiv -flatten -make_assert p_pad p_bare m; "
              "hierarchy -top m; sat -verify -prove-asserts -show-ports m")
    r = subprocess.run(["yosys", "-q", "-p", script], capture_output=True, text=True)
    return r.returncode == 0


def run():
    dirs = _dirs()
    pc = Papercutter(SyntaxTree.fromText(PARENT), port_directions=dirs)
    checks = []

    # named input: padded back to the declared 5 bits
    (_, t), = _cuts(pc, "n")
    assert ".a({1'b0, n})" in _flat(t), t
    checks.append((t, ".a({1'b0, n})", ".a(n)"))

    # the rs_decoder shape: shrunk to [0:0], driven only by a constant
    (decl, t), = _cuts(pc, "c", amount=4)
    assert decl == "logic [0:0] c;", decl
    assert ".a({4'b0, c})" in _flat(t), t
    checks.append((t, ".a({4'b0, c})", ".a(c)"))

    # positional input
    (_, t), = _cuts(pc, "r")
    assert "u2 ({1'b0, r}, sg, pk, out2)" in _flat(t), t
    checks.append((t, "{1'b0, r}, sg", "r, sg"))

    # driven by an output port: a write, left to truncate as before
    (_, t), = _cuts(pc, "o")
    assert ".y(o)" in _flat(t), t

    # read through an expression: sized by the expression, untouched
    (_, t), = _cuts(pc, "e")
    assert ".a(e & {5{b}})" in _flat(t), t

    # signed: copies of the new top bit, still signed
    (_, t), = _cuts(pc, "sg")
    assert ".s($signed({sg[2], sg}))" in _flat(t), t
    assert "u2 ({1'b0, r}" not in _flat(t) and "u2 (r, $signed({sg[2], sg}), pk, out2)" in _flat(t), t
    checks.append((t, "$signed({sg[2], sg})", "sg"))

    # packed 2-D: outer narrowing pads a whole element; inner narrowing is left alone
    by_decl = dict(_cuts(pc, "pk"))
    t = by_decl["logic [1:0][1:0] pk;"]
    assert ".k({2'b0, pk})" in _flat(t), t
    checks.append((t, "{2'b0, pk}", "pk"))
    t = by_decl["logic [2:0][0:0] pk;"]
    assert ".k(pk)" in _flat(t), t

    # a module the direction map does not know: left alone
    (_, t), = _cuts(pc, "x")
    assert ".a(x)" in _flat(t), t

    # without a direction map nothing is padded
    bare_pc = Papercutter(SyntaxTree.fromText(PARENT))
    (_, t), = _cuts(bare_pc, "n")
    assert ".a(n)" in _flat(t), t

    for text, padded, bare in checks:
        assert not _errors(text), (_errors(text), text)
        if shutil.which("yosys"):
            flat = _flat(text)
            assert flat.count(padded) >= 1
            assert _equivalent(text, text.replace(padded, bare)), (padded, text)

    print("  test_narrow_port_padding: OK (narrowed input connections padded to the declared width; "
          "outputs, expressions, inner dims, unknown modules untouched; equivalent by yosys)")


if __name__ == "__main__":
    run()
