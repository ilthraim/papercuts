"""
papercuts C++ bindings
"""
from __future__ import annotations
__all__: list[str] = ['cut', 'insert_muxes']
def cut(tree: ..., bitShrink: bool = False, ternaryRemove: bool = False, ifRemove: bool = False) -> list[...]:
    """
    Cut a SyntaxTree into multiple trees based on mux types
    """
def insert_muxes(tree: ..., bitMux: bool = False, ternaryMux: bool = False, ifMux: bool = False) -> ...:
    """
    Insert muxes into a SyntaxTree
    """
