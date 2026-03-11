# cpp/CMakeLists.txt

cmake_minimum_required(VERSION 3.20)
project(papercuts_cpp
    VERSION 0.1
    LANGUAGES CXX
)

set(CMAKE_CXX_STANDARD 20)          # slang requires C++20
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON) # Useful for IDE support / clangd

# ---- Fetch slang ----
include(FetchContent)

# FetchContent_Declare(
#     slang
#     GIT_REPOSITORY https://github.com/MikePopoloski/slang.git
#     GIT_TAG        ddae1965df2fd6d94cc1730d823b96f495307782      # March 4 2026
# )
FetchContent_Declare(
    slang
    GIT_REPOSITORY https://github.com/ilthraim/slang.git
    GIT_TAG        TokenRewriter      # March 4 2026
)

FetchContent_MakeAvailable(slang)

add_executable(papercuts
    src/main.cpp
    src/papercuts.cpp
    src/utils.cpp
)

target_include_directories(papercuts
    PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(papercuts
    PRIVATE
        slang::slang
)