"""The mux manifest: cut identity, exclusivity and inertness, from the muxer itself.

Three facts a consumer of the muxed design cannot safely re-derive, and which
this checks come out of the tool instead:

  * `pc_sel<N>` really is cut N. The bit-shrink band is the trap: a parameterized
    packed range is enumerated as a cut but cannot be muxed, and when the two
    sides disagreed on that band's width every later family's selects slid off
    their cut index. Insertion now sizes the band from the same symbolic ranges
    the cutter gets, so the index holds and the unmuxable select is simply inert.
  * which selects are inert (`inserted: false`) -- reserved so indices stay
    aligned, driving nothing. Treating one as a proven cut would claim a rewrite
    nothing ever exercised.
  * which cuts are mutually exclusive, from the cutter's own node lists rather
    than from two log rows sharing a line number.
"""

import re

from papercuts import Papercutter
from papercuts.manifest import build_manifest, check_manifest
from papercuts.pypercuts import insert_muxes_report
from papercuts.utils import print_tree
from pyslang.syntax import SyntaxTree

# `par` has a parameterized range (enumerated, not muxable); `lit` is literal.
# The shift binop has keep-left only, so it must not be paired with anything.
SRC = """
module m #(parameter W = 8) (input logic [W-1:0] d, input logic c, output logic [W-1:0] q);
    logic [W-1:0] par;
    logic [7:0] lit;
    assign par = d;
    assign lit = d[7:0];
    assign q = c ? (par & lit) : (lit >> 1);
endmodule
"""

SYMBOLIC = {"par": [(7, 0)]}  # what [W-1:0] evaluates to, as the pipeline supplies it


class _Mod:
    """Stand-in for the pipeline's ModuleCuts (build_manifest only reads these)."""

    def __init__(self, name, pc, cut_infos):
        self.name, self.pc, self.cut_infos = name, pc, cut_infos


