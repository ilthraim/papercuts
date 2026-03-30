# python/papercuts/__init__.py

# pyslang is a top-level module, NOT a subpackage

# pypercuts is inside our package
from papercuts.pypercuts import insert_muxes, Papercutter, rename_module

__all__ = ["Papercutter", "insert_muxes", "rename_module"]