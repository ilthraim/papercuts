#pragma once

#include "slang/util/BumpAllocator.h"
#include <string>
#include <string_view>
#include <span>

namespace papercuts {
    std::string_view persistString(slang::BumpAllocator& alloc, const std::string& str);
}