#include "papercuts/papercuts.h"
#include "slang/syntax/AllSyntax.h"
#include "slang/syntax/SyntaxNode.h"
#include "slang/syntax/SyntaxTree.h"
#include <iostream>
#include <memory>
#include <stdexcept>
#include <vector>

using namespace slang::syntax;
using namespace slang::parsing;

namespace papercuts {
    void BitShrinkRewriter::handle(const ContinuousAssignSyntax& node) {
        std::cout << "Visiting node: " << node.kind << std::endl;

        this->visitDefault(node);
    }

    void BitShrinkRewriter::handle(const RangeSelectSyntax& node) {
        if (node.left->kind == SyntaxKind::IntegerLiteralExpression) {
            auto* literal = static_cast<const LiteralExpressionSyntax*>(node.left.get());
        } else {
            throw std::runtime_error("Expected left side of RangeSelect to be an integer literal");
        }
        std::cout << "Shrinking node: " << node.kind << std::endl;
        auto f = this->factory;
        int newDim = 1;
        //auto persistentStr = this->alloc.allocate(size_t size, size_t alignment)
        auto& newNode = f.literalExpression(SyntaxKind::IntegerLiteralExpression, this->makeToken(TokenKind::IntegerLiteral, "1"));

        this->replace(*node.left, newNode);
    }

    std::vector<std::shared_ptr<SyntaxTree>> shrinkBits (const std::shared_ptr<SyntaxTree>) {
        return {};
    }
}