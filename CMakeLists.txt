cmake_minimum_required(VERSION 3.20)
project(papercuts VERSION 0.1 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ---- Python ----
set(Python_FIND_VIRTUALENV FIRST)
find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)
message(STATUS "Python executable: ${Python_EXECUTABLE}")
message(STATUS "Python version: ${Python_VERSION}")
#set(PYBIND11_FINDPYTHON ON)

# ---- Fetch dependencies ----
include(FetchContent)

# Force slang's bundled boost::regex into standalone mode. Otherwise its
# auto-detection (regex/config.hpp) sees the stale system boost-devel 1.66
# headers at /usr/include/boost and pulls them in, colliding with slang's
# vendored boost_unordered.hpp (redefinition of boost::exception_detail::*).
add_compile_definitions(BOOST_REGEX_STANDALONE)

# Keep slang's SLANG_ASSERT active in EVERY build, Release included. The macro is
# a no-op unless SLANG_ASSERT_ENABLED (or SLANG_DEBUG) is set, so without this the
# wheel built through pyproject.toml/scikit-build silently skips slang's own
# preconditions while a build from CMakePresets.json (which passes the same flag in
# CMAKE_CXX_FLAGS) enforces them. That asymmetry hid a real violation: deadcode.py
# called AnalysisManager::analyze on an unfrozen Compilation, which only ever
# asserted on the preset build. Papercuts' output is a proof, so a precondition
# violation should fail loudly wherever it happens, not depend on how it was built.
# Set here, before FetchContent pulls slang in, so it applies to slang's targets too.
add_compile_definitions(SLANG_ASSERT_ENABLED)


# Configure slang build options BEFORE FetchContent_MakeAvailable
set(SLANG_INCLUDE_PYLIB ON CACHE BOOL "" FORCE)    # <-- builds pyslang!
set(SLANG_INCLUDE_TOOLS OFF CACHE BOOL "" FORCE)    # don't need slang CLI tools
set(SLANG_INCLUDE_TESTS OFF CACHE BOOL "" FORCE)    # don't need slang tests
set(SLANG_INCLUDE_INSTALL OFF CACHE BOOL "" FORCE)  # we handle our own install
set(SLANG_USE_MIMALLOC OFF CACHE BOOL "" FORCE)     # incompatible with python's global symbol visibility requirements

# Pin slang to the fork's `papercuts` branch: upstream plus the resolveReplacement
# fix (composes nested replacements; see CLAUDE.md, mux invariant 3) and an
# ASTContext(Symbol) binding overload. Pin the SHA, not the tag -- the tag
# (papercuts-2026-09) only keeps the commit reachable on GitHub.
FetchContent_Declare(slang
    GIT_REPOSITORY https://github.com/ilthraim/slang.git
    GIT_TAG        3ceae7d20cbe61e13ccfea1a3950d917f0e3fc74  # tag papercuts-2026-09
)

# Populate slang FIRST -- it locates nanobind (from the active Python env, else
# FetchContent) and defines nanobind_add_module/nanobind_add_stub for us.
FetchContent_MakeAvailable(slang)

if(NOT TARGET pyslang)
    message(FATAL_ERROR "pyslang target not found — is SLANG_INCLUDE_PYLIB ON?")
endif()

# ---- Core C++ library ----
add_library(papercuts_core
    cpp/src/papercuts.cpp
    cpp/src/utils.cpp
)

target_include_directories(papercuts_core
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/cpp/include
)

target_link_libraries(papercuts_core
    PUBLIC
        slang::slang
)

# ---- Test executable ----
add_executable(pc cpp/src/main.cpp)
target_link_libraries(pc PRIVATE papercuts_core)

# ---- Python extension module ----
# Same split-mode runtime and ABI floor as upstream pyslang, so SyntaxTree can
# cross between the two modules.
nanobind_add_module(pypercuts BACKEND_MODULE nanobind_backend STABLE_ABI_VERSION 3.11
    cpp/bindings/bindings.cpp)
target_link_libraries(pypercuts PRIVATE papercuts_core)

set_target_properties(pypercuts PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/python/papercuts"
)

