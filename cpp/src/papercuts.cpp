#include "papercuts/papercuts.h"

#include "papercuts/utils.h"
#include <iostream>
#include <memory>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

#include "slang/parsing/TokenKind.h"
#include "slang/syntax/AllSyntax.h"
#include "slang/syntax/SyntaxKind.h"
#include "slang/syntax/SyntaxNode.h"
#include "slang/syntax/SyntaxPrinter.h"
#include "slang/syntax/SyntaxTree.h"
#include "slang/syntax/SyntaxVisitor.h"
#include "slang/text/SourceManager.h"
#include "slang/util/Util.h"

using namespace slang::syntax;
using namespace slang::parsing;

namespace papercuts {
void ModuleNameRewriter::handle(const ModuleHeaderSyntax& node) {
    auto pstring = persistString(this->alloc, this->newName);
    auto newToken = this->makeToken(TokenKind::Identifier, pstring);

    this->replaceToken(node, 2, newToken, true);
}

std::shared_ptr<SyntaxTree> ModuleNameRewriter::renameModule(const std::shared_ptr<SyntaxTree> tree,
                                                             std::string newName) {
    this->newName = newName;
    return this->transform(tree);
}

void ModuleNameFinder::handle(const ModuleHeaderSyntax& node) {
    this->moduleName = std::string(node.name.valueText());
}

std::string ModuleNameFinder::getModuleName(const std::shared_ptr<SyntaxTree> tree) {
    visit(tree->root());
    if (moduleName.empty()) {
        throw std::runtime_error("No module declaration found in the syntax tree");
    }
    return moduleName;
}

SubmoduleRenamer::SubmoduleRenamer(const std::shared_ptr<SyntaxTree> tree, std::unordered_set<std::string> excluded)
    : tree(tree), excluded(std::move(excluded)) {
    ModuleNameFinder finder;
    this->moduleName = finder.getModuleName(tree);
}

void SubmoduleRenamer::handle(const HierarchyInstantiationSyntax& node) {
    // Leave excluded modules' instantiations untouched: they keep their original
    // module name (and #(...) overrides) so the verbatim excluded definition, which
    // is emitted under its original name, still resolves.
    if (excluded.contains(std::string(node.type.valueText()))) {
        return;
    }
    if (node.instances.size() == 1) { // if there's only one instance we can just rename without splitting it out
        auto newType =
            makeToken(TokenKind::Identifier,
                      persistString(alloc, moduleName + "_" + std::string(node.instances[0]->decl->name.valueText())));

        replaceToken(node, 1, newType, true);
    }
    else {
        for (const auto& instance : node.instances) {
            std::string oldTriviaText;
            for (const auto& t : node.getFirstToken().trivia())
                oldTriviaText += t.getRawText();

            auto& newInst = parse(persistString(
                alloc, oldTriviaText + (node.attributes.size() > 0 ? node.attributes.toString() + " " : "") +
                           moduleName + "_" + std::string(instance->decl->name.valueText()) +
                           (node.parameters ? node.parameters->toString() : "") + " " +
                           std::string(instance->decl->name.valueText()) + " (" + instance->connections.toString() +
                           ");"));

            insertBefore(node, newInst);
            std::cout << "Inserted new instance: " << newInst.toString() << std::endl;
        }
        remove(node);
    }
}

std::shared_ptr<SyntaxTree> SubmoduleRenamer::renameSubmodules() {
    return this->transform(tree);
}

std::shared_ptr<SyntaxTree> renameSubmodules(const std::shared_ptr<SyntaxTree> tree,
                                             const std::vector<std::string>& excluded) {
    SubmoduleRenamer rewriter(tree, std::unordered_set<std::string>(excluded.begin(), excluded.end()));
    return rewriter.renameSubmodules();
}

std::shared_ptr<SyntaxTree> renameModule(const std::shared_ptr<SyntaxTree> tree, std::string newName) {
    ModuleNameRewriter rewriter;
    return rewriter.renameModule(tree, newName);
}

std::string getModuleName(const std::shared_ptr<SyntaxTree> tree) {
    ModuleNameFinder finder;
    return finder.getModuleName(tree);
}

std::shared_ptr<SyntaxTree> insertMuxes(const std::shared_ptr<SyntaxTree> tree, bool bitMux, bool ternaryMux,
                                        bool ifMux) {
    std::shared_ptr<SyntaxTree> newTree = tree;
    std::vector<std::shared_ptr<SyntaxTree>> keepAlive;

    MuxContext context;
    

    BitMuxer BM(context);
    TernaryMuxer TM(context);
    IfMuxer IM(context);
    ParentSetter PS;

    if (bitMux) {
        BM.initialize(tree);
    }
    if (ternaryMux) {
        auto transformed = TM.insertTernaryMuxes(newTree);
        keepAlive.push_back(newTree);
        newTree = transformed;
    }
    // PS.visit(newTree->root());
    if (ifMux) {
        auto transformed = IM.insertIfMuxes(newTree);
        keepAlive.push_back(newTree);
        newTree = transformed;
    }
    PS.visit(newTree->root());
    if (bitMux) {
        auto transformed = BM.insertBitShrinkMuxes(newTree);
        keepAlive.push_back(newTree);
        newTree = transformed;
    }

    InputAdder IA;
    {
        auto transformed = IA.addInputs(newTree, context.muxCount);
        keepAlive.push_back(newTree);
        newTree = transformed;
    }

    auto stabilized = SyntaxPrinter::printFile(*newTree);
    newTree = SyntaxTree::fromText(stabilized, tree->sourceManager());

    return newTree;
}

void InputAdder::handle(const PortListSyntax& node) {
    if (node.kind == SyntaxKind::NonAnsiPortList || node.kind == SyntaxKind::WildcardPortList)
        throw std::logic_error("Papercuts only supports ANSI port lists");

    auto& ansiNode = node.as<AnsiPortListSyntax>();
    std::string newPortStr = "input logic ";
    for (int i = 0; i < numInputs; i++) {
        newPortStr += "pc_sel" + std::to_string(i);
        if (i != numInputs - 1 || ansiNode.ports.size() > 0) {
            newPortStr += ", ";
        }
    }
    insertAtFront(ansiNode.ports, parse(newPortStr));
}
std::shared_ptr<SyntaxTree> InputAdder::addInputs(std::shared_ptr<SyntaxTree> tree, int numInputs) {
    this->numInputs = numInputs;
    return transform(tree);
}

// MARK: BitMuxer
std::shared_ptr<SyntaxTree> BitMuxer::insertBitShrinkMuxes(const std::shared_ptr<SyntaxTree> tree) {
    if (!initialized) {
        throw std::logic_error("BitMuxer must be initialized before insertBitShrinkMuxes");
    }
    initialized = false; // Reset the initialized flag for the next time we want to use this BitMuxer
    return transform(tree);
}

void BitMuxer::initialize(const std::shared_ptr<SyntaxTree> tree) {
    widthMap.clear();
    BitShrinkCollector collector;
    auto widthMapNodes = collector.getFoundNodes(tree);
    for (const auto& t : widthMapNodes) {
        widthMap[std::string(t.decl->name.valueText())] = t.width;
    }

    initialized = true;
}

void BitMuxer::handle(const DataDeclarationSyntax& node) {
    std::string oldTriviaText;
    for (const auto& t : node.getFirstToken().trivia())
        oldTriviaText += t.getRawText();

    for (const auto& decl : node.declarators) {
        std::string nodeName{decl->name.valueText()};
        auto it = widthMap.find(nodeName);
        if (it == widthMap.end())
            continue;

        std::string selector = "pc_sel" + std::to_string(context.muxCount++);
        std::string newName = nodeName + "_papercuts";

        auto& newDecl = parse(oldTriviaText + "logic[" + std::to_string(it->second - 1) + ":0] " + newName + ";");

        auto& newAssign = parse(oldTriviaText + "assign " + newName + " = {!" + selector + " & " + nodeName + "[" +
                                std::to_string(it->second - 1) + "], " + nodeName + "[" +
                                std::to_string(it->second - 2) + ":0]};");

        insertAfter(node, newDecl);
        insertAfter(node, newAssign);
    }

    this->visitDefault(node);
}

void BitMuxer::handle(const IdentifierNameSyntax& node) {
    if (node.parent && node.parent->kind == SyntaxKind::AssignmentExpression &&
        &node == node.parent->as<BinaryExpressionSyntax>().left) {
        return; // Don't replace the left side of an assignment expression
    }

    auto it = widthMap.find(std::string(node.identifier.valueText()));
    if (it != widthMap.end()) {
        std::string nodeName{node.identifier.valueText()};
        std::string newName = nodeName + "_papercuts";
        replaceToken(node, 0, makeId(persistString(alloc, newName)), true);
    }
}

void BitMuxer::handle(const IdentifierSelectNameSyntax& node) {
    if (node.parent && node.parent->kind == SyntaxKind::AssignmentExpression &&
        &node == node.parent->as<BinaryExpressionSyntax>().left) {
        return; // Don't replace the left side of an assignment expression
    }

    std::string oldName{node.identifier.valueText()};

    auto it = widthMap.find(oldName);
    if (it != widthMap.end()) {
        std::string newName = oldName + "_papercuts";
        replaceToken(node, 0, makeId(persistString(alloc, newName)), true);
    }
}

void BitMuxer::handle(const SyntaxNode& node) {
    // Check to see if this is the left side of a declaration
    if (node.parent && node.parent->kind == SyntaxKind::AssignmentExpression &&
        &node == node.parent->as<BinaryExpressionSyntax>().left) {
        return; // If it is, we don't want to replace it
    }
    visitDefault(node);
}

// MARK: BitShrinker
BitShrinker::BitShrinker(const std::shared_ptr<SyntaxTree> tree) : tree(tree) {
    // Initialize the widthMap with the widths of all the nodes we want to shrink bits in
    BitShrinkCollector collector;
    this->shrinkNodes = collector.getFoundNodes(tree);
    this->cutCount = shrinkNodes.size();
}

std::vector<std::shared_ptr<SyntaxTree>> BitShrinker::shrinkAllBits() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;
    nodesToShrink.clear();
    runMap.clear();

