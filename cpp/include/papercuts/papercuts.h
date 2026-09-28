#pragma once
#include <cstddef>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <unordered_set>
#include <unordered_map>
#include <utility>
#include <vector>

#include "slang/parsing/TokenKind.h"
#include "slang/syntax/AllSyntax.h"
#include "slang/syntax/SyntaxKind.h"
#include "slang/syntax/SyntaxNode.h"
#include "slang/syntax/SyntaxTree.h"
#include "slang/syntax/SyntaxPrinter.h"
#include "slang/syntax/SyntaxRewriter.h"
#include "slang/syntax/SyntaxVisitor.h"

#include "papercuts/utils.h"

using namespace slang::syntax;
using namespace slang::parsing;

namespace papercuts {
struct MuxContext {
    int muxCount = 0;
    // Selects a muxer actually emitted a control for. Every index in [0, muxCount)
    // is RESERVED and becomes a port, but a family that is disabled -- or a target
    // its muxer cannot build, such as a parameterized bit-shrink -- leaves its
    // select driving nothing. The difference is what the mux manifest reports as
    // `inserted: false`, so a consumer never mistakes an inert select for a cut
    // that was actually exercised.
    std::vector<size_t> insertedSelects;
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
    // Syntax lists are not nodes: their elements occupy the owning node's flat
    // child index space, so every node child here gets `node` as its parent.
    void visit(SyntaxNode& node) {
        for (size_t i = 0; i < node.getChildCount(); i++) {
            if (auto child = node.childNode(i)) { // If not a token
                child->parent = &node;
                visit(*child);
            }
        }
    }
};

/// Source text of a syntax list, trivia included (lists no longer have toString()).
template<typename TList>
std::string listToString(const TList& list) {
    slang::syntax::SyntaxPrinter printer;
    for (size_t i = 0; i < list.getChildCount(); i++) {
        auto child = list.getChild(i);
        if (child.isNode())
            printer.print(*child.node());
        else
            printer.print(child.token());
    }
    return printer.str();
}


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

// Renames the module type of instantiations from an explicit old -> new map:
// with {"adder": "adder_muxed"}, `adder u0 (...)` becomes `adder_muxed u0 (...)`.
// Unlike SubmoduleRenamer this derives nothing from the parent and never splits a
// multi-instance declaration, because every instance of one module type gets the
// same new name. That is what a whole-hierarchy rename (e.g. the muxed copy of a
// design, which must coexist with the original in one miter) needs.
class InstanceTypeRenamer : public SyntaxRewriter<InstanceTypeRenamer> {
private:
    std::unordered_map<std::string, std::string> renames;
public:
    explicit InstanceTypeRenamer(std::unordered_map<std::string, std::string> renames);
    void handle(const HierarchyInstantiationSyntax& node);
    std::shared_ptr<SyntaxTree> apply(const std::shared_ptr<SyntaxTree> tree);
};

class InputAdder: public SyntaxRewriter<InputAdder> { 
private:
    int numInputs = 0;
public:
    void handle(const PortListSyntax& node);
    std::shared_ptr<SyntaxTree> addInputs(std::shared_ptr<SyntaxTree> tree, int numInputs);
};

// MARK: Base functions

// `caseMux` and `binopMux` do not insert controls yet -- they only reserve their
// families' select numbers, so that the families after them stay aligned with cut
// indices and stay aligned once those muxers land. See insertMuxes' definition.
// Names of the modules instantiated inside `tree`, in source order, deduplicated.
std::vector<std::string> getInstantiatedModules(const std::shared_ptr<SyntaxTree> tree);

// See MuxWirer.
std::shared_ptr<SyntaxTree> wireMuxHierarchy(
    const std::shared_ptr<SyntaxTree> tree, const std::vector<std::string>& extraPorts,
    const std::map<std::string, std::vector<std::pair<std::string, std::string>>>& conns);

// `symbolicRanges` is the same map Papercutter takes: signal -> the (left, right)
// each packed dimension evaluates to, for ranges whose bounds are not literals.
// It MUST match what the cutter was given, because the bit-shrink band's width is
// computed from it on both sides; a mismatch shifts every later family's selects
// off its cut index (which is what `--in-situ` used to do, silently).
// `insertedOut`, when non-null, receives the sorted select numbers a control was
// actually emitted for -- the manifest's source of truth for which cuts the muxed
// design can exercise.
std::shared_ptr<SyntaxTree> insertMuxes(
    const std::shared_ptr<SyntaxTree> tree, bool bitMux, bool ternaryMux, bool ifMux, bool caseMux = false,
    bool binopMux = false, bool constForceMux = false, bool binopsInConditionsOnly = false,
    const std::unordered_map<std::string, std::vector<std::pair<int, int>>>& symbolicRanges = {},
    bool shrinkWithIntermediate = false, std::vector<size_t>* insertedOut = nullptr);

std::shared_ptr<SyntaxTree> renameModule(const std::shared_ptr<SyntaxTree> tree, std::string newName);

std::shared_ptr<SyntaxTree> renameSubmodules(const std::shared_ptr<SyntaxTree> tree,
                                             const std::vector<std::string>& excluded = {});

// See InstanceTypeRenamer. Instantiations whose type is absent from `renames`
// are left untouched.
std::shared_ptr<SyntaxTree> renameInstanceTypes(const std::shared_ptr<SyntaxTree> tree,
                                                const std::map<std::string, std::string>& renames);

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
            return SeparatedSyntaxList<TNode>{};

