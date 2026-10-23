"""X sources in the CEGIS miter must mean what they mean to Jasper (cegis/xpair.py).

Measured on 2026-10-23 with papercuts' own pcjg.tcl, for an 'x literal, a case
default 'x, an out-of-range bit select and a division by zero: golden vs golden
is proven, and golden vs a cut replacing the X with 0, or with 1, is a cex.
Jasper treats each X as a free value and auto-maps the two designs' X sources
onto each other. Before pairing, the BTOR2 flow disagreed both ways: each copy
had its own X (golden != golden), and division by zero was all-ones (so the
"-> 1" cut passed).

Each case is a golden module `t` with an X source and a muxed twin where
pc_sel0 / pc_sel1 replace that X with 0 / 1. Expected, per (sel0, sel1):
(0, 0) equivalent, (1, 0) and (0, 1) counterexamples.
"""

import os
import subprocess
import tempfile

from papercuts.cegis.btor2z3 import Design
from papercuts.cegis.comb import CombCegis
from papercuts.cegis.netlist import build_btor

CASES = {
    "literal": dict(
        ports="input logic s, input logic [3:0] a, output logic [3:0] y",
        golden="assign y = s ? a : 'x;",
        muxed="assign y = s ? a : (pc_sel0 ? 4'b0000 : (pc_sel1 ? 4'b1111 : 'x));"),
    "case_default": dict(
        ports="input logic [1:0] s, input logic [3:0] a, input logic [3:0] b, output logic [3:0] y",
        golden="always_comb case (s) 2'd0: y = a; 2'd1: y = b; default: y = 'x; endcase",
        muxed="always_comb case (s) 2'd0: y = a; 2'd1: y = b; "
              "default: y = pc_sel0 ? 4'b0000 : (pc_sel1 ? 4'b1111 : 'x); endcase"),
    "out_of_range": dict(
        ports="input logic [3:0] a, input logic [2:0] i, output logic y",
        golden="assign y = a[i];",
        muxed="assign y = (i < 3'd4 || !(pc_sel0 | pc_sel1)) ? a[i] : pc_sel1;"),
    "divide_by_zero": dict(
        ports="input logic [3:0] a, input logic [3:0] b, output logic [3:0] y",
        golden="assign y = a / b;",
        muxed="assign y = (b == 4'd0 && (pc_sel0 | pc_sel1)) ? (pc_sel1 ? 4'b1111 : 4'b0000) : a / b;"),
    # A registered case default: the X reaches the register's next state, each bit
    # only its own. If the flip-flop were not traced bit by bit, every X bit would
    # key on all of q and the pairing would be ambiguous (xtea's `state`).
    "register_default": dict(
        ports="input logic clk, input logic [1:0] s, input logic [3:0] a, output logic [3:0] y",
        golden="logic [3:0] q; always_ff @(posedge clk) case (s) 2'd0: q <= a; 2'd1: q <= ~a; "
               "default: q <= 'x; endcase assign y = q;",
        muxed="logic [3:0] q; always_ff @(posedge clk) case (s) 2'd0: q <= a; 2'd1: q <= ~a; "
              "default: q <= pc_sel0 ? 4'b0000 : (pc_sel1 ? 4'b1111 : 'x); endcase assign y = q;"),
    # The X is assigned to `cl`; the muxed copy reads it through a bit-mux
    # companion whose low bits are plain aliases of cl, as papercuts writes them.
    # Keying on the companion would leave the X unpaired, and golden != golden.
    "companion": dict(
        ports="input logic s, input logic [3:0] a, output logic [3:0] y",
        golden="logic [3:0] cl; assign cl = s ? a : 'x; assign y = cl;",
        muxed="logic [3:0] cl; logic [3:0] cl_papercuts; "
              "assign cl = s ? a : (pc_sel0 ? 4'b0000 : (pc_sel1 ? 4'b1111 : 'x)); "
              "assign cl_papercuts = {cl[3], cl[2:0]}; assign y = cl_papercuts;"),
}


def _ports(spec):
    ins, outs = [], []
    for p in spec.split(","):
        toks = p.split()
        (ins if toks[0] == "input" else outs).append((toks[-1], " ".join(toks[1:-1])))
    return ins, outs


def miter_source(case):
    ins, outs = _ports(case["ports"])
    sv = f"module t({case['ports']});\n    {case['golden']}\nendmodule\n"
    sv += (f"module t_muxed({case['ports']}, input logic pc_sel0, input logic pc_sel1);\n"
           f"    {case['muxed']}\nendmodule\n")
    decl = ",\n    ".join([f"input {t} {n}" for n, t in ins] + ["input logic pc_sel0, input logic pc_sel1",
                                                                 "output logic eq"])
    conn = ", ".join(f".{n}({n})" for n, _ in ins)
    body = "".join(f"    {t} {n}__orig, {n}__mod;\n" for n, t in outs)
    body += f"    t orig({conn}, " + ", ".join(f".{n}({n}__orig)" for n, _ in outs) + ");\n"
    body += f"    t_muxed mod({conn}, " + ", ".join(f".{n}({n}__mod)" for n, _ in outs) + ", .pc_sel0, .pc_sel1);\n"
    body += "    assign eq = " + " & ".join(f"({n}__orig == {n}__mod)" for n, _ in outs) + ";\n"
    body += "    always @(*) assert(eq);\n"
    return sv + f"module miter(\n    {decl});\n{body}endmodule\n"


def verdicts(name, case, tmp):
    src = os.path.join(tmp, f"{name}.sv")
    with open(src, "w") as f:
        f.write(miter_source(case))
    base = os.path.join(tmp, name)
    rep = build_btor([src], "miter", base)
    design = Design.from_file(base + ".btor", base + ".info")
    out = []
    for asg in ((False, False), (True, False), (False, True)):
        c = CombCegis(design, verbose=False, selects=["pc_sel0", "pc_sel1"])
        out.append("eq" if c.verify({"pc_sel0": asg[0], "pc_sel1": asg[1]}) is None else "cex")
    return out, rep


def test_jasper_verdicts():
    with tempfile.TemporaryDirectory() as tmp:
        for name, case in CASES.items():
            got, rep = verdicts(name, case, tmp)
            assert got == ["eq", "cex", "cex"], (name, got, rep.summary())


def test_report():
    with tempfile.TemporaryDirectory() as tmp:
        _, rep = verdicts("literal", CASES["literal"], tmp)
        assert (rep.bits, rep.paired, rep.unpaired) == (8, 4, []), rep.summary()
        _, rep = verdicts("companion", CASES["companion"], tmp)
        assert rep.paired == 4 and not rep.unpaired, rep.summary()
        _, rep = verdicts("divide_by_zero", CASES["divide_by_zero"], tmp)
        assert rep.divs >= 1, rep.summary()


def test_yosys_alone_disagrees():
    # Why the pass exists: without it, golden != golden on an X literal.
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "literal.sv")
        with open(src, "w") as f:
            f.write(miter_source(CASES["literal"]))
        base = os.path.join(tmp, "plain")
        subprocess.run(["yosys", "-q", "-p", f"plugin -i slang; read_slang {src} --top miter; "
                        f"prep -top miter -flatten; write_btor -s -x {base}.btor"], check=True, capture_output=True)
        d = Design.from_file(base + ".btor")
        assert len(d.input_names(r"^_anon\d+$")) == 2


def run():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_cegis_xpair: OK ({len(tests)} tests)")


if __name__ == "__main__":
    run()