    for (const auto& t : shrinkNodes) {
        nodesToShrink.clear();
        runMap.clear();
        nodesToShrink.emplace(t.decl->name.valueText());
        runMap.emplace(t.decl, t.width);
        auto newTree = transform(tree);
        newTrees.emplace_back(newTree);
    }

    return newTrees;
}

std::shared_ptr<SyntaxTree> BitShrinker::shrinkBitsIndex(const std::vector<size_t>& indicesToShrink) {
    nodesToShrink.clear();
    runMap.clear();

    for (size_t i : indicesToShrink) {
        if (i >= cutCount) {
            throw std::out_of_range("Index out of range for shrinkBitsIndex");
        }
        nodesToShrink.emplace(shrinkNodes[i].decl->name.valueText());
        runMap.emplace(shrinkNodes[i].decl, shrinkNodes[i].width);
    }

    return transform(tree);
}

void BitShrinker::handle(const DeclaratorSyntax& node) {
    if (runMap.contains(&node)) {
        auto newName = std::string(node.name.valueText()) + "_papercuts";
        int newWidth = runMap[&node] - 1; // Get the width of the node and calculate the new width after shrinking
        auto& parentDecl = node.parent->as<DataDeclarationSyntax>();
        auto& type = parentDecl.type;

        auto& newDecl = factory.declarator(makeId(persistString(alloc, newName), SingleSpace),
                                           std::span<VariableDimensionSyntax*>{}, nullptr);

        auto declElem = std::span(alloc.emplace<TokenOrSyntax>(&newDecl), size_t{1});
        SeparatedSyntaxList<DeclaratorSyntax> declList(declElem);

        auto& newDataDecl = factory.dataDeclaration(std::span<AttributeInstanceSyntax*>{},
                                                    *deepClone(parentDecl.modifiers, alloc), *deepClone(*type, alloc),
                                                    declList, makeSemicolon());
        insertAfter(parentDecl, newDataDecl);

        std::string oldTriviaText;
        for (const auto& t : parentDecl.getFirstToken().trivia())
            oldTriviaText += t.getRawText();

        std::string assignText = oldTriviaText + "assign " + newName + " = {1'b0, " +
                                 std::string(node.name.valueText()) + "[" + std::to_string(newWidth - 1) + ":0]};";
        auto& newAssign = parse(assignText);

        insertAfter(parentDecl, newAssign);
    }
}

void BitShrinker::handle(const IdentifierNameSyntax& node) {
    // Check to see if this is the left side of a declaration
    if (node.parent && node.parent->kind == SyntaxKind::AssignmentExpression &&
        &node == node.parent->as<BinaryExpressionSyntax>().left) {
        return; // If it is, we don't want to replace it
    }

    if (nodesToShrink.contains(std::string(node.identifier.valueText()))) {
        replaceToken(node, 0, makeId(persistString(alloc, std::string(node.identifier.valueText()) + "_papercuts")),
                     true);
    }
}

void BitShrinker::handle(const IdentifierSelectNameSyntax& node) {
    // Check to see if this is the left side of a declaration
    if (node.parent && node.parent->kind == SyntaxKind::AssignmentExpression &&
        &node == node.parent->as<BinaryExpressionSyntax>().left) {
        return; // If it is, we don't want to replace it
    }

    if (nodesToShrink.contains(std::string(node.identifier.valueText()))) {
        replaceToken(node, 0, makeId(persistString(alloc, std::string(node.identifier.valueText()) + "_papercuts")),
                     true);
    }
}

