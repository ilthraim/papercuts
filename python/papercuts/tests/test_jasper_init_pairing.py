"""The Jasper script pairs uninitialized flops that auto-mapping skipped.

A cut that gives an uninitialized flop (or one bit of it) a constant input makes
Jasper treat it as a constant from power-up and leave it out of the reset_x
auto-mapping. The imp's copy then starts free, and the check fails on a power-up
value that CEGIS (and Jasper, for every other flop) takes as equal on both sides
(hls correlation: `delta_reg` after a force-to-1, bit 16 of the bit-shrunk sqrt
registers; runs/cegis_comb_dataset/ISSUES.md 102). The script pairs each imp
reset_x signal still unmapped with the spec signal of the same name and width,
after the elaborator's rename map and before the proof.
"""

from papercuts.ec import generate_jasper_tcl_script


def run():
    tcl = generate_jasper_tcl_script(clock="clk")
    rule = tcl.index("check_sec -interface -unmapped -imp -signal_type reset_x")
    assert tcl.index("source [lindex $argv 9]") < rule, tcl
    assert tcl.index("check_sec -auto_map_reset_x_values on") < rule, tcl
    assert rule < tcl.index("set res [check_sec -prove"), tcl
    assert "check_sec -map -init -spec $s -imp $i" in tcl, tcl
    assert "get_signal_info -width $s" in tcl and "get_signal_info -width $i" in tcl, tcl
    # The same rule runs in the power-up variant (no reset).
    assert "-unmapped -imp -signal_type reset_x" in generate_jasper_tcl_script(), tcl
    return "unmapped imp reset_x paired with same-name, same-width spec signals before the proof"
