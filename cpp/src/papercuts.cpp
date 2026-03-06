#include "papercuts/papercuts.h"

#include "papercuts/utils.h"
#include <charconv>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <vector>

#include "slang/syntax/AllSyntax.h"
#include "slang/syntax/SyntaxNode.h"
#include "slang/syntax/SyntaxTree.h"

using namespace slang::syntax;
using namespace slang::parsing;

namespace papercuts {
// void BitShrinkRewriter::handle(const RangeSelectSyntax& node) {
//     const LiteralExpressionSyntax* left;
//     // Make sure we are only trying to shrink if the left side is an integer literal, otherwise
//     we don't know how to handle it (yet?) if (node.left->kind ==
//     SyntaxKind::IntegerLiteralExpression) {
//         left = static_cast<const LiteralExpressionSyntax*>(node.left.get());
//     } else {
//         throw std::runtime_error("Expected left side of RangeSelect to be an integer literal");
//     }
//     std::cout << "Shrinking node: " << node.kind << std::endl;

//     // Extract the integer value from the literal expression
//     std::string_view literalText = left->literal.valueText();
//     int newDim;
//     auto [ptr, ec] = std::from_chars(literalText.data(), literalText.data() + literalText.size(),
//     newDim);

//     if (ec != std::errc()) {
//         throw std::runtime_error("Failed to parse integer literal: " + std::string(literalText));
//     }

//     newDim--; // Decrement the dimension to shrink the bit width by 1

//     if (newDim < 1)
//         return; // If the new dimension is less than 1 (e.g. [0,0]), we can't shrink it any
//         further, so we just return without making any changes

//     // Need to allocate a new string on the heap to ensure it lives long enough for the new node
//     // persistString will copy the string into the bump allocator and return a view of it
//     auto temp = std::to_string(newDim);

//     auto f = this->factory;
//     auto& newNode = f.literalExpression(SyntaxKind::IntegerLiteralExpression,
//     this->makeToken(TokenKind::IntegerLiteral, persistString(this->alloc, temp)));

//     this->replace(*node.left, newNode);
// }

void BitShrinkRewriter::handle(const DeclaratorSyntax& node) {
    if ((this->nodesToShrink.find(&node) != nodesToShrink.end()) && !this->doneShrinking) {
        std::cout << "Found node to shrink: " << node.kind << std::endl;
        this->remove(node);
        this->nodesToShrink.erase(&node); // Remove the node from the set after shrinking
        this->doneShrinking = true; // Set the flag to indicate we've shrunk a node in this tree
    }
}

std::vector<std::shared_ptr<SyntaxTree>> BitShrinkRewriter::shrinkBits(
    const std::shared_ptr<SyntaxTree> tree) {
    BitShrinkVisitor visitor;
    auto foundNodes = visitor.getFoundNodes(tree);
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    this->nodesToShrink.clear();
    for (const auto& node : foundNodes) {
        this->nodesToShrink.insert(node);
    }

    while (!this->nodesToShrink.empty()) {
        this->doneShrinking = false; // Reset the flag for each call to shrinkBits
        newTrees.push_back(this->transform(tree));
    }

    return newTrees;
}

void BitShrinkVisitor::handle(const DeclaratorSyntax& node) {
    this->foundNodes.insert(&node);
    this->visitDefault(node);
}

std::unordered_set<const DeclaratorSyntax*> BitShrinkVisitor::getFoundNodes(
    const std::shared_ptr<SyntaxTree> tree) {
    tree->root().visit(*this);

    return this->foundNodes;
}

} // namespace papercuts