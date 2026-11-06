#include <nanobind/nanobind.h>
#include <nanobind/stl/map.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/tuple.h>
#include <nanobind/stl/unordered_map.h>
#include <nanobind/stl/vector.h>
#include "papercuts/papercuts.h"
#include "slang/syntax/SyntaxPrinter.h"
#include "slang/syntax/SyntaxTree.h"

namespace nb = nanobind;
using slang::syntax::SyntaxTree;

// A rewritten tree shares nodes and token data with the tree it was made from,
// and nothing ties their lifetimes: once Python drops the input (main's
// `tree_ = rename_module(tree_, ...)` chain does, every step), the result
// dangles. It showed as a literal reading other text of the file (hls
// seidel_2d: 106'h38e38e38e38e39 printed as 106'h_block_pp0_sta), invalid
// UTF-8 (opencores p16c5x) and a silent crash (opencores ae18), and only now
// and then; MALLOC_PERTURB_ makes it every time (ISSUES.md 65). So every tree
// handed to Python is re-parsed from its own text first and owns all of it; the
// text goes into the tree's SourceManager, which outlives it either way.
static std::shared_ptr<SyntaxTree> standalone(const std::shared_ptr<SyntaxTree>& t) {
    return SyntaxTree::fromText(slang::syntax::SyntaxPrinter::printFile(*t), t->sourceManager());
}

static std::vector<std::shared_ptr<SyntaxTree>> standalone(const std::vector<std::shared_ptr<SyntaxTree>>& ts) {
    std::vector<std::shared_ptr<SyntaxTree>> out;
    out.reserve(ts.size());
    for (auto& t : ts)
        out.push_back(standalone(t));
    return out;
}

