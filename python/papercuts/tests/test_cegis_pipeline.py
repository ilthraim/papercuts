"""`--method cegis-comb` through the whole pipeline, with a stub backend.

The stub proves (or, for one test, refutes) whatever it is handed and records
each call, so this checks the plumbing without Jasper: the backend sees the
self-check, the elaboration gate and ONE joint check of the whole consolidated
design (never a check per cut or per module); the set lands in papercuts.log
and results.jsonl; every module's share of the set is applied; a failed joint
check is fatal.
"""

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile

import papercuts.__main__ as pm
from papercuts.backends.base import ECBackend

DESIGN = """
module child(input logic clk, input logic [3:0] a, input logic s, output logic [3:0] q);
    logic [3:0] r;
    always_ff @(posedge clk) r <= s ? a : (a & 4'hF);
    assign q = r | 4'h0;
endmodule

module top(input logic clk, input logic [3:0] a, input logic s, output logic [3:0] y);
    logic [3:0] q0, q1;
    child c0(.clk(clk), .a(a), .s(s), .q(q0));
    child c1(.clk(clk), .a(~a), .s(s), .q(q1));
    assign y = q0 ^ q1;
endmodule
"""


def make_stub(calls, refute_joint=False):
    class Stub(ECBackend):
        name = "stub"

        async def check(self, run):
            calls.append(run)
            ok = not (refute_joint and run.index == -1)
            run.valid, run.verdict = ok, "proven" if ok else "cex"
            return ok

        async def check_from_power_up(self, run):
            run.power_up = True
            return await self.check(run)

    return Stub


def pipeline(tmp, extra, refute_joint=False):
    """Run main() in `tmp` (it writes ./outputs); returns (backend calls, SystemExit or None)."""
    with open(os.path.join(tmp, "design.sv"), "w") as f:
        f.write(DESIGN)
    calls = []
    saved = (os.getcwd(), sys.argv, pm.get_backend)
    os.chdir(tmp)
    method = [] if "--method" in extra else ["--method", "cegis-comb"]
    sys.argv = ["papercuts", "design.sv", "-e"] + method + extra
    pm.get_backend = lambda name: make_stub(calls, refute_joint)
    exit_ = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(pm.main())
    except SystemExit as e:
        exit_ = e
    finally:
        os.chdir(saved[0])
        sys.argv, pm.get_backend = saved[1], saved[2]
    return calls, exit_


def results(tmp):
    with open(os.path.join(tmp, "outputs", "papercuts.results.jsonl")) as f:
        return [json.loads(line) for line in f]


def test_elaborated():
    with tempfile.TemporaryDirectory() as tmp:
        calls, exit_ = pipeline(tmp, [])
        assert exit_ is None, exit_
        # self-check, elaboration gate, one joint check; nothing per cut or per module
        assert [c.index for c in calls] == [0, 0, -1], [c.impl_module_path for c in calls]
        joint = calls[-1]
        assert joint.is_top and joint.impl_module_path.endswith("joint/top_consolidated.sv")
        # the joint check starts from power-up; the gates keep the reset
        assert getattr(joint, "power_up", False) and not any(getattr(c, "power_up", False) for c in calls[:-1])
        with open(os.path.join(tmp, joint.impl_module_path)) as f:
            text = f.read()
        assert all(f"module {m}" in text for m in ("top", "top_c0", "top_c1")), text

        cuts = [r for r in results(tmp) if r["phase"] == "cuts"]
        assert len(cuts) == 18 and all(r["method"] == "cegis-comb" for r in cuts)
        in_set = [r for r in cuts if r["in_set"]]
        assert in_set, cuts
        # exclusive halves of one ternary are never both in the set
        for mod in ("top_c0", "top_c1"):
            assert sum(1 for r in in_set if r["module"] == mod and r["type"].startswith("ternary")) <= 1
        joint_rec = [r for r in results(tmp) if r["phase"] == "consolidate"]
        assert len(joint_rec) == 1 and joint_rec[0]["applied"] == len(in_set) and joint_rec[0]["valid"]

        with open(os.path.join(tmp, "outputs", "papercuts.log")) as f:
            log = f.read()
        assert "method cegis-comb" in log and "valid  set" in log and "in set" in log
        assert f"joint check of the consolidated design ({len(in_set)} cuts applied): PROVEN" in log

        with open(os.path.join(tmp, "outputs", "cegis", "summary.json")) as f:
            summary = json.load(f)
        assert summary["cuts"] == 18 and summary["in_set"] == len(in_set), summary
        assert summary["joint"]["valid"] and summary["btor"]["states"] > 0, summary
        assert sum(f["planned"] for f in summary["by_family"].values()) == 18, summary


