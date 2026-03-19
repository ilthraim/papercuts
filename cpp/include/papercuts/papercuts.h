#pragma once
#include <cstddef>
#include <iostream>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

#include "slang/parsing/TokenKind.h"
#include "slang/syntax/AllSyntax.h"
#include "slang/syntax/SyntaxKind.h"
#include "slang/syntax/SyntaxNode.h"
#include "slang/syntax/SyntaxTree.h"
#include "slang/syntax/SyntaxVisitor.h"

using namespace slang::syntax;
using namespace slang::parsing;

namespace papercuts {
struct MuxContext {
    int muxCount = 0;
};

class ASTPrinter : public SyntaxVisitor<ASTPrinter> {
public:
    void handle(const SyntaxNode& node) { 
        std::cout << node.kind << std::endl;
        this->visitDefault(node); 
        }
};

class TestRewriter : public SyntaxRewriter<TestRewriter> {
private:
public:
    void handle(const SyntaxNode& node) {
    }
};

class TestVisitor : public SyntaxVisitor<TestVisitor> {
private:
    std::unordered_set<SyntaxNode*> visitedNodes;
public:
    void handle(const SyntaxNode& node) {
        if (visitedNodes.find(node.parent) != visitedNodes.end()) {
            std::cout << "Already visited parent node: " << node.kind << " of type " << node.parent->kind << std::endl;
        } else {
            std::cout << "not found parent node for " << node.kind << std::endl;
        }
        visitedNodes.insert(const_cast<SyntaxNode*>(&node));

        this->visitDefault(node);
    }
};

class ParentSetter{
public:
    void visit(SyntaxNode& node) {
        bool isList = node.kind == SyntaxKind::SeparatedList || node.kind == SyntaxKind::SyntaxList || node.kind == SyntaxKind::TokenList;
        if (!isList) {
            for (size_t i = 0; i < node.getChildCount(); i++) {
                auto child = node.childNode(i);
                if (child) { // If not a token
                    if (child->kind == SyntaxKind::SeparatedList || child->kind == SyntaxKind::SyntaxList || child->kind == SyntaxKind::TokenList) { // If child is a list we need to set the parents of all the elements
                        for (size_t j = 0; j < child->getChildCount(); j++) {
                            auto grandChild = child->childNode(j);
                            if (grandChild) { // If not a token
                                grandChild->parent = &node;
                            }
                        }
                    }
                    child->parent = &node;

                    visit(*child);
                }
            }
        } else { // If this is a list, just visit all the children
            for (size_t i = 0; i < node.getChildCount(); i++) {
                auto child = node.childNode(i);
                if (child) {
                    visit(*child);
                }
            }

        }
    }
};


class ModuleNameRewriter : public SyntaxRewriter<ModuleNameRewriter> {
private:
    std::string newName; // Store the new name we want to give to the module
public:
    void handle(const ModuleHeaderSyntax&);
    std::shared_ptr<SyntaxTree> renameModule(const std::shared_ptr<SyntaxTree>, std::string);
};

// MARK: Base functions

std::vector<std::shared_ptr<SyntaxTree>> cutAll(const std::shared_ptr<SyntaxTree>, bool bitShrink, bool ternaryRemove,
                                             bool ifRemove);

std::shared_ptr<SyntaxTree> insertMuxes(const std::shared_ptr<SyntaxTree> tree, bool bitMux, bool ternaryMux,
                                        bool ifMux);

// MARK: Base Rewriter
template<typename TDerived>
class PapercutsRewriter : public SyntaxRewriter<TDerived> {
protected:
    using SyntaxRewriter<TDerived>::makeToken;
    using SyntaxRewriter<TDerived>::factory;

