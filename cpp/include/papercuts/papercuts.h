#pragma once
#include <cstddef>
#include <iostream>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <unordered_set>
#include <utility>
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

// MARK: Utility classes

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

class ModuleNameFinder : public SyntaxVisitor<ModuleNameFinder> {
private:
    std::string moduleName;
public:
    void handle(const ModuleHeaderSyntax&);
    std::string getModuleName(const std::shared_ptr<SyntaxTree> tree);
};

class SubmoduleRenamer : public SyntaxRewriter<SubmoduleRenamer> {
private:
    std::string moduleName; // Store the new name we want to give to the module
    std::shared_ptr<SyntaxTree> tree;
    std::unordered_set<std::string> excluded; // Module names to leave un-renamed (kept verbatim)
public:
    SubmoduleRenamer(const std::shared_ptr<SyntaxTree> tree, std::unordered_set<std::string> excluded = {});
    void handle(const HierarchyInstantiationSyntax& node);
    std::shared_ptr<SyntaxTree> renameSubmodules();
};

class InputAdder: public SyntaxRewriter<InputAdder> { 
private:
    int numInputs = 0;
public:
    void handle(const PortListSyntax& node);
    std::shared_ptr<SyntaxTree> addInputs(std::shared_ptr<SyntaxTree> tree, int numInputs);
};

// MARK: Base functions

std::shared_ptr<SyntaxTree> insertMuxes(const std::shared_ptr<SyntaxTree> tree, bool bitMux, bool ternaryMux,
                                        bool ifMux);

std::shared_ptr<SyntaxTree> renameModule(const std::shared_ptr<SyntaxTree> tree, std::string newName);

std::shared_ptr<SyntaxTree> renameSubmodules(const std::shared_ptr<SyntaxTree> tree,
                                             const std::vector<std::string>& excluded = {});

std::string getModuleName(const std::shared_ptr<SyntaxTree> tree);

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
// One shrinkable packed dimension of a declarator. A multi-dimensional vector
// like `logic [3:0][7:0] x;` yields one target per packed dimension (dimIndex 0
// -> [3:0], dimIndex 1 -> [7:0]); a plain `logic [7:0] y;` yields a single
// target with dimIndex 0. `width` is that dimension's bit count, kept for cut
// reporting and the legacy intermediate-wire strategy. `dimIndex` is the index
// into the type's packed dimension list (0 = leftmost/outermost).
struct BitShrinkTarget {
    const DeclaratorSyntax* decl;
    int width;
    int dimIndex;
};

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
    std::vector<BitShrinkTarget> shrinkNodes; // One entry per shrinkable declarator (legacy single-dim only)
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
    std::vector<BitShrinkTarget> shrinkNodes; // One entry per shrinkable packed dimension
    bool allowSigned;   // Signed decls are only shrinkable when narrowing in place (not with intermediate wires)
    bool allowNets;     // Net decls (wire/tri/...) are only shrinkable when narrowing in place
    bool allowMultiDim; // Multi-packed-dim vectors are only shrinkable when narrowing in place
public:
    BitShrinkCollector(bool allowSigned = false, bool allowNets = false, bool allowMultiDim = false)
        : allowSigned(allowSigned), allowNets(allowNets), allowMultiDim(allowMultiDim) {}
    void handle(const DeclaratorSyntax&);
    std::vector<BitShrinkTarget> getFoundNodes(const std::shared_ptr<SyntaxTree>);
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
    std::vector<const ConditionalExpressionSyntax*> ternaryNodes;
    std::unordered_map<const ConditionalExpressionSyntax*, bool> nodesToChange;
    const std::shared_ptr<SyntaxTree> tree;
    size_t cutCount;
public:
    TernaryRemover(const::std::shared_ptr<SyntaxTree> tree);
    void handle(const ConditionalExpressionSyntax&);
    std::vector<std::shared_ptr<SyntaxTree>> removeAllTernaries();
    std::shared_ptr<SyntaxTree> removeTernaryIndex(const std::vector<size_t>& indicesToRemove);
    size_t getCutCount() const { return cutCount; }
};

