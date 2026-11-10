"""A Run is tool-neutral; the Jasper backend renders it as the script arguments.

The pipeline states each check as two Designs (top, library, overrides), the
renames to pair and the proof limits. The Jasper script reads the candidate as
its spec and the golden design as its imp, from one shared library, and takes
(5) lib/<top>.sv, (6) lib, (7) the candidate's override, (8) whether that
override is the top's, (9) a rename map, (10) -time_limit, (11) -cex_limit, (12) the top, which
both contexts elaborate by name.
These are the arguments the pipeline passed before Run became neutral, for
each kind of check it makes; anything the script cannot express is refused.

ECBackend.check owns the verdict: a backend returns one of VERDICTS, only
"proven" is valid, and anything else is an error, not a silent failure.
"""

import asyncio
import os
import tempfile

from papercuts.backends.base import ECBackend
from papercuts.ec import jasper_args, run_jasper
from papercuts.utils import Design, Rename, Run

LIB = "/o/concrete_sources"
GOLDEN = Design("top", LIB)


def _raises(fn, exc=ValueError):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


def run():
    # self-check: no override, the top file on both sides
    assert jasper_args(Run(GOLDEN, GOLDEN, "/w"), None) == [
        f"{LIB}/top.sv", LIB, f"{LIB}/top.sv", "True", "-", "-", "-", "top"]
    # elaboration gate / joint check: a whole-design file keyed by the top, with a rename map
    gate = Run(GOLDEN, GOLDEN.override("top", "/o/blob.sv"), "/w", renames=(Rename("signal", "a.b", "a_b"),))
    assert jasper_args(gate, "/w/rename_map0.tcl") == [
        f"{LIB}/top.sv", LIB, "/o/blob.sv", "True", "/w/rename_map0.tcl", "-", "-", "top"]
    # per-cut check below the top: stops at the first cex, with a time budget
    cut = Run(GOLDEN, GOLDEN.override("child", "/o/child/child_pc3.sv"), "/w", index=3,
              stop_at_first_cex=True, time_limit_s=600)
    assert jasper_args(cut, None) == [
        f"{LIB}/top.sv", LIB, "/o/child/child_pc3.sv", "False", "-", "600s", "1", "top"]
    # a cut on the top itself
    assert jasper_args(Run(GOLDEN, GOLDEN.override("top", "/o/top/top_pc0.sv"), "/w",
                           stop_at_first_cex=True), None)[2:] == ["/o/top/top_pc0.sv", "True", "-", "-", "1", "top"]

    # what the script cannot express is refused, not approximated
    _raises(lambda: jasper_args(Run(GOLDEN.override("a", "/x.sv"), GOLDEN, "/w"), None))
    _raises(lambda: jasper_args(Run(GOLDEN, Design("top", "/elsewhere"), "/w"), None))
    _raises(lambda: jasper_args(Run(GOLDEN, Design("other", LIB), "/w"), None))
    _raises(lambda: jasper_args(Run(GOLDEN, GOLDEN.override("a", "/a.sv").override("b", "/b.sv"), "/w"), None))

    # the rename map is written into the run's work dir and passed as argument 9
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["PAPERCUTS_JG"] = "echo"  # no Jasper: just record the command line
        try:
            gate = Run(GOLDEN, GOLDEN.override("top", "/o/blob.sv"), tmp, renames=(Rename("signal", "a.b", "a_b"),))
            try:
                asyncio.run(run_jasper(gate))
            except FileNotFoundError:
                pass  # no jg.log: expected, the command only echoed
        finally:
            del os.environ["PAPERCUTS_JG"]
        with open(os.path.join(tmp, "rename_map0.tcl")) as f:
            assert f.read() == "check_sec -map -init -spec {a_b} -imp {a.b}\n"
        assert os.path.join(tmp, "rename_map0.tcl") in gate.output, gate.output

    # the base class records the verdict and rejects one outside the vocabulary
    class Fixed(ECBackend):
        name = "fixed"

        def __init__(self, verdict):
            super().__init__()
            self.verdict = verdict

        async def _check(self, run):
            return self.verdict

    for verdict, valid in (("proven", True), ("cex", False), ("inconclusive", False),
                           ("unmapped", False), ("error", False)):
        r = Run(GOLDEN, GOLDEN, "/w")
        assert asyncio.run(Fixed(verdict).check(r)) is valid and r.verdict == verdict and r.valid is valid
    _raises(lambda: asyncio.run(Fixed("determined").check(Run(GOLDEN, GOLDEN, "/w"))))
    _raises(lambda: asyncio.run(Fixed("proven").check_from_power_up(Run(GOLDEN, GOLDEN, "/w"))), NotImplementedError)
    return "Run renders to the same Jasper arguments for every check kind; verdicts validated by the base class"


if __name__ == "__main__":
    print(run())