    Token makeSemicolon(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::Semicolon, ";", trivia); }
    Token makeOpenBrace(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::OpenBrace, "{", trivia); }
    Token makeCloseBrace(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::CloseBrace, "}", trivia); }
    Token makeEquals(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::Equals, "=", trivia); }
    Token makeOpenBracket(std::span<const Trivia> trivia = {}) {
        return makeToken(TokenKind::OpenBracket, "[", trivia);
    }
    Token makeCloseBracket(std::span<const Trivia> trivia = {}) {
        return makeToken(TokenKind::CloseBracket, "]", trivia);
    }
    Token makeColon(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::Colon, ":", trivia); }

    ExpressionSyntax& makeIntLiteral(const std::string_view value, std::span<const Trivia> trivia = {}) {
        return factory.literalExpression(SyntaxKind::IntegerLiteralExpression,
                                         makeToken(TokenKind::IntegerLiteral, value, trivia));
    }

    template<typename TNode>
    SeparatedSyntaxList<TNode> makeSeparatedList(std::span<TNode* const> nodes,
                                                 std::optional<Token> separator = std::nullopt) {
        if (nodes.empty())
            return SeparatedSyntaxList<TNode>{std::span<TokenOrSyntax>{}};

        slang::SmallVector<TokenOrSyntax> buffer;
        const size_t count = separator ? (nodes.size() * 2 - 1) : nodes.size();
        buffer.reserve(count);

        for (size_t i = 0; i < nodes.size(); ++i) {
            buffer.push_back(nodes[i]);
            if (separator && i + 1 < nodes.size())
                buffer.push_back(separator->deepClone(this->alloc));
        }

        return SeparatedSyntaxList<TNode>(buffer.copy(this->alloc));
    }

    template<typename TNode>
    SyntaxList<TNode> makeSyntaxList(std::span<TNode* const> nodes) {
        if (nodes.empty())
            return SyntaxList<TNode>{std::span<TNode*>{}};

        slang::SmallVector<TNode*> buffer;
        buffer.reserve(nodes.size());
        for (TNode* node : nodes)
            buffer.push_back(node);

        return SyntaxList<TNode>(buffer.copy(this->alloc));
    }

    // Helper function to wrap an expression in a conditional pattern -> conditional predidate
    // When inserting muxes, the parser will spit out an arbitrary parenthesized expression, but we need to convert
    // that to a conditional predicate in order to replace the predicate of an if statement or ternary operator
    ConditionalPredicateSyntax& makeConditionalPredicate(ExpressionSyntax& expression) {
        auto& pattern = factory.conditionalPattern(expression, {});
        std::array<ConditionalPatternSyntax*, 1> patternArr{&pattern};
        return factory.conditionalPredicate(makeSeparatedList<ConditionalPatternSyntax>(patternArr));
    }

    static const Trivia NewLine;

private:
};

template<typename TDerived>
const Trivia PapercutsRewriter<TDerived>::NewLine{TriviaKind::EndOfLine, "\n"sv};

// MARK: BitShrink
class BitMuxer : public PapercutsRewriter<BitMuxer> {
private:
    bool initialized = false;
    MuxContext& context;
    std::unordered_map<std::string, int> widthMap;
public:
    BitMuxer(MuxContext& context) : context(context) {}
    std::shared_ptr<SyntaxTree> insertBitShrinkMuxes(const std::shared_ptr<SyntaxTree>);
    void initialize(const std::shared_ptr<SyntaxTree>);
    void handle(const DataDeclarationSyntax& node);
    void handle(const IdentifierNameSyntax& node);
    void handle(const IdentifierSelectNameSyntax& node);
    void handle(const SyntaxNode& node);
};

class BitShrinker : public PapercutsRewriter<BitShrinker> {
private:
    std::vector<std::pair<const DeclaratorSyntax*, int>> shrinkNodes; // Vector to store the width of each DeclaratorSyntax node
    std::unordered_map<const DeclaratorSyntax*, int> runMap;
    std::unordered_set<std::string> nodesToShrink; // Set to store the names of the nodes we want to shrink bits in
    const std::shared_ptr<SyntaxTree> tree; // Store the current tree we're shrinking bits in
    size_t cutCount;
public:
    BitShrinker(const std::shared_ptr<SyntaxTree> tree);
    void handle(const DeclaratorSyntax& node);
    void handle(const IdentifierNameSyntax& node);
    void handle(const IdentifierSelectNameSyntax& node);
    void handle(const SyntaxNode& node);
    std::vector<std::shared_ptr<SyntaxTree>> shrinkAllBits();
    std::shared_ptr<SyntaxTree> shrinkBitsIndex(const std::vector<size_t>& indicesToShrink);
    size_t getCutCount() const { return cutCount; }
};

