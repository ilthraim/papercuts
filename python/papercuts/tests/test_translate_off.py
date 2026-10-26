"""translate_off regions are skipped exactly as Jasper skips them.

Jasper skips `// <word> translate_off` regions for synopsys, exemplar,
synthesis, pragma, ambit and cadence (measured, papercuts.pragmas), and no
other word. papercuts' slang parse skipped none, so simulation-only code became
real in the elaborated copy (opencores aemb: an initial block of $random into
the register file; mem_ctrl: the only `default:` of a full_case state case).
yosys-slang, which builds the CEGIS netlists, skipped a different set (xilinx
on, exemplar off). Each output below is inverted inside one region: skipped
means `y = a`.
"""

import os
import re
import subprocess
import tempfile

from papercuts.cegis.netlist import READ_SLANG
from papercuts.elaborator import elaborate
from papercuts.pragmas import TRANSLATE_OFF_WORDS

KEPT = ("xilinx", "altera", "synopsis", "Synopsys")  # not skipped by Jasper


def design() -> str:
    words = TRANSLATE_OFF_WORDS + KEPT
    ports = ", ".join(f"output reg o_{w.lower()}{'_cap' if w[0].isupper() else ''}" for w in words)
    body = []
    for w in words:
        o = f"o_{w.lower()}{'_cap' if w[0].isupper() else ''}"
        body += [f"  always @* begin", f"    {o} = a;", f"    // {w} translate_off",
                 f"    {o} = ~a;", f"    // {w} translate_on", "  end"]
    body += ["  /* synopsys translate_off */", "  initial $display(\"sim only\");",
             "  /* synopsys translate_on */"]
    return f"module t(input a, {ports});\n" + "\n".join(body) + "\nendmodule\n"


def inverted(text: str) -> set:
    return set(re.findall(r"(o_\w+) = ~\s*\(?a\)?;", text))


def run():
    expect = {f"o_{w.lower()}{'_cap' if w[0].isupper() else ''}" for w in KEPT}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.v")
        with open(path, "w") as f:
            f.write(design())
        out = elaborate([path])
        assert inverted(out) == expect, (inverted(out), out)
        assert "sim only" not in out, out
        script = READ_SLANG.format(files=path, top="t", slang_args="") + "; proc; opt; write_verilog -noattr " \
            + os.path.join(tmp, "y.v")
        r = subprocess.run(["yosys", "-q", "-p", script], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]
        net = open(os.path.join(tmp, "y.v")).read()
    # yosys may alias equal outputs (`assign o_altera = o_xilinx;`): follow them
    alias = dict(re.findall(r"assign (o_\w+) = (o_\w+);", net))
    inv = inverted(net)
    inv |= {o for o, src in alias.items() if src in inv}
    assert inv == expect, (inv, net)
    return f"skipped for {', '.join(TRANSLATE_OFF_WORDS)}; kept for {', '.join(KEPT)}; elaborator and read_slang agree"
