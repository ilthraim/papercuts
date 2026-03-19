import sys
import ctypes

# Force pyslang's symbols to be globally visible
# so pypercuts can share its slang types
_orig_flags = sys.getdlopenflags()
sys.setdlopenflags(_orig_flags | ctypes.RTLD_GLOBAL)
import pyslang
sys.setdlopenflags(_orig_flags)

from papercuts.pypercuts import *