void BitShrinker::handle(const SyntaxNode& node) {
    // Check to see if this is the left side of a declaration
    if (node.parent && node.parent->kind == SyntaxKind::AssignmentExpression &&
        &node == node.parent->as<BinaryExpressionSyntax>().left) {
        return; // If it is, we don't want to replace it
    }
    visitDefault(node);
}

void BitShrinkCollector::handle(const DeclaratorSyntax& node) {
    // Pull the shared type off the declarator's parent. logic/reg/bit live in a
    // DataDeclarationSyntax; wire/tri/... live in a NetDeclarationSyntax.
    const DataTypeSyntax* type = nullptr;
    if (auto* dataDecl = node.parent->as_if<DataDeclarationSyntax>()) {
        auto kind = dataDecl->type->kind;
        if (kind != SyntaxKind::LogicType && kind != SyntaxKind::RegType && kind != SyntaxKind::BitType) {
            return; // Only the integer vector types carry a shrinkable packed range
        }
        type = dataDecl->type;
    } else if (auto* netDecl = allowNets ? node.parent->as_if<NetDeclarationSyntax>() : nullptr) {
        if (netDecl->strength || netDecl->delay) {
            // Strength/delay can't be faithfully reproduced when we split and narrow, so skip.
            std::cout << "Skipping net with strength/delay: " << node.name.valueText() << std::endl;
            return;
        }
        type = netDecl->type;
    } else {
        return;
    }

    // logic/reg/bit (and `wire logic`) are IntegerTypeSyntax; a bare `wire [7:0]` is
    // ImplicitTypeSyntax. Both expose signing and the packed dimensions.
    Token signing;
    const SyntaxList<VariableDimensionSyntax>* dims = nullptr;
    if (auto* intType = type->as_if<IntegerTypeSyntax>()) {
        signing = intType->signing;
        dims = &intType->dimensions;
    } else if (auto* impType = type->as_if<ImplicitTypeSyntax>()) {
        signing = impType->signing;
        dims = &impType->dimensions;
    } else {
        return; // e.g. a named/struct net type we don't shrink
    }

    if (!allowSigned && signing && signing.kind != TokenKind::UnsignedKeyword) {
        // Zero-extending a signed value ({1'b0, ...}) corrupts its sign, so the
        // intermediate-wire strategy must skip signed decls. Narrowing in place
        // preserves signedness, so narrow mode allows them.
        std::cout << "Skipping signed declaration: " << node.name.valueText() << std::endl;
        return;
    }
    if (dims->size() == 0) {
        return;
    }
    if (!allowMultiDim && dims->size() > 1) {
        // Only narrow mode knows how to rebuild a multi-packed-dim range in place;
        // the intermediate-wire strategy can't, so it still skips these.
        std::cout << "Skipping multi-dimensional declaration: " << node.name.valueText() << std::endl;
        return;
    }

    // Emit one shrink target per packed dimension. Narrow mode shrinks each
    // dimension independently ([3:0][7:0] -> a cut for [3:0] and a cut for [7:0]);
    // single-dim mode only ever sees one dimension here.
    for (size_t di = 0; di < dims->size(); ++di) {
        // Only simple literal ranges are shrinkable. Anything else (wildcard/queue
        // dimensions, or bounds concretization left non-literal) is skipped per
        // dimension rather than throwing, so the other dimensions still yield cuts.
        auto* dimSpec = (*dims)[di]->specifier->as_if<RangeDimensionSpecifierSyntax>();
        if (!dimSpec)
            continue;
        auto* dimSelect = dimSpec->selector->as_if<RangeSelectSyntax>();
        if (!dimSelect)
            continue;
        auto* left = dimSelect->left->as_if<LiteralExpressionSyntax>();
        auto* right = dimSelect->right->as_if<LiteralExpressionSyntax>();
        if (!left || !right)
            continue;

        int leftVal = tokenToInt(left->literal);
        int rightVal = tokenToInt(right->literal);
        int width = std::abs(leftVal - rightVal) + 1;

        if (width <= 1) {
            continue;
        }

        shrinkNodes.emplace_back(BitShrinkTarget{&node, width, static_cast<int>(di)});
    }
}

std::vector<BitShrinkTarget> BitShrinkCollector::getFoundNodes(
    const std::shared_ptr<SyntaxTree> tree) {
    tree->root().visit(*this);

    return this->shrinkNodes;
}

// MARK: TernaryMuxer
std::shared_ptr<SyntaxTree> TernaryMuxer::insertTernaryMuxes(const std::shared_ptr<SyntaxTree> tree) {
    return transform(tree);
}

void TernaryMuxer::handle(const ConditionalExpressionSyntax& node) {
    std::string sel0 = "pc_sel" + std::to_string(context.muxCount++);
    std::string sel1 = "pc_sel" + std::to_string(context.muxCount++);
    std::string newNodeStr = "(" + sel1 + " | " + "(!" + sel0 + " & (" + node.predicate->toString() + ")))";
    auto& newNode = parse(newNodeStr);

    auto& newPred = makeConditionalPredicate(newNode.as<ExpressionSyntax>());

    replace(*node.predicate, newPred);
    visitDefault(node);
}

// MARK: TernaryRemover

TernaryRemover::TernaryRemover(const ::std::shared_ptr<SyntaxTree> tree) : tree(tree) {
    TernaryCollector collector;
    ternaryNodes = collector.getFoundNodes(tree);
    cutCount = ternaryNodes.size() * 2;
}

void TernaryRemover::handle(const ConditionalExpressionSyntax& node) {
    if (nodesToChange.contains(&node)) {
        auto replacement = nodesToChange[&node] ? node.left : node.right;
        this->replace(node, *replacement);
    }
    visitDefault(node);
}

std::shared_ptr<SyntaxTree> TernaryRemover::removeTernaryIndex(const std::vector<size_t>& indicesToRemove) {
    nodesToChange.clear();

    for (size_t i : indicesToRemove) {
        if (i >= cutCount) {
            throw std::out_of_range("Index out of range for removeTernaryIndex");
        }
        size_t nodeIndex = i / 2;
        bool removeLeft = (i % 2 != 0);
        nodesToChange.emplace(ternaryNodes[nodeIndex], removeLeft);
    }

    return transform(tree);
}

std::vector<std::shared_ptr<SyntaxTree>> TernaryRemover::removeAllTernaries() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    nodesToChange.clear();

    for (const auto& node : ternaryNodes) {
        nodesToChange.clear();
        this->nodesToChange.emplace(node, false);
        auto newTree = transform(tree);
        newTrees.emplace_back(newTree);
        nodesToChange.clear();
        this->nodesToChange.emplace(node, true);
        newTree = transform(tree);
        newTrees.emplace_back(newTree);
    }

    return newTrees;
}

