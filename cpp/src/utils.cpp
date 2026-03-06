#include "papercuts/utils.h"

namespace papercuts {
    std::string_view persistString(slang::BumpAllocator& alloc, const std::string& str) {
        std::span<const char> src(str.data(), str.size());
        std::span<char> persistent = alloc.copyFrom(src);
        return {persistent.data(), persistent.size()};
    }
}