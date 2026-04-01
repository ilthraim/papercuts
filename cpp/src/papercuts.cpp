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

SubmoduleRenamer::SubmoduleRenamer(const std::shared_ptr<SyntaxTree> tree) : tree(tree) {
    ModuleNameFinder finder;
    this->moduleName = finder.getModuleName(tree);
}

void SubmoduleRenamer::handle(const HierarchyInstantiationSyntax& node) {
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

            auto& newInst = parse(
                persistString(alloc, 
                    oldTriviaText + 
                    (node.attributes.size() > 0 ? node.attributes.toString() + " " : "") + 
                    moduleName + "_" + std::string(instance->decl->name.valueText()) + 
                    (node.parameters ? node.parameters->toString() : "")
                    + " " + std::string(instance->decl->name.valueText()) + " (" + instance->connections.toString() + ");"
                )
            );

            insertBefore(node, newInst);
            std::cout << "Inserted new instance: " << newInst.toString() << std::endl;
        }
        remove(node);
    }
}

std::shared_ptr<SyntaxTree> SubmoduleRenamer::renameSubmodules() {
    return this->transform(tree);
}

std::shared_ptr<SyntaxTree> renameSubmodules(const std::shared_ptr<SyntaxTree> tree) {
    SubmoduleRenamer rewriter(tree);
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

    MuxContext context;

    BitMuxer BM(context);
    TernaryMuxer TM(context);
    IfMuxer IM(context);
    ParentSetter PS;

    if (bitMux) {
        BM.initialize(tree);
    }
    if (ternaryMux) {
        newTree = TM.insertTernaryMuxes(newTree);
    }
    // PS.visit(newTree->root());
    if (ifMux) {
        newTree = IM.insertIfMuxes(newTree);
    }
    PS.visit(newTree->root());
    if (bitMux) {
        newTree = BM.insertBitShrinkMuxes(newTree);
    }

    return newTree;
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
    for (const auto& [node, width] : widthMapNodes) {
        widthMap[std::string(node->name.valueText())] = width;
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

    for (const auto& pair : shrinkNodes) {
        nodesToShrink.clear();
        runMap.clear();
        nodesToShrink.emplace(pair.first->name.valueText());
        runMap.emplace(pair);
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
        nodesToShrink.emplace(shrinkNodes[i].first->name.valueText());
        runMap.emplace(shrinkNodes[i]);
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
    auto* dataDecl = node.parent->as_if<DataDeclarationSyntax>();
    if (!dataDecl) {
        return; // If the parent of this DeclaratorSyntax node is not a DataDeclarationSyntax node, we can skip it
    }
    auto& type = dataDecl->type;

    if (type->kind == SyntaxKind::LogicType) {
        auto& intType = type->as<IntegerTypeSyntax>();
        if (intType.signing && intType.signing.kind != TokenKind::UnsignedKeyword) {
            std::cout << "Skipping signed logic declaration: " << node.name.valueText() << std::endl;
            return; // Skip this node if it's a signed logic declaration
        }
        auto& dims = intType.dimensions;
        if (dims.size() == 0) {
            std::cout << "Skipping single-bit logic declaration: " << node.name.valueText() << std::endl;
            return; // Skip this node if it's a single-bit logic declaration
        }
        if (dims.size() > 1) {
            std::cout << "Skipping multi-dimensional logic declaration: " << node.name.valueText() << std::endl;
            return; // Skip this node if it's a multi-dimensional logic declaration
        }
        // We only want to look at one-dimensional logic decls, and no wildcard or queue dimensions
        // (idk even what those are)
        auto& dim = dims[0]->specifier->as<RangeDimensionSpecifierSyntax>();
        // We shouldn't ever have bit selection here but in case we do throw an assert
        auto& dimSelect = dim.selector->as<RangeSelectSyntax>();

        // Get the left and right bounds of the range and calculate the width -> should be fine to
        // cast here because (theoretically) concretization should have occurred already
        auto& left = dimSelect.left->as<LiteralExpressionSyntax>();
        auto& right = dimSelect.right->as<LiteralExpressionSyntax>();

        int leftVal, rightVal;
        leftVal = tokenToInt(left.literal);
        rightVal = tokenToInt(right.literal);
        int width = std::abs(leftVal - rightVal) + 1;

        if (width <= 1) {
            std::cout << "Skipping single-bit logic declaration: " << node.name.valueText() << std::endl;
            return; // Skip this node if it's a single-bit logic declaration
        }

        shrinkNodes.emplace_back(&node, width); // Insert the node and its width into the vector
    }
}

std::vector<std::pair<const DeclaratorSyntax*, int>> BitShrinkCollector::getFoundNodes(
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
                std::cout << "If statement has no else clause, replacing with empty statement" << std::endl;
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

// MARK: Papercutter

Papercutter::Papercutter(const std::shared_ptr<SyntaxTree> tree) : tree(tree) {

    BitShrinkCollector BSC;
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
}

std::vector<std::shared_ptr<SyntaxTree>> Papercutter::cutAll() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    auto bitShrinkTrees = shrinkAllBits();
    newTrees.insert(newTrees.end(), bitShrinkTrees.begin(), bitShrinkTrees.end());

    auto ternaryRemoveTrees = removeAllTernaries();
    newTrees.insert(newTrees.end(), ternaryRemoveTrees.begin(), ternaryRemoveTrees.end());

    auto ifRemoveTrees = removeAllIfs();
    newTrees.insert(newTrees.end(), ifRemoveTrees.begin(), ifRemoveTrees.end());

    return newTrees;
}

std::shared_ptr<SyntaxTree> Papercutter::cutIndex(std::vector<size_t> indicesToCut) {

    clearState();

    for (size_t i : indicesToCut) {
        if (i >= cutCount) {
            throw std::out_of_range("Index out of range for cutIndex");
        }

        if (i < BSRCount) {
            size_t nodeIndex = i;
            nodesToShrink.emplace(shrinkNodes[nodeIndex].first->name.valueText());
            runMap.emplace(shrinkNodes[nodeIndex]);
        }
        else if (i < BSRCount + TRCount) {
            size_t nodeIndex = (i - BSRCount) / 2;
            bool removeLeft = ((i - BSRCount) % 2 != 0);
            ternaryNodesToChange.emplace(ternaryNodes[nodeIndex], removeLeft);
        }
        else {
            size_t nodeIndex = (i - BSRCount - TRCount) / 2;
            bool removeTrueBranch = ((i - BSRCount - TRCount) % 2 != 0);
            ifNodesToChange.emplace(ifNodes[nodeIndex], removeTrueBranch);
        }
    }

    auto newTree = transform(tree);
    clearState();
    return newTree;
}

std::vector<std::shared_ptr<SyntaxTree>> Papercutter::shrinkAllBits() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;
    nodesToShrink.clear();
    runMap.clear();

    for (const auto& pair : shrinkNodes) {
        nodesToShrink.clear();
        runMap.clear();
        nodesToShrink.emplace(pair.first->name.valueText());
        runMap.emplace(pair);
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

void Papercutter::handle(const DeclaratorSyntax& node) {
    if (runMap.contains(&node)) {
        auto newName = std::string(node.name.valueText()) + "_papercuts";
        int newWidth = runMap[&node] - 1; // Get the width of the node and calculate the new width after shrinking
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
}

void Papercutter::handle(const IdentifierNameSyntax& node) {
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
                std::cout << "If statement has no else clause, replacing with empty statement" << std::endl;
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

} // namespace papercuts