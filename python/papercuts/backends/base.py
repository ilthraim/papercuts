from __future__ import annotations

import argparse
from abc import ABC, abstractmethod

from papercuts.utils import VERDICTS, Run, clock_list


class ECBackend(ABC):
    """An equivalence-checking backend.

    A backend proves or refutes each candidate rewrite (a :class:`Run`) against
    the golden source. A subclass implements :meth:`_check` (and
    :meth:`_check_from_power_up`, to serve cegis-comb), returning one of
    ``VERDICTS``; :meth:`check` records it, and only "proven" makes a run valid.
    A subclass should set ``run.output``.

    What every backend must honour (the pipeline relies on each):

    - ``run.golden`` / ``run.candidate``: a definition in an override file wins
      over the library copy at every depth (see :class:`Design`).
    - ``run.renames``: pair each renamed instance and signal across the sides.
    - ``run.time_limit_s``: stop after that budget, as "inconclusive".
    - "unmapped", never "proven", when a compared point had no partner.

    Backends are discovered as plugins via the ``papercuts.backends`` entry-point
    group, so out-of-tree packages (e.g. environment-specific formal-tool
    wrappers) can register additional backends without modifying papercuts.

    Clocking is a property of the design rather than of any one tool, so the
    ``--clock``/``--reset`` vocabulary is defined here and each backend renders
    it into its own tool's syntax. Every check elaborates at the design top, so
    one pair of top-level names covers every run in a pipeline invocation.
    """

    #: Short identifier used to select the backend on the command line.
    name: str = "base"

    def __init__(self, clock: "list[str] | None" = None, reset: "str | None" = None) -> None:
        #: Every top-level clock signal, declared together; [] for a clock-free check.
        self.clock = clock_list(clock)
        #: Top-level reset expression, or None for no reset constraint.
        self.reset = reset

    @classmethod
    def add_cli_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register command-line arguments.

        The base implementation registers the clocking options every backend
        understands; a subclass adding its own arguments should call
        ``super().add_cli_args(parser)`` first.
        """
        parser.add_argument(
            "--clock",
            action="append",
            default=None,
            metavar="SIGNAL",
            help="Top-level clock signal to declare to the formal tool. Repeat it "
            "for every clock of a multi-clock design (--clock clk1 --clock clk2): all "
            "are declared together, at the same rate and in phase. A clock left out "
            "is an ordinary input that changes at most every other cycle, so cuts "
            "that depend on it can prove wrongly. Omitted by default, which leaves "
            "the check clock-free.",
        )
        parser.add_argument(
            "--reset",
            default=None,
            metavar="EXPR",
            help="Top-level reset expression to declare to the formal tool "
            "(e.g. '!rst_n'). Omitted by default, which leaves the check "
            "unconstrained by any reset.",
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "ECBackend":
        """Construct the backend from parsed command-line arguments.

        A backend whose ``__init__`` does not accept ``clock``/``reset`` must
        override this.
        """
        return cls(clock=args.clock, reset=args.reset)

    def prepare(self) -> None:
        """Write any tool scripts this backend needs (optional).

        Called once, after construction and before any :meth:`check`. Runs only
        when equivalence checking was actually requested, so a backend that is
        never selected writes nothing.
        """

    def default_excluded_modules(self) -> set[str]:
        """Module names this backend recommends never cutting.

        A backend targeting an environment with vendor IP, blackboxes, or
        library primitives that should be left untouched can return their
        definition names here (exact match or ``fnmatch`` glob). The pipeline
        keeps such modules in the golden source but skips enumerating and
        checking cuts on them. The user can still extend this set with
        ``--exclude-module`` or discard it with ``--no-default-excludes``.

        Default: no exclusions.
        """
        return set()

    async def check(self, run: Run) -> bool:
        """Equivalence-check a single run under the clock and reset. Sets
        ``run.verdict`` and ``run.valid``; returns ``run.valid``."""
        return self._record(run, await self._check(run))

    async def check_from_power_up(self, run: Run) -> bool:
        """Equivalence-check a run from every power-up state the two designs
        share, with no reset: each register starts at an arbitrary value, the
        same in both designs, and the proof covers all time from there.

        This is what ``--method cegis-comb`` claims for its consolidated design
        (its spec: equal registers give equal outputs and equal next states),
        and it implies equivalence under any reset sequence. Checking the claim
        with a reset instead can fail spuriously: a formal tool's reset
        analysis simulates the reset with every other input unknown, so a
        register whose reset value depends on an input comes out unknown in one
        design and constant in the other, and the two then start apart
        (opencores__gost28147-89__mac, 2026-10-23).
        """
        return self._record(run, await self._check_from_power_up(run))

    @staticmethod
    def _record(run: Run, verdict: str) -> bool:
        if verdict not in VERDICTS:
            raise ValueError(f"backend verdict {verdict!r} is not one of {VERDICTS}")
        run.verdict = verdict
        run.valid = verdict == "proven"
        return run.valid

    @abstractmethod
    async def _check(self, run: Run) -> str:
        """Prove or refute ``run`` under the clock and reset; return a verdict."""
        raise NotImplementedError

    async def _check_from_power_up(self, run: Run) -> str:
        """As :meth:`_check`, from every shared power-up state with no reset. A
        backend that cannot run such a check cannot serve cegis-comb."""
        raise NotImplementedError(f"backend {self.name!r} has no power-up check (needed by --method cegis-comb)")
