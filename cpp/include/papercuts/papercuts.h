#pragma once
#include <iostream>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <unordered_set>

#include "slang/syntax/AllSyntax.h"
#include "slang/syntax/SyntaxKind.h"
#include "slang/syntax/SyntaxNode.h"
#include "slang/syntax/SyntaxVisitor.h"
#include "slang/parsing/TokenKind.h"

using namespace slang::syntax;
using namespace slang::parsing;

namespace papercuts {
class ASTPrinter  : public SyntaxVisitor<ASTPrinter> {
public:
    void handle(const DataDeclarationSyntax& node) {
        this->visitDefault(node);
    }
};

class TestRewriter : public SyntaxRewriter<TestRewriter> {
public:
    void handle(const DataDeclarationSyntax& node) {
        const auto& modifiers = node.modifiers;
        std::cout << "Found a data declaration with " 
                  << modifiers.size() << " modifiers!" << std::endl;
        
        for (size_t i = 0; i < modifiers.size(); i++) {
            std::cout << "  Modifier[" << i << "]: " 
                      << modifiers[i].valueText() << std::endl;

            this->removeToken(modifiers, i);
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

// Mark: Base Rewriter
template<typename TDerived>
class PapercutsRewriter : public SyntaxRewriter<TDerived> {
protected:
    using SyntaxRewriter<TDerived>::makeToken;
    using SyntaxRewriter<TDerived>::factory;

    Token makeSemicolon(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::Semicolon, ";", trivia); }
    Token makeOpenBrace(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::OpenBrace, "{", trivia); }
    Token makeCloseBrace(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::CloseBrace, "}", trivia); }
    Token makeEquals(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::Equals, "=", trivia); }
    Token makeOpenBracket(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::OpenBracket, "[", trivia); }
    Token makeCloseBracket(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::CloseBracket, "]", trivia); }
    Token makeColon(std::span<const Trivia> trivia = {}) { return makeToken(TokenKind::Colon, ":", trivia); }

    ExpressionSyntax& makeIntLiteral(const std::string_view value, std::span<const Trivia> trivia = {}) {
        return factory.literalExpression(SyntaxKind::IntegerLiteralExpression, makeToken(TokenKind::IntegerLiteral, value, trivia));
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

    static const Trivia NewLine;
private:
};

template<typename TDerived>
const Trivia PapercutsRewriter<TDerived>::NewLine{TriviaKind::EndOfLine, "\n"sv};

// MARK: BitShrink
class BitShrinkRewriter : public PapercutsRewriter<BitShrinkRewriter> {
private:
    std::string_view nodeToShrink; // Store the name of the current node we want to shrink
    const DeclaratorSyntax* currentNode; // Store the current DeclaratorSyntax node we want to shrink
    std::string newName; // Store the new name we want to give to the node we're shrinking
    int newWidth; // Store the new width we want to give to the node we're shrinking
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
class TernaryRemover : public PapercutsRewriter<TernaryRemover> {
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
class IfRemover : public PapercutsRewriter<IfRemover> {
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