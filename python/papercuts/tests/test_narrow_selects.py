"""Narrow-mode bit-shrink rewrites constant selects into the dropped bits.

Narrowing only edits the declaration, so a select written against the original
range (`t[3:0]`, `m[1]`) would be out of range once `t` is `[2:0]` -- the formal
tool rejects the whole cut. The dropped bits instead behave as they do for a
bare narrowed read: they read as 0, and writes to them are discarded.
"""

from papercuts import Papercutter
from pyslang.syntax import SyntaxTree
from pyslang import ast

SRC = """
module narrow_selects (
    input  logic [3:0] a,
    input  logic       b,
    output logic [5:0] y,
    output logic [1:0] z,
    output logic [1:0] w
    );
    logic [3:0] t;
    logic [1:0] m;
    logic [2:0][1:0] mx;
    logic [0:3] asc;
    logic [1:0] q;
    assign t[3:0] = a;
    assign m[1] = b;
    assign m[0] = b;
    assign asc[0:3] = a;
    always_comb begin
        mx[2] = 2'b11;
        mx[1][1:0] = a[1:0];
        mx[0] = a[3:2];
    end
    always_ff @(posedge b) q[1:0] <= a[1:0];
    sub u (.o(q[1:0]));
    assign y = {t[3:0], m[1], m[0]};
    assign z = mx[2][1:0] ^ mx[1];
    assign w = asc[2:3];
endmodule

module sub (output logic [1:0] o);
    assign o = 2'b01;
endmodule
"""


def _cut_for(pc, info, name):
    """Text of the bitshrink cut whose narrowed declaration is `name`."""
    for i, (kind, _) in enumerate(info):
        if kind != "bitshrink":
            continue
        text = str(pc.cut_index([i]).root)
        for ln in text.splitlines():
            ln = ln.strip()
            if ln.startswith("logic") and ln.endswith(f" {name};") and ln != _orig_decl[name]:
                return text
    raise AssertionError(f"no bitshrink cut narrows {name}")


_orig_decl = {
    "t": "logic [3:0] t;",
    "m": "logic [1:0] m;",
    "mx": "logic [2:0][1:0] mx;",
    "asc": "logic [0:3] asc;",
    "q": "logic [1:0] q;",
}


def _errors(text):
    tree = SyntaxTree.fromText(text)
    comp = ast.Compilation()
    comp.addSyntaxTree(tree)
    diags = [d for d in comp.getAllDiagnostics() if d.isError()]
    # Out-of-range constant selects are warnings in slang but errors in the
    # formal tools, so treat them as failures too.
    diags += [d for d in comp.getAllDiagnostics() if "out of" in str(d.code).lower()
              or "bounds" in str(d.code).lower()]
    return diags


def _flat(text):
    return " ".join(text.split())


def run():
    tree = SyntaxTree.fromText(SRC)
    pc = Papercutter(tree)
    info = pc.cut_info()

    t = _flat(_cut_for(pc, info, "t"))
    assert "assign t[2:0] = a;" in t, t                       # write clamped
    assert "y = {{1'b0, t[2:0]}, m[1], m[0]}" in t, t        # read zero-padded

    m = _flat(_cut_for(pc, info, "m"))
    assert "assign m[1]" not in m, m                          # dead write removed
    assert "y = {t[3:0], 1'b0, m[0]}" in m, m                 # dead read is 0

    mx = _flat(_cut_for(pc, info, "mx"))
    assert "mx[2] = 2'b11" not in mx, mx                      # dead procedural write
    assert "z = 2'b0 ^ mx[1];" in mx, mx                      # dead multi-dim read

    asc = _flat(_cut_for(pc, info, "asc"))
    assert "assign asc[0:2] = a;" in asc, asc                 # ascending clamp
    assert "w = {asc[2:2], 1'b0};" in asc, asc                # ascending pads low end

    q = _flat(_cut_for(pc, info, "q"))
    assert "q[0:0] <= a[1:0]" in q, q                         # nonblocking clamp
    assert ".o(q[0:0])" in q, q                               # port connection clamp

    for name in _orig_decl:
        text = _cut_for(pc, info, name)
        errs = _errors(text)
        assert not errs, f"{name}: {[str(e.code) for e in errs]}\n{text}"

    print("test_narrow_selects: OK (constant selects into dropped bits rewritten)")


if __name__ == "__main__":
    run()