class BitShrinkCollector : public SyntaxVisitor<BitShrinkCollector> {
private:
    std::vector<std::pair<const DeclaratorSyntax*, int>> shrinkNodes; // Vector to store the width of each DeclaratorSyntax node
public:
    void handle(const DeclaratorSyntax&);
    std::vector<std::pair<const DeclaratorSyntax*, int>> getFoundNodes(const std::shared_ptr<SyntaxTree>);
};

// MARK: Ternary
class TernaryMuxer : public PapercutsRewriter<TernaryMuxer> {
private:
    MuxContext& context;

public:
    TernaryMuxer(MuxContext& context) : context(context) {}
    std::shared_ptr<SyntaxTree> insertTernaryMuxes(const std::shared_ptr<SyntaxTree>);
    void handle(const ConditionalExpressionSyntax&);
};

class TernaryRemover : public PapercutsRewriter<TernaryRemover> {
private:
    std::set<const ConditionalExpressionSyntax*> ternaryNodes;
    std::unordered_map<const ConditionalExpressionSyntax*, bool> nodesToChange;
    const std::shared_ptr<SyntaxTree> tree;
    size_t cutCount;
public:
    TernaryRemover(const::std::shared_ptr<SyntaxTree> tree);
    void handle(const ConditionalExpressionSyntax&);
    std::vector<std::shared_ptr<SyntaxTree>> removeAllTernaries();
    size_t getCutCount() const { return cutCount; }
};

class TernaryCollector : public SyntaxVisitor<TernaryCollector> {
private:
    std::set<const ConditionalExpressionSyntax*> foundNodes;

public:
    void handle(const ConditionalExpressionSyntax&);
    std::set<const ConditionalExpressionSyntax*> getFoundNodes(const std::shared_ptr<SyntaxTree>);
};

// MARK: If
class IfMuxer : public PapercutsRewriter<IfMuxer> {
private:
    MuxContext& context;

public:
    IfMuxer(MuxContext& context) : context(context) {}
    std::shared_ptr<SyntaxTree> insertIfMuxes(const std::shared_ptr<SyntaxTree>);
    void handle(const ConditionalStatementSyntax&);
};

class IfRemover : public PapercutsRewriter<IfRemover> {
private:
    std::set<const ConditionalStatementSyntax*> ifNodes;
    std::unordered_map<const ConditionalStatementSyntax*, bool> nodesToChange;
    const std::shared_ptr<SyntaxTree> tree;
    size_t cutCount;
public:
    IfRemover(const::std::shared_ptr<SyntaxTree> tree);
    void handle(const ConditionalStatementSyntax&);
    std::vector<std::shared_ptr<SyntaxTree>> removeAllIfs();
    size_t getCutCount() const { return cutCount; }
};

class IfCollector : public SyntaxVisitor<IfCollector> {
private:
    std::set<const ConditionalStatementSyntax*> foundNodes;

public:
    void handle(const ConditionalStatementSyntax&);
    std::set<const ConditionalStatementSyntax*> getFoundNodes(const std::shared_ptr<SyntaxTree>);
};

// MARK: Papercutter

class Papercutter {
private:
    std::shared_ptr<SyntaxTree> tree;
    size_t cutCount = 0;
    BitShrinker BSR;
    TernaryRemover TR;
    IfRemover IR;
public:
    Papercutter(const std::shared_ptr<SyntaxTree> tree);
    std::vector<std::shared_ptr<SyntaxTree>> cutAll();
    std::shared_ptr<SyntaxTree> cutIndex(size_t index);
};

} // namespace papercuts