def run():
    pc = Papercutter(SyntaxTree.fromText(SRC), symbolic_ranges=SYMBOLIC)
    cut_infos = list(pc.cut_info())
    _, inserted = insert_muxes_report(SyntaxTree.fromText(SRC), True, True, True, caseMux=True,
                                      binopMux=True, constForceMux=True, symbolicRanges=SYMBOLIC)
    inserted = set(inserted)

    mod = _Mod("m", pc, cut_infos)
    manifest = build_manifest(mode="in-situ", top="m", suffix="_muxed", modules=[mod],
                              muxed_names={"m": "m_muxed"}, muxed_files={"m": "/out/muxed/m_muxed.sv"},
                              golden_files={"m": "/out/orig/m.sv"}, inserted={"m": inserted},
                              forwarded={}, base_dir="/out")
    assert check_manifest(manifest) == [], check_manifest(manifest)
    assert (manifest["clock"], manifest["reset"]) == (None, None), "no --clock/--reset: both null"
    clocked = build_manifest(mode="in-situ", top="m", suffix="_muxed", modules=[mod],
                             muxed_names={"m": "m_muxed"}, muxed_files={"m": "/out/muxed/m_muxed.sv"},
                             golden_files={"m": "/out/orig/m.sv"}, inserted={"m": inserted},
                             forwarded={}, base_dir="/out", clock="clk", reset="!rst_n")
    assert (clocked["clock"], clocked["reset"]) == ("clk", "!rst_n"), "--clock/--reset recorded verbatim"
    cuts = manifest["modules"]["m"]["cuts"]
    types = [c["type"] for c in cuts]

    # The bit-shrink band: one cut per shrinkable dimension, `par` before `lit`.
    bits = [i for i, t in enumerate(types) if t == "bitshrink"]
    assert len(bits) == 2, types
    by_line = {cuts[i]["line"]: cuts[i] for i in bits}
    par_cut = by_line[3]  # `logic [W-1:0] par;`
    lit_cut = by_line[4]  # `logic [7:0] lit;`
    assert par_cut["inserted"] is False, "a parameterized range cannot be muxed"
    assert lit_cut["inserted"] is True, "a literal range must be muxed"

    # Every other cut got a control, and each select's number IS its cut index.
    for c in cuts:
        assert c["port"] == f"pc_sel{c['index']}"
        if c is not par_cut:
            assert c["inserted"] is True, c

    # Alignment, the thing that actually broke: the binop selects must be the ones
    # the muxed source uses for that operator, not shifted by the unmuxable bit cut.
    muxed, _ = insert_muxes_report(SyntaxTree.fromText(SRC), True, True, True, caseMux=True,
                                   binopMux=True, constForceMux=True, symbolicRanges=SYMBOLIC)
    text = print_tree(muxed)
    and_left = next(c for c in cuts if c["type"] == "binop(and,keep-left)")
    and_right = next(c for c in cuts if c["type"] == "binop(and,keep-right)")
    # `par` is the unmuxable one, so reads of it are NOT redirected to a companion;
    # `lit` is muxed and reads go to lit_papercuts.
    assert re.search(rf"\b{and_left['port']} \? \(\s*par\b", text), text
    assert re.search(rf"\b{and_right['port']} \? \(\s*lit_papercuts\b", text), text
    # An inert select is still a PORT (that is what keeps the indices aligned); it
    # just drives nothing, so it appears in the header and nowhere in the body.
    header, body = text.split(");", 1)
    assert f"pc_sel{par_cut['index']}" in header, "a reserved select must still be a port"
    assert f"pc_sel{par_cut['index']}" not in body, "an inert select must drive nothing"

    # Exclusivity: pairs come from the cutter's node lists. The shift keeps left
    # only, so it has no partner.
    pairs = {tuple(p) for p in manifest["modules"]["m"]["exclusive"]}
    assert (and_left["index"], and_right["index"]) in pairs, pairs
    ternary = [c["index"] for c in cuts if c["type"].startswith("ternary")]
    assert (ternary[0], ternary[1]) in pairs
    shift = next(c for c in cuts if c["type"] == "binop(shr,keep-left)")
    assert all(shift["index"] not in p for p in pairs), f"a shift cut has no complement: {pairs}"
    assert all(par_cut["index"] not in p for p in pairs), "bit-shrink cuts pair with nothing"

    # check_manifest catches a band that slipped, which is how the bug showed up.
    broken = {"version": 1, "modules": {"m": {"cuts": cuts, "exclusive": [[0, len(cuts)]], "forwarded": []}}}
    assert check_manifest(broken), "an out-of-range pair must be reported"

    # Level 1: the manifest is also checked against the source actually written.
    # One select port per cut, every inserted cut referenced in the body, every
    # reserved one referenced nowhere but the port list.
    sound = {"version": 1, "modules": {"m": {"cuts": cuts, "exclusive": [], "forwarded": []}}}
    assert check_manifest(sound, muxed_text={"m": text}) == [], \
        check_manifest(sound, muxed_text={"m": text})

    # a cut that claims a control it does not have (what a slipped band looks like)
    lying = {"version": 1, "modules": {"m": {"cuts": [dict(c) for c in cuts], "exclusive": [], "forwarded": []}}}
    lying["modules"]["m"]["cuts"][par_cut["index"]]["inserted"] = True
    assert any("drives nothing" in p for p in check_manifest(lying, muxed_text={"m": text})), \
        check_manifest(lying, muxed_text={"m": text})

    # a cut count that does not match the port list
    short = {"version": 1, "modules": {"m": {"cuts": [dict(c) for c in cuts][:-1], "exclusive": [],
                                             "forwarded": []}}}
    assert any("select ports" in p for p in check_manifest(short, muxed_text={"m": text})), \
        check_manifest(short, muxed_text={"m": text})

    print(f"test_manifest: OK ({len(cuts)} cuts, {len(pairs)} exclusive pairs, 1 inert)")


if __name__ == "__main__":
    run()
