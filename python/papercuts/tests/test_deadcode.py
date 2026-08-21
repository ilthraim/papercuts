"""Dead-code pruning coverage: whole-signal + bit-level, and the
correctness-critical invariants that must NEVER prune live logic.

Exercises papercuts.deadcode.{analyze_dead, compute_dead_bits, prune_dead_source}:
  * whole dead signals (unread / undriven) and their drivers are removed
  * OUTPUT ports driven-but-not-read-internally are kept (observed at boundary)
  * signals reached only through a `.*` wildcard port connection are kept
  * partially-dead buses: only the dead-bit assignments go, decl + live bits stay
  * pruning iterates to a fixpoint (a removed cone exposes upstream dead code)
  * module port interface is never altered

These use no formal backend -- pruning is a pure source-to-source transform.
"""

from papercuts.deadcode import (
    analyze_dead, compute_dead_bits, prune_dead_source, _compile,
)


def _analyze(src, top):
    comp, _sm = _compile(src, top)
    assert not any(d.isError() for d in comp.getAllDiagnostics()), \
        "test design failed to elaborate"
    values, decls = analyze_dead(comp)
    bits = compute_dead_bits(comp, exclude_hps=values)
    return values, decls, bits


def _elaborates(src, top):
    comp, _sm = _compile(src, top)
    return not any(d.isError() for d in comp.getAllDiagnostics())


# --- 1. whole-signal dead removal ----------------------------------------
def _test_whole_signal():
    src = """
module top(input logic [3:0] a, output logic [3:0] y);
  logic [3:0] used;
  logic [3:0] wasted;    // driven, never read
  logic [3:0] floating;  // never driven, never read
  assign used = a + 1;
  assign wasted = a ^ 4'hF;
  assign y = used;
endmodule
"""
    values, _decls, _bits = _analyze(src, "top")
    assert "top.wasted" in values, "driven-but-unread signal not flagged"
    assert "top.floating" in values, "undriven/unread signal not flagged"
    assert "top.used" not in values and "top.y" not in values

    out, stats = prune_dead_source(src, "top")
    assert "wasted" not in out and "floating" not in out, "dead signals not pruned"
    assert "used" in out and "assign y = used" in out, "live logic wrongly removed"
    assert stats.signals >= 2
    assert _elaborates(out, "top")


# --- 2. OUTPUT ports are never pruned -------------------------------------
def _test_outputs_preserved():
    # z is an output: driven but never read *inside* the module. It is observed
    # at the boundary, so it must NOT be treated as dead.
    src = """
module top(input logic [3:0] a, output logic [3:0] y, output logic [3:0] z);
  assign y = a + 1;
  assign z = a ^ 4'hF;
endmodule
"""
    values, _decls, bits = _analyze(src, "top")
    assert "top.z" not in values, "output port wrongly flagged whole-dead"
    assert "top.z" not in bits, "output port wrongly flagged bit-dead"

    out, _stats = prune_dead_source(src, "top")
    assert "assign z" in out, "output driver wrongly pruned"
    assert _elaborates(out, "top")


# --- 3. wildcard .* port connections count as a use ----------------------
def _test_wildcard_ports():
    # `i` is read ONLY through `sub`'s input via `.*`; `o` is driven by `sub`
    # via `.*` and read by `py`. Both must survive. `deadnet` is genuinely dead.
    src = """
module sub(input logic [3:0] i, output logic [3:0] o);
  assign o = i + 1;
endmodule
module top(input logic [3:0] pa, output logic [3:0] py);
  logic [3:0] i, o, deadnet;
  assign i = pa;
  assign deadnet = pa ^ 4'hF;
  assign py = o;
  sub u(.*);
endmodule
"""
    values, _decls, _bits = _analyze(src, "top")
    assert "top.i" not in values, ".*-connected net wrongly flagged dead"
    assert "top.o" not in values, ".*-driven net wrongly flagged dead"
    assert "top.deadnet" in values, "genuinely dead net not flagged"

    out, _stats = prune_dead_source(src, "top")
    assert "deadnet" not in out, "dead net not pruned"
    # `i` must still be driven (its assignment survives, possibly reformatted).
    assert "assign i =" in out, ".*-used net's driver wrongly pruned"
    assert _elaborates(out, "top")


# --- 4. partially-dead bus: only dead-bit assignments removed ------------
def _test_bit_level():
    # b[2] is written but never read; b[0], b[1], b[3] feed y. Only the b[2]
    # assignment should go; the declaration and live-bit assignments stay.
    src = """
module top(input logic [3:0] a, output logic [2:0] y);
  logic [3:0] b;
  assign b[0] = a[0];
  assign b[1] = a[1];
  assign b[2] = a[2] & a[3];   // dead bit
  assign b[3] = a[3];
  assign y = {b[3], b[1], b[0]};
endmodule
"""
    values, _decls, bits = _analyze(src, "top")
    assert "top.b" not in values, "partially-dead bus wrongly flagged whole-dead"
    assert bits.get("top.b") == {2}, f"expected dead bit {{2}}, got {bits.get('top.b')}"

    out, stats = prune_dead_source(src, "top")
    assert "assign b[2]" not in out, "dead-bit assignment not pruned"
    for keep in ("assign b[0]", "assign b[1]", "assign b[3]"):
        assert keep in out, f"live-bit assignment wrongly pruned: {keep}"
    assert "b" in out and "logic" in out, "declaration wrongly removed"
    assert stats.bits == 1
    assert _elaborates(out, "top")


# --- 5. transitive fixpoint ----------------------------------------------
def _test_fixpoint():
    # dead1 is read only by dead2's driver; removing dead2 makes dead1 dead too.
    src = """
module top(input logic [3:0] a, output logic [3:0] y);
  logic [3:0] live, dead1, dead2;
  assign dead1 = a + 4'd1;        // read only by dead2
  assign dead2 = dead1 ^ 4'hA;    // dead
  assign live = a + 4'd2;
  assign y = live;
endmodule
"""
    out, stats = prune_dead_source(src, "top")
    assert "dead1" not in out and "dead2" not in out, "transitive dead not removed"
    assert "live" in out and "assign y = live" in out
    assert stats.iters >= 2, f"expected >=2 fixpoint iterations, got {stats.iters}"
    assert _elaborates(out, "top")


# --- 6. module port interface is never altered ---------------------------
def _test_ports_preserved():
    src = """
module top(input logic [7:0] a, input logic unusedin, output logic [7:0] y);
  logic [7:0] t;
  assign t = a + 1;
  assign y = t;
endmodule
"""
    out, _stats = prune_dead_source(src, "top")
    # Every port must still be declared, even `unusedin` (an unread input port).
    for port in ("a", "unusedin", "y"):
        assert port in out, f"port {port!r} disappeared from the interface"
    assert _elaborates(out, "top")


def run():
    _test_whole_signal()
    _test_outputs_preserved()
    _test_wildcard_ports()
    _test_bit_level()
    _test_fixpoint()
    _test_ports_preserved()
    print("test_deadcode: OK (whole-signal, outputs kept, .* uses, bit-level, "
          "fixpoint, ports preserved)")


if __name__ == "__main__":
    run()