# ---- pyslang package ----
# Upstream pyslang is a package, not a bare extension:
#
#     pyslang/__init__.py         re-exports pyslang.pyslang, aliases pyslang.ast etc.
#     pyslang/pyslang.abi3.so     the extension, imported as pyslang.pyslang
#     pyslang/py.typed
#     pyslang/*.pyi               stubs for the re-exported names
#     pyslang/pyslang/*.pyi       the same stubs, for pyslang.pyslang.*
#
# slang's own install rules (component `pylib`) assume a wheel whose install-dir
# is pyslang/, so they put these at the root of ours. We assemble the package
# once in the build tree and both install it and copy it into python/ from there,
# so the dev tree and the wheel cannot disagree. slang's `pylib` component is left
# out of the wheel in pyproject.toml.
set(PYSLANG_SRC_PKG "${slang_SOURCE_DIR}/pyslang/pyslang")
set(PYSLANG_STUBS "${slang_BINARY_DIR}/bindings/pyslang-stubs/pyslang")
set(PYSLANG_PKG "${CMAKE_BINARY_DIR}/pyslang-pkg/pyslang")

add_custom_target(pyslang_pkg ALL
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${PYSLANG_PKG}"
    COMMAND ${CMAKE_COMMAND} -E make_directory "${PYSLANG_PKG}/pyslang"
    COMMAND ${CMAKE_COMMAND} -E copy $<TARGET_FILE:pyslang> "${PYSLANG_PKG}/"
    COMMAND ${CMAKE_COMMAND} -E copy
        "${PYSLANG_SRC_PKG}/__init__.py" "${PYSLANG_SRC_PKG}/py.typed" "${PYSLANG_PKG}/"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${PYSLANG_STUBS}" "${PYSLANG_PKG}"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${PYSLANG_STUBS}" "${PYSLANG_PKG}/pyslang"
    DEPENDS pyslang pyslang_stub
    COMMENT "Assembling the pyslang package"
    VERBATIM
)

install(DIRECTORY "${PYSLANG_PKG}"
    DESTINATION .
    COMPONENT papercuts_py
    USE_SOURCE_PERMISSIONS
)

# ---- Install the extension into the papercuts package ----
# Must land inside the package so it imports as `papercuts.pypercuts` from an
# installed wheel.
install(TARGETS pypercuts
    LIBRARY DESTINATION papercuts
    COMPONENT papercuts_py
)

# ---- Dev convenience (for cmake --build) ----
# Mirror the assembled package into python/ so `import pyslang` resolves there.
# The directory is replaced wholesale: a stale stub or extension can't survive.
add_custom_target(copy_pyslang ALL
    COMMAND ${CMAKE_COMMAND} -E rm -rf "${CMAKE_SOURCE_DIR}/python/pyslang"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${PYSLANG_PKG}" "${CMAKE_SOURCE_DIR}/python/pyslang"
    DEPENDS pyslang_pkg
    COMMENT "Copying the pyslang package to python/"
    VERBATIM
)

# ---- Stub generation ----
# pypercuts is imported by stubgen, which imports pyslang, which (split mode)
# imports nanobind_backend. Use the build env's copy if it has one, else the one
# slang fetched into its build tree for its own stubs.
set(PYPERCUTS_STUB_PATH "${CMAKE_SOURCE_DIR}/python/papercuts" "${CMAKE_BINARY_DIR}/pyslang-pkg")
if(EXISTS "${slang_BINARY_DIR}/bindings/nanobind_backend")
    list(APPEND PYPERCUTS_STUB_PATH "${slang_BINARY_DIR}/bindings/nanobind_backend")
endif()

nanobind_add_stub(pypercuts_stub
    MODULE pypercuts
    OUTPUT "${CMAKE_SOURCE_DIR}/python/papercuts/pypercuts.pyi"
    PYTHON_PATH ${PYPERCUTS_STUB_PATH}
    DEPENDS pypercuts pyslang_pkg
)

install(FILES "${CMAKE_SOURCE_DIR}/python/papercuts/pypercuts.pyi"
    DESTINATION papercuts
    COMPONENT papercuts_py
)
