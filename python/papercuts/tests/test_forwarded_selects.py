"""Select forwarding up the hierarchy: same order, linear time.

Every muxed module carries, as extra ports, the selects of every muxed module
beneath it. Deduplicating with a list membership test made this quadratic in the
selects carried; opencores pairing (17718 concretized modules) sat in it for
over 10 minutes after every module had been muxed (ISSUES 115).
"""

import time

from papercuts.__main__ import forwarded_selects


def _reference(modules, kids, own):
    """The old list-based algorithm, for the order contract."""
    fwd = {}

    def forwarded(name):
        if name in fwd:
            return fwd[name]
        out = []
        for child in kids.get(name, []):
            for entry in [(child, i) for i in range(own.get(child, 0))] + forwarded(child):
                if entry not in out:
                    out.append(entry)
        fwd[name] = out
        return out

    for name in modules:
        forwarded(name)
    return fwd


def run():
    # diamond: top -> a, b; a -> leaf; b -> leaf, a. leaf reached three ways.
    kids = {"top": ["a", "b"], "a": ["leaf"], "b": ["leaf", "a"], "leaf": []}
    own = {"top": 1, "a": 2, "b": 1, "leaf": 3}
    got = forwarded_selects(kids, kids, own)
    assert got == _reference(kids, kids, own), got
    assert got["top"] == [("a", 0), ("a", 1), ("leaf", 0), ("leaf", 1), ("leaf", 2), ("b", 0)], got["top"]
    assert got["leaf"] == []

    # wide and shared: 3000 leaves of 20 selects under 30 groups, all under top
    kids = {"top": [f"g{j}" for j in range(30)]}
    for j in range(30):
        kids[f"g{j}"] = [f"l{i}" for i in range(j * 100, j * 100 + 100)]
    own = {f"l{i}": 20 for i in range(3000)}
    t0 = time.time()
    got = forwarded_selects(kids, kids, own)
    took = time.time() - t0
    assert len(got["top"]) == 60000, len(got["top"])
    assert took < 5, f"{took:.1f}s for 60000 forwarded selects"

    try:
        forwarded_selects(["x"], {"x": ["y"], "y": ["x"]}, {})
    except SystemExit as e:
        assert "cycle" in str(e), e
    else:
        raise AssertionError("cycle not reported")
    print(f"  test_forwarded_selects: OK (order as before; 60000 selects in {took:.2f}s; cycle reported)")
