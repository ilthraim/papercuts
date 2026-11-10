import sys
from dataclasses import dataclass
from typing import Callable, Union
from pyslang.syntax import SyntaxNode, SyntaxRewriter, SyntaxTree, SyntaxPrinter
from pyslang.parsing import Token

# MARK: Output control
# Verbose (debug) output is off by default; the pipeline enables it with
# set_verbose(True) when the user passes --verbose. `status` lines are always
# printed (they are the user-facing progress), `vprint` lines only when verbose.
_VERBOSE = False


def set_verbose(verbose: bool) -> None:
    """Enable/disable debug (`vprint`) output for the whole pipeline."""
    global _VERBOSE
    _VERBOSE = verbose


def is_verbose() -> bool:
    return _VERBOSE


def vprint(*args, **kwargs) -> None:
    """print() that is silenced unless --verbose was passed."""
    if _VERBOSE:
        print(*args, **kwargs)


def status(msg: str) -> None:
    """Always-on, user-facing progress line, prefixed and flushed."""
    print(f"[papercuts] {msg}", flush=True)


def rewrite_wrapper(f, *args, **kwargs) -> Callable[[SyntaxNode, SyntaxRewriter], None]:
    return lambda node, rewriter: f(node, rewriter, *args, **kwargs)

def visitor_wrapper(
    f, *args, **kwargs
) -> Callable[[Union[Token, SyntaxNode]], None]:
    return lambda node: f(node, *args, **kwargs)

def print_tree(tree: SyntaxTree) -> str:
    """Serialize a SyntaxTree, falling back when source-loc metadata is invalid."""
    try:
        return SyntaxPrinter.printFile(tree)
    except RuntimeError:
        # Some rewritten trees can carry invalid source buffer ids in pyslang.
        # `str(tree.root)` prints from CST text without querying those locations.
        return str(tree.root)
    
# MARK: Run
@dataclass(frozen=True)
class Design:
    """One side of an equivalence check, in terms any formal tool can resolve.

    ``lib_dir`` holds one file per definition, named ``<definition>.sv``. The
    design is everything reachable from ``top``, each definition resolved by name
    from ``lib_dir``, EXCEPT that every definition in an ``overrides`` file wins
    over the library copy of the same name. Each override is keyed by the
    definition it was written for; a file may define more than that one (the
    elaborated blob and the joint consolidated file define the whole design and
    are keyed by the top).

    A backend must make the override win at every depth. Letting the library copy
    of a definition shadow the override (as Jasper's `-y` did for a `-v` file
    below the top) checks golden against golden and proves anything
    (fv_dataset, 2026-11-01).
    """

    top: str
    lib_dir: str
    overrides: "tuple[tuple[str, str], ...]" = ()

    def override(self, definition: str, path: str) -> "Design":
        """This design with `definition` taken from `path`."""
        return Design(self.top, self.lib_dir, self.overrides + ((definition, path),))

    @property
    def override_map(self) -> "dict[str, str]":
        return dict(self.overrides)


@dataclass(frozen=True)
class Rename:
    """A name the candidate spells differently from the golden design (elaboration
    flattens `g[0].u` to `g_0_u`). Paths are hierarchical, relative to the top.
    ``kind`` is "instance" (everything inside pairs by name once the instances are
    paired) or "signal" (a variable or net; a backend pairs a renamed state element
    as the same initial state, not as one shared free value)."""

    kind: str
    golden: str
    candidate: str


#: Every verdict a backend may report. Only "proven" makes a run valid.
#: "unmapped": the proof finished, but a point the check compares (a black-box
#: input, say) had no partner on the other side, so it was compared with nothing.
VERDICTS = ("proven", "cex", "inconclusive", "unmapped", "error")


@dataclass
class Run:
    """One equivalence check: is ``candidate`` equivalent to ``golden``?"""

    golden: Design
    candidate: Design
    #: Directory the backend may write its own files into (tool projects, logs).
    work_dir: str
    index: int = 0
    #: Pipeline bookkeeping, never read by a backend: the file this run's rewrite
    #: is written to (one of ``candidate.overrides``).
    rewrite_path: "str | None" = None
    valid: bool = False
    output: str = ""
    #: One of VERDICTS once a check has run (ECBackend.check sets it), else None.
    verdict: "str | None" = None
    #: For a bit-shrink cut, the number of bits actually removed from the targeted
    #: dimension. 1 for a plain single-bit shrink; higher when --iterative-bitshrink
    #: greedily removes several bits. Meaningless (stays 1) for non-bitshrink cuts.
    shrink_amount: int = 1
    #: --method cegis-comb only: whether the cut is in the selected (maximum)
    #: set. There, ``valid`` is the cut's validity on its own under the
    #: combinational spec, which neither implies nor follows from membership.
    #: None under --method fv.
    in_set: "bool | None" = None
    #: Names the candidate changed (the elaboration gate), for the backend to pair.
    renames: "tuple[Rename, ...]" = ()
    #: Proof budget in seconds (None: none); a check that runs out is inconclusive.
    time_limit_s: "int | None" = None
    #: The caller only needs to know whether the designs differ, not every point
    #: where they do: the backend may stop at the first counterexample (a per-cut
    #: check). An optimization; ignoring it is correct.
    stop_at_first_cex: bool = False
