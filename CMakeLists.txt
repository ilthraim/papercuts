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

FetchContent_Declare(pybind11
    GIT_REPOSITORY https://github.com/pybind/pybind11.git
    GIT_TAG        v3.0.2
    GIT_SHALLOW    ON
)

# Configure slang build options BEFORE FetchContent_MakeAvailable
set(SLANG_INCLUDE_PYLIB ON CACHE BOOL "" FORCE)    # <-- builds pyslang!
set(SLANG_INCLUDE_TOOLS OFF CACHE BOOL "" FORCE)    # don't need slang CLI tools
set(SLANG_INCLUDE_TESTS OFF CACHE BOOL "" FORCE)    # don't need slang tests
set(SLANG_INCLUDE_INSTALL OFF CACHE BOOL "" FORCE)  # we handle our own install
set(SLANG_USE_MIMALLOC OFF CACHE BOOL "" FORCE)     # incompatible with python's global symbol visibility requirements

FetchContent_Declare(slang
    GIT_REPOSITORY https://github.com/MikePopoloski/slang.git
)

# Populate slang and pybind11 FIRST — so pyslang reuses them
FetchContent_MakeAvailable(slang pybind11)



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
pybind11_add_module(pypercuts cpp/bindings/bindings.cpp)
target_link_libraries(pypercuts PRIVATE papercuts_core)

# ---- Install BOTH modules into the Python package ----
install(TARGETS pypercuts
    LIBRARY DESTINATION .
    COMPONENT pylib
)

# ---- Dev convenience (for cmake --build) ----

if(NOT TARGET pyslang)
    message(FATAL_ERROR "pyslang target not found — is SLANG_INCLUDE_PYLIB ON?")
endif()

# ---- Copy targets ----
# Can't use add_custom_command(TARGET pyslang ...) because
# pyslang was created in a subdirectory. Use a custom target instead.

add_custom_target(copy_pyslang ALL
    COMMAND ${CMAKE_COMMAND} -E copy
        $<TARGET_FILE:pyslang>
        "${CMAKE_SOURCE_DIR}/python/$<TARGET_FILE_NAME:pyslang>"
    COMMENT "Copying pyslang to python/"
    DEPENDS pyslang
    VERBATIM
)

add_custom_target(copy_pypercuts ALL
    COMMAND ${CMAKE_COMMAND} -E copy
        $<TARGET_FILE:pypercuts>
        "${CMAKE_SOURCE_DIR}/python/papercuts/$<TARGET_FILE_NAME:pypercuts>"
    COMMENT "Copying pypercuts to python/"
    DEPENDS pypercuts
    VERBATIM
)

set_target_properties(pypercuts PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/python/papercuts"
)

# ---- Stub generation ----

# Only attempt if pybind11-stubgen is installed
execute_process(
    COMMAND "${Python_EXECUTABLE}" -c "import pybind11_stubgen"
    RESULT_VARIABLE STUBGEN_AVAILABLE
    OUTPUT_QUIET ERROR_QUIET
)

if(STUBGEN_AVAILABLE EQUAL 0)
    add_custom_target(stubs ALL
        COMMAND ${CMAKE_COMMAND} -E env
            "PYTHONPATH=${CMAKE_SOURCE_DIR}/python"
            "${Python_EXECUTABLE}" -m pybind11_stubgen pyslang
                --output-dir "${CMAKE_SOURCE_DIR}/python"
        COMMAND ${CMAKE_COMMAND} -E env
            "PYTHONPATH=${CMAKE_SOURCE_DIR}/python"
            "${Python_EXECUTABLE}" -m pybind11_stubgen pypercuts
                --output-dir "${CMAKE_SOURCE_DIR}/python/papercuts"
        DEPENDS copy_pyslang copy_pypercuts
        COMMENT "Generating Python type stubs"
        VERBATIM
    )
else()
    message(STATUS "pybind11-stubgen not found — skipping stub generation")
    message(STATUS "  Install with: pip install pybind11-stubgen")
endif()