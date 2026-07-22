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
@dataclass
class Run:
    """A single test run with input and expected output."""

    top_module_path: str
    spec_lib_path: str
    impl_module_path: str
    impl_module_folder: str
    is_top: bool
    index: int = 0
    valid: bool = False
    output: str = ""
    #: Normalized formal verdict for this run, when the backend can report one:
    #: "proven" | "cex" | "inconclusive" | "error". None until a check runs (or
    #: if the backend never sets it). "error" also covers a killed/aborted tool.
    verdict: "str | None" = None