void TernaryCollector::handle(const ConditionalExpressionSyntax& node) {
    this->foundNodes.emplace_back(&node);
    this->visitDefault(node);
}

std::vector<const ConditionalExpressionSyntax*> TernaryCollector::getFoundNodes(
    const std::shared_ptr<SyntaxTree> tree) {
    tree->root().visit(*this);

    return this->foundNodes;
}

// MARK: IfMuxer
std::shared_ptr<SyntaxTree> IfMuxer::insertIfMuxes(const std::shared_ptr<SyntaxTree> tree) {
    return transform(tree);
}

void IfMuxer::handle(const ConditionalStatementSyntax& node) {
    std::string sel0 = "pc_sel" + std::to_string(context.muxCount++);
    std::string sel1 = "pc_sel" + std::to_string(context.muxCount++);
    std::string newNodeStr = "(" + sel1 + " | " + "(!" + sel0 + " & (" + node.predicate->toString() + ")))";
    auto& newNode = parse(newNodeStr);

    auto& newPred = makeConditionalPredicate(newNode.as<ExpressionSyntax>());
    replace(*node.predicate, newPred);
    visitDefault(node);
}

// MARK: IfRemover
IfRemover::IfRemover(const ::std::shared_ptr<SyntaxTree> tree) : tree(tree) {
    IfCollector collector;
    ifNodes = collector.getFoundNodes(tree);
    cutCount = ifNodes.size() * 2;
}

void IfRemover::handle(const ConditionalStatementSyntax& node) {
    if (nodesToChange.contains(&node)) {
        if (nodesToChange[&node]) { // If true, replace with the true branch of the if statement
            if (node.elseClause == nullptr) {
                this->remove(node);
            }
            else {
                auto replacement = node.elseClause->clause;
                this->replace(node, *replacement);
            }
        }
        else {
            auto replacement = node.statement;
            this->replace(node, *replacement);
        }
    }
}

std::shared_ptr<SyntaxTree> IfRemover::removeIfIndex(const std::vector<size_t>& indicesToRemove) {
    nodesToChange.clear();

    for (size_t i : indicesToRemove) {
        if (i >= cutCount) {
            throw std::out_of_range("Index out of range for removeIfIndex");
        }
        size_t nodeIndex = i / 2;
        bool removeTrueBranch = (i % 2 != 0);
        nodesToChange.emplace(ifNodes[nodeIndex], removeTrueBranch);
    }

    return transform(tree);
}

std::vector<std::shared_ptr<SyntaxTree>> IfRemover::removeAllIfs() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    this->nodesToChange.clear();

    for (const auto& node : ifNodes) {
        nodesToChange.clear();
        nodesToChange.emplace(node, false);
        auto newTree = transform(tree);
        newTrees.emplace_back(newTree);
        nodesToChange.clear();
        nodesToChange.emplace(node, true);
        newTree = transform(tree);
        newTrees.emplace_back(newTree);
    }

    return newTrees;
}

void IfCollector::handle(const ConditionalStatementSyntax& node) {
    this->foundNodes.emplace_back(&node);
    this->visitDefault(node);
}

std::vector<const ConditionalStatementSyntax*> IfCollector::getFoundNodes(const std::shared_ptr<SyntaxTree> tree) {
    tree->root().visit(*this);

    return this->foundNodes;
}

// MARK: CaseRemover
CaseRemover::CaseRemover(const ::std::shared_ptr<SyntaxTree> tree) : tree(tree) {
    CaseCollector collector;
    caseNodes = collector.getFoundNodes(tree);
    cutCount = caseNodes.size();
}

void CaseRemover::handle(const CaseStatementSyntax& node) {
    if (nodesToChange.contains(&node)) {
        for (size_t idx : nodesToChange[&node]) {
            this->remove(*node.items[idx]);
        }
    }
    visitDefault(node);
}

std::shared_ptr<SyntaxTree> CaseRemover::removeCaseIndex(const std::vector<size_t>& indicesToRemove) {
    nodesToChange.clear();

    for (size_t i : indicesToRemove) {
        if (i >= cutCount) {
            throw std::out_of_range("Index out of range for removeCaseIndex");
        }
        nodesToChange[caseNodes[i].first].insert(caseNodes[i].second);
    }

    return transform(tree);
}

std::vector<std::shared_ptr<SyntaxTree>> CaseRemover::removeAllCases() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    nodesToChange.clear();

    for (const auto& node : caseNodes) {
        nodesToChange.clear();
        nodesToChange[node.first].insert(node.second);
        auto newTree = transform(tree);
        newTrees.emplace_back(newTree);
    }

    return newTrees;
}

void CaseCollector::handle(const CaseStatementSyntax& node) {
    bool hasDefault = false;
    for (auto item : node.items) {
        if (item->kind == SyntaxKind::DefaultCaseItem) {
            hasDefault = true;
            break;
        }
    }

    if (hasDefault) {
        for (size_t i = 0; i < node.items.size(); i++) {
            if (node.items[i]->kind != SyntaxKind::DefaultCaseItem) {
                this->foundNodes.emplace_back(&node, i);
            }
        }
    }

    this->visitDefault(node);
}

std::vector<std::pair<const CaseStatementSyntax*, size_t>> CaseCollector::getFoundNodes(
    const std::shared_ptr<SyntaxTree> tree) {
    tree->root().visit(*this);

    return this->foundNodes;
}

// MARK: BinopRemover

// Operators whose operands can be dropped to test for a dead operand. Excludes
// assignments (left is an lvalue), comparisons/equality (1-bit result), and the
// property operators (->, <->). mul/div/mod stay in: even when the backend
// blackboxes wide ones, the reduction is still a decidable check.
static bool isReducibleBinop(SyntaxKind kind) {
    switch (kind) {
        case SyntaxKind::AddExpression:
        case SyntaxKind::SubtractExpression:
        case SyntaxKind::MultiplyExpression:
        case SyntaxKind::DivideExpression:
        case SyntaxKind::ModExpression:
        case SyntaxKind::PowerExpression:
        case SyntaxKind::BinaryAndExpression:
        case SyntaxKind::BinaryOrExpression:
        case SyntaxKind::BinaryXorExpression:
        case SyntaxKind::BinaryXnorExpression:
        case SyntaxKind::LogicalAndExpression:
        case SyntaxKind::LogicalOrExpression:
        case SyntaxKind::LogicalShiftLeftExpression:
        case SyntaxKind::LogicalShiftRightExpression:
        case SyntaxKind::ArithmeticShiftLeftExpression:
        case SyntaxKind::ArithmeticShiftRightExpression:
            return true;
        default:
            return false;
    }
}

