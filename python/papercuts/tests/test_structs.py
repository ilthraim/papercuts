"""Struct-typed parameters and `$unit`-scope typedefs in elaborated emission.

Two emitter bugs, both about structs, are exercised here:

1. Aggregate constants (structs, arrays) were serialized with pyslang's default
   `[a,b,c]` repr, which is not legal SystemVerilog. A struct/array literal must
   be an assignment pattern `'{a, b, c}`. This affected every constant-emission
   site: parameter defaults, folded constants, and inlined parameter references.

2. Typedefs declared at compilation-unit (`$unit`) file scope -- rather than
   inside a package -- were dropped entirely. `run()` emitted packages and
   modules but never walked `root.compilationUnits`, so a struct type used as a
   parameter type left the emitted module referencing an undefined type.

A third, supporting fix rides along: a member access on a constant struct
parameter (`STAGE.lo`) must fold to its scalar value. Otherwise the parameter
inlines as `'{...}` and `.lo` is applied to an assignment pattern -- both
illegal (no type context, and you cannot select a member of a pattern literal).

A fourth fix (SRC_UNPACKED) covers typedef *emission* for unpacked dimensions:
slang serializes an unpacked-array struct member (or an unpacked-array typedef)
as `logic[7:0]$[0:1]`, where `$` separates the element type from an unpacked
dimension that belongs after the identifier. The typedef emitter must relocate
it (`logic[7:0] tbl[0:1]`) instead of emitting the raw `$` form.

The golden assertion is that the re-emitted design compiles cleanly on its own.
"""

import contextlib
import io
import tempfile
from pathlib import Path

from papercuts.elaborator import elaborate
from pyslang.ast import Compilation
from pyslang.syntax import SyntaxTree

# `$unit`-scope struct typedefs (not in a package), a nested struct, struct-typed
# parameters with positional/named defaults, and both a member access used as an
# index (STAGE.lo) and a member access used as a mask (CFG.gain) -- each must fold
# to a scalar. Warning-free by construction so emission stays quiet.
SRC = """
typedef struct {
    logic [3:0] mode;
    logic [7:0] gain;
} cfg_t;

typedef struct {
    cfg_t        base;
    logic [31:0] lo;
    logic [31:0] hi;
} stage_t;

module widget #(
    cfg_t   CFG   = '{mode: 4'd5, gain: 8'hA5},
    stage_t STAGE = '{base: '{mode: 4'd3, gain: 8'd7}, lo: 32'd2, hi: 32'd9}
) (
    input  logic [7:0] din,
    input  logic [7:0] mask,
    output logic [7:0] dout
);
    logic [7:0] acc;
    always_comb begin
        acc = (mask[STAGE.lo]) ? (din & CFG.gain) : 8'd0;
    end
    assign dout = acc;
endmodule
"""

# Unpacked dimensions in typedef emission: a struct with single- and multi-dim
# unpacked-array members, a nested packed struct (keyword must be preserved), and
# a standalone unpacked-array typedef. slang renders these with the `$` element/
# dimension separator; the emitter must move the dimension after the identifier.
SRC_UNPACKED = """
typedef struct packed { logic [3:0] a; logic [7:0] b; } packed_t;
typedef logic [7:0] byte_arr_t [0:3];

typedef struct {
    logic [31:0] lo;
    logic [7:0]  tbl [0:1];
    logic [3:0]  mat [0:1][0:2];
    packed_t     nested;
} bundle_t;

module bundler #(
    bundle_t B = '{lo: 32'd2, tbl: '{8'd1, 8'd2},
                   mat: '{'{0, 0, 0}, '{0, 0, 0}}, nested: '{a: 4'd1, b: 8'd2}}
) (
    input  logic [7:0] din,
    output logic [7:0] dout
);
    byte_arr_t lut;
    always_comb begin
        dout = din & B.nested.b;
    end
endmodule
"""


def _elaborate(src):
    # elaborate() reads from files (SyntaxTree.fromFile), so stage the source.
    # Suppress the elaborator's input-diagnostic prints so the test stays quiet;
    # genuine input errors still raise ElaborationError.
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "design.sv"
        f.write_text(src)
        with contextlib.redirect_stderr(io.StringIO()):
            return elaborate([str(f)])


def _compile_errors(text):
    comp = Compilation()
    comp.addSyntaxTree(SyntaxTree.fromText(text))
    return [d for d in comp.getAllDiagnostics() if d.isError()]


def run():
    out = _elaborate(SRC)

    # Bug 2: both $unit-scope typedefs must be re-emitted (before the module that
    # uses them), or the parameter types dangle.
    assert "typedef struct" in out, "no typedef emitted"
    assert "cfg_t;" in out and "stage_t;" in out, \
        f"$unit typedefs dropped from emitted output:\n{out}"

    # Bug 1: struct defaults must be assignment patterns, never the `[...]` repr.
    assert "'{" in out, "no assignment pattern emitted for struct defaults"
    assert "= [" not in out, f"struct default emitted as illegal `[...]` literal:\n{out}"
    # Nested struct -> nested pattern (recursive rendering of the aggregate).
    assert "'{'{" in out, f"nested struct default not rendered recursively:\n{out}"

    # Supporting fold: a constant member access must resolve to a scalar, not to a
    # pattern with a `.member` selected off it. Patterns are legal only in the
    # parameter defaults; none should leak into the module body.
    body = out.split("always_comb", 1)[1].split("endmodule", 1)[0]
    assert "'{" not in body, f"assignment pattern leaked into module body:\n{body}"
    assert ".lo" not in out and ".gain" not in out, \
        f"member access on a struct param survived instead of folding:\n{out}"
    assert "mask[32'd2]" in out, \
        f"STAGE.lo did not fold to its scalar index value:\n{out}"

    # Golden check: the re-emitted design compiles standalone with no errors.
    errs = _compile_errors(out)
    assert not errs, f"elaborated output has {len(errs)} compile error(s)"

    # --- Bug 4: unpacked dimensions in typedef emission ----------------------
    up = _elaborate(SRC_UNPACKED)

    # The `$` element/dimension separator must never reach the output.
    assert "$" not in up, f"raw `$` unpacked-dim separator leaked into output:\n{up}"
    # Unpacked dims relocated after the identifier, for struct members...
    assert "tbl[0:1]" in up, f"unpacked struct member dim not relocated:\n{up}"
    assert "mat[0:1][0:2]" in up, f"multi-dim unpacked member not relocated:\n{up}"
    # ...and for a standalone unpacked-array typedef.
    assert "byte_arr_t[0:3]" in up, f"unpacked-array typedef not relocated:\n{up}"
    # The `packed` keyword on a packed struct must survive the field re-render.
    assert "struct packed {" in up, f"packed struct keyword lost:\n{up}"
    # Nested named struct member kept as a type reference, not re-inlined.
    assert "packed_t nested;" in up, f"nested named struct member mangled:\n{up}"

    up_errs = _compile_errors(up)
    assert not up_errs, f"unpacked-array output has {len(up_errs)} compile error(s)"

    print("test_structs: OK (struct params + $unit typedefs + const member fold "
          "+ unpacked-dim typedefs)")


if __name__ == "__main__":
    run()
