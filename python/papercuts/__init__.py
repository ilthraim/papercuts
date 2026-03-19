# python/papercuts/__init__.py

# pyslang is a top-level module, NOT a subpackage

# pypercuts is inside our package
from papercuts.pypercuts import cut, insert_muxes

__all__ = ["cut", "insert_muxes"]