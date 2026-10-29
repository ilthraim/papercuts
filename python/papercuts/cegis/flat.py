"""
A flattened pyosys module as integers, for the passes that walk the whole
netlist in Python (netlist.control_inputs, xpair.pair_x).

Every wire bit has an id: bit i of the k-th wire in `m.wires_` is
`base[k] + i`. A constant bit is X (an 'x) or CONST (anything else). Cell ports
and connections are arrays of ids, and `root[id]` is the bit's alias class
under the module's connections (`assign a = b`), the same for every bit of one
class.

These passes used to keep a `(wire name, offset)` tuple per bit, a fresh name
string in each. On the large koios miters (0.9-1.9M cells, 9-59M wire bits)
that alone took tens of GB and hit the 64 GB batch cap (runs ISSUES.md 119).
"""

from __future__ import annotations

from array import array
from bisect import bisect_right

from pyosys import libyosys as ys

X = -1  # a constant-X bit
CONST = -2  # any other constant bit


class Flat:
    def __init__(self, m):
        self.names: list[str] = []
        self.base = array("q")
        self.widths: list[int] = []
        self.inputs: list[tuple[str, int, int]] = []  # (name, first id, width) of each input port
        first = {}  # IdString index -> first id
        n = 0
        for w in m.wires_.values():
            name = w.name.str()
            first[w.name.index_] = n
            self.names.append(name)
            self.base.append(n)
            self.widths.append(w.width)
            if w.port_input:
                self.inputs.append((name, n, w.width))
            n += w.width
        self.n = n

        def ids(sig) -> array:
            out = array("q")
            for b in sig.to_sigbit_vector():
                w = b.wire
                out.append(first[w.name.index_] + b.offset if w is not None
                           else X if b.data == ys.State.Sx else CONST)
            return out

        self.cells = []  # (cell, type, {port: ids}, {port: ids}), ports in connections_ order
        for cell in m.cells_.values():
            ins, outs = {}, {}
            for port, sig in cell.connections_.items():
                (ins if cell.input(port) else outs)[port.str()] = ids(sig)
            self.cells.append((cell, cell.type.str(), ins, outs))
        self.conns = [(ids(lhs), ids(rhs)) for lhs, rhs in m.connections_]

        root = array("q", range(n))

        def find(k):
            while root[k] != k:
                root[k] = root[root[k]]
                k = root[k]
            return k

        for lhs, rhs in self.conns:
            for a, b in zip(lhs, rhs):
                if a >= 0 and b >= 0:
                    root[find(a)] = find(b)
        for i in range(n):
            root[i] = find(i)
        self.root = root

    def name_of(self, i: int) -> tuple[str, int]:
        """(wire name, offset) of bit id `i`."""
        k = bisect_right(self.base, i) - 1
        return self.names[k], i - self.base[k]
