from __future__ import annotations

import argparse
from abc import ABC, abstractmethod

from papercuts.utils import Run


class ECBackend(ABC):
    """An equivalence-checking backend.

    A backend proves or refutes each candidate rewrite (a :class:`Run`) against
    the golden source, setting ``run.valid`` (and optionally ``run.output``).

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

    def __init__(self, clock: "str | None" = None, reset: "str | None" = None) -> None:
        #: Top-level clock signal name, or None for a clock-free check.
        self.clock = clock
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
            default=None,
            metavar="SIGNAL",
            help="Top-level clock signal to declare to the formal tool. "
            "Omitted by default, which leaves the check clock-free.",
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

    @abstractmethod
    async def check(self, run: Run) -> bool:
        """Equivalence-check a single run.

        Implementations must set ``run.valid`` (True iff the rewrite is proven
        equivalent to the golden source) and should populate ``run.output``.
        The return value is ``run.valid`` for convenience.
        """
        raise NotImplementedError
