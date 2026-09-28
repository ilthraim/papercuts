#include <nanobind/nanobind.h>
#include <nanobind/stl/map.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/tuple.h>
#include <nanobind/stl/unordered_map.h>
#include <nanobind/stl/vector.h>
#include "papercuts/papercuts.h"

namespace nb = nanobind;

NB_MODULE(pypercuts, m) {
    // Ensure pyslang types are registered first
    nb::module_::import_("pyslang");

    m.doc() = "papercuts C++ bindings";

    m.def("insert_muxes",
        [](const std::shared_ptr<slang::syntax::SyntaxTree> tree, bool bitMux, bool ternaryMux, bool ifMux,
           bool caseMux, bool binopMux, bool constForceMux, bool binopsInConditionsOnly,
           const std::unordered_map<std::string, std::vector<std::pair<int, int>>>& symbolicRanges,
           bool shrinkWithIntermediate) {
            return papercuts::insertMuxes(tree, bitMux, ternaryMux, ifMux, caseMux, binopMux,
                                          constForceMux, binopsInConditionsOnly, symbolicRanges,
                                          shrinkWithIntermediate);
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
        "Insert muxes into a SyntaxTree. `symbolicRanges` must be the same map the "
        "Papercutter for this module was given, or the bit-shrink band's width differs "
        "between enumeration and insertion and every later select shifts off its cut index."
    );

    m.def("insert_muxes_report",
        [](const std::shared_ptr<slang::syntax::SyntaxTree> tree, bool bitMux, bool ternaryMux, bool ifMux,
           bool caseMux, bool binopMux, bool constForceMux, bool binopsInConditionsOnly,
           const std::unordered_map<std::string, std::vector<std::pair<int, int>>>& symbolicRanges,
           bool shrinkWithIntermediate) {
            std::vector<size_t> inserted;
            auto tr = papercuts::insertMuxes(tree, bitMux, ternaryMux, ifMux, caseMux, binopMux,
                                             constForceMux, binopsInConditionsOnly, symbolicRanges,
                                             shrinkWithIntermediate, &inserted);
            return nb::make_tuple(tr, inserted);
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
        "insert_muxes, plus the sorted select numbers a control was actually emitted for. "
        "Every index below the port count is reserved; the ones missing here drive nothing."
    );

    m.def("get_instantiated_modules", &papercuts::getInstantiatedModules,
        nb::arg("tree"),
        "Names of the modules instantiated in a SyntaxTree, in source order"
    );

    m.def("wire_mux_hierarchy", &papercuts::wireMuxHierarchy,
        nb::arg("tree"),
        nb::arg("extraPorts"),
        nb::arg("conns"),
        "Append forwarded pc_sel ports and connect them through to child instances"
    );

    m.def("rename_module", &papercuts::renameModule,
        nb::arg("tree"),
        nb::arg("newName"),
        "Rename the module in a SyntaxTree"
    );

    m.def("get_module_name", &papercuts::getModuleName,
        nb::arg("tree"),
        "Get the name of the module in a SyntaxTree"
    );

    m.def("rename_submodules", &papercuts::renameSubmodules,
        nb::arg("tree"),
        nb::arg("excluded") = std::vector<std::string>{},
        "Rename submodules in a SyntaxTree based on the parent module name. "
        "Instantiations whose module name is in `excluded` are left untouched."
    );

    m.def("rename_instance_types", &papercuts::renameInstanceTypes,
        nb::arg("tree"),
        nb::arg("renames"),
        "Rename the module type of instantiations from an {old: new} map. "
        "Instantiations whose type is not a key are left untouched."
    );

    nb::class_<papercuts::Papercutter>(m, "Papercutter")
        .def(nb::init<const std::shared_ptr<slang::syntax::SyntaxTree>, bool, bool,
                      std::unordered_map<std::string, std::vector<std::pair<int, int>>>>(),
             nb::arg("tree"),
             nb::arg("shrink_with_intermediate") = false,
             nb::arg("binops_in_conditions_only") = false,
             nb::arg("symbolic_ranges") =
                 std::unordered_map<std::string, std::vector<std::pair<int, int>>>{})
        .def("cut_all", &papercuts::Papercutter::cutAll)
        .def("cut_index", &papercuts::Papercutter::cutIndex,
             nb::arg("indices"),
             nb::arg("amounts") = std::unordered_map<size_t, int>{})
        .def("cut_index_text", &papercuts::Papercutter::cutIndexText,
             nb::arg("indices"),
             nb::arg("amounts") = std::unordered_map<size_t, int>{})
        .def("cut_info", &papercuts::Papercutter::cutInfo)
        .def("cut_pairs", &papercuts::Papercutter::cutPairs,
             "Mutually exclusive cut index pairs (the two halves of one site).")
        .def("cut_shrink_widths", &papercuts::Papercutter::cutShrinkWidths)
        .def("shrink_all_bits", &papercuts::Papercutter::shrinkAllBits)
        .def("remove_all_ternaries", &papercuts::Papercutter::removeAllTernaries)
        .def("remove_all_ifs", &papercuts::Papercutter::removeAllIfs)
        .def("remove_all_cases", &papercuts::Papercutter::removeAllCases)
        .def("remove_all_binops", &papercuts::Papercutter::removeAllBinops)
        .def("remove_all_const_forces", &papercuts::Papercutter::removeAllConstForces)
        .def("get_cut_count", &papercuts::Papercutter::getCutCount);
}
