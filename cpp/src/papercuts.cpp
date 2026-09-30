#include "papercuts/papercuts.h"

#include "papercuts/utils.h"
#include <algorithm>
#include <iostream>
#include <memory>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

#include "slang/parsing/TokenKind.h"
#include "slang/syntax/AllSyntax.h"
#include "slang/syntax/SyntaxFacts.h"
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

namespace {
// How a packed range's bounds constrain a bit-shrink of that dimension.
struct RangeBounds {
    bool literal = false;                       // both bounds literal: `width` is exact
    int width = 0;                              // only set when `literal`
    const ExpressionSyntax* symbolic = nullptr; // the sole non-literal bound, if exactly one
};

// Classify `[a:b]`. The single non-literal bound is the MSB end in both
// `[W-1:0]` and `[0:W-1]`, so narrowing it is direction-correct either way; a
// range with two non-literal bounds has no inferable direction and is reported
// as neither literal nor symbolic (i.e. not shrinkable).
RangeBounds classifyRange(const RangeSelectSyntax& sel) {
    auto* left = sel.left->as_if<LiteralExpressionSyntax>();
    auto* right = sel.right->as_if<LiteralExpressionSyntax>();
    if (left && right) {
        int l = tokenToInt(left->literal), r = tokenToInt(right->literal);
        return {true, std::abs(l - r) + 1, nullptr};
    }
    if (!left && !right)
        return {};
    return {false, 0, left ? sel.right : sel.left};
}
// Whether the bit muxer can actually build a companion for this target. The
// companion's RHS names the retained bits, which needs literal bounds; a
// parameterized dimension (`[W-1:0]`) is enumerated as a cut but cannot be muxed.
// Its select is still RESERVED (see insertMuxes), so the cuts after it keep their
// indices -- the select is simply a port that drives nothing.
bool canBuildCompanion(const BitShrinkTarget& target) {
    const DataTypeSyntax* type = nullptr;
    if (auto* dataDecl = target.decl->parent->as_if<DataDeclarationSyntax>())
        type = dataDecl->type;
    else if (auto* netDecl = target.decl->parent->as_if<NetDeclarationSyntax>())
        type = netDecl->type;
    if (!type)
        return false;

    const SyntaxList<VariableDimensionSyntax>* dims = nullptr;
    if (auto* intType = type->as_if<IntegerTypeSyntax>())
        dims = &intType->dimensions;
    else if (auto* impType = type->as_if<ImplicitTypeSyntax>())
        dims = &impType->dimensions;
    if (!dims || target.dimIndex < 0 || static_cast<size_t>(target.dimIndex) >= dims->size())
        return false;

    auto* dimSpec = (*dims)[target.dimIndex]->specifier->as_if<RangeDimensionSpecifierSyntax>();
    if (!dimSpec)
        return false;
    auto* dimSelect = dimSpec->selector->as_if<RangeSelectSyntax>();
    if (!dimSelect)
        return false;
    return classifyRange(*dimSelect).literal;
}
} // namespace

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

        // The attribute list's elements come first in the flat child index
        // space, so the module type token sits right after them.
        replaceToken(node, node.attributes.getChildCount(), newType, true);
    }
    else {
        for (const auto& instance : node.instances) {
            std::string oldTriviaText;
            for (const auto& t : node.getFirstToken().trivia())
                oldTriviaText += t.getRawText();

            auto& newInst = parse(persistString(
                alloc, oldTriviaText + (node.attributes.size() > 0 ? listToString(node.attributes) + " " : "") +
                           moduleName + "_" + std::string(instance->decl->name.valueText()) +
                           (node.parameters ? node.parameters->toString() : "") + " " +
                           std::string(instance->decl->name.valueText()) + " (" + listToString(instance->connections) +
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

InstanceTypeRenamer::InstanceTypeRenamer(std::unordered_map<std::string, std::string> renames)
    : renames(std::move(renames)) {}

void InstanceTypeRenamer::handle(const HierarchyInstantiationSyntax& node) {
    auto it = renames.find(std::string(node.type.valueText()));
    if (it == renames.end())
        return;
    // The module type is the first token after the attribute list's elements
    // in the flat child index space; instance names, parameter overrides and
    // port connections are left exactly as they are.
    replaceToken(node, node.attributes.getChildCount(), makeId(persistString(alloc, it->second)), true);
}

std::shared_ptr<SyntaxTree> InstanceTypeRenamer::apply(const std::shared_ptr<SyntaxTree> tree) {
    return this->transform(tree);
}

std::shared_ptr<SyntaxTree> renameInstanceTypes(const std::shared_ptr<SyntaxTree> tree,
                                                const std::map<std::string, std::string>& renames) {
    InstanceTypeRenamer rewriter({renames.begin(), renames.end()});
    return rewriter.apply(tree);
}

std::shared_ptr<SyntaxTree> renameModule(const std::shared_ptr<SyntaxTree> tree, std::string newName) {
    ModuleNameRewriter rewriter;
    return rewriter.renameModule(tree, newName);
}

std::string getModuleName(const std::shared_ptr<SyntaxTree> tree) {
    ModuleNameFinder finder;
    return finder.getModuleName(tree);
}

namespace {
// Case cuts prune an item so the selector falls through to whatever matches next.
// Modelling that with a select means reproducing the item's match condition, and
// casez/casex match on wildcards that no SystemVerilog operator reproduces exactly
// -- `==?` wildcards z only in its right operand, while casez wildcards z in both
// and casex wildcards x as well. A selector carrying x or z would therefore make
// the mux disagree with the cut it stands for, so refuse rather than emit
// something unsound. Ordinary `case` is unaffected, and so is cutting: the cutter
// removes an item regardless of the case flavour, and still does.
void rejectWildcardCases(const std::shared_ptr<SyntaxTree>& tree,
                         const std::vector<std::pair<const CaseStatementSyntax*, size_t>>& found) {
    auto& sm = tree->sourceManager();
    std::string offenders;
    const CaseStatementSyntax* last = nullptr;
    for (const auto& [caseStmt, itemIdx] : found) {
        if (caseStmt == last)
            continue; // one report per case statement, not per prunable item
        auto kind = caseStmt->caseKeyword.kind;
        if (kind != TokenKind::CaseZKeyword && kind != TokenKind::CaseXKeyword)
            continue;
        last = caseStmt;
        auto loc = caseStmt->caseKeyword.location();
        offenders += "\n  " + std::string(sm.getFileName(loc)) + ":" +
                     std::to_string(sm.getLineNumber(loc)) + ": " +
                     std::string(caseStmt->caseKeyword.valueText());
    }

    if (!offenders.empty()) {
        throw std::runtime_error(
            "mux insertion does not support casez/casex; their wildcard matching cannot be "
            "reproduced faithfully by a select. Re-run with case muxing disabled to reserve "
            "these selects and leave them inert, or rewrite as plain case:" + offenders);
    }
}
} // namespace

// MARK: MuxWirer
namespace {
// Module names instantiated in a tree, in source order, deduplicated.
class InstantiationCollector : public SyntaxVisitor<InstantiationCollector> {
public:
    std::vector<std::string> found;
    void handle(const HierarchyInstantiationSyntax& node) {
        std::string name(node.type.valueText());
        if (std::find(found.begin(), found.end(), name) == found.end())
            found.push_back(name);
        visitDefault(node);
    }
};
} // namespace

std::vector<std::string> getInstantiatedModules(const std::shared_ptr<SyntaxTree> tree) {
    InstantiationCollector IC;
    tree->root().visit(IC);
    return IC.found;
}

void MuxWirer::handle(const PortListSyntax& node) {
    if (extraPorts->empty())
        return;
    if (node.kind == SyntaxKind::NonAnsiPortList || node.kind == SyntaxKind::WildcardPortList)
        throw std::logic_error("Papercuts only supports ANSI port lists");

    auto& ansiNode = node.as<AnsiPortListSyntax>();
    std::string portStr = "input logic ";
    for (size_t i = 0; i < extraPorts->size(); ++i) {
        portStr += (*extraPorts)[i];
        if (i + 1 < extraPorts->size())
            portStr += ", ";
    }
    // Appended for the same reason InputAdder appends: a positional instantiation
    // of this module must keep binding its original ports.
    if (ansiNode.ports.size() > 0)
        insertAtBack(ansiNode.ports, parse(portStr), makeComma());
    else
        insertAtBack(ansiNode.ports, parse(portStr));
}

void MuxWirer::handle(const HierarchyInstantiationSyntax& node) {
    auto it = conns->find(std::string(node.type.valueText()));
    if (it == conns->end() || it->second.empty())
        return;

    for (auto* inst : node.instances) {
        // An instance connected positionally cannot take a named connection, and
        // the select ports sit at the end of the child's list, so append them in
        // the same order instead. A `.*` instance needs the explicit named form:
        // left to the wildcard, the child's pc_sel0 would bind to whatever pc_sel0
        // is visible in the parent -- which is the parent's OWN select of that
        // number, not the forwarded one.
        bool ordered = false;
        for (auto* c : inst->connections) {
            if (c->kind == SyntaxKind::OrderedPortConnection) {
                ordered = true;
                break;
            }
        }

        for (const auto& [port, signal] : it->second) {
            std::string text = ordered ? signal : "." + port + "(" + signal + ")";
            auto& conn = parse(text);
            if (inst->connections.size() > 0)
                insertAtBack(inst->connections, conn, makeComma());
            else
                insertAtBack(inst->connections, conn);
        }
    }
}

std::shared_ptr<SyntaxTree> MuxWirer::wire(
    const std::shared_ptr<SyntaxTree> tree, const std::vector<std::string>& extraPorts,
    const std::map<std::string, std::vector<std::pair<std::string, std::string>>>& conns) {
    this->extraPorts = &extraPorts;
    this->conns = &conns;
    auto out = transform(tree);
    // Re-parse so the result is a clean tree, as insertMuxes also returns.
    return SyntaxTree::fromText(SyntaxPrinter::printFile(*out), tree->sourceManager());
}

std::shared_ptr<SyntaxTree> wireMuxHierarchy(
    const std::shared_ptr<SyntaxTree> tree, const std::vector<std::string>& extraPorts,
    const std::map<std::string, std::vector<std::pair<std::string, std::string>>>& conns) {
    MuxWirer MW;
    return MW.wire(tree, extraPorts, conns);
}

std::shared_ptr<SyntaxTree> insertMuxes(const std::shared_ptr<SyntaxTree> tree, bool bitMux, bool ternaryMux,
                                        bool ifMux, bool caseMux, bool binopMux, bool constForceMux,
                                        bool binopsInConditionsOnly,
                                        const std::unordered_map<std::string, std::vector<std::pair<int, int>>>& symbolicRanges,
                                        bool shrinkWithIntermediate, std::vector<size_t>* insertedOut,
                                        const PortDirections& portDirections) {
    std::shared_ptr<SyntaxTree> newTree = tree;
    std::vector<std::shared_ptr<SyntaxTree>> keepAlive;

    // The band widths come from the cutter itself, on the ORIGINAL tree and with
    // the same configuration, so `pc_sel<N>` is the control for cut N by
    // construction rather than by two counts happening to agree. Recounting here
    // is what broke: the cutter counts a parameterized bit-shrink as a cut and
    // clears the symbolic ranges under --shrink-with-intermediate, and a muxer
    // that reached either conclusion on its own shifted every later band.
    Papercutter cutter(tree, shrinkWithIntermediate, binopsInConditionsOnly, symbolicRanges, portDirections);
    auto bands = cutter.cutBands();  // ternary, if, bitshrink, case, binop, force-const

    // A family whose muxer is disabled -- or a target it cannot build -- still
    // reserves its select numbers, so the families after it stay on their cut
    // indices. Reserved selects become ports with nothing wired to them.
    size_t baseTernary = 0;
    size_t baseIf = baseTernary + bands[0];
    size_t baseBit = baseIf + bands[1];
    size_t baseCase = baseBit + bands[2];
    size_t baseBinop = baseCase + bands[3];
    size_t baseConstForce = baseBinop + bands[4];

    // The muxers collect their own targets, so they must see the same effective
    // symbolic ranges the cutter did -- it drops them entirely for the
    // intermediate-wire strategy.
    std::unordered_map<std::string, std::vector<std::pair<int, int>>> effectiveRanges =
        shrinkWithIntermediate
            ? std::unordered_map<std::string, std::vector<std::pair<int, int>>>{}
            : symbolicRanges;

    MuxContext context;
    context.muxCount = static_cast<int>(baseConstForce + bands[5]);
    if (static_cast<size_t>(context.muxCount) != cutter.getCutCount()) {
        throw std::logic_error("insertMuxes: reserved " + std::to_string(context.muxCount) +
                               " selects for " + std::to_string(cutter.getCutCount()) +
                               " cuts; the select bands and the cut bands have diverged");
    }

    // Only for the wildcard check below; the band width above is the cutter's.
    auto caseNodes = CaseCollector().getFoundNodes(tree);

    // Case cuts prune an item; muxing that means reproducing the item's match, which
    // casez/casex wildcards make impossible to do faithfully. Reject up front rather
    // than emit an unsound mux -- but only when case muxing was actually asked for.
    if (caseMux)
        rejectWildcardCases(tree, caseNodes);

    ParentSetter PS;

    // Ternary and `if` first, and together: they are the only passes that both read
    // and rewrite arbitrary expressions, so they have to run while the tree is still
    // the one their targets were collected from.
    if (ternaryMux || ifMux || binopMux || caseMux) {
        ExprMuxer EM;
        EM.initialize(newTree, ternaryMux, baseTernary, ifMux, baseIf, binopMux, baseBinop,
                      binopsInConditionsOnly, caseMux, baseCase);
        auto transformed = EM.insertExprMuxes(newTree);
        context.insertedSelects.insert(context.insertedSelects.end(), EM.insertedSelects.begin(),
                                       EM.insertedSelects.end());
        keepAlive.push_back(newTree);
        newTree = transformed;
    }

    // Bit-shrink and const-force are declaration-level and keyed by declarator
    // pointer, so each is initialized on the tree it transforms. Nothing above
    // touches declarations, so their target sets and order are unaffected.
    PS.visit(newTree->root());
    if (bitMux) {
        BitMuxer BM(context);
        BM.initialize(newTree, effectiveRanges, &portDirections);
        auto transformed = BM.insertBitShrinkMuxes(newTree, baseBit);
        keepAlive.push_back(newTree);
        newTree = transformed;
    }

    if (constForceMux) {
        // Write sites are recognised by climbing to the enclosing assignment, so the
        // parent pointers must be live on this tree -- the pass above produced a
        // fresh one.
        PS.visit(newTree->root());
        ConstForceMuxer CFM(context);
        CFM.initialize(newTree, &portDirections);
        auto transformed = CFM.insertConstForceMuxes(newTree, baseConstForce);
        keepAlive.push_back(newTree);
        newTree = transformed;
    }

    if (insertedOut) {
        auto sels = context.insertedSelects;
        std::sort(sels.begin(), sels.end());
        sels.erase(std::unique(sels.begin(), sels.end()), sels.end());
        *insertedOut = std::move(sels);
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

    // With no muxes to control there is nothing to add. Falling through would
    // splice a bare "input logic " into the list, which parses cleanly but
    // swallows the first existing port's name.
    if (numInputs <= 0)
        return;

    auto& ansiNode = node.as<AnsiPortListSyntax>();
    std::string newPortStr = "input logic ";
    for (int i = 0; i < numInputs; i++) {
        newPortStr += "pc_sel" + std::to_string(i);
        if (i != numInputs - 1)
            newPortStr += ", ";
    }
    // Appended, not prepended: a parent may connect this module positionally, and
    // ports added at the front would shift every one of those connections onto the
    // wrong port. At the back they are simply the last ports, which is also the
    // order wireMuxHierarchy appends their connections in.
    if (ansiNode.ports.size() > 0)
        insertAtBack(ansiNode.ports, parse(newPortStr), makeComma());
    else
        insertAtBack(ansiNode.ports, parse(newPortStr));
}
std::shared_ptr<SyntaxTree> InputAdder::addInputs(std::shared_ptr<SyntaxTree> tree, int numInputs) {
    this->numInputs = numInputs;
    return transform(tree);
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

// Build the packed-range string for a declaration's shared type, narrowing every
// dimension named in `narrow` by that target's `amount` bits. e.g. dims [3:0][7:0]
// narrowed at dimIndex 1 by 1 -> "[3:0][6:0]". Dimensions not in `narrow` are
// emitted verbatim (via toString) so any bound form survives untouched.
//
// A literal range is recomputed arithmetically; a parameterized one is narrowed
// symbolically by subtracting from its non-literal bound ([WIDTH-1:0] -drop=2->
// [(WIDTH-1)-2:0]). The target's `width` clamps the drop either way -- over-dropping
// a symbolic range yields a reversed range like [-1:0], which is legal SV and
// silently *widens* the signal rather than erroring.
std::string buildPackedRanges(const SyntaxList<VariableDimensionSyntax>& dims,
                              const std::vector<BitShrinkTarget>& narrow);

// One packed dimension of a declaration targeted by the intermediate-wire
// strategy, in declared-index terms. `high`/`low` are the most- and
// least-significant ends whichever way the range was written ([7:0] and [0:7]
// both give high=7, low=0), `inner` is how many bits one position at this
// dimension spans (the product of every dimension below it), and `drop` is how
// many positions this cut removes off the high end (0 = dimension untouched).
struct IntermediateDim {
    int high, low, width, inner, drop;
    bool descending;
    // Name of the select that gates this dimension's cut. Empty for the cutter,
    // which applies the cut unconditionally; the muxer fills it in so the dropped
    // bits become a choice between the shrunk value and the original.
    std::string select;
};

// Describe every packed dimension of a declaration, folding in the active cuts.
// Returns nullopt if any dimension is not a literal range -- intermediate mode
// clears symbolicRanges, so that should not happen, but a non-range dimension
// (wildcard/queue) must not be rebuilt blindly.
std::optional<std::vector<IntermediateDim>> describeDims(
        const SyntaxList<VariableDimensionSyntax>& dims,
        const std::vector<BitShrinkTarget>& targets,
        const std::vector<std::string>& selects = {}) {
    std::vector<IntermediateDim> out;
    for (size_t di = 0; di < dims.size(); ++di) {
        auto* spec = dims[di]->specifier->as_if<RangeDimensionSpecifierSyntax>();
        if (!spec)
            return std::nullopt;
        auto* sel = spec->selector->as_if<RangeSelectSyntax>();
        if (!sel)
            return std::nullopt;
        if (!classifyRange(*sel).literal)
            return std::nullopt;

        int l = tokenToInt(sel->left->as<LiteralExpressionSyntax>().literal);
        int r = tokenToInt(sel->right->as<LiteralExpressionSyntax>().literal);
        IntermediateDim d{};
        d.descending = l >= r;
        d.high = std::max(l, r);
        d.low = std::min(l, r);
        d.width = std::abs(l - r) + 1;
        d.inner = 1;
        d.drop = 0;
        for (size_t ti = 0; ti < targets.size(); ++ti) {
            if (targets[ti].dimIndex == (int)di) {
                // Keep at least one position: never shrink a dimension away entirely.
                d.drop = std::clamp(targets[ti].amount, 1, std::max(1, d.width - 1));
                if (ti < selects.size())
                    d.select = selects[ti];
            }
        }
        out.push_back(d);
    }
    for (size_t i = 0; i < out.size(); ++i)
        for (size_t j = i + 1; j < out.size(); ++j)
            out[i].inner *= out[j].width;
    return out;
}

// `n` copies of `bit`, written plainly when there is only one so the common
// single-bit shrink still reads as `{1'b0, x[6:0]}`.
std::string fillBits(int n, const std::string& bit) {
    return n == 1 ? bit : "{" + std::to_string(n) + "{" + bit + "}}";
}

// The bit whose value the dropped high bits must take to sign-extend the
// retained value. Only meaningful for a cut on the outermost dimension, since an
// element or part-select of a packed array is unsigned regardless of the
// declaration's signing -- so a cut on any inner dimension puts zero-fill above
// that dimension's top retained position, making the retained value's MSB 0 and
// sign-extension degenerate into zero-extension. When that inner cut is gated by
// a select the degeneration is conditional too, so the bit becomes a mux.
std::string signFillBit(const std::string& name, const std::vector<IntermediateDim>& dims,
                        size_t level = 0, const std::string& prefix = "") {
    if (level == dims.size())
        return name + prefix; // a single bit

    const auto& d = dims[level];
    if (level > 0 && d.drop > 0) {
        std::string orig = signFillBit(name, dims, level + 1, prefix + "[" + std::to_string(d.high) + "]");
        if (d.select.empty())
            return "1'b0";
        return "(" + d.select + " ? 1'b0 : " + orig + ")";
    }
    // At the outermost dimension the drop has already chosen the new top position;
    // deeper untouched dimensions keep theirs.
    return signFillBit(name, dims, level + 1,
                       prefix + "[" + std::to_string(d.high - (level == 0 ? d.drop : 0)) + "]");
}

std::string buildIntermediateValue(const std::string& name,
                                   const std::vector<IntermediateDim>& dims, size_t level,
                                   const std::string& prefix, const std::string& fillBit);

// Render positions [hi..lo] of dimension `level`, most-significant first. With no
// cut below them they are contiguous, so one range select covers the lot; if a
// deeper dimension is cut each position has to be rebuilt on its own.
std::string renderPositions(const std::string& name, const std::vector<IntermediateDim>& dims,
                            size_t level, const std::string& prefix, int hi, int lo,
                            bool cutBelow) {
    const auto& d = dims[level];
    if (!cutBelow) {
        if (hi == lo)
            return name + prefix + "[" + std::to_string(hi) + "]";
        return name + prefix +
               (d.descending ? "[" + std::to_string(hi) + ":" + std::to_string(lo) + "]"
                             : "[" + std::to_string(lo) + ":" + std::to_string(hi) + "]");
    }
    std::string out;
    for (int i = hi; i >= lo; --i) {
        // Positions below the outermost dimension are unsigned, so they zero-fill.
        out += buildIntermediateValue(name, dims, level + 1,
                                      prefix + "[" + std::to_string(i) + "]", "1'b0");
        if (i != lo)
            out += ", ";
    }
    // A lone position is already a self-contained expression; don't wrap it.
    return hi == lo ? out : "{" + out + "}";
}

// Build the value the companion signal takes: the original at its original width,
// with the bits each cut drops replaced by `fillBit` -- unconditionally for the
// cutter, or behind that dimension's select for the muxer.
std::string buildIntermediateValue(const std::string& name,
                                   const std::vector<IntermediateDim>& dims, size_t level,
                                   const std::string& prefix, const std::string& fillBit) {
    bool cutBelow = false;
    for (size_t i = level + 1; i < dims.size(); ++i)
        if (dims[i].drop > 0)
            cutBelow = true;

    const auto& d = dims[level];
    if (d.drop == 0 && !cutBelow)
        return name + prefix; // nothing cut at or below here; pass the subarray through

    int keepHigh = d.high - d.drop;
    std::string retained = renderPositions(name, dims, level, prefix, keepHigh, d.low, cutBelow);
    if (d.drop == 0)
        return retained; // only deeper dimensions are cut, and those are applied already

    std::string fill = fillBits(d.drop * d.inner, fillBit);
    std::string dropped =
        d.select.empty()
            ? fill
            : "(" + d.select + " ? " + fill + " : " +
                  renderPositions(name, dims, level, prefix, d.high, keepHigh + 1, cutBelow) + ")";
    return "{" + dropped + ", " + retained + "}";
}

std::string buildIntermediateRhs(const std::string& name,
                                 const std::vector<IntermediateDim>& dims, bool isSigned) {
    std::string fillBit = "1'b0";
    if (isSigned && dims[0].drop > 0)
        fillBit = signFillBit(name, dims);
    return buildIntermediateValue(name, dims, 0, "", fillBit);
}

std::optional<DeclShape> shapeOf(const DataDeclarationSyntax& node) {
    // The collector only accepts logic/reg/bit here, so the packed range is always
    // on an IntegerTypeSyntax.
    auto* intType = node.type->as_if<IntegerTypeSyntax>();
    if (!intType)
        return std::nullopt;

    DeclShape out;
    std::string mods = trimWs(listToString(node.modifiers));
    out.typeHead = (mods.empty() ? "" : mods + " ") + std::string(intType->keyword.valueText());
    if (intType->signing)
        out.typeHead += " " + std::string(intType->signing.valueText());
    out.dims = &intType->dimensions;
    out.isSigned = intType->signing && intType->signing.kind != TokenKind::UnsignedKeyword;
    return out;
}

std::optional<DeclShape> shapeOf(const NetDeclarationSyntax& node) {
    // logic/reg/bit (and `wire logic`) are IntegerTypeSyntax; a bare `wire [7:0]`
    // is ImplicitTypeSyntax. Both expose signing and the packed dimensions.
    Token keyword, signing;
    const SyntaxList<VariableDimensionSyntax>* dims = nullptr;
    if (auto* intType = node.type->as_if<IntegerTypeSyntax>()) {
        keyword = intType->keyword;
        signing = intType->signing;
        dims = &intType->dimensions;
    }
    else if (auto* impType = node.type->as_if<ImplicitTypeSyntax>()) {
        signing = impType->signing;
        dims = &impType->dimensions;
    }
    else {
        return std::nullopt;
    }

    DeclShape out;
    out.typeHead = std::string(node.netType.valueText());
    if (keyword)
        out.typeHead += " " + std::string(keyword.valueText());
    if (signing)
        out.typeHead += " " + std::string(signing.valueText());
    out.dims = dims;
    out.isSigned = signing && signing.kind != TokenKind::UnsignedKeyword;
    return out;
}

// The companion declaration and the continuous assignment that drives it, as
// source text. `selects` pairs positionally with `targets`; leave it empty for an
// unconditional shrink. Returns nullopt when a dimension is not a literal range.
struct IntermediateWire {
    std::string decl, assign;
};

std::optional<IntermediateWire> buildIntermediateWire(const std::string& trivia,
                                                      const DeclShape& shape, const std::string& name,
                                                      const std::vector<BitShrinkTarget>& targets,
                                                      const std::vector<std::string>& selects = {}) {
    auto described = describeDims(*shape.dims, targets, selects);
    if (!described)
        return std::nullopt;

    std::string newName = name + "_papercuts";
    IntermediateWire out;
    out.decl = trivia + shape.typeHead + " " + buildPackedRanges(*shape.dims, {}) + " " + newName + ";";
    out.assign = trivia + "assign " + newName + " = " +
                 buildIntermediateRhs(name, *described, shape.isSigned) + ";";
    return out;
}

std::string buildPackedRanges(const SyntaxList<VariableDimensionSyntax>& dims,
                              const std::vector<BitShrinkTarget>& narrow) {
    std::string out;
    for (size_t di = 0; di < dims.size(); ++di) {
        auto it = std::find_if(narrow.begin(), narrow.end(),
                               [&](const BitShrinkTarget& t) { return t.dimIndex == (int)di; });
        if (it == narrow.end()) {
            out += trimWs(std::string(dims[di]->toString()));
            continue;
        }

        auto& rsel = dims[di]->specifier->as<RangeDimensionSpecifierSyntax>().selector->as<RangeSelectSyntax>();
        // Keep at least one bit: never shrink to a zero-width or reversed range.
        int drop = std::clamp(it->amount, 1, std::max(1, it->width - 1));

        if (auto bounds = classifyRange(rsel); bounds.literal) {
            // [7:0] -drop=2-> [5:0]; [0:7] -drop=2-> [0:5].
            int l = tokenToInt(rsel.left->as<LiteralExpressionSyntax>().literal);
            int r = tokenToInt(rsel.right->as<LiteralExpressionSyntax>().literal);
            (l >= r ? l : r) -= drop;
            out += "[" + std::to_string(l) + ":" + std::to_string(r) + "]";
        } else {
            std::string sym = "(" + trimWs(std::string(bounds.symbolic->toString())) + ")-" +
                              std::to_string(drop);
            bool symIsLeft = bounds.symbolic == rsel.left;
            out += "[" + (symIsLeft ? sym : trimWs(std::string(rsel.left->toString()))) + ":" +
                   (symIsLeft ? trimWs(std::string(rsel.right->toString())) : sym) + "]";
        }
    }
    return out;
}
} // namespace

// Defined with the const-force cut logic further down. True when this identifier
// is written: (part of) the LHS of an assignment -- of ANY assignment operator,
// `<=` included -- or connected to an instance's output/inout port. A muxer
// must not redirect it onto a read-side companion, nor a cut substitute it.
static bool isAssignmentWriteTarget(const SyntaxNode& node, const PortDirections* ports);

// MARK: BitMuxer
std::shared_ptr<SyntaxTree> BitMuxer::insertBitShrinkMuxes(const std::shared_ptr<SyntaxTree> tree,
                                                           size_t baseSel) {
    if (!initialized) {
        throw std::logic_error("BitMuxer must be initialized before insertBitShrinkMuxes");
    }
    initialized = false; // Reset the initialized flag for the next time we want to use this BitMuxer
    this->baseSel = baseSel;
    return transform(tree);
}

void BitMuxer::initialize(const std::shared_ptr<SyntaxTree> tree,
                          const std::unordered_map<std::string, std::vector<std::pair<int, int>>>& symbolicRanges,
                          const PortDirections* ports) {
    this->ports = ports;
    targetsByDecl.clear();
    muxedNames.clear();

    // Same configuration Papercutter uses, so the muxer sees exactly the cuts the
    // cutter enumerates and select N stays the control for bit-shrink cut N. That
    // includes the symbolic ranges: without them a parameterized dimension is a cut
    // to the cutter and invisible here, and the two band widths diverge.
    BitShrinkCollector collector(symbolicRanges);
    shrinkNodes = collector.getFoundNodes(tree);
    for (size_t i = 0; i < shrinkNodes.size(); ++i) {
        // `i` is the target's position in the cut order and fixes its select, so a
        // target that cannot be muxed still consumes its index. It must NOT reach
        // muxedNames, though: that set redirects reads onto the companion wire, and
        // redirecting onto a wire no declaration was emitted for produces source
        // that references an undeclared signal.
        if (!canBuildCompanion(shrinkNodes[i]))
            continue;
        targetsByDecl[shrinkNodes[i].decl].push_back(i);
        muxedNames.insert(std::string(shrinkNodes[i].decl->name.valueText()));
    }

    initialized = true;
}

void BitMuxer::muxDeclaration(const SyntaxNode& node,
                              const SeparatedSyntaxList<DeclaratorSyntax>& declarators,
                              const DeclShape& shape) {
    std::string trivia;
    for (const auto& t : node.getFirstToken().trivia())
        trivia += t.getRawText();

    for (const auto* d : declarators) {
        auto it = targetsByDecl.find(d);
        if (it == targetsByDecl.end())
            continue;

        // One select per target, numbered by the target's position in the cut order.
        std::vector<BitShrinkTarget> targets;
        std::vector<std::string> selects;
        for (size_t idx : it->second) {
            targets.push_back(shrinkNodes[idx]);
            selects.push_back("pc_sel" + std::to_string(baseSel + idx));
        }

        auto wire = buildIntermediateWire(trivia, shape, std::string(d->name.valueText()), targets,
                                          selects);
        if (!wire)
            continue;
        insertAfter(node, parse(wire->decl));
        insertAfter(node, parse(wire->assign));
        for (size_t idx : it->second)
            context.insertedSelects.push_back(baseSel + idx);
    }
}

void BitMuxer::handle(const DataDeclarationSyntax& node) {
    if (auto shape = shapeOf(node))
        muxDeclaration(node, node.declarators, *shape);
    this->visitDefault(node);
}

void BitMuxer::handle(const NetDeclarationSyntax& node) {
    if (auto shape = shapeOf(node))
        muxDeclaration(node, node.declarators, *shape);
    this->visitDefault(node);
}

void BitMuxer::handle(const IdentifierNameSyntax& node) {
    // The companion carries the READ value; the original signal keeps its driver.
    // Every assignment operator counts, `<=` included: redirecting a procedural
    // write leaves the original undriven and gives the companion a second driver,
    // so the muxed design stops matching the original with all selects off.
    if (isAssignmentWriteTarget(node, ports)) {
        return;
    }

    std::string nodeName{node.identifier.valueText()};
    if (muxedNames.contains(nodeName)) {
        replaceToken(node, 0, makeId(persistString(alloc, nodeName + "_papercuts")), true);
    }
}

void BitMuxer::handle(const IdentifierSelectNameSyntax& node) {
    if (isAssignmentWriteTarget(node, ports)) {
        return; // see handle(IdentifierNameSyntax)
    }

    std::string oldName{node.identifier.valueText()};
    if (muxedNames.contains(oldName)) {
        replaceToken(node, 0, makeId(persistString(alloc, oldName + "_papercuts")), true);
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
                                           SyntaxList<VariableDimensionSyntax>{}, nullptr);

        auto declElem = std::span(alloc.emplace<TokenOrSyntax>(&newDecl), size_t{1});
        SeparatedSyntaxList<DeclaratorSyntax> declList(alloc, declElem);

        auto& newDataDecl = factory.dataDeclaration(SyntaxList<AttributeInstanceSyntax>{},
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
    } else if (auto* netDecl = node.parent->as_if<NetDeclarationSyntax>()) {
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

    if (dims->size() == 0) {
        return;
    }
    // Emit one shrink target per packed dimension. Narrow mode shrinks each
    // dimension independently ([3:0][7:0] -> a cut for [3:0] and a cut for [7:0]);
    // single-dim mode only ever sees one dimension here.
    for (size_t di = 0; di < dims->size(); ++di) {
        // Non-range dimensions (wildcard/queue) are skipped per dimension rather
        // than throwing, so the other dimensions still yield cuts.
        auto* dimSpec = (*dims)[di]->specifier->as_if<RangeDimensionSpecifierSyntax>();
        if (!dimSpec)
            continue;
        auto* dimSelect = dimSpec->selector->as_if<RangeSelectSyntax>();
        if (!dimSelect)
            continue;

        // A literal range carries its own width; a parameterized one (`[WIDTH-1:0]`)
        // is only shrinkable when the caller supplied what it evaluates to.
        auto bounds = classifyRange(*dimSelect);
        int width = bounds.width;
        if (!bounds.literal) {
            // A parameterized bound is only shrinkable when the caller supplied the
            // range it evaluates to. symbolicRanges holds one (left, right) per
            // packed dimension, in declaration order (outermost first), so this
            // dimension's bounds are looked up by its index -- letting every
            // dimension of a multi-packed-dim vector be sized independently.
            if (!bounds.symbolic)
                continue;
            auto it = symbolicRanges.find(std::string(node.name.valueText()));
            if (it == symbolicRanges.end())
                continue;
            const auto& dimRanges = it->second;
            if (di >= dimRanges.size())
                continue; // no evaluated bounds supplied for this dimension
            auto [l, r] = dimRanges[di];
            // Removing bits means moving the range's HIGH end inward, so the
            // symbolic bound has to be that end. It usually is (`[W-1:0]` and
            // `[0:W-1]` alike), but not for a reversed range such as the `[-1:0]`
            // a placeholder `parameter W = 0` produces -- there the symbolic left
            // is the LOW end, and subtracting from it would widen the signal.
            if ((bounds.symbolic == dimSelect->left) != (l > r))
                continue;
            width = std::abs(l - r) + 1;
        }

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

// MARK: ConstForceCollector
// Collect 1-bit scalar declarators (the complement of what BitShrinkCollector
// keeps: it bails on 0 dims / width<=1). Same type gate as bit-shrink; signing
// is irrelevant when substituting a constant.
// MARK: ConstForceMuxer
std::shared_ptr<SyntaxTree> ConstForceMuxer::insertConstForceMuxes(const std::shared_ptr<SyntaxTree> tree,
                                                                   size_t baseSel) {
    if (!initialized) {
        throw std::logic_error("ConstForceMuxer must be initialized before insertConstForceMuxes");
    }
    initialized = false;
    this->baseSel = baseSel;
    return transform(tree);
}

void ConstForceMuxer::initialize(const std::shared_ptr<SyntaxTree> tree, const PortDirections* ports) {
    this->ports = ports;
    ordinalByDecl.clear();
    muxedNames.clear();

    ConstForceCollector collector;
    auto found = collector.getFoundNodes(tree);
    declCount = found.size();
    for (size_t i = 0; i < found.size(); ++i) {
        ordinalByDecl[found[i]] = i;
        muxedNames.insert(std::string(found[i]->name.valueText()));
    }

    initialized = true;
}

void ConstForceMuxer::muxDeclaration(const SyntaxNode& node,
                                     const SeparatedSyntaxList<DeclaratorSyntax>& declarators,
                                     const DeclShape& shape) {
    std::string trivia;
    for (const auto& t : node.getFirstToken().trivia())
        trivia += t.getRawText();

    for (const auto* d : declarators) {
        auto it = ordinalByDecl.find(d);
        if (it == ordinalByDecl.end())
            continue;

        std::string name(d->name.valueText());
        std::string newName = name + "_papercuts";
        std::string sel0 = "pc_sel" + std::to_string(baseSel + 2 * it->second);
        std::string sel1 = "pc_sel" + std::to_string(baseSel + 2 * it->second + 1);
        context.insertedSelects.push_back(baseSel + 2 * it->second);
        context.insertedSelects.push_back(baseSel + 2 * it->second + 1);

        insertAfter(node, parse(trivia + shape.typeHead + " " +
                                buildPackedRanges(*shape.dims, {}) + " " + newName + ";"));
        // Force-1 is checked first so the two selects can never disagree; with both
        // low the signal passes through untouched.
        insertAfter(node, parse(trivia + "assign " + newName + " = " + sel1 + " ? 1'b1 : (" + sel0 +
                                " ? 1'b0 : " + name + ");"));
    }
}

void ConstForceMuxer::handle(const DataDeclarationSyntax& node) {
    if (auto shape = shapeOf(node))
        muxDeclaration(node, node.declarators, *shape);
    this->visitDefault(node);
}

void ConstForceMuxer::handle(const NetDeclarationSyntax& node) {
    if (auto shape = shapeOf(node))
        muxDeclaration(node, node.declarators, *shape);
    this->visitDefault(node);
}

void ConstForceMuxer::handle(const IdentifierNameSyntax& node) {
    // The cut substitutes a literal at read sites only, so the companion signal is
    // redirected the same way -- writes keep driving the original.
    if (isAssignmentWriteTarget(node, ports))
        return;

    std::string name{node.identifier.valueText()};
    if (muxedNames.contains(name))
        replaceToken(node, 0, makeId(persistString(alloc, name + "_papercuts")), true);
}

void ConstForceCollector::handle(const DeclaratorSyntax& node) {
    const DataTypeSyntax* type = nullptr;
    if (auto* dataDecl = node.parent->as_if<DataDeclarationSyntax>()) {
        auto kind = dataDecl->type->kind;
        if (kind != SyntaxKind::LogicType && kind != SyntaxKind::RegType && kind != SyntaxKind::BitType)
            return;
        type = dataDecl->type;
    } else if (auto* netDecl = node.parent->as_if<NetDeclarationSyntax>()) {
        if (netDecl->strength || netDecl->delay)
            return;
        type = netDecl->type;
    } else {
        return;
    }

    const SyntaxList<VariableDimensionSyntax>* dims = nullptr;
    if (auto* intType = type->as_if<IntegerTypeSyntax>())
        dims = &intType->dimensions;
    else if (auto* impType = type->as_if<ImplicitTypeSyntax>())
        dims = &impType->dimensions;
    else
        return;

    if (dims->size() == 0) {
        foundNodes.push_back(&node); // bare scalar (`logic x;`) is 1 bit
        return;
    }
    if (dims->size() != 1)
        return;
    auto* dimSpec = (*dims)[0]->specifier->as_if<RangeDimensionSpecifierSyntax>();
    if (!dimSpec)
        return;
    auto* dimSelect = dimSpec->selector->as_if<RangeSelectSyntax>();
    if (!dimSelect)
        return;
    auto bounds = classifyRange(*dimSelect);
    if (bounds.literal && bounds.width == 1)
        foundNodes.push_back(&node); // `logic [0:0] x;`
}

std::vector<const DeclaratorSyntax*> ConstForceCollector::getFoundNodes(
    const std::shared_ptr<SyntaxTree> tree) {
    tree->root().visit(*this);
    return this->foundNodes;
}

// MARK: ExprMuxer
std::shared_ptr<SyntaxTree> ExprMuxer::insertExprMuxes(const std::shared_ptr<SyntaxTree> tree) {
    if (!initialized)
        throw std::logic_error("ExprMuxer must be initialized before insertExprMuxes");
    initialized = false;
    return transform(tree);
}

void ExprMuxer::initialize(const std::shared_ptr<SyntaxTree> tree, bool ternaryMux,
                           size_t baseTernary, bool ifMux, size_t baseIf, bool binopMux,
                           size_t baseBinop, bool binopsInConditionsOnly, bool caseMux,
                           size_t baseCase) {
    ternarySel.clear();
    ifSel.clear();
    binopSel.clear();
    caseSel.clear();

    if (ternaryMux) {
        TernaryCollector TC;
        auto nodes = TC.getFoundNodes(tree);
        for (size_t i = 0; i < nodes.size(); ++i)
            ternarySel[nodes[i]] = baseTernary + 2 * i;
    }
    if (ifMux) {
        IfCollector IC;
        auto nodes = IC.getFoundNodes(tree);
        for (size_t i = 0; i < nodes.size(); ++i)
            ifSel[nodes[i]] = baseIf + 2 * i;
    }
    if (binopMux) {
        // The collector emits keep-left then, unless this is a shift, keep-right for
        // each node, so the two selects of a node are adjacent and keep-left is first.
        BinopCollector BC(binopsInConditionsOnly);
        auto found = BC.getFoundNodes(tree);
        for (size_t i = 0; i < found.size(); ++i) {
            const auto& [node, keepLeft] = found[i];
            if (keepLeft)
                binopSel[node] = {baseBinop + i, std::nullopt};
            else
                binopSel[node].second = baseBinop + i;
        }
    }
    if (caseMux) {
        CaseCollector CC;
        auto found = CC.getFoundNodes(tree);
        for (size_t i = 0; i < found.size(); ++i)
            caseSel[found[i].first][found[i].second] = baseCase + i;
    }

    initialized = true;
}

ExpressionSyntax& ExprMuxer::muxPredicate(const ConditionalPredicateSyntax& pred, size_t sel) {
    std::string sel0 = "pc_sel" + std::to_string(sel);
    std::string sel1 = "pc_sel" + std::to_string(sel + 1);

    // The original predicate expression is reused as a node, so a mux nested inside
    // it still resolves when the tree is cloned.
    auto& inner = makeBinary(SyntaxKind::BinaryAndExpression, makeNot(makeIdentExpr(sel0)),
                             TokenKind::And, "&", makeParen(*pred.conditions[0]->expr, true));
    return makeParen(makeBinary(SyntaxKind::BinaryOrExpression, makeIdentExpr(sel1), TokenKind::Or,
                                "|", makeParen(inner, true)));
}

void ExprMuxer::handle(const ConditionalExpressionSyntax& node) {
    if (auto it = ternarySel.find(&node); it != ternarySel.end()) {
        replace(*node.predicate, makeConditionalPredicate(muxPredicate(*node.predicate, it->second)));
        insertedSelects.push_back(it->second);
        insertedSelects.push_back(it->second + 1);
    }
    visitDefault(node);
}

void ExprMuxer::handle(const ConditionalStatementSyntax& node) {
    if (auto it = ifSel.find(&node); it != ifSel.end()) {
        replace(*node.predicate, makeConditionalPredicate(muxPredicate(*node.predicate, it->second)));
        insertedSelects.push_back(it->second);
        insertedSelects.push_back(it->second + 1);
    }
    visitDefault(node);
}

void ExprMuxer::handle(const CaseStatementSyntax& node) {
    auto found = caseSel.find(&node);
    if (found == caseSel.end()) {
        visitDefault(node);
        return;
    }

    // A case cut removes an item, sending the selector to whatever matches NEXT --
    // not straight to the default. Only a priority chain reproduces that, so the
    // statement becomes an if/else-if chain over the same items in the same order,
    // each guarded by its select. The collector guarantees a default, which becomes
    // the final else, so the chain always terminates. casez/casex are rejected
    // before we get here: `==?` cannot reproduce their wildcard matching.
    const auto& selForItem = found->second;

    SyntaxNode* tail = nullptr;
    for (auto* item : node.items) {
        if (item->kind == SyntaxKind::DefaultCaseItem)
            tail = item->as<DefaultCaseItemSyntax>().clause;
    }

    size_t outermost = node.items.size();
    for (size_t i = 0; i < node.items.size(); ++i) {
        if (node.items[i]->kind != SyntaxKind::DefaultCaseItem) {
            outermost = i;
            break;
        }
    }

    // Built back to front, so each iteration's else clause is the chain so far.
    for (size_t r = node.items.size(); r-- > 0;) {
        // Lists now propagate const to their elements; the operands are reused
        // (never mutated) in the replacement, as they were under the old API.
        auto* item = const_cast<CaseItemSyntax*>(node.items[r]);
        if (item->kind != SyntaxKind::StandardCaseItem)
            continue;
        auto& std_item = item->as<StandardCaseItemSyntax>();

        // (expr === E0) || (expr === E1) || ... -- the selector node is reused once
        // per comparison, which composes fine (each occurrence resolves on its own).
        ExpressionSyntax* match = nullptr;
        for (auto* e : std_item.expressions) {
            auto& cmp = makeParen(makeBinary(SyntaxKind::CaseEqualityExpression, *node.expr,
                                             TokenKind::TripleEquals, "===", *e));
            match = match ? &makeBinary(SyntaxKind::LogicalOrExpression, *match, TokenKind::DoubleOr,
                                        "||", cmp)
                          : &cmp;
        }
        if (!match)
            continue;

        ExpressionSyntax* guard = match;
        if (auto sel = selForItem.find(r); sel != selForItem.end()) {
            guard = &makeBinary(SyntaxKind::LogicalAndExpression,
                                makeNot(makeIdentExpr("pc_sel" + std::to_string(sel->second))),
                                TokenKind::DoubleAnd, "&&", makeParen(*match, true));
            insertedSelects.push_back(sel->second);
        }

        ElseClauseSyntax* elseClause =
            tail ? &factory.elseClause(makeToken(TokenKind::ElseKeyword, "else", spaced()), *tail)
                 : nullptr;

        // `unique`/`priority` qualifies the whole chain, so it belongs on the
        // outermost `if` only.
        tail = &factory.conditionalStatement(
            nullptr, SyntaxList<AttributeInstanceSyntax>{},
            r == outermost ? node.uniqueOrPriority : Token{},
            makeToken(TokenKind::IfKeyword, "if", spaced()),
            makeToken(TokenKind::OpenParenthesis, "(", spaced()), makeConditionalPredicate(*guard),
            makeToken(TokenKind::CloseParenthesis, ")"), std_item.clause->as<StatementSyntax>(),
            elseClause);
    }

    if (tail)
        replace(node, *tail);
    // Descend so cuts nested in the item statements and in the selector still apply.
    visitDefault(node);
}

void ExprMuxer::handle(const BinaryExpressionSyntax& node) {
    if (auto it = binopSel.find(&node); it != binopSel.end()) {
        auto [selL, selR] = it->second;

        // The operator is rebuilt rather than reused: splicing `node` itself back in
        // as an operand of its own replacement would make resolveReplacement chase
        // the replacement in a cycle. Its operands, though, are reused as nodes, so
        // a binop nested in either side keeps its own selects -- and keep-left then
        // yields the cut form of the left operand, which is what composing the two
        // cuts means.
        auto& original = makeParen(makeBinary(node.kind, *node.left, node.operatorToken, *node.right));

        // Innermost first: keep-right when its select is set, else the original.
        ExpressionSyntax* value = &original;
        if (selR)
            value = &makeParen(makeTernary(makeIdentExpr("pc_sel" + std::to_string(*selR)),
                                           makeParen(*node.right, true), original));
        auto& muxed = makeParen(makeTernary(makeIdentExpr("pc_sel" + std::to_string(selL)),
                                            makeParen(*node.left, true), *value));
        replace(node, muxed);
        insertedSelects.push_back(selL);
        if (selR)
            insertedSelects.push_back(*selR);
    }
    // Descend regardless: nested replacements are registered here and resolved into
    // the reused operands when the tree is cloned.
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
        bool keepTrueBranch = (i % 2 != 0);
        nodesToChange.emplace(ifNodes[nodeIndex], keepTrueBranch);
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

namespace {
// Every identifier referenced under a node.
class IdentifierNameCollector : public SyntaxVisitor<IdentifierNameCollector> {
public:
    std::unordered_set<std::string> names;
    void handle(const IdentifierNameSyntax& node) {
        names.insert(std::string(node.identifier.valueText()));
        visitDefault(node);
    }
    void handle(const IdentifierSelectNameSyntax& node) {
        names.insert(std::string(node.identifier.valueText()));
        visitDefault(node);
    }
};

// Signals qualified by an edge in an event list, one entry per edge event.
class EdgeSignalCollector : public SyntaxVisitor<EdgeSignalCollector> {
public:
    std::vector<std::string> names;
    void handle(const SignalEventExpressionSyntax& node) {
        if (node.edge) {
            IdentifierNameCollector inc;
            node.expr->visit(inc);
            for (const auto& n : inc.names)
                names.push_back(n);
        }
        visitDefault(node);
    }
};
} // namespace

void IfCollector::handle(const ProceduralBlockSyntax& node) {
    // Only this block's own event list decides; its body is visited with whatever
    // that list says, and restored afterwards so sibling blocks are unaffected.
    std::vector<std::string> edges;
    if (auto* timed = node.statement->as_if<TimingControlStatementSyntax>()) {
        EdgeSignalCollector esc;
        timed->timingControl->visit(esc);
        edges = std::move(esc.names);
    }

    auto saved = asyncEdgeSignals;
    asyncEdgeSignals.clear();
    if (edges.size() >= 2)  // one edge is an ordinary synchronous block
        asyncEdgeSignals.insert(edges.begin(), edges.end());
    this->visitDefault(node);
    asyncEdgeSignals = std::move(saved);
}

void IfCollector::handle(const ConditionalStatementSyntax& node) {
    if (!asyncEdgeSignals.empty()) {
        IdentifierNameCollector inc;
        node.predicate->visit(inc);
        for (const auto& n : inc.names) {
            if (asyncEdgeSignals.contains(n)) {
                // The asynchronous reset test: not a cut site in any form. Keep
                // descending, though -- the ifs inside it are ordinary cuts.
                this->visitDefault(node);
                return;
            }
        }
    }
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

// Operators whose operands can be dropped to test for a dead operand: bitwise
// (&, |, ^, ~^), logical (&&, ||), and shifts (<<, >>, <<<, >>>). Excludes
// assignments (left is an lvalue), comparisons/equality (1-bit result), and the
// property operators (->, <->). Arithmetic (+, -, *, /, %, **) is deliberately
// excluded: reducing an arithmetic operand rarely isolates dead logic.
static bool isReducibleBinop(SyntaxKind kind) {
    switch (kind) {
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

// Short, stable operator tag for a reducible binary expression, embedded in the
// cut type string (e.g. "binop(mul,keep-left)") so per-type logging can tell
// which operator a cut tried to prune. Kept terse and vendor-neutral; anything
// not in the reducible set collapses to the generic "binop".
static std::string_view binopName(SyntaxKind kind) {
    switch (kind) {
        case SyntaxKind::BinaryAndExpression: return "and";
        case SyntaxKind::BinaryOrExpression: return "or";
        case SyntaxKind::BinaryXorExpression: return "xor";
        case SyntaxKind::BinaryXnorExpression: return "xnor";
        case SyntaxKind::LogicalAndExpression: return "land";
        case SyntaxKind::LogicalOrExpression: return "lor";
        case SyntaxKind::LogicalShiftLeftExpression: return "shl";
        case SyntaxKind::LogicalShiftRightExpression: return "shr";
        case SyntaxKind::ArithmeticShiftLeftExpression: return "ashl";
        case SyntaxKind::ArithmeticShiftRightExpression: return "ashr";
        default: return "binop";
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
    // In conditions-only mode, skip binops that aren't inside a conditional's
    // predicate (predicateDepth tracks ConditionalPredicateSyntax nesting). We
    // still descend so nested binops -- including those deeper in a condition,
    // e.g. the `a & b` in `if ((a & b) | c)` -- are reached.
    if (isReducibleBinop(node.kind) && (!conditionsOnly || predicateDepth > 0)) {
        this->foundNodes.emplace_back(&node, true); // keep-left
        if (!isShiftBinop(node.kind)) {
            this->foundNodes.emplace_back(&node, false); // keep-right
        }
    }
    this->visitDefault(node);
}

// A ConditionalPredicateSyntax is the condition of an `if` statement or a
// ternary (`?:`) -- the only two grammar nodes that own one. Mark its whole
// subtree as "inside a condition" so conditions-only mode can gate on it.
void BinopCollector::handle(const ConditionalPredicateSyntax& node) {
    predicateDepth++;
    this->visitDefault(node);
    predicateDepth--;
}

std::vector<std::pair<const BinaryExpressionSyntax*, bool>> BinopCollector::getFoundNodes(
    const std::shared_ptr<SyntaxTree> tree) {
    tree->root().visit(*this);

    return this->foundNodes;
}

// MARK: Papercutter

Papercutter::Papercutter(const std::shared_ptr<SyntaxTree> tree, bool shrinkWithIntermediate,
                         bool binopsInConditionsOnly,
                         std::unordered_map<std::string, std::vector<std::pair<int, int>>> symbolicRanges,
                         PortDirections portDirections)
    : tree(tree), shrinkWithIntermediate(shrinkWithIntermediate),
      binopsInConditionsOnly(binopsInConditionsOnly), portDirections(std::move(portDirections)) {

    // Both strategies handle signed decls, nets and multi-packed-dim vectors.
    // Parameterized ranges remain narrow-only: the intermediate wire has to name
    // the retained bits explicitly, which needs a literal width.
    if (shrinkWithIntermediate)
        symbolicRanges.clear();
    BitShrinkCollector BSC(std::move(symbolicRanges));
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

    BinopCollector BC(binopsInConditionsOnly);
    binopNodes = BC.getFoundNodes(tree);
    cutCount += binopNodes.size();
    BRCount = binopNodes.size();

    ConstForceCollector CFC;
    constForceNodes = CFC.getFoundNodes(tree);
    cutCount += constForceNodes.size() * 2; // force-0 and force-1 per signal
    CFRCount = constForceNodes.size() * 2;
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

    auto constForceTrees = removeAllConstForces();
    newTrees.insert(newTrees.end(), constForceTrees.begin(), constForceTrees.end());

    return newTrees;
}

void Papercutter::selectCuts(const std::vector<size_t>& indicesToCut,
                             const std::unordered_map<size_t, int>& amounts) {
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
            bool keepTrueBranch = ((i - TRCount) % 2 != 0);
            ifNodesToChange.emplace(ifNodes[nodeIndex], keepTrueBranch);
        }
        else if (i < TRCount + IRCount + BSRCount) {
            size_t nodeIndex = i - TRCount - IRCount;
            BitShrinkTarget t = shrinkNodes[nodeIndex];  // copy so we can set the amount
            auto ai = amounts.find(i);
            if (ai != amounts.end())
                t.amount = ai->second;
            nodesToShrink.emplace(t.decl->name.valueText());
            runMap[t.decl].push_back(t);
        }
        else if (i < TRCount + IRCount + BSRCount + CRCount) {
            size_t nodeIndex = i - TRCount - IRCount - BSRCount;
            caseNodesToChange[caseNodes[nodeIndex].first].insert(caseNodes[nodeIndex].second);
        }
        else if (i < TRCount + IRCount + BSRCount + CRCount + BRCount) {
            size_t nodeIndex = i - TRCount - IRCount - BSRCount - CRCount;
            binopNodesToChange.emplace(binopNodes[nodeIndex].first, binopNodes[nodeIndex].second);
        }
        else {
            size_t rel = i - TRCount - IRCount - BSRCount - CRCount - BRCount;
            bool polarity = (rel % 2 != 0); // even -> force 0, odd -> force 1
            constForceActive[std::string(constForceNodes[rel / 2]->name.valueText())] = polarity;
        }
    }
}

std::shared_ptr<SyntaxTree> Papercutter::cutIndex(std::vector<size_t> indicesToCut,
                                                  std::unordered_map<size_t, int> amounts) {

    clearState();

    selectCuts(indicesToCut, amounts);

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
std::string Papercutter::cutIndexText(std::vector<size_t> indicesToCut,
                                      std::unordered_map<size_t, int> amounts) {

    clearState();

    selectCuts(indicesToCut, amounts);

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

    // Ifs: 2 cuts per node. Same even/odd polarity as ternaries above.
    //   even index -> keepTrueBranch=false -> keep the else clause (false branch)
    //   odd index  -> keepTrueBranch=true  -> keep the true branch (statement)
    for (const auto* node : ifNodes) {
        size_t line = lineOf(*node);
        info.emplace_back("if(keep-false)", line);
        info.emplace_back("if(keep-true)", line);
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

    // Binops: keep one operand (shifts keep-left only). The operator is tagged
    // (e.g. "binop(mul,keep-left)") so logging can split successes by which
    // operator was pruned, not just which side was kept.
    for (const auto& pair : binopNodes) {
        std::string op(binopName(pair.first->kind));
        std::string side = pair.second ? "keep-left" : "keep-right";
        info.emplace_back("binop(" + op + "," + side + ")", lineOf(*pair.first));
    }

    // Const-force: 2 cuts per 1-bit scalar (force reads to 0, then to 1).
    for (const auto* decl : constForceNodes) {
        size_t line = sm.getLineNumber(decl->name.location());
        info.emplace_back("force-const(0)", line);
        info.emplace_back("force-const(1)", line);
    }

    return info;
}

std::vector<std::pair<size_t, size_t>> Papercutter::cutPairs() {
    // Same bands, same order, as cutInfo(). Bit-shrink and case contribute one cut
    // per site, so they pair with nothing.
    std::vector<std::pair<size_t, size_t>> pairs;
    size_t base = 0;
    for (size_t i = 0; i < ternaryNodes.size(); ++i)
        pairs.emplace_back(base + 2 * i, base + 2 * i + 1);
    base += ternaryNodes.size() * 2;
    for (size_t i = 0; i < ifNodes.size(); ++i)
        pairs.emplace_back(base + 2 * i, base + 2 * i + 1);
    base += ifNodes.size() * 2;
    base += shrinkNodes.size();
    base += caseNodes.size();
    // Binops carry one entry per (node, side); a shift has keep-left only, so pair
    // consecutive entries only when they belong to the same node.
    for (size_t i = 0; i + 1 < binopNodes.size(); ++i) {
        if (binopNodes[i].first == binopNodes[i + 1].first) {
            pairs.emplace_back(base + i, base + i + 1);
            ++i;
        }
    }
    base += binopNodes.size();
    for (size_t i = 0; i < constForceNodes.size(); ++i)
        pairs.emplace_back(base + 2 * i, base + 2 * i + 1);
    return pairs;
}

std::vector<size_t> Papercutter::cutShrinkWidths() {
    // Aligned 1:1 with cutInfo()/cutAll() indices. Only bitshrink cuts carry a
    // meaningful width (the current bit width of their targeted packed dimension);
    // every other cut type reports 0. Callers use width-1 as the upper bound for
    // an iterative multi-bit shrink of that dimension.
    std::vector<size_t> widths(cutCount, 0);
    // The bitshrink band sits after ternaries and ifs (see selectCuts), one entry
    // per shrinkNodes target.
    for (size_t j = 0; j < shrinkNodes.size(); ++j) {
        size_t globalIdx = TRCount + IRCount + j;
        widths[globalIdx] = static_cast<size_t>(shrinkNodes[j].width);
    }
    return widths;
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

std::vector<std::shared_ptr<SyntaxTree>> Papercutter::removeAllConstForces() {
    std::vector<std::shared_ptr<SyntaxTree>> newTrees;

    for (const auto* decl : constForceNodes) {
        for (bool polarity : {false, true}) {
            constForceActive.clear();
            constForceActive[std::string(decl->name.valueText())] = polarity;
            newTrees.emplace_back(transform(tree));
        }
    }
    clearState();
    return newTrees;
}


void Papercutter::emitIntermediateWires(const SyntaxNode& decl,
                                        const SeparatedSyntaxList<DeclaratorSyntax>& declarators,
                                        const DeclShape& shape) {
    std::string trivia;
    for (const auto& t : decl.getFirstToken().trivia())
        trivia += t.getRawText();

    for (const auto* d : declarators) {
        if (!runMap.contains(d))
            continue;
        auto wire = buildIntermediateWire(trivia, shape, std::string(d->name.valueText()), runMap[d]);
        if (!wire)
            continue; // non-literal or non-range dimension; leave the signal alone
        insertAfter(decl, parse(wire->decl));
        insertAfter(decl, parse(wire->assign));
    }
}

void Papercutter::handle(const DataDeclarationSyntax& node) {
    if (shrinkWithIntermediate) {
        if (auto shape = shapeOf(node))
            emitIntermediateWires(node, node.declarators, *shape);
        // Descend regardless so cuts nested in an initializer are still applied.
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

    std::string mods = trimWs(listToString(node.modifiers));
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
        // runMap[d] holds the dimensions to narrow for this declarator (usually
        // one; more if the caller combined several dim-cuts of the same signal).
        repls.push_back(trivia + modsOut + typeHead + " " + buildPackedRanges(dims, runMap[d]) + " " +
                        trimWs(std::string(d->toString())) + ";");
    }

    for (const auto& r : repls)
        insertBefore(node, parse(r));
    remove(node);
}

void Papercutter::handle(const NetDeclarationSyntax& node) {
    if (shrinkWithIntermediate) {
        // The companion signal keeps the original net type, so a continuous
        // assignment drives it exactly as the design drives the original.
        // Strength/delay nets were rejected by the collector.
        if (auto shape = shapeOf(node))
            emitIntermediateWires(node, node.declarators, *shape);
        // Descend regardless so initializer expression cuts still fire.
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
        repls.push_back(trivia + typeHead + " " + buildPackedRanges(*dims, runMap[d]) + " " +
                        trimWs(std::string(d->toString())) + ";");
    }

    for (const auto& r : repls)
        insertBefore(node, parse(r));
    remove(node);
}

void Papercutter::handle(const DeclaratorSyntax& node) {
    // Bit-shrink is applied at the declaration level in both modes -- narrowed in
    // place by handle(DataDeclarationSyntax)/handle(NetDeclarationSyntax), or given
    // a companion wire there by emitIntermediateWires. This handler exists only to
    // guarantee we still descend into the declarator's children, so that expression
    // cuts living inside an initializer -- e.g. the `sel ? a : b` in
    // `wire w = sel ? a : b;` or the `a & b` in `wire v = a & b;` -- are reached and
    // actually applied. Overriding handle() takes over dispatch for this node, so
    // without an explicit visitDefault the initializer subtree is never visited:
    // such cuts were collected (they show up in cut_info) but silently applied to
    // nothing.
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

// True when `cur` is the whole expression of a port connection whose port is not
// an input. The connection's direction is the child's, so it comes from `ports`
// (PortDirections); a connection that cannot be resolved counts as a write.
static bool isOutputConnection(const SyntaxNode* cur, const PortDirections* ports) {
    const SyntaxNode* p = cur->parent;
    // a connection's expression sits inside property/sequence wrappers
    while (p && (p->kind == SyntaxKind::ParenthesizedExpression || p->kind == SyntaxKind::SimpleSequenceExpr ||
                 p->kind == SyntaxKind::SimplePropertyExpr)) {
        cur = p;
        p = p->parent;
    }
    if (!p || (p->kind != SyntaxKind::NamedPortConnection && p->kind != SyntaxKind::OrderedPortConnection))
        return false;
    const SyntaxNode* conn = p;
    const SyntaxNode* inst = conn->parent;
    while (inst && inst->kind != SyntaxKind::HierarchicalInstance)
        inst = inst->parent;
    const SyntaxNode* instantiation = inst ? inst->parent : nullptr;
    while (instantiation && instantiation->kind != SyntaxKind::HierarchyInstantiation)
        instantiation = instantiation->parent;
    if (!inst || !instantiation || !ports)
        return true;
    auto def = ports->find(std::string(instantiation->as<HierarchyInstantiationSyntax>().type.valueText()));
    if (def == ports->end())
        return true;
    const auto& list = def->second;
    if (conn->kind == SyntaxKind::NamedPortConnection) {
        std::string name{conn->as<NamedPortConnectionSyntax>().name.valueText()};
        for (const auto& [port, dir] : list)
            if (port == name)
                return dir != "in";
        return true;
    }
    size_t idx = 0;
    for (auto* c : inst->as<HierarchicalInstanceSyntax>().connections) {
        if (c == conn)
            return idx < list.size() ? list[idx].second != "in" : true;
        ++idx;
    }
    return true;
}

// True when this identifier sits in a write position -- the LHS of any
// assignment, or a connection to an instance's output/inout port -- climbing out
// of enclosing concatenations/streams first ({a, x} = ..., .y({a, x})). Such
// occurrences must not be substituted with a constant or redirected onto a
// read-side companion: the child's port would drive the companion (a second
// driver) and leave the original undriven.
static bool isAssignmentWriteTarget(const SyntaxNode& node, const PortDirections* ports) {
    const SyntaxNode* cur = &node;
    const SyntaxNode* parent = node.parent;
    while (parent && (parent->kind == SyntaxKind::ConcatenationExpression ||
                      parent->kind == SyntaxKind::StreamingConcatenationExpression)) {
        cur = parent;
        parent = parent->parent;
    }
    if (auto* bin = parent ? parent->as_if<BinaryExpressionSyntax>() : nullptr)
        return SyntaxFacts::isAssignmentOperator(bin->kind) &&
               static_cast<const SyntaxNode*>(bin->left) == cur;
    return isOutputConnection(cur, ports);
}

void Papercutter::handle(const IdentifierNameSyntax& node) {
    // Narrow mode never renames reads for bit-shrink, but const-force substitutes
    // matching reads with a 1-bit literal here (the declaration stays intact).
    if (!shrinkWithIntermediate) {
        if (!constForceActive.empty()) {
            auto it = constForceActive.find(std::string(node.identifier.valueText()));
            if (it != constForceActive.end() && !isAssignmentWriteTarget(node, &portDirections)) {
                this->replace(node, makeIntLiteral(it->second ? "1'b1" : "1'b0", node.identifier.trivia()));
                return;
            }
        }
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