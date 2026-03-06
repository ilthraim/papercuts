#pragma once
#include <memory>
#include <unordered_set>

#include "slang/syntax/AllSyntax.h"
#include "slang/syntax/SyntaxNode.h"
#include "slang/syntax/SyntaxVisitor.h"

using namespace slang::syntax;

// Your project headers will go here as you build out the C++ side
namespace papercuts {
class BitShrinkRewriter : public SyntaxRewriter<BitShrinkRewriter> {
private:
    // Store pointers to nodes that we want to modify here
    std::unordered_set<const DeclaratorSyntax*> nodesToShrink;
    bool doneShrinking = false; // Flag to indicate if we've already shrunk bits in the current tree
public:
    void handle(const DeclaratorSyntax&);

    std::vector<std::shared_ptr<SyntaxTree>> shrinkBits(const std::shared_ptr<SyntaxTree>);
};

class BitShrinkVisitor : public SyntaxVisitor<BitShrinkVisitor> {
private:
    std::unordered_set<const DeclaratorSyntax*> foundNodes;

public:
    void handle(const DeclaratorSyntax&);
    std::unordered_set<const DeclaratorSyntax*> getFoundNodes(const std::shared_ptr<SyntaxTree>);
};

} // namespace papercuts