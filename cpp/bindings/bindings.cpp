#include <pybind11/pybind11.h>
#include <pybind11/stl.h>  // for std::vector, std::shared_ptr automatic conversion
#include <pybind11/typing.h>  // for py::list, py::dict, etc. automatic conversion
#include "papercuts/papercuts.h"

namespace py = pybind11;

PYBIND11_MODULE(pypercuts, m) {
    // Ensure pyslang types are registered first
    py::module_::import("pyslang");

    m.doc() = "papercuts C++ bindings";

    m.def("insert_muxes", &papercuts::insertMuxes,
        py::arg("tree"),
        py::arg("bitMux") = false,
        py::arg("ternaryMux") = false,
        py::arg("ifMux") = false,
        "Insert muxes into a SyntaxTree"
    );

    m.def("rename_module", &papercuts::renameModule,
        py::arg("tree"),
        py::arg("newName"),
        "Rename the module in a SyntaxTree"
    );

    m.def("get_module_name", &papercuts::getModuleName,
        py::arg("tree"),
        "Get the name of the module in a SyntaxTree"
    );

    m.def("rename_submodules", &papercuts::renameSubmodules,
        py::arg("tree"),
        "Rename submodules in a SyntaxTree based on the parent module name"
    );

    py::classh<papercuts::Papercutter>(m, "Papercutter")
        .def(py::init<const std::shared_ptr<slang::syntax::SyntaxTree>>())
        .def("cut_all", &papercuts::Papercutter::cutAll)
        .def("cut_index", &papercuts::Papercutter::cutIndex)
        .def("shrink_all_bits", &papercuts::Papercutter::shrinkAllBits)
        .def("remove_all_ternaries", &papercuts::Papercutter::removeAllTernaries)
        .def("remove_all_ifs", &papercuts::Papercutter::removeAllIfs)
        .def("get_cut_count", &papercuts::Papercutter::getCutCount);
}
