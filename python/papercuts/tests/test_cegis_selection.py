"""select_cuts end to end: manifest -> elaborate -> miter -> BTOR2 -> loop -> cuts.

The manifests are written here in papercuts' shape around hand-muxed fixtures
(cegis_fixtures/*_wrapper.sv), so these tests need yosys with the slang plugin
and pyosys, but no Jasper.
"""

import json
import os
import re
import tempfile

from papercuts.cegis.selection import select_cuts
from papercuts.manifest import Manifest, ManifestError

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cegis_fixtures")


def split_wrapper(wrapper, orig, muxed, outdir):
    """Write <orig>.sv and <muxed>.sv from a *_wrapper.sv holding both modules."""
    with open(os.path.join(FIX, wrapper)) as f:
        src = f.read()
    by = {n: t for t, n in re.findall(r"(module\s+(\w+).*?endmodule\n)", src, re.S)}
    po, pm = os.path.join(outdir, f"{orig}.sv"), os.path.join(outdir, f"{muxed}.sv")
    for path, name in ((po, orig), (pm, muxed)):
        with open(path, "w") as f:
            f.write(by[name])
    return po, pm


def n_selects(path):
    with open(path) as f:
        head = f.read().split(");", 1)[0]
    return len({int(n) for n in re.findall(r"pc_sel(\d+)", head)})


def manifest(tmp, top, golden, muxed, cuts, exclusive=()):
    """cuts: [(index, type, line, inserted)]"""
    data = {"version": 1, "generator": "papercuts-test", "mode": "in-situ", "top": top, "mux_suffix": "_muxed",
            "clock": None, "reset": None,
            "files": {"golden": [os.path.basename(golden)], "muxed": [os.path.basename(muxed)]},
            "modules": {top: {"muxed_name": f"{top}_muxed", "golden_file": os.path.basename(golden),
                              "muxed_file": os.path.basename(muxed),
                              "cuts": [{"index": i, "type": t, "line": ln, "port": f"pc_sel{i}", "inserted": ins}
                                       for i, t, ln, ins in cuts],
                              "exclusive": [list(p) for p in exclusive], "forwarded": []}}}
    path = os.path.join(tmp, "mux_manifest.json")
    with open(path, "w") as f:
        json.dump(data, f)
    return Manifest.load(path)


def test_combinational_poly():
    with tempfile.TemporaryDirectory() as tmp:
        po, pm = split_wrapper("poly_wrapper.sv", "poly", "poly_muxed", tmp)
        cuts = [(i, "ternary(keep-false)", 1, True) for i in range(n_selects(pm))]
        r = select_cuts(manifest(tmp, "poly", po, pm, cuts), os.path.join(tmp, "work"))
        assert r.ids == ["poly#0", "poly#2", "poly#3", "poly#4"], r.ids
        assert r.base_ok, r.base_evidence


def test_counter_bitshrink_is_off():
    # a sequential checker proves ctr#0 under the reset; the combinational spec rejects it
    with tempfile.TemporaryDirectory() as tmp:
        po, pm = split_wrapper("ctr_wrapper.sv", "ctr", "ctr_muxed", tmp)
        cuts = [(0, "bitshrink", 3, True), (1, "force-const(1)", 4, True)]
        r = select_cuts(manifest(tmp, "ctr", po, pm, cuts), os.path.join(tmp, "work"))
        assert r.ids == [], r.ids
        assert len(r.registers) == 1 and r.base_ok, (r.registers, r.base_evidence)


def test_accumulator():
    with tempfile.TemporaryDirectory() as tmp:
        po, pm = split_wrapper("seq_wrapper.sv", "acc", "acc_muxed", tmp)
        cuts = [(i, "x", 1, True) for i in range(n_selects(pm))]
        r = select_cuts(manifest(tmp, "acc", po, pm, cuts), os.path.join(tmp, "work"))
        assert r.ids == ["acc#3"], r.ids


def test_inert_select_is_untested_not_on():
    # poly#0 is a valid cut, but reserved-not-inserted: pinned off, reported untested
    with tempfile.TemporaryDirectory() as tmp:
        po, pm = split_wrapper("poly_wrapper.sv", "poly", "poly_muxed", tmp)
        cuts = [(i, "ternary(keep-false)", 1, i != 0) for i in range(n_selects(pm))]
        r = select_cuts(manifest(tmp, "poly", po, pm, cuts), os.path.join(tmp, "work"))
        assert r.ids == ["poly#2", "poly#3", "poly#4"], r.ids
        assert [c.id for c in r.untested] == ["poly#0"]


def test_pin_off_and_individual():
    # poly#0 is valid but pinned off (as the pipeline does for a no-op cut): it is
    # neither searched nor reported on/off, and individual validity covers the rest
    with tempfile.TemporaryDirectory() as tmp:
        po, pm = split_wrapper("poly_wrapper.sv", "poly", "poly_muxed", tmp)
        cuts = [(i, "ternary(keep-false)", 1, True) for i in range(n_selects(pm))]
        r = select_cuts(manifest(tmp, "poly", po, pm, cuts), os.path.join(tmp, "work"), pin_off=["poly#0"])
        assert r.ids == ["poly#2", "poly#3", "poly#4"], r.ids
        assert [c.id for c in r.pinned_off] == ["poly#0"]
        assert set(r.individual) == {f"poly#{i}" for i in range(1, n_selects(pm))}, r.individual
        assert all(r.individual[i] for i in r.ids), r.individual


def test_manifest_must_match_ports():
    with tempfile.TemporaryDirectory() as tmp:
        po, pm = split_wrapper("poly_wrapper.sv", "poly", "poly_muxed", tmp)
        cuts = [(i, "x", 1, True) for i in range(n_selects(pm) - 1)]  # one select undescribed
        try:
            select_cuts(manifest(tmp, "poly", po, pm, cuts), os.path.join(tmp, "work"))
        except ManifestError:
            return
    raise AssertionError("expected ManifestError")


def run():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"test_cegis_selection: OK ({len(tests)} tests)")


if __name__ == "__main__":
    run()