class TernaryCollector : public SyntaxVisitor<TernaryCollector> {
private:
    std::vector<const ConditionalExpressionSyntax*> foundNodes;

public:
    void handle(const ConditionalExpressionSyntax&);
    std::vector<const ConditionalExpressionSyntax*> getFoundNodes(const std::shared_ptr<SyntaxTree>);
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
    std::vector<const ConditionalStatementSyntax*> ifNodes;
    std::unordered_map<const ConditionalStatementSyntax*, bool> nodesToChange;
    const std::shared_ptr<SyntaxTree> tree;
    size_t cutCount;
public:
    IfRemover(const::std::shared_ptr<SyntaxTree> tree);
    void handle(const ConditionalStatementSyntax&);
    std::vector<std::shared_ptr<SyntaxTree>> removeAllIfs();
    std::shared_ptr<SyntaxTree> removeIfIndex(const std::vector<size_t>& indicesToRemove);
    size_t getCutCount() const { return cutCount; }
};

class IfCollector : public SyntaxVisitor<IfCollector> {
private:
    std::vector<const ConditionalStatementSyntax*> foundNodes;

public:
    void handle(const ConditionalStatementSyntax&);
    std::vector<const ConditionalStatementSyntax*> getFoundNodes(const std::shared_ptr<SyntaxTree>);
};

// MARK: Case
class CaseRemover : public PapercutsRewriter<CaseRemover> {
private:
    std::vector<std::pair<const CaseStatementSyntax*, size_t>> caseNodes; // (case statement, prunable item index)
    std::unordered_map<const CaseStatementSyntax*, std::unordered_set<size_t>> nodesToChange;
    const std::shared_ptr<SyntaxTree> tree;
    size_t cutCount;
public:
    CaseRemover(const::std::shared_ptr<SyntaxTree> tree);
    void handle(const CaseStatementSyntax&);
    std::vector<std::shared_ptr<SyntaxTree>> removeAllCases();
    std::shared_ptr<SyntaxTree> removeCaseIndex(const std::vector<size_t>& indicesToRemove);
    size_t getCutCount() const { return cutCount; }
};

class CaseCollector : public SyntaxVisitor<CaseCollector> {
private:
    std::vector<std::pair<const CaseStatementSyntax*, size_t>> foundNodes; // Only for case statements with a default

public:
    void handle(const CaseStatementSyntax&);
    std::vector<std::pair<const CaseStatementSyntax*, size_t>> getFoundNodes(const std::shared_ptr<SyntaxTree>);
};

// MARK: Binop
class BinopRemover : public PapercutsRewriter<BinopRemover> {
private:
    std::vector<std::pair<const BinaryExpressionSyntax*, bool>> binopNodes; // (binary expr, keepLeft)
    std::unordered_map<const BinaryExpressionSyntax*, bool> nodesToChange;
    const std::shared_ptr<SyntaxTree> tree;
    size_t cutCount;
public:
    BinopRemover(const::std::shared_ptr<SyntaxTree> tree);
    void handle(const BinaryExpressionSyntax&);
    std::vector<std::shared_ptr<SyntaxTree>> removeAllBinops();
    std::shared_ptr<SyntaxTree> removeBinopIndex(const std::vector<size_t>& indicesToRemove);
    size_t getCutCount() const { return cutCount; }
};

class BinopCollector : public SyntaxVisitor<BinopCollector> {
private:
    std::vector<std::pair<const BinaryExpressionSyntax*, bool>> foundNodes; // (binary expr, keepLeft); shifts keep-left only
    // When true, only collect binops that sit inside the condition of an `if`
    // statement or ternary (`?:`) -- i.e. within a ConditionalPredicateSyntax.
    // Binops in branch bodies, assignment RHSs, etc. are skipped.
    bool conditionsOnly = false;
    // Nesting depth of ConditionalPredicateSyntax around the node being visited.
    // >0 means "currently inside a conditional's condition expression".
    int predicateDepth = 0;

public:
    explicit BinopCollector(bool conditionsOnly = false) : conditionsOnly(conditionsOnly) {}
    void handle(const BinaryExpressionSyntax&);
    void handle(const ConditionalPredicateSyntax&);
    std::vector<std::pair<const BinaryExpressionSyntax*, bool>> getFoundNodes(const std::shared_ptr<SyntaxTree>);
};

// MARK: Papercutter

class Papercutter: public PapercutsRewriter<Papercutter> {
private:
    std::shared_ptr<SyntaxTree> tree;
    size_t cutCount = 0;
    size_t BSRCount = 0;
    size_t TRCount = 0;
    size_t IRCount = 0;
    size_t CRCount = 0;
    size_t BRCount = 0;