def test_bitshrink_consolidates_as_its_mux():
    # A constant-index write to the top bit: narrowing `r` in place would leave
    # `r[3] <= ...` out of range (vtr__multiclock_reader_writer). Consolidation
    # must use the companion form the mux models, so `r` keeps its width.
    design = """
module top(input logic clk, input logic [2:0] a, input logic b, output logic [2:0] y);
    reg [3:0] r;
    always @(posedge clk) begin
        r[2:0] <= a;
        r[3] <= b;
    end
    assign y = r[2:0];
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            calls, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
            cut = [r for r in results(tmp) if r["phase"] == "cuts" and r["type"].startswith("bitshrink")]
            assert cut and cut[0]["in_set"], cut  # bit 3 is never read: the shrink is valid
            with open(os.path.join(tmp, calls[-1].impl_module_path)) as f:
                text = f.read()
            assert "r_papercuts" in text and "r[3] <=" in text, text
    finally:
        DESIGN = saved


def test_force_const_consolidates():
    # `t` is always 1, so forcing its reads to 1'b1 is valid and selected. Under
    # cegis-comb (intermediate-wire bit-shrink) every force-const cut used to come
    # out identical to the source: a no-op, held off, never in a set
    # (runs/cegis_comb_dataset/ISSUES.md 103).
    design = """
module top(input logic a, input logic b, input logic c, output logic y);
    wire t;
    assign t = a | ~a;
    assign y = t ? b : c;
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            calls, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
            cuts = [r for r in results(tmp) if r["phase"] == "cuts"]
            fc = {r["type"]: r for r in cuts if r["type"].startswith("force-const")}
            assert set(fc) == {"force-const(0)", "force-const(1)"}, cuts
            assert all(r["verdict"] != "noop" for r in fc.values()), fc
            assert fc["force-const(1)"]["in_set"] and not fc["force-const(0)"]["valid"], fc
            with open(os.path.join(tmp, calls[-1].impl_module_path)) as f:
                text = f.read()
            assert "t ?" not in text, text  # the read of t is gone from the joint check's design
    finally:
        DESIGN = saved


def test_memory_words_are_paired():
    # opencores cr_div: memory words were not listed as registers, so they started
    # apart in the two copies and the run stopped with MiterMismatch, cuts all off
    design = """
module top(input logic clk, input logic we, input logic [1:0] wa, input logic [1:0] ra,
           input logic [3:0] d, output logic [3:0] q);
    reg [3:0] mem [0:3];
    always @(posedge clk) if (we) mem[wa] <= d;
    assign q = mem[ra] | 4'h0;
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
            with open(os.path.join(tmp, "outputs", "cegis", "summary.json")) as f:
                summary = json.load(f)
            assert summary["registers"] >= 4 and summary["in_set"] >= 1, summary
    finally:
        DESIGN = saved


def test_escaped_port_names():
    # EPFL netlists name their ports `\\opcode[0] `; the miter must re-escape them
    design = r"""
