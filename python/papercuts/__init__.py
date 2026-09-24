# python/papercuts/__init__.py

# pyslang is a top-level module, NOT a subpackage

# pypercuts is inside our package
from papercuts.pypercuts import (insert_muxes, Papercutter, rename_module, get_module_name,
                                 rename_submodules, rename_instance_types)

__all__ = ["Papercutter", "insert_muxes", "rename_module", "get_module_name", "rename_submodules",
           "rename_instance_types"]