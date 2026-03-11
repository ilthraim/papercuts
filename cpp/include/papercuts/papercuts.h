#pragma once
#include <memory>
#include <string>
#include <string_view>
#include <unordered_set>

#include "slang/syntax/AllSyntax.h"
#include "slang/syntax/SyntaxNode.h"
#include "slang/syntax/SyntaxVisitor.h"

using namespace slang::syntax;

namespace papercuts {
class ModuleNameRewriter : public SyntaxRewriter<ModuleNameRewriter> {
    public:
        void handle(const ModuleHeaderSyntax&);
};

// MARK: BitShrink
class BitShrinkRewriter : public SyntaxRewriter<BitShrinkRewriter> {
private:
    std::string_view nodeToShrink; // Store the name of the current node we want to shrink
    int newWidth; // Store the new width we want to shrink to
    std::unordered_map<const DeclaratorSyntax*, int> widthMap; // Map to store the width of each DeclaratorSyntax node
    bool done = false; // Flag to indicate if we've already shrunk bits in the current tree
public:
    void handle(const DeclaratorSyntax&);
    void handle(const IdentifierNameSyntax&);
    void handle(const IdentifierSelectNameSyntax&);
    std::vector<std::shared_ptr<SyntaxTree>> shrinkBits(const std::shared_ptr<SyntaxTree>);
};

class BitShrinkCollector : public SyntaxVisitor<BitShrinkCollector> {
private:
    std::unordered_map<const DeclaratorSyntax*, int> widthMap; // Map to store the width of each DeclaratorSyntax node
public:
    void handle(const DeclaratorSyntax&);
    std::unordered_map<const DeclaratorSyntax*, int> getFoundNodes(const std::shared_ptr<SyntaxTree>);
};

//MARK: Ternary
class TernaryRemover : public SyntaxRewriter<TernaryRemover> {
private:
    std::unordered_set<const ConditionalExpressionSyntax*> nodesToChange;
    bool done = false;
    bool LR = false; // Flag to indicate whether to replace with the left or right side of the ternary operator
public:
    void handle(const ConditionalExpressionSyntax&);
    std::vector<std::shared_ptr<SyntaxTree>> removeTernaries(const std::shared_ptr<SyntaxTree>);
};

class TernaryCollector : public SyntaxVisitor<TernaryCollector> {
private:
    std::unordered_set<const ConditionalExpressionSyntax*> foundNodes;
public:
    void handle(const ConditionalExpressionSyntax&);
    std::unordered_set<const ConditionalExpressionSyntax*> getFoundNodes(const std::shared_ptr<SyntaxTree>);
};

// MARK: If
class IfRemover : public SyntaxRewriter<IfRemover> {
private:
    std::unordered_set<const ConditionalStatementSyntax*> nodesToChange;
    bool done = false;
    bool TF = false; // Flag to indicate whether to replace with the true or false branch of the if statement
public:
    void handle(const ConditionalStatementSyntax&);
    std::vector<std::shared_ptr<SyntaxTree>> removeIfs(const std::shared_ptr<SyntaxTree>);
};

class IfCollector : public SyntaxVisitor<IfCollector> {
private:
    std::unordered_set<const ConditionalStatementSyntax*> foundNodes;
public:
    void handle(const ConditionalStatementSyntax&);
    std::unordered_set<const ConditionalStatementSyntax*> getFoundNodes(const std::shared_ptr<SyntaxTree>);
};
} // namespace papercuts