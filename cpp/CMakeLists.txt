# cpp/CMakeLists.txt

cmake_minimum_required(VERSION 3.20)
project(papercuts_cpp
    VERSION 0.1
    LANGUAGES CXX
)

# Attempt to use venv python
set(Python3_FIND_VIRTUALENV FIRST) 
find_package(Python3 REQUIRED COMPONENTS Interpreter Development)

# Verify python location
message(STATUS "Python executable: ${Python3_EXECUTABLE}")
message(STATUS "Python version: ${Python3_VERSION}")

set(CMAKE_CXX_STANDARD 20)          # slang requires C++20
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON) # Useful for IDE support / clangd
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ---- Tell pybind11 to reuse our already-found Python ----
set(PYBIND11_FINDPYTHON ON)

# ---- Fetch slang ----
include(FetchContent)

FetchContent_Declare(
    slang
    GIT_REPOSITORY https://github.com/MikePopoloski/slang.git
)

FetchContent_Declare(
    pybind11
    GIT_REPOSITORY https://github.com/pybind/pybind11.git
    GIT_TAG        v2.13.6
)

FetchContent_MakeAvailable(slang pybind11)


# ---- Core library (for export to python) ----
add_library(papercuts
    src/papercuts.cpp
    src/utils.cpp
)

target_include_directories(papercuts
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(papercuts
    PUBLIC
        slang::slang
)

# ---- Executable for testing ----

add_executable(pc
    src/main.cpp
    src/papercuts.cpp
    src/utils.cpp
)

target_link_libraries(pc
    PRIVATE
        papercuts
)

# ---- Python Bindings ----


pybind11_add_module(pypercuts
    bindings/bindings.cpp
)

target_link_libraries(pypercuts
    PRIVATE
        papercuts
)

# Install .so to the Python package directory
install(TARGETS pypercuts
    LIBRARY DESTINATION papercuts
)