// Shifts only get a keep-left cut (a<<b -> a, i.e. shift amount was 0); keeping
// the right operand makes the result the shift count and near-always falsifies.
static bool isShiftBinop(SyntaxKind kind) {
    switch (kind) {
        case SyntaxKind::LogicalShiftLeftExpression:
        case SyntaxKind::LogicalShiftRightExpression:
        case SyntaxKind::ArithmeticShiftLeftExpression:
        case SyntaxKind::ArithmeticShiftRightExpression:
            return true;
        default:
            return false;
    }
}

BinopRemover::BinopRemover(const ::std::shared_ptr<SyntaxTree> tree) : tree(tree) {
    BinopCollector collector;
    binopNodes = collector.getFoundNodes(tree);
    cutCount = binopNodes.size();
}

void BinopRemover::handle(const BinaryExpressionSyntax& node) {
    if (nodesToChange.contains(&node)) {
        auto replacement = nodesToChange[&node] ? node.left : node.right;
        this->replace(node, *replacement);
    }
    visitDefault(node);
}

std::shared_ptr<SyntaxTree> BinopRemover::removeBinopIndex(const std::vector<size_t>& indicesToRemove) {
    nodesToChange.clear();

    for (size_t i : indicesToRemove) {
        if (i >= cutCount) {
            throw std::out_of_range("Index out of range for removeBinopIndex");
        }
        nodesToChange.emplace(binopNodes[i].first, binopNodes[i].second);
    }

    return transform(tree);
}

std::vector<std::shared_ptr<SyntaxTree>> BinopRemover::removeAllBinops() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    nodesToChange.clear();

    for (const auto& node : binopNodes) {
        nodesToChange.clear();
        nodesToChange.emplace(node.first, node.second);
        auto newTree = transform(tree);
        newTrees.emplace_back(newTree);
    }

    return newTrees;
}

void BinopCollector::handle(const BinaryExpressionSyntax& node) {
    if (isReducibleBinop(node.kind)) {
        this->foundNodes.emplace_back(&node, true); // keep-left
        if (!isShiftBinop(node.kind)) {
            this->foundNodes.emplace_back(&node, false); // keep-right
        }
    }
    this->visitDefault(node);
}

std::vector<std::pair<const BinaryExpressionSyntax*, bool>> BinopCollector::getFoundNodes(
    const std::shared_ptr<SyntaxTree> tree) {
    tree->root().visit(*this);

    return this->foundNodes;
}

// MARK: Papercutter

Papercutter::Papercutter(const std::shared_ptr<SyntaxTree> tree, bool shrinkWithIntermediate)
    : tree(tree), shrinkWithIntermediate(shrinkWithIntermediate) {

    // Narrow (default) mode can shrink signed decls, nets, and multi-packed-dim
    // vectors; intermediate-wire mode cannot.
    BitShrinkCollector BSC(!shrinkWithIntermediate, !shrinkWithIntermediate, !shrinkWithIntermediate);
    shrinkNodes = BSC.getFoundNodes(tree);
    cutCount += shrinkNodes.size();
    BSRCount = shrinkNodes.size();

    TernaryCollector TC;
    ternaryNodes = TC.getFoundNodes(tree);
    cutCount += ternaryNodes.size() * 2;
    TRCount = ternaryNodes.size() * 2;

    IfCollector IC;
    ifNodes = IC.getFoundNodes(tree);
    cutCount += ifNodes.size() * 2;
    IRCount = ifNodes.size() * 2;

    CaseCollector CC;
    caseNodes = CC.getFoundNodes(tree);
    cutCount += caseNodes.size();
    CRCount = caseNodes.size();

    BinopCollector BC;
    binopNodes = BC.getFoundNodes(tree);
    cutCount += binopNodes.size();
    BRCount = binopNodes.size();
}

std::vector<std::shared_ptr<SyntaxTree>> Papercutter::cutAll() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    auto ternaryRemoveTrees = removeAllTernaries();
    newTrees.insert(newTrees.end(), ternaryRemoveTrees.begin(), ternaryRemoveTrees.end());

    auto ifRemoveTrees = removeAllIfs();
    newTrees.insert(newTrees.end(), ifRemoveTrees.begin(), ifRemoveTrees.end());

    auto bitShrinkTrees = shrinkAllBits();
    newTrees.insert(newTrees.end(), bitShrinkTrees.begin(), bitShrinkTrees.end());

    auto caseRemoveTrees = removeAllCases();
    newTrees.insert(newTrees.end(), caseRemoveTrees.begin(), caseRemoveTrees.end());

    auto binopRemoveTrees = removeAllBinops();
    newTrees.insert(newTrees.end(), binopRemoveTrees.begin(), binopRemoveTrees.end());

    return newTrees;
}

void Papercutter::selectCuts(const std::vector<size_t>& indicesToCut) {
    for (size_t i : indicesToCut) {
        if (i >= cutCount) {
            throw std::out_of_range("Index out of range for cutIndex");
        }

        if (i < TRCount) {
            size_t nodeIndex = (i) / 2;
            bool removeLeft = ((i) % 2 != 0);
            ternaryNodesToChange.emplace(ternaryNodes[nodeIndex], removeLeft);
        }
        else if (i < TRCount + IRCount) {
            size_t nodeIndex = (i - TRCount) / 2;
            bool removeTrueBranch = ((i - TRCount) % 2 != 0);
            ifNodesToChange.emplace(ifNodes[nodeIndex], removeTrueBranch);
        }
        else if (i < TRCount + IRCount + BSRCount) {
            size_t nodeIndex = i - TRCount - IRCount;
            const auto& t = shrinkNodes[nodeIndex];
            nodesToShrink.emplace(t.decl->name.valueText());
            runMap[t.decl].push_back(t);
        }
        else if (i < TRCount + IRCount + BSRCount + CRCount) {
            size_t nodeIndex = i - TRCount - IRCount - BSRCount;
            caseNodesToChange[caseNodes[nodeIndex].first].insert(caseNodes[nodeIndex].second);
        }
        else {
            size_t nodeIndex = i - TRCount - IRCount - BSRCount - CRCount;
            binopNodesToChange.emplace(binopNodes[nodeIndex].first, binopNodes[nodeIndex].second);
        }
    }
}

