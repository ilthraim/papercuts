"""Module names generated from deep instance paths stay short enough for files.

Each nested instance becomes a module named by its mangled hierarchical path,
and that name becomes <name>.sv (plus _muxed / _consolidated). koios
clstm_like's paths passed 255 bytes: "File name too long". Long names keep a
prefix and a hash of the full path.
"""

import os
import tempfile

from papercuts.elaborator import MAX_MODULE_NAME, elaborate_design

SEG = "a_very_long_instance_name_segment_number"


def design(depth):
    mods = [f"module leaf(input logic a, output logic y); assign y = ~a; endmodule"]
    child = "leaf"
    for i in range(depth):
        mods.append(f"module m{i}(input logic a, output logic y); {child} {SEG}{i} (.a(a), .y(y)); endmodule")
        child = f"m{i}"
    mods.append(f"module top(input logic a, output logic y); {child} {SEG}_top (.a(a), .y(y)); endmodule")
    return "\n".join(mods)


def run():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.sv")
        with open(path, "w") as f:
            f.write(design(8))
        r = elaborate_design([path])
    names = [l.split()[1].rstrip("(") for l in r.source.splitlines() if l.startswith("module ")]
    assert max(len(n) for n in names) <= MAX_MODULE_NAME, [len(n) for n in names]
    assert len(set(names)) == len(names), names
    return f"{len(names)} modules, longest name {max(len(n) for n in names)} chars"
