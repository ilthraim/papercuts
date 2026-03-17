#pragma once

#include "slang/util/BumpAllocator.h"
#include "slang/parsing/Token.h"
#include <string>
#include <string_view>
#include <span>

namespace papercuts {
    std::string_view persistString(slang::BumpAllocator& alloc, const std::string& str);
    int tokenToInt(const slang::parsing::Token& token);

    template<class T>
    std::span<T, 1> singleSpan(T& t) {
        return std::span<T, 1>(&t, 1);
    }
}