std::shared_ptr<SyntaxTree> Papercutter::cutIndex(std::vector<size_t> indicesToCut) {

    clearState();

    selectCuts(indicesToCut);

    auto newTree = transform(tree);

    // Stabilize before the finishing pass
    auto stabilized = SyntaxPrinter::printFile(*newTree);
    newTree = SyntaxTree::fromText(stabilized, tree->sourceManager());

    // The finishing pass only redirects identifiers to their `_papercuts` wires,
    // so it is needed for the intermediate-wire strategy alone. Narrow mode never
    // renames anything, so its pass-1 output is already final.
    if (shrinkWithIntermediate) {
        auto finalNodesToShrink = nodesToShrink;
        clearState();
        // Need to restore parents here
        auto parentSetter = ParentSetter();
        parentSetter.visit(newTree->root());
        nodesToShrink = finalNodesToShrink; // Restore the nodesToShrink state for a finishing pass on identifier names
        newTree = transform(newTree); // Do a finishing pass to replace identifier names with the _papercuts versions for any ifs/ternarys that were cut

        // Stabilize the final result too, so callers always get a single-buffer tree.
        stabilized = SyntaxPrinter::printFile(*newTree);
        newTree = SyntaxTree::fromText(stabilized, tree->sourceManager());
    }

    return newTree;
}

// Serialize a single cut (or set of cuts) straight to text. This is the fast
// path for cut enumeration: it returns the same string a caller would get from
// print_tree(cutIndex(indices)), but skips the intermediate re-parse that
// cutIndex performs to hand back a live SyntaxTree cause we only need the text.
std::string Papercutter::cutIndexText(std::vector<size_t> indicesToCut) {

    clearState();

    selectCuts(indicesToCut);

    auto newTree = transform(tree);

    auto stabilized = SyntaxPrinter::printFile(*newTree);

    if (shrinkWithIntermediate) {
        newTree = SyntaxTree::fromText(stabilized, tree->sourceManager());
        auto finalNodesToShrink = nodesToShrink;
        clearState();
        auto parentSetter = ParentSetter();
        parentSetter.visit(newTree->root());
        nodesToShrink = finalNodesToShrink;
        newTree = transform(newTree);
        stabilized = SyntaxPrinter::printFile(*newTree);
    }

    return stabilized;
}

std::vector<std::pair<std::string, size_t>> Papercutter::cutInfo() {
    // Describe every cut in the SAME order cutAll() produces its trees, so that
    // index i here corresponds 1:1 to cutAll()[i] (and to cutIndex({i})).
    // Line numbers are relative to the tree this Papercutter was constructed
    // from (the concretized per-module source).
    std::vector<std::pair<std::string, size_t>> info;
    info.reserve(cutCount);

    auto& sm = tree->sourceManager();
    auto lineOf = [&](const SyntaxNode& node) -> size_t {
        return sm.getLineNumber(node.sourceRange().start());
    };

    // Ternaries: 2 cuts per node (matches removeAllTernaries + cutIndex mapping).
    //   even index -> nodesToChange=false -> keep node.right (false branch)
    //   odd index  -> nodesToChange=true  -> keep node.left  (true branch)
    for (const auto* node : ternaryNodes) {
        size_t line = lineOf(*node);
        info.emplace_back("ternary(keep-false)", line);
        info.emplace_back("ternary(keep-true)", line);
    }

    // Ifs: 2 cuts per node.
    //   even index -> removeTrueBranch=false -> keep the true branch (statement)
    //   odd index  -> removeTrueBranch=true  -> keep the else clause (false branch)
    for (const auto* node : ifNodes) {
        size_t line = lineOf(*node);
        info.emplace_back("if(keep-true)", line);
        info.emplace_back("if(keep-false)", line);
    }

    // Bit shrinks: 1 cut per shrinkable packed dimension (a multi-dim vector
    // contributes one entry per dimension). Line is the declarator's name.
    for (const auto& t : shrinkNodes) {
        info.emplace_back("bitshrink", sm.getLineNumber(t.decl->name.location()));
    }

    // Cases: 1 cut per prunable item (prune the item, falling through to default).
    for (const auto& pair : caseNodes) {
        info.emplace_back("case(prune-item)", lineOf(*pair.first->items[pair.second]));
    }

    // Binops: keep one operand (shifts keep-left only).
    for (const auto& pair : binopNodes) {
        info.emplace_back(pair.second ? "binop(keep-left)" : "binop(keep-right)", lineOf(*pair.first));
    }

    return info;
}

std::vector<std::shared_ptr<SyntaxTree>> Papercutter::shrinkAllBits() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;
    nodesToShrink.clear();
    runMap.clear();

    for (const auto& t : shrinkNodes) {
        nodesToShrink.clear();
        runMap.clear();
        nodesToShrink.emplace(t.decl->name.valueText());
        runMap[t.decl].push_back(t);
        auto newTree = transform(tree);
        newTrees.emplace_back(newTree);
    }
    clearState();
    return newTrees;
}

std::vector<std::shared_ptr<SyntaxTree>> Papercutter::removeAllTernaries() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    ternaryNodesToChange.clear();

    for (const auto& node : ternaryNodes) {
        ternaryNodesToChange.clear();
        this->ternaryNodesToChange.emplace(node, false);
        auto newTree = transform(tree);
        newTrees.emplace_back(newTree);
        ternaryNodesToChange.clear();
        this->ternaryNodesToChange.emplace(node, true);
        newTree = transform(tree);
        newTrees.emplace_back(newTree);
    }
    clearState();
    return newTrees;
}

std::vector<std::shared_ptr<SyntaxTree>> Papercutter::removeAllIfs() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    this->ifNodesToChange.clear();

    for (const auto& node : ifNodes) {
        ifNodesToChange.clear();
        ifNodesToChange.emplace(node, false);
        auto newTree = transform(tree);
        newTrees.emplace_back(newTree);
        ifNodesToChange.clear();
        ifNodesToChange.emplace(node, true);
        newTree = transform(tree);
        newTrees.emplace_back(newTree);
    }
    clearState();
    return newTrees;
}

std::vector<std::shared_ptr<SyntaxTree>> Papercutter::removeAllCases() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    caseNodesToChange.clear();

    for (const auto& node : caseNodes) {
        caseNodesToChange.clear();
        caseNodesToChange[node.first].insert(node.second);
        auto newTree = transform(tree);
        newTrees.emplace_back(newTree);
    }
    clearState();
    return newTrees;
}

std::vector<std::shared_ptr<SyntaxTree>> Papercutter::removeAllBinops() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    binopNodesToChange.clear();

    for (const auto& node : binopNodes) {
        binopNodesToChange.clear();
        binopNodesToChange.emplace(node.first, node.second);
        auto newTree = transform(tree);
        newTrees.emplace_back(newTree);
    }
    clearState();
    return newTrees;
}

namespace {
// Trim ASCII whitespace off both ends of a string.
std::string trimWs(std::string s) {
    size_t b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos)
        return std::string();
    size_t e = s.find_last_not_of(" \t\r\n");
    return s.substr(b, e - b + 1);
}