NB_MODULE(pypercuts, m) {
    // Ensure pyslang types are registered first
    nb::module_::import_("pyslang");

    m.doc() = "papercuts C++ bindings";

    m.def("insert_muxes",
        [](const std::shared_ptr<slang::syntax::SyntaxTree> tree, bool bitMux, bool ternaryMux, bool ifMux,
           bool caseMux, bool binopMux, bool constForceMux, bool binopsInConditionsOnly,
           const std::unordered_map<std::string, std::vector<std::pair<int, int>>>& symbolicRanges,
           bool shrinkWithIntermediate, const papercuts::PortDirections& portDirections) {
            return standalone(papercuts::insertMuxes(tree, bitMux, ternaryMux, ifMux, caseMux, binopMux,
                                                     constForceMux, binopsInConditionsOnly, symbolicRanges,
                                                     shrinkWithIntermediate, nullptr, portDirections));
        },
        nb::arg("tree"),
        nb::arg("bitMux") = false,
        nb::arg("ternaryMux") = false,
        nb::arg("ifMux") = false,
        nb::arg("caseMux") = false,
        nb::arg("binopMux") = false,
        nb::arg("constForceMux") = false,
        nb::arg("binopsInConditionsOnly") = false,
        nb::arg("symbolicRanges") = std::unordered_map<std::string, std::vector<std::pair<int, int>>>{},
        nb::arg("shrinkWithIntermediate") = false,
        nb::arg("portDirections") = papercuts::PortDirections{},
        "Insert muxes into a SyntaxTree. `symbolicRanges` must be the same map the "
        "Papercutter for this module was given, or the bit-shrink band's width differs "
        "between enumeration and insertion and every later select shifts off its cut index."
    );

    m.def("insert_muxes_report",
        [](const std::shared_ptr<slang::syntax::SyntaxTree> tree, bool bitMux, bool ternaryMux, bool ifMux,
           bool caseMux, bool binopMux, bool constForceMux, bool binopsInConditionsOnly,
           const std::unordered_map<std::string, std::vector<std::pair<int, int>>>& symbolicRanges,
           bool shrinkWithIntermediate, const papercuts::PortDirections& portDirections) {
            std::vector<size_t> inserted;
            auto tr = papercuts::insertMuxes(tree, bitMux, ternaryMux, ifMux, caseMux, binopMux,
                                             constForceMux, binopsInConditionsOnly, symbolicRanges,
                                             shrinkWithIntermediate, &inserted, portDirections);
            return nb::make_tuple(standalone(tr), inserted);
        },
        nb::arg("tree"),
        nb::arg("bitMux") = false,
        nb::arg("ternaryMux") = false,
        nb::arg("ifMux") = false,
        nb::arg("caseMux") = false,
        nb::arg("binopMux") = false,
        nb::arg("constForceMux") = false,
        nb::arg("binopsInConditionsOnly") = false,
        nb::arg("symbolicRanges") = std::unordered_map<std::string, std::vector<std::pair<int, int>>>{},
        nb::arg("shrinkWithIntermediate") = false,
        nb::arg("portDirections") = papercuts::PortDirections{},
        "insert_muxes, plus the sorted select numbers a control was actually emitted for. "
        "Every index below the port count is reserved; the ones missing here drive nothing."
    );

    m.def("get_instantiated_modules", &papercuts::getInstantiatedModules,
        nb::arg("tree"),
        "Names of the modules instantiated in a SyntaxTree, in source order"
    );

    m.def("wire_mux_hierarchy",
        [](const std::shared_ptr<SyntaxTree> tree, const std::vector<std::string>& extraPorts,
           const std::map<std::string, std::vector<std::pair<std::string, std::string>>>& conns) {
            return standalone(papercuts::wireMuxHierarchy(tree, extraPorts, conns));
        },
        nb::arg("tree"),
        nb::arg("extraPorts"),
        nb::arg("conns"),
        "Append forwarded pc_sel ports and connect them through to child instances"
    );

    m.def("rename_module",
        [](const std::shared_ptr<SyntaxTree> tree, std::string newName) {
            return standalone(papercuts::renameModule(tree, std::move(newName)));
        },
        nb::arg("tree"),
        nb::arg("newName"),
        "Rename the module in a SyntaxTree"
    );

    m.def("get_module_name", &papercuts::getModuleName,
        nb::arg("tree"),
        "Get the name of the module in a SyntaxTree"
    );

    m.def("rename_submodules",
        [](const std::shared_ptr<SyntaxTree> tree, const std::vector<std::string>& excluded) {
            return standalone(papercuts::renameSubmodules(tree, excluded));
        },
        nb::arg("tree"),
        nb::arg("excluded") = std::vector<std::string>{},
        "Rename submodules in a SyntaxTree based on the parent module name. "
        "Instantiations whose module name is in `excluded` are left untouched."
    );

    m.def("rename_instance_types",
        [](const std::shared_ptr<SyntaxTree> tree, const std::map<std::string, std::string>& renames) {
            return standalone(papercuts::renameInstanceTypes(tree, renames));
        },
        nb::arg("tree"),
        nb::arg("renames"),
        "Rename the module type of instantiations from an {old: new} map. "
        "Instantiations whose type is not a key are left untouched."
    );

    nb::class_<papercuts::Papercutter>(m, "Papercutter")
        .def(nb::init<const std::shared_ptr<slang::syntax::SyntaxTree>, bool, bool,
                      std::unordered_map<std::string, std::vector<std::pair<int, int>>>,
                      papercuts::PortDirections>(),
             nb::arg("tree"),
             nb::arg("shrink_with_intermediate") = false,
             nb::arg("binops_in_conditions_only") = false,
             nb::arg("symbolic_ranges") =
                 std::unordered_map<std::string, std::vector<std::pair<int, int>>>{},
             nb::arg("port_directions") = papercuts::PortDirections{})
        .def("cut_all", [](papercuts::Papercutter& p) { return standalone(p.cutAll()); })
        .def("cut_index",
             [](papercuts::Papercutter& p, std::vector<size_t> indices, std::unordered_map<size_t, int> amounts) {
                 return standalone(p.cutIndex(std::move(indices), std::move(amounts)));
             },
             nb::arg("indices"),
             nb::arg("amounts") = std::unordered_map<size_t, int>{})
        .def("cut_index_text", &papercuts::Papercutter::cutIndexText,
             nb::arg("indices"),
             nb::arg("amounts") = std::unordered_map<size_t, int>{})
        .def_prop_rw("const_priority", &papercuts::Papercutter::getConstPriority,
                     &papercuts::Papercutter::setConstPriority,
                     "When True, an active force-const cut wins over a binop cut whose dropped operand reads its "
                     "signal and over a case cut whose selector reads it. Ternary and if cuts keep priority. True by default.")
        .def("cut_info", &papercuts::Papercutter::cutInfo)
        .def("cut_pairs", &papercuts::Papercutter::cutPairs,
             "Mutually exclusive cut index pairs (the two halves of one site).")
        .def("cut_shrink_widths", &papercuts::Papercutter::cutShrinkWidths)
        .def("shrink_all_bits", [](papercuts::Papercutter& p) { return standalone(p.shrinkAllBits()); })
        .def("remove_all_ternaries", [](papercuts::Papercutter& p) { return standalone(p.removeAllTernaries()); })
        .def("remove_all_ifs", [](papercuts::Papercutter& p) { return standalone(p.removeAllIfs()); })
        .def("remove_all_cases", [](papercuts::Papercutter& p) { return standalone(p.removeAllCases()); })
        .def("remove_all_binops", [](papercuts::Papercutter& p) { return standalone(p.removeAllBinops()); })
        .def("remove_all_const_forces", [](papercuts::Papercutter& p) { return standalone(p.removeAllConstForces()); })
        .def("get_cut_count", &papercuts::Papercutter::getCutCount);
}
