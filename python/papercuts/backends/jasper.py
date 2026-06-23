from __future__ import annotations

from papercuts.backends.base import ECBackend
from papercuts.ec import run_jasper
from papercuts.utils import Run


class JasperBackend(ECBackend):
    """Default backend: Cadence JasperGold SEC, run directly via ``ec.run_jasper``.

    Preserves the original behaviour -- requires ``jg``/``csh`` on PATH and the
    ``pcjg.tcl`` script written by the pipeline.
    """

    name = "jg"

    async def check(self, run: Run) -> bool:
        await run_jasper(run)
        return run.valid