// Build the packed-range string for a declaration's shared type, dropping one bit
// off the high end of every dimension whose index is in `narrowIdx`. e.g. dims
// [3:0][7:0] with narrowIdx={1} -> "[3:0][6:0]". Non-narrowed dimensions are
// emitted verbatim (via toString) so any bound form survives untouched; narrowed
// dimensions are recomputed from their literal bounds (the collector only ever
// targets simple literal ranges, so the casts here are safe).
std::string buildPackedRanges(const SyntaxList<VariableDimensionSyntax>& dims,
                              const std::unordered_set<int>& narrowIdx) {
    std::string out;
    for (size_t di = 0; di < dims.size(); ++di) {
        if (narrowIdx.contains(static_cast<int>(di))) {
            auto& rsel = dims[di]->specifier->as<RangeDimensionSpecifierSyntax>().selector->as<RangeSelectSyntax>();
            int leftVal = tokenToInt(rsel.left->as<LiteralExpressionSyntax>().literal);
            int rightVal = tokenToInt(rsel.right->as<LiteralExpressionSyntax>().literal);
            // Drop one bit off the high end: [7:0] -> [6:0], [0:7] -> [0:6].
            if (leftVal >= rightVal)
                leftVal -= 1;
            else
                rightVal -= 1;
            out += "[" + std::to_string(leftVal) + ":" + std::to_string(rightVal) + "]";
        } else {
            out += trimWs(std::string(dims[di]->toString()));
        }
    }
    return out;
}
} // namespace

void Papercutter::handle(const DataDeclarationSyntax& node) {
    // In legacy intermediate-wire mode the per-declarator handler does all the
    // work; just descend so it fires.
    if (shrinkWithIntermediate) {
        visitDefault(node);
        return;
    }

    // Narrow (default) mode: shrink each targeted declarator by rebuilding this
    // declaration in place. The packed range ([7:0]) lives on the shared type, so
    // when several signals share one declaration (e.g. `logic [2:0] a, b;`) we
    // split it: the non-shrunk declarators stay together at the original width and
    // each shrunk declarator becomes its own narrowed declaration. This keeps
    // per-signal cuts independent even though they share a type.
    std::vector<const DeclaratorSyntax*> targeted;
    std::vector<const DeclaratorSyntax*> kept;
    for (const auto* decl : node.declarators) {
        if (runMap.contains(decl))
            targeted.push_back(decl);
        else
            kept.push_back(decl);
    }

    if (targeted.empty()) {
        visitDefault(node);
        return;
    }

    // Only declarators the collector accepted (unsigned logic/reg/bit) ever land
    // in runMap, so the shared type is guaranteed narrow-able. It may carry more
    // than one packed dimension (e.g. `logic [3:0][7:0] x;`); each targeted cut
    // names the specific dimension to narrow via runMap.
    auto& intType = node.type->as<IntegerTypeSyntax>();
    auto& dims = intType.dimensions;

    // Leading trivia (newline + indentation) of the whole declaration, reused so
    // each emitted declaration lands on its own indented line.
    std::string trivia;
    for (const auto& t : node.getFirstToken().trivia())
        trivia += t.getRawText();

    std::string mods = trimWs(std::string(node.modifiers.toString()));
    std::string modsOut = mods.empty() ? "" : mods + " ";

    std::string typeHead = std::string(intType.keyword.valueText());
    if (intType.signing)
        typeHead += " " + std::string(intType.signing.valueText());

    std::string origRange = buildPackedRanges(dims, {});

    // Preserve source order: kept declarators first (original width), then one
    // narrowed declaration per shrunk declarator.
    std::vector<std::string> repls;
    if (!kept.empty()) {
        std::string s = trivia + modsOut + typeHead + " " + origRange + " ";
        for (size_t i = 0; i < kept.size(); ++i) {
            s += trimWs(std::string(kept[i]->toString()));
            if (i + 1 < kept.size())
                s += ", ";
        }
        s += ";";
        repls.push_back(s);
    }
    for (const auto* d : targeted) {
        // The dimensions to narrow for this declarator (usually one; more if the
        // caller combined several dim-cuts of the same signal in one tree).
        std::unordered_set<int> narrowIdx;
        for (const auto& t : runMap[d])
            narrowIdx.insert(t.dimIndex);
        repls.push_back(trivia + modsOut + typeHead + " " + buildPackedRanges(dims, narrowIdx) + " " +
                        trimWs(std::string(d->toString())) + ";");
    }

    for (const auto& r : repls)
        insertBefore(node, parse(r));
    remove(node);
}

void Papercutter::handle(const NetDeclarationSyntax& node) {
    // Nets are only collected in narrow (default) mode; the intermediate-wire path
    // never targets them, so just descend so initializer expression cuts still fire.
    if (shrinkWithIntermediate) {
        visitDefault(node);
        return;
    }

    // Same split-and-narrow rewrite as handle(DataDeclarationSyntax): kept
    // declarators stay at the original width, each shrunk one becomes its own
    // narrowed net declaration.
    std::vector<const DeclaratorSyntax*> targeted;
    std::vector<const DeclaratorSyntax*> kept;
    for (const auto* decl : node.declarators) {
        if (runMap.contains(decl))
            targeted.push_back(decl);
        else
            kept.push_back(decl);
    }

    if (targeted.empty()) {
        visitDefault(node);
        return;
    }

    // Only narrow-able declarators reach runMap, so the packed dimensions live on
    // an IntegerTypeSyntax (`wire logic [7:0]`) or ImplicitTypeSyntax (bare
    // `wire [7:0]`), and there may be more than one (`wire [3:0][7:0]`). Nets with
    // strength/delay were skipped by the collector.
    Token keyword, signing;
    const SyntaxList<VariableDimensionSyntax>* dims = nullptr;
    if (auto* intType = node.type->as_if<IntegerTypeSyntax>()) {
        keyword = intType->keyword;
        signing = intType->signing;
        dims = &intType->dimensions;
    } else {
        auto& impType = node.type->as<ImplicitTypeSyntax>();
        signing = impType.signing;
        dims = &impType.dimensions;
    }

    std::string trivia;
    for (const auto& t : node.getFirstToken().trivia())
        trivia += t.getRawText();

    std::string typeHead = std::string(node.netType.valueText());
    if (keyword)
        typeHead += " " + std::string(keyword.valueText());
    if (signing)
        typeHead += " " + std::string(signing.valueText());

    std::string origRange = buildPackedRanges(*dims, {});

    std::vector<std::string> repls;
    if (!kept.empty()) {
        std::string s = trivia + typeHead + " " + origRange + " ";
        for (size_t i = 0; i < kept.size(); ++i) {
            s += trimWs(std::string(kept[i]->toString()));
            if (i + 1 < kept.size())
                s += ", ";
        }
        s += ";";
        repls.push_back(s);
    }
    for (const auto* d : targeted) {
        std::unordered_set<int> narrowIdx;
        for (const auto& t : runMap[d])
            narrowIdx.insert(t.dimIndex);
        repls.push_back(trivia + typeHead + " " + buildPackedRanges(*dims, narrowIdx) + " " +
                        trimWs(std::string(d->toString())) + ";");
    }

    for (const auto& r : repls)
        insertBefore(node, parse(r));
    remove(node);
}

