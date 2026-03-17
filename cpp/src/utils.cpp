#include "papercuts/utils.h"
#include <charconv>

namespace papercuts {
    std::string_view persistString(slang::BumpAllocator& alloc, const std::string& str) {
        std::span<const char> src(str.data(), str.size());
        std::span<char> persistent = alloc.copyFrom(src);
        return {persistent.data(), persistent.size()};
    }

    int tokenToInt(const slang::parsing::Token& token) {
        auto text = token.valueText();
        int value;
        auto result = std::from_chars(text.data(), text.data() + text.size(), value);

        if (result.ec != std::errc()) {
            return 0;
        }
        return value;
    }

}