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
    """

    #: Short identifier used to select the backend on the command line.
    name: str = "base"

    @classmethod
    def add_cli_args(cls, parser: argparse.ArgumentParser) -> None:
        """Register backend-specific command-line arguments (optional)."""

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "ECBackend":
        """Construct the backend from parsed command-line arguments."""
        return cls()

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
