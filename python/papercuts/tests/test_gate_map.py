"""The elaboration gate is told what the elaborator renamed.

Flattening a generate scope renames what it holds: `g[0].u` -> `g_0_u`, a flop
`h.r` -> `h_r`. Jasper pairs uninitialized flops, blackboxes and X sources by
name, so the renamed ones were free on each side and the gate failed on
designs that were equivalent (opencores bubblesortmodule, xge_mac, sbd_sqrt_fp,
the koios designs). Jasper proved all three opencores ones once given:
`-hierarchy` rules for instances, deepest first, and `-init` maps for signals
(ISSUES 96). A signal the dead-code prune removed is not mapped. The rules go
as one list command per depth: one command each stalled opencores pairing's
gate (10966 rules, ~6/min and slowing; as lists, 87 s, same mappings; ISSUES 109).
"""

import os
import re
import tempfile

from pyslang.syntax import SyntaxTree

from papercuts.ec import generate_jasper_tcl_script, jasper_map_script
from papercuts.elaborator import build_compilation_from, elaborate_design, gate_renames
from papercuts.utils import Rename


DESIGN = """
module leaf(input logic clk, input logic d, output logic q);
    always_ff @(posedge clk) q <= d;
endmodule
module mid(input logic clk, input logic [1:0] d, output logic [1:0] q);
    for (genvar i = 0; i < 2; i++) begin : g
        leaf u(.clk(clk), .d(d[i]), .q(q[i]));
    end
endmodule
module top(input logic clk, input logic [1:0] d, output logic [1:0] q, output logic r_out);
    if (1) begin : h
        logic r;
        always_ff @(posedge clk) r <= d[0];
    end
    for (genvar j = 0; j < 1; j++) begin : k
        mid m(.clk(clk), .d(d), .q(q));
    end
    assign r_out = h.r;
endmodule
"""


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.sv")
        with open(path, "w") as f:
            f.write(DESIGN)
        elab = elaborate_design([path], top="top")
    ren = set(elab.renames)
    assert ("signal", "h.r", "h_r") in ren, ren
    assert ("instance", "k[0].m", "k_0_m") in ren, ren
    assert ("instance", "k[0].m.g[0].u", "k_0_m.g_0_u") in ren, ren
    assert not any(r[1].startswith("k[0].m.g[0].u.") for r in ren), ren  # leaf keeps its names

    kept = gate_renames(elab.renames, build_compilation_from([SyntaxTree.fromText(elab.source)]))
    assert Rename("signal", "h.r", "h_r") in kept, kept  # golden = original, candidate = elaborated

    # The Jasper script reads the candidate as its spec: rules map candidate -> golden.
    script = jasper_map_script(kept)
    lines = script.splitlines()
    assert "check_sec -map -init -spec {h_r} -imp {h.r}" in lines, script
    rules = [l for l in lines if "-hierarchy" in l]
    assert rules == ["check_sec -map -hierarchy -spec {{k_0_m.g_0_u} {k_0_m.g_1_u}} "
                     "-imp {{k[0].m.g[0].u} {k[0].m.g[1].u}}",
                     "check_sec -map -hierarchy -spec {{k_0_m}} -imp {{k[0].m}}"], rules  # one per depth, deepest first

    # a renamed signal the prune removed is not mapped
    pruned = re.sub(r"\n\s*(reg|logic)\s+h_r;", "\n", elab.source)
    pruned = re.sub(r"always_ff @\(posedge clk\) begin\s*h_r <= d\[0\];\s*end", "", pruned)
    pruned = pruned.replace("assign r_out = h_r;", "assign r_out = 1'b0;")
    assert "h_r" not in pruned, pruned
    kept2 = gate_renames(elab.renames, build_compilation_from([SyntaxTree.fromText(pruned)]))
    script2 = jasper_map_script(kept2)
    assert "h_r" not in script2 and "k_0_m" in script2, script2

    # a golden name Jasper may spell differently is left unmapped (fails loudly)
    assert jasper_map_script((Rename("signal", "\\esc[ .x", "esc_x"),)) == ""

    tcl = generate_jasper_tcl_script(clock=["clk"])
    assert 'if {[lindex $argv 9] ne "-"}' in tcl and "source [lindex $argv 9]" in tcl, tcl
    assert tcl.index("source [lindex $argv 9]") > tcl.index("check_sec -setup"), tcl
    return f"{len(lines)} map commands: instance rules deepest first, -init for signals, pruned left out"