        slang::SmallVector<TokenOrSyntax> buffer;
        const size_t count = separator ? (nodes.size() * 2 - 1) : nodes.size();
        buffer.reserve(count);

        for (size_t i = 0; i < nodes.size(); ++i) {
            buffer.push_back(nodes[i]);
            if (separator && i + 1 < nodes.size())
                buffer.push_back(separator->deepClone(this->alloc));
        }

        return SeparatedSyntaxList<TNode>(this->alloc, buffer);
    }

    template<typename TNode>
    SyntaxList<TNode> makeSyntaxList(std::span<TNode* const> nodes) {
        if (nodes.empty())
            return SyntaxList<TNode>{};

        slang::SmallVector<TNode*> buffer;
        buffer.reserve(nodes.size());
        for (TNode* node : nodes)
            buffer.push_back(node);

        return SyntaxList<TNode>(this->alloc, buffer);
    }

    // MARK: expression construction
    //
    // These build replacements out of EXISTING nodes rather than re-parsed text.
    // That distinction is load-bearing: slang's CloneVisitor resolves a replacement
    // by visiting it (resolveReplacement), so a pending change *inside* a reused
    // operand still gets applied. Building the same expression by parsing
    // `node->toString()` produces a fresh subtree instead, and every nested
    // replacement under it is silently dropped.

    /// A single leading space, so generated operators don't jam against operands.
    std::span<const Trivia> spaced() { return {&SyntaxRewriter<TDerived>::SingleSpace, 1}; }

    IdentifierNameSyntax& makeIdentExpr(const std::string& name, bool leadingSpace = false) {
        return factory.identifierName(
            this->makeId(persistString(this->alloc, name), leadingSpace ? spaced() : std::span<const Trivia>{}));
    }

    ParenthesizedExpressionSyntax& makeParen(ExpressionSyntax& expr, bool leadingSpace = false) {
        return factory.parenthesizedExpression(
            makeToken(TokenKind::OpenParenthesis, "(", leadingSpace ? spaced() : std::span<const Trivia>{}),
            expr, makeToken(TokenKind::CloseParenthesis, ")"));
    }

    ExpressionSyntax& makeNot(ExpressionSyntax& operand) {
        return factory.prefixUnaryExpression(SyntaxKind::UnaryLogicalNotExpression,
                                             makeToken(TokenKind::Exclamation, "!"),
                                             SyntaxList<AttributeInstanceSyntax>{}, operand);
    }

    ExpressionSyntax& makeBinary(SyntaxKind kind, ExpressionSyntax& left, TokenKind opKind,
                                 std::string_view opText, ExpressionSyntax& right) {
        return factory.binaryExpression(kind, left, makeToken(opKind, opText, spaced()),
                                        SyntaxList<AttributeInstanceSyntax>{}, right);
    }

    /// Rebuild a binary expression around its original operator token (and so its
    /// original spacing), reusing both operand nodes.
    ExpressionSyntax& makeBinary(SyntaxKind kind, ExpressionSyntax& left, Token operatorToken,
                                 ExpressionSyntax& right) {
        return factory.binaryExpression(kind, left, operatorToken,
                                        SyntaxList<AttributeInstanceSyntax>{}, right);
    }

    ExpressionSyntax& makeTernary(ExpressionSyntax& predicate, ExpressionSyntax& whenTrue,
                                  ExpressionSyntax& whenFalse) {
        return factory.conditionalExpression(makeConditionalPredicate(predicate),
                                             makeToken(TokenKind::Question, "?", spaced()),
                                             SyntaxList<AttributeInstanceSyntax>{}, whenTrue,
                                             makeColon(spaced()), whenFalse);
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
// The declared type of a variable or net declaration, spelled back out so a
// companion signal can be declared to match it exactly -- signing, net type and
// every packed dimension included.
struct DeclShape {
    std::string typeHead;
    const SyntaxList<VariableDimensionSyntax>* dims = nullptr;
    bool isSigned = false;
};

// like `logic [3:0][7:0] x;` yields one target per packed dimension (dimIndex 0
// -> [3:0], dimIndex 1 -> [7:0]); a plain `logic [7:0] y;` yields a single
// target with dimIndex 0. `width` is that dimension's bit count, kept for cut
// reporting and the legacy intermediate-wire strategy. `dimIndex` is the index
// into the type's packed dimension list (0 = leftmost/outermost).
struct BitShrinkTarget {
    const DeclaratorSyntax* decl;
    int width;
    int dimIndex;
    // Bits to drop off the high end of this packed dimension. Default 1 (the
    // classic one-bit shrink); the iterative shrink path sets it higher to
    // narrow the same dimension by several bits at once.
    int amount = 1;
};

// Adds forwarded select ports to a module and connects them through to the
// instances that need them. Cuts belong to a module DEFINITION, so every instance
// of a module shares one set of selects: a parent forwards one signal per
// (module, cut index) rather than one per instance.
class MuxWirer : public PapercutsRewriter<MuxWirer> {
private:
    // Ports to append, already named.
    const std::vector<std::string>* extraPorts = nullptr;
    // Instantiated module name -> the (child port, parent signal) pairs to add to
    // each of its instances.
    const std::map<std::string, std::vector<std::pair<std::string, std::string>>>* conns = nullptr;

public:
    std::shared_ptr<SyntaxTree> wire(
        const std::shared_ptr<SyntaxTree> tree, const std::vector<std::string>& extraPorts,
        const std::map<std::string, std::vector<std::pair<std::string, std::string>>>& conns);
    void handle(const PortListSyntax& node);
    void handle(const HierarchyInstantiationSyntax& node);
};

class BitMuxer : public PapercutsRewriter<BitMuxer> {
private:
    bool initialized = false;
    MuxContext& context;
    // The same targets, in the same order, that Papercutter enumerates as bit-shrink
    // cuts. A select is allocated per target rather than per declarator, so a
    // multi-packed-dim signal gets one per dimension exactly as it gets one cut per
    // dimension.
    std::vector<BitShrinkTarget> shrinkNodes;
    // Declarator -> its indices into shrinkNodes. Keyed by pointer, so initialize()
    // must run on the tree that will be transformed.
    std::unordered_map<const DeclaratorSyntax*, std::vector<size_t>> targetsByDecl;
    // Names that got a companion signal, for redirecting reads onto it.
    std::unordered_set<std::string> muxedNames;
    // context.muxCount when the bit pass began; select for shrinkNodes[i] is
    // baseSel + i, which keeps selects aligned with cut indices.
    size_t baseSel = 0;

    void muxDeclaration(const SyntaxNode& node, const SeparatedSyntaxList<DeclaratorSyntax>& declarators,
                        const DeclShape& shape);
public:
    BitMuxer(MuxContext& context) : context(context) {}
    std::shared_ptr<SyntaxTree> insertBitShrinkMuxes(const std::shared_ptr<SyntaxTree>, size_t baseSel);
    void initialize(const std::shared_ptr<SyntaxTree>,
                    const std::unordered_map<std::string, std::vector<std::pair<int, int>>>& symbolicRanges = {});
    void handle(const DataDeclarationSyntax& node);
    void handle(const NetDeclarationSyntax& node);
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
    // Signal name -> the (left, right) each of its packed dimensions actually
    // evaluates to, outermost dimension first, for ranges whose bounds are not
    // literals (`[WIDTH-1:0]`). Such a dimension is only shrinkable when these are
    // known: they give both its true width and, crucially, its direction. A
    // reversed range is legal SV -- `[W-1:0]` with a placeholder `parameter W = 0`
    // is `[-1:0]`, a 2-bit range whose *left* is the low end -- so subtracting from
    // the symbolic bound would silently widen it. One entry per dimension lets a
    // multi-packed-dim vector be shrunk on every dimension, not just literal ones.
    std::unordered_map<std::string, std::vector<std::pair<int, int>>> symbolicRanges;
public:
    // Signed declarations, nets and multi-packed-dim vectors are all shrinkable by
    // both strategies -- narrow-in-place rebuilds the range, and the intermediate
    // wire names the retained bits -- so there is nothing left to gate them on.
    explicit BitShrinkCollector(
        std::unordered_map<std::string, std::vector<std::pair<int, int>>> symbolicRanges = {})
        : symbolicRanges(std::move(symbolicRanges)) {}
    void handle(const DeclaratorSyntax&);
    std::vector<BitShrinkTarget> getFoundNodes(const std::shared_ptr<SyntaxTree>);
};

// MARK: ExprMuxer
//
// One pass for every expression-level mux family (ternary, if, and later binop and
// case). Two properties make it correct where the old per-family passes were not:
//
//  * It collects on the tree it will transform and never re-collects. The old
//    passes ran in sequence, and each one's output contains `|`, `&` and `?:` of
//    its own -- so a later collector saw operators the cutter never enumerated.
//  * It builds replacements from the ORIGINAL operand nodes rather than from
//    re-parsed text, so a mux nested inside another mux's operand survives.
//
// Select numbers come from explicit per-family base offsets rather than a running
// counter, so a family's numbering does not depend on which other families ran.
class ExprMuxer : public PapercutsRewriter<ExprMuxer> {
private:
    bool initialized = false;
    // Node -> the first of its selects; the second (where a family has two) is +1.
    std::unordered_map<const ConditionalExpressionSyntax*, size_t> ternarySel;
    std::unordered_map<const ConditionalStatementSyntax*, size_t> ifSel;
    // Binop -> (keep-left select, keep-right select). Shifts collect keep-left only,
    // so the second is absent for those.
    std::unordered_map<const BinaryExpressionSyntax*, std::pair<size_t, std::optional<size_t>>> binopSel;
    // Case statement -> {prunable item index -> its select}. The default item is
    // never prunable and so never appears here.
    std::unordered_map<const CaseStatementSyntax*, std::unordered_map<size_t, size_t>> caseSel;

    // Replacement predicate for a ternary or an `if`: sel1 | (!sel0 & (<pred>)).
    // sel0 forces the false branch, sel1 the true branch -- matching the even/odd
    // cut order for both families.
    ExpressionSyntax& muxPredicate(const ConditionalPredicateSyntax& pred, size_t sel);

public:
    ExprMuxer() = default;
    std::shared_ptr<SyntaxTree> insertExprMuxes(const std::shared_ptr<SyntaxTree>);
    // `baseTernary` / `baseIf` are the select numbers this tree's first ternary and
    // first `if` cut occupy.
    // Selects this pass emitted a control for; see MuxContext::insertedSelects.
    std::vector<size_t> insertedSelects;
    void initialize(const std::shared_ptr<SyntaxTree>, bool ternaryMux, size_t baseTernary,
                    bool ifMux, size_t baseIf, bool binopMux, size_t baseBinop,
                    bool binopsInConditionsOnly, bool caseMux, size_t baseCase);
    void handle(const ConditionalExpressionSyntax& node);
    void handle(const ConditionalStatementSyntax& node);
    void handle(const BinaryExpressionSyntax& node);
    void handle(const CaseStatementSyntax& node);
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

// Every `if` that is a cut site. The exception is the reset test of an
// asynchronous block: in `always @(posedge clk or posedge rst) if (rst) ...` the
// condition has to stay a bare reference to a signal in the event list, or the
// asynchronous load pattern is gone and the result is not synthesizable -- yosys
// rejects it outright ("condition cannot be matched to any signal from the event
// list"). Both the mux form and the cut form break it, so the site is not a cut
// at all. Only the condition that names an edge signal is skipped; ifs nested
// inside the same block are ordinary cut sites, because the outermost `if` is
// the one the pattern depends on.
class IfCollector : public SyntaxVisitor<IfCollector> {
private:
    std::vector<const ConditionalStatementSyntax*> foundNodes;
    // Signals carrying an edge in the enclosing block's event list, when there is
    // more than one such event (which is what makes the block asynchronous).
    std::unordered_set<std::string> asyncEdgeSignals;

public:
    void handle(const ProceduralBlockSyntax&);
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

class ConstForceMuxer : public PapercutsRewriter<ConstForceMuxer> {
private:
    bool initialized = false;
    MuxContext& context;
    // The 1-bit scalars Papercutter enumerates as const-force cuts, in cut order.
    // Keyed by pointer, so initialize() must run on the tree being transformed.
    std::unordered_map<const DeclaratorSyntax*, size_t> ordinalByDecl;
    size_t declCount = 0;
    // Names that got a companion signal, for redirecting reads onto it.
    std::unordered_set<std::string> muxedNames;
    // context.muxCount when this pass began; the pair of selects for signal i is
    // baseSel + 2*i (force 0) and baseSel + 2*i + 1 (force 1), matching cut order.
    size_t baseSel = 0;

    void muxDeclaration(const SyntaxNode& node, const SeparatedSyntaxList<DeclaratorSyntax>& declarators,
                        const DeclShape& shape);
public:
    ConstForceMuxer(MuxContext& context) : context(context) {}
    std::shared_ptr<SyntaxTree> insertConstForceMuxes(const std::shared_ptr<SyntaxTree>, size_t baseSel);
    void initialize(const std::shared_ptr<SyntaxTree>);
    void handle(const DataDeclarationSyntax& node);
    void handle(const NetDeclarationSyntax& node);
    void handle(const IdentifierNameSyntax& node);
};

// MARK: ForceConst
class ConstForceCollector : public SyntaxVisitor<ConstForceCollector> {
private:
    std::vector<const DeclaratorSyntax*> foundNodes; // 1-bit scalar logic/reg/bit + nets

public:
    void handle(const DeclaratorSyntax&);
    std::vector<const DeclaratorSyntax*> getFoundNodes(const std::shared_ptr<SyntaxTree>);
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
    size_t CFRCount = 0;

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

    // Const-force variables
    std::vector<const DeclaratorSyntax*> constForceNodes;    // 1-bit scalars, one per signal
    std::unordered_map<std::string, bool> constForceActive;  // active run: signal name -> polarity (true=1)

    void clearState() {
        nodesToShrink.clear();
        runMap.clear();
        ternaryNodesToChange.clear();
        ifNodesToChange.clear();
        caseNodesToChange.clear();
        binopNodesToChange.clear();
        constForceActive.clear();
    }

    // Populate the *NodesToChange maps for the given global cut indices.
    // `amounts` optionally overrides the shrink amount (bits to drop) for any
    // bitshrink index; indices absent from the map default to 1 bit. Non-bitshrink
    // indices ignore it.
    // Emit the intermediate-wire bit-shrink form for every targeted declarator of
    // one declaration: a companion signal of the same declared type whose dropped
    // bits are forced, inserted directly after the original. Shared by the
    // variable and net handlers, which differ only in how `typeHead` is spelled.
    void emitIntermediateWires(const SyntaxNode& decl,
                               const SeparatedSyntaxList<DeclaratorSyntax>& declarators,
                               const DeclShape& shape);

    void selectCuts(const std::vector<size_t>& indicesToCut,
                    const std::unordered_map<size_t, int>& amounts = {});
public:
    // `symbolicRanges` maps signal name -> the (left, right) each of its packed
    // dimensions evaluates to (outermost first), enabling bit-shrink on
    // declarations whose range is parameterized (`logic [WIDTH-1:0] x;`), including
    // every dimension of a multi-packed-dim vector. Empty (the default) keeps such
    // declarations uncut.
    Papercutter(const std::shared_ptr<SyntaxTree> tree, bool shrinkWithIntermediate = false,
                bool binopsInConditionsOnly = false,
                std::unordered_map<std::string, std::vector<std::pair<int, int>>> symbolicRanges = {});
    std::vector<std::shared_ptr<SyntaxTree>> cutAll();
    // `amounts`: optional per-bitshrink-index override of the number of bits to
    // drop (default 1). Enables iterative multi-bit shrinking; other cut families
    // and unlisted bitshrink indices are unaffected.
    std::shared_ptr<SyntaxTree> cutIndex(std::vector<size_t> indicesToCut,
                                         std::unordered_map<size_t, int> amounts = {});
    // Like cutIndex(...) followed by print_tree, but skips the re-parse: returns
    // the cut source directly for fast printing on the python side
    std::string cutIndexText(std::vector<size_t> indicesToCut,
                             std::unordered_map<size_t, int> amounts = {});
    // Per-cut (type, line) aligned 1:1 with cutAll() indices. Line numbers are
    // relative to the source tree this Papercutter was constructed from.
    // Width of each cut band, in cutInfo() order: ternary, if, bitshrink, case,
    // binop, force-const. THE place these widths are decided. insertMuxes takes
    // its select offsets from here rather than recounting, because when the two
    // disagreed -- over a parameterized bit-shrink the cutter counts and the muxer
    // cannot build -- every select after that band silently stopped matching its
    // cut index.
    std::vector<size_t> cutBands() const {
        return {TRCount, IRCount, BSRCount, CRCount, BRCount, CFRCount};
    }

    std::vector<std::pair<std::string, size_t>> cutInfo();
    // Cut index pairs that are mutually exclusive because they are the two halves
    // of one site (ternary/if keep-false + keep-true, a binop's keep-left +
    // keep-right, force-const 0 + 1). Turning both on is just the dominant one, so
    // counting both inflates the cut count. Built from the same node lists, in the
    // same order, as cutInfo() -- never inferred from the log's line numbers.
    std::vector<std::pair<size_t, size_t>> cutPairs();
    // Per-cut shrinkable width aligned 1:1 with cutAll() indices: for a bitshrink
    // cut, the current bit width of the targeted packed dimension (so the caller
    // can bound an iterative shrink at width-1); 0 for every non-bitshrink cut.
    std::vector<size_t> cutShrinkWidths();

    std::vector<std::shared_ptr<SyntaxTree>> shrinkAllBits();
    std::vector<std::shared_ptr<SyntaxTree>> removeAllTernaries();
    std::vector<std::shared_ptr<SyntaxTree>> removeAllIfs();
    std::vector<std::shared_ptr<SyntaxTree>> removeAllCases();
    std::vector<std::shared_ptr<SyntaxTree>> removeAllBinops();
    std::vector<std::shared_ptr<SyntaxTree>> removeAllConstForces();

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