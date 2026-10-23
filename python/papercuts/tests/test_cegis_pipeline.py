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