module top(input \a[0] , input \a[1] , input s, output \y[0] );
    assign \y[0]  = s ? (\a[0]  & 1'b1) : \a[1] ;
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
            with open(os.path.join(tmp, "outputs", "cegis", "summary.json")) as f:
                assert json.load(f)["in_set"] >= 1
    finally:
        DESIGN = saved


def test_blocking_read_after_write():
    # opencores osdvu: a read after a blocking write in the same block, through a
    # companion, differed from the original with every cut off (MiterMismatch)
    design = """
module top(input logic clk, input logic rst, output logic z, output logic [3:0] c);
    reg [3:0] cnt;
    always @(posedge clk) begin
        if (rst) cnt = 4'd9;
        else cnt = cnt - 1;
        z <= (cnt == 4'd0) & 1'b1;
    end
    assign c = cnt;
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
    finally:
        DESIGN = saved


def test_multibit_conditions():
    # opencores osdvu: the if/ternary mux guard `sel1 | (!sel0 & (cond))` kept only
    # bit 0 of a multi-bit cond, so the muxed design differed with every cut off
    design = """
module top(input logic clk, input logic [3:0] n, input logic [5:0] c, output logic [1:0] y, output logic z);
    always @(posedge clk) begin
        if (n) z <= 1'b1;
        else z <= 1'b0;
    end
    assign y = c ? 2'd2 : 2'd0;
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
    finally:
        DESIGN = saved


def test_full_case_pragma():
    # opencores datetime: `// synopsys full_case` makes uncovered values
    # don't-cares; the elaborator must keep it, and the CEGIS side must still see
    # the muxed copy equal to the original with every cut off
    design = """
module top(input logic [1:0] s, input logic [3:0] a, output logic [3:0] y);
    always @(s or a)
        case (s) // synopsys full_case parallel_case
            2'd0: y = a;
            2'd1: y = a & 4'hF;
            2'd2: y = ~a;
        endcase
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
            with open(os.path.join(tmp, "outputs", "elab", "top_elaborated.sv")) as f:
                assert "synopsys full_case parallel_case" in f.read()
    finally:
        DESIGN = saved


def test_register_initialised_and_never_set_is_free_at_power_up():
    # hls nussinov: `initial ap_done_reg = 0`, never set. yosys folded the golden
    # flip-flop to 0 and R held the muxed one to it, so a cut that only matters
    # while the register is 1 came out valid; Jasper starts it free and found the
    # cex in 2 cycles. The flip-flop must survive, paired, in both copies.
    design = """
module top(input logic clk, input logic a, input logic b, output logic y);
    reg r = 1'b0;
    always @(posedge clk) r <= 1'b0;
    assign y = r ? a : b;
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
            cuts = [r for r in results(tmp) if r["phase"] == "cuts" and r["type"].startswith("ternary")]
            assert cuts and not any(c["in_set"] for c in cuts), cuts
    finally:
        DESIGN = saved


def test_register_constant_in_golden():
    # hls floyd_warshall: yosys folded the golden `done` flop to a constant 0 while
    # the muxed copy's muxes kept it a flip-flop; unpaired it started free in one
    # copy. The front end now keeps every flip-flop, so both are paired.
    design = """
module top(input logic clk, input logic a, input logic [3:0] d, output logic done, output logic [3:0] q);
    reg r = 1'b0;
    always @(posedge clk) begin
        r <= 1'b0 & a;
        q <= d;
    end
    assign done = r;
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
    finally:
        DESIGN = saved


def test_clock_read_as_data():
    # opencores cop reads wb_clk_i as data; a counterexample that depended on the
    # clock's value did not record it, and the conflict core could not reproduce it
    design = """
module top(input logic clk, input logic a, input logic [3:0] d, output logic y, output logic [3:0] q);
    always @(posedge clk) q <= d | 4'h0;
    assign y = (clk & a) | 1'b0;
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
    finally:
        DESIGN = saved


def test_memory_out_of_range_read():
    # opencores wb_lcd: a 67-word memory read with a 7-bit address; memory_map
    # gives each missing word undriven bits, many per output bit and so ambiguous
    # by key alone; they pair by structural name instead
    design = """
module top(input logic clk, input logic we, input logic [2:0] wa, input logic [2:0] ra,
           input logic [3:0] d, output logic [3:0] q);
    reg [3:0] ram [0:4];
    always @(posedge clk) begin
        if (we) ram[wa] <= d;
        q <= ram[ra] & 4'hF;
    end
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
    finally:
        DESIGN = saved


def test_clock_select_cuts_are_held_off():
    # opencores ptc: cuts on a clock-select mux looked free to the model (every
    # register steps once per model step) and the joint check refuted them
    design = """
module top(input logic clk, input logic eclk, input logic sel, input logic [3:0] d, output logic [3:0] q);
    wire cclk = sel ? eclk : clk;
    always @(posedge cclk) q <= d;
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
            cuts = [r for r in results(tmp) if r["phase"] == "cuts"]
            ternary = [r for r in cuts if r["type"].startswith("ternary")]
            assert ternary and all(r["verdict"] == "untested" and not r["in_set"] for r in ternary), ternary
            # forcing cclk to a constant reaches the clock too (these were no-ops
            # until force-const worked in intermediate-wire mode)
            force = [r for r in cuts if r["type"].startswith("force-const")]
            assert len(force) == 2 and all(r["verdict"] == "untested" and not r["in_set"] for r in force), force
            with open(os.path.join(tmp, "outputs", "cegis", "summary.json")) as f:
                assert json.load(f)["control_net"] == len(ternary) + len(force)
    finally:
        DESIGN = saved


def test_two_x_arms_of_one_case():
    # hls floyd_warshall, nussinov, trisolv: slang's front end folds a case's
    # `'x` arm into its `'x` default (one X per bit), the muxed copy -- the case
    # turned into an if-chain -- keeps both. Left free, the copies differed with
    # every cut off; tied to one value they agree (ISSUES.md 70).
    design = """
module top(input logic [1:0] s, input logic [3:0] a, input logic [3:0] b, output logic [3:0] y);
    always @(s or a or b)
        case (s)
            2'd0: y = a;
            2'd1: y = 'x;
            2'd2: y = b & 4'hF;
            default: y = 'x;
        endcase
endmodule
"""
    global DESIGN
    saved, DESIGN = DESIGN, design
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _, exit_ = pipeline(tmp, [])
            assert exit_ is None, exit_
    finally:
        DESIGN = saved


def test_in_situ():
    with tempfile.TemporaryDirectory() as tmp:
        calls, exit_ = pipeline(tmp, ["--in-situ"])
        assert exit_ is None, exit_
        assert [c.index for c in calls] == [0, -1]  # no elaboration gate in situ
        with open(os.path.join(tmp, "outputs", "consolidated_sources", "child.sv")) as f:
            child = f.read()
        with open(os.path.join(tmp, "outputs", "orig", "child.sv")) as f:
            assert child != f.read(), "no cut applied to the shared definition"


def test_failed_joint_check_is_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        calls, exit_ = pipeline(tmp, [], refute_joint=True)
        assert exit_ is not None and "not equivalent" in str(exit_.code), exit_
        with open(os.path.join(tmp, "outputs", "papercuts.log")) as f:
            assert "FAILED (cex)" in f.read()


def test_fv_method_unchanged():
    # the default method still checks every cut and each consolidated module,
    # with iterative bit-shrink on and no CEGIS columns in the log
    with tempfile.TemporaryDirectory() as tmp:
        calls, exit_ = pipeline(tmp, [])
        n_cegis = len(calls)
        calls, exit_ = pipeline(tmp, ["--method", "fv"])
        assert exit_ is None, exit_
        assert len(calls) > n_cegis + 18, len(calls)  # 18 cuts, more bit-shrink probes, 3 modules
        assert sum(1 for c in calls if c.index == -1) == 3  # one consolidated check per module
        with open(os.path.join(tmp, "outputs", "papercuts.log")) as f:
            log = f.read()
        assert "valid  set" not in log and "in set" not in log and "joint check" not in log
        assert "Y(-" in log  # the stub proves every probe, so bit-shrinks went past one bit


def test_cli_errors():
    with tempfile.TemporaryDirectory() as tmp:
        with contextlib.redirect_stderr(io.StringIO()):
            for extra, argv in (([], ["design.sv", "--method", "cegis-comb"]),  # no -e
                                (["--iterative-bitshrink"], None)):
                if argv is None:
                    _, exit_ = pipeline(tmp, extra)
                else:
                    saved = sys.argv
                    sys.argv = ["papercuts"] + argv
                    try:
                        asyncio.run(pm.main())
                        exit_ = None
                    except SystemExit as e:
                        exit_ = e
                    finally:
                        sys.argv = saved
                assert exit_ is not None and exit_.code == 2, (extra, argv, exit_)


def run():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_cegis_pipeline: OK ({len(tests)} tests)")


if __name__ == "__main__":
    run()