void Papercutter::handle(const DeclaratorSyntax& node) {
    // This per-declarator handler exists only to build the legacy intermediate-wire
    // bit-shrink form; narrow (default) shrink is done at the DataDeclaration level
    // in handle(DataDeclarationSyntax). Regardless of mode we MUST still descend
    // into the declarator's children (via visitDefault) so that expression cuts
    // living inside an initializer -- e.g. the `sel ? a : b` in `wire w = sel ? a : b;`
    // or the `a & b` in `wire v = a & b;` -- are reached and actually applied.
    // Overriding handle() takes over dispatch for this node, so without an explicit
    // visitDefault the initializer subtree is never visited: such cuts were collected
    // (they show up in cut_info) but silently applied to nothing.
    if (!shrinkWithIntermediate) {
        visitDefault(node);
        return;
    }
    if (runMap.contains(&node)) {
        auto newName = std::string(node.name.valueText()) + "_papercuts";
        // Wire mode is single-dim only, so this declarator has exactly one target.
        int newWidth = runMap[&node].front().width - 1; // Calculate the new width after shrinking
        auto* parentDecl = node.parent->as_if<DataDeclarationSyntax>();
        if (!parentDecl) {
            return; // If the parent of this DeclaratorSyntax node is not a DataDeclarationSyntax node, we can skip it
        }

        auto& type = parentDecl->type;

        auto& newDecl = factory.declarator(makeId(persistString(alloc, newName), SingleSpace),
                                           std::span<VariableDimensionSyntax*>{}, nullptr);

        auto declElem = std::span(alloc.emplace<TokenOrSyntax>(&newDecl), size_t{1});
        SeparatedSyntaxList<DeclaratorSyntax> declList(declElem);

        auto& newDataDecl = factory.dataDeclaration(std::span<AttributeInstanceSyntax*>{},
                                                    *deepClone(parentDecl->modifiers, alloc), *deepClone(*type, alloc),
                                                    declList, makeSemicolon());
        insertAfter(*parentDecl, newDataDecl);

        std::string oldTriviaText;
        for (const auto& t : parentDecl->getFirstToken().trivia())
            oldTriviaText += t.getRawText();

        std::string assignText = oldTriviaText + "assign " + newName + " = {1'b0, " +
                                 std::string(node.name.valueText()) + "[" + std::to_string(newWidth - 1) + ":0]};";
        auto& newAssign = parse(assignText);

        insertAfter(*parentDecl, newAssign);
    }
    // Descend so initializer cuts inside this declarator are still applied in
    // wire mode (the original declaration is retained alongside the new wire).
    visitDefault(node);
}

void Papercutter::handle(const BinaryExpressionSyntax& node) {
    if (binopNodesToChange.contains(&node)) {
        auto replacement = binopNodesToChange[&node] ? node.left : node.right;
        this->replace(node, *replacement);
    }
    // we don't want to replace the assignment of a node we're shrinking
    if (auto leftNode = node.left->as_if<IdentifierNameSyntax>()) {
        if (std::string(leftNode->identifier.valueText()).find("_papercuts") != std::string::npos) {
            return; // If the left side of this assignment is a node we're shrinking, we don't want to replace it
        }
        else visitDefault(node);
    }
    else visitDefault(node);

}

void Papercutter::handle(const IdentifierNameSyntax& node) {
    // Narrow mode never renames reads (the signal keeps its name, only its
    // declared width changes), so identifier redirection is wire-mode only.
    // Still descend so any nested nodes (e.g. cuts inside select indices) are
    // reached; a plain identifier name has no child nodes, so this is a no-op.
    if (!shrinkWithIntermediate) {
        visitDefault(node);
        return;
    }
    // Check to see if this is the left side of a declaration
    if (node.parent && node.parent->kind == SyntaxKind::AssignmentExpression &&
        &node == node.parent->as<BinaryExpressionSyntax>().left) {
        return; // If it is, we don't want to replace it
    }

    if (nodesToShrink.contains(std::string(node.identifier.valueText()))) {
        replaceToken(node, 0, makeId(persistString(alloc, std::string(node.identifier.valueText()) + "_papercuts")),
                     true);
    }
}

void Papercutter::handle(const IdentifierSelectNameSyntax& node) {
    // Narrow mode never renames reads; identifier redirection is wire-mode only.
    // Descend anyway so cuts nested in select-index expressions are still reached.
    if (!shrinkWithIntermediate) {
        visitDefault(node);
        return;
    }
    // Check to see if this is the left side of a declaration
    if (node.parent && node.parent->kind == SyntaxKind::AssignmentExpression &&
        &node == node.parent->as<BinaryExpressionSyntax>().left) {
        return; // If it is, we don't want to replace it
    }

    if (nodesToShrink.contains(std::string(node.identifier.valueText()))) {
        replaceToken(node, 0, makeId(persistString(alloc, std::string(node.identifier.valueText()) + "_papercuts")),
                     true);
    }
}

void Papercutter::handle(const SyntaxNode& node) {
    // Check to see if this is the left side of a declaration
    if (node.parent && node.parent->kind == SyntaxKind::AssignmentExpression &&
        &node == node.parent->as<BinaryExpressionSyntax>().left) {
        return; // If it is, we don't want to replace it
    }
    visitDefault(node);
}

void Papercutter::handle(const ConditionalExpressionSyntax& node) {
    if (ternaryNodesToChange.contains(&node)) {
        auto replacement = ternaryNodesToChange[&node] ? node.left : node.right;
        this->replace(node, *replacement);
    }
    visitDefault(node);
}

void Papercutter::handle(const ConditionalStatementSyntax& node) {
    if (ifNodesToChange.contains(&node)) {
        if (!ifNodesToChange[&node]) {
            if (node.elseClause == nullptr) {
                this->remove(node);
            }
            else {
                auto replacement = node.elseClause->clause;
                this->replace(node, *replacement);
            }
        }
        else {
            auto replacement = node.statement;
            this->replace(node, *replacement);
        }
    }
    this->visitDefault(node);
}

void Papercutter::handle(const CaseStatementSyntax& node) {
    if (caseNodesToChange.contains(&node)) {
        for (size_t idx : caseNodesToChange[&node]) {
            this->remove(*node.items[idx]);
        }
    }
    this->visitDefault(node);
}

} // namespace papercuts