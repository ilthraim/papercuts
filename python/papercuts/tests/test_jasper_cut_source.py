"""A non-top check compiles the module under test from its own file.

The spec context used to read the cut (or consolidated module) as a `-v`
library file beside `-y concrete_sources`. A module instantiated by one that
`-y` pulled in resolves through `-y`, so Jasper compiled the golden copy and
proved golden against golden: every cut on a module 2+ levels below the top
was "proven" (fv_dataset, 2026-11-01: 49 of 81 designs; gost28147-89 cfb's
`tmp1` shrunk to [0:0] passed). Passing the file as a plain source makes its
definition win; Jasper then rejected that cut.
"""

from papercuts.ec import generate_jasper_tcl_script


def run():
    tcl = generate_jasper_tcl_script(clock="clk")
    spec = tcl[tcl.index("check_sec -compile_context spec"):tcl.index("check_sec -compile_context imp")]
    analyzes = [l.strip() for l in spec.splitlines() if l.strip().startswith("analyze")]
    assert analyzes == [
        "analyze -sv17 -y [lindex $argv 6] [lindex $argv 7] +libext+.sv",
        "analyze -sv17 [lindex $argv 7] -y [lindex $argv 6] [lindex $argv 5] +libext+.sv",
    ], analyzes
    assert not any(" -v " in a for a in analyzes), analyzes
    return "module under test is a plain source in both spec branches"