    // Bit-shrink strategy. When false (default), a shrink narrows the declaration
    // in place (e.g. `logic [7:0] x;` -> `logic [6:0] x;`). When true, it keeps
    // the legacy behavior: introduce an intermediate `x_papercuts` wire with its
    // MSB forced to 0 and redirect all reads to it.
    bool shrinkWithIntermediate = false;

    // When true, the binop cut family only targets binops inside the condition
    // of an `if` statement or ternary (`?:`); binops elsewhere are not collected.
    // Other cut families are unaffected.
    bool binopsInConditionsOnly = false;

    // Bit shrinker variables
    std::vector<BitShrinkTarget> shrinkNodes; // One entry per shrinkable packed dimension (order = cut order)
    // Active cut(s), keyed by declarator -> the packed dimensions to narrow this
    // run. A vector (not a single value) so several dimensions of one signal can
    // be narrowed together, e.g. combining two dim-cuts of `logic [3:0][7:0] x`.
    std::unordered_map<const DeclaratorSyntax*, std::vector<BitShrinkTarget>> runMap;
    std::unordered_set<std::string> nodesToShrink; // Set to store the names of the nodes we want to shrink bits in

    // Ternary remover variables
    std::vector<const ConditionalExpressionSyntax*> ternaryNodes;
    std::unordered_map<const ConditionalExpressionSyntax*, bool> ternaryNodesToChange;

    // If remover variables
    std::vector<const ConditionalStatementSyntax*> ifNodes;
    std::unordered_map<const ConditionalStatementSyntax*, bool> ifNodesToChange;

    // Case remover variables
    std::vector<std::pair<const CaseStatementSyntax*, size_t>> caseNodes;
    std::unordered_map<const CaseStatementSyntax*, std::unordered_set<size_t>> caseNodesToChange;

    // Binop remover variables
    std::vector<std::pair<const BinaryExpressionSyntax*, bool>> binopNodes; // (binary expr, keepLeft)
    std::unordered_map<const BinaryExpressionSyntax*, bool> binopNodesToChange;

    void clearState() {
        nodesToShrink.clear();
        runMap.clear();
        ternaryNodesToChange.clear();
        ifNodesToChange.clear();
        caseNodesToChange.clear();
        binopNodesToChange.clear();
    }

    // Populate the *NodesToChange maps for the given global cut indices
    void selectCuts(const std::vector<size_t>& indicesToCut);
public:
    Papercutter(const std::shared_ptr<SyntaxTree> tree, bool shrinkWithIntermediate = false,
                bool binopsInConditionsOnly = false);
    std::vector<std::shared_ptr<SyntaxTree>> cutAll();
    std::shared_ptr<SyntaxTree> cutIndex(std::vector<size_t> indicesToCut);
    // Like cutIndex(...) followed by print_tree, but skips the re-parse: returns
    // the cut source directly for fast printing on the python side
    std::string cutIndexText(std::vector<size_t> indicesToCut);
    // Per-cut (type, line) aligned 1:1 with cutAll() indices. Line numbers are
    // relative to the source tree this Papercutter was constructed from.
    std::vector<std::pair<std::string, size_t>> cutInfo();

    std::vector<std::shared_ptr<SyntaxTree>> shrinkAllBits();
    std::vector<std::shared_ptr<SyntaxTree>> removeAllTernaries();
    std::vector<std::shared_ptr<SyntaxTree>> removeAllIfs();
    std::vector<std::shared_ptr<SyntaxTree>> removeAllCases();
    std::vector<std::shared_ptr<SyntaxTree>> removeAllBinops();

    void handle(const DataDeclarationSyntax& node);
    void handle(const NetDeclarationSyntax& node);
    void handle(const DeclaratorSyntax& node);
    void handle(const IdentifierNameSyntax& node);
    void handle(const IdentifierSelectNameSyntax& node);
    void handle(const SyntaxNode& node);
    void handle(const ConditionalExpressionSyntax&);
    void handle(const ConditionalStatementSyntax&);
    void handle(const CaseStatementSyntax&);
    void handle(const BinaryExpressionSyntax& node);

    size_t getCutCount() const { return cutCount; }
};

} // namespace papercuts