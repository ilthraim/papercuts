"""Value selection for the synthesizer: which of the tied maximum sets it proposes.

Steering changes which candidate comes next, and with it how many verifier calls
the search spends. It cannot change the result's cardinality: the objective still
maximizes the number of cuts, and hints give way to the constraints.

A steerer has `mode`, "hint" or "lex", and:

    observe(lits)   a conflict core was learned: [(select, value in the candidate)]
    phases()        {select: bool} for mode "hint": Optimize.set_initial_value
    weights()       {select: int} for mode "lex": a second objective, ranked below the
                    cut count. Positive prefers the select on, negative off, absent neutral

Week 7-8's learned model is one more class with this shape.
"""

from __future__ import annotations

MODES = ("hint", "lex")


class AdaptiveSteer:
    """The VSIDS-style baseline. A select's score is raised each time it is on in a
    core and lowered each time it is off in one: a core with an off literal says the
    failure needs that select off, so turning it on may repair it. All scores decay
    each observation, done VSIDS's way by growing the increment."""

    RESCALE = 1e100

    def __init__(self, names: list[str], mode: str = "hint", decay: float = 0.9):
        if mode not in MODES:
            raise ValueError(f"steering mode {mode!r}: expected one of {MODES}")
        if not 0 < decay <= 1:
            raise ValueError(f"decay {decay}: expected 0 < decay <= 1")
        self.names = list(names)
        self.mode = mode
        self.decay = decay
        self.score = dict.fromkeys(self.names, 0.0)
        self.inc = 1.0

    def observe(self, lits: list[tuple[str, bool]]) -> None:
        self.inc /= self.decay
        for n, v in lits:
            self.score[n] += self.inc if v else -self.inc
        if self.inc > self.RESCALE:
            self.score = {n: s / self.inc for n, s in self.score.items()}
            self.inc = 1.0

    def phases(self) -> dict[str, bool]:
        return {n: s <= 0 for n, s in self.score.items()}

    def weights(self) -> dict[str, int]:
        """Scaled to integers, the largest magnitude 1000; a score rounding to 0 is neutral."""
        top = max((abs(s) for s in self.score.values()), default=0.0)
        if top == 0:
            return {}
        out = {n: -round(1000 * s / top) for n, s in self.score.items()}
        return {n: w for n, w in out.items() if w}
