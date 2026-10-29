"""xpair._fold: one memoized walk gives what a walk per X source gave.

pair_x used to walk, for every X source, everything the X reaches: the copies
it reaches at all, and the first keyed bits on every path (a keyed bit ends a
path for the keys, not for the copies). That cost X sources x netlist and ran
for hours on the koios miters (runs ISSUES.md 119). `_fold` computes both per
node once, over strongly connected components, since registers make cycles.
Checked here against that per-source walk on random graphs with cycles.
"""

import random

from papercuts.cegis.xpair import _EMPTY, _fold, _union


def _reference(starts, succ, members, keyed):
    """The old xpair.reach, per start node: (first keyed on every path, copies)."""
    out = {}
    for s in starts:
        seen, found, sides = set(), set(), 0
        todo = [(s, True)]
        while todo:
            r, first = todo.pop()
            if (r, first) in seen:
                continue
            seen.add((r, first))
            sides |= members[r]
            hits = keyed.get(r)
            if hits and first:
                found |= hits
            todo.extend((w, first and not hits) for w in succ[r])
        out[s] = (frozenset(found), sides)
    return out


def run():
    rng = random.Random(7)
    graphs = 0
    for trial in range(300):
        n = rng.randint(1, 40)
        succ = [[rng.randrange(n) for _ in range(rng.choice([0, 1, 1, 2, 3]))] for _ in range(n)]
        members = [rng.choice([0, 0, 0, 1, 2, 3]) for _ in range(n)]
        keyed = {v: frozenset(rng.sample(range(50), rng.randint(1, 3))) for v in range(n) if rng.random() < 0.25}
        starts = rng.sample(range(n), rng.randint(1, n))
        want = _reference(starts, succ, members, keyed)

        sides, keys = {}, {}
        # several calls sharing one memo, as pair_x's starts arrive
        cut = rng.randint(0, len(starts))
        for part in (starts[:cut], starts[cut:]):
            _fold(part, succ.__getitem__, members.__getitem__, int.__or__, sides)
            _fold(part, lambda v: () if v in keyed else succ[v], lambda v: keyed.get(v, _EMPTY), _union, keys)
        for s in starts:
            assert (keys[s], sides[s]) == want[s], (trial, s, keys[s], sides[s], want[s])
        graphs += 1

    # a long chain and a long cycle: no recursion limit
    n = 200000
    chain = [[i + 1] for i in range(n - 1)] + [[0]]
    val = {}
    _fold([0], chain.__getitem__, lambda v: 1 if v == n - 1 else 0, int.__or__, val)
    assert len(val) == n and all(v == 1 for v in val.values())
    print(f"  test_xpair_fold: OK ({graphs} random graphs match the per-source walk; {n}-node cycle)")
