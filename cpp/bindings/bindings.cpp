#include <pybind11/pybind11.h>
#include <pybind11/stl.h>  // for std::vector, std::shared_ptr automatic conversion
#include <pybind11/typing.h>  // for py::list, py::dict, etc. automatic conversion
#include "papercuts/papercuts.h"

namespace py = pybind11;

PYBIND11_MODULE(pypercuts, m) {
    // Ensure pyslang types are registered first
    py::module_::import("pyslang");

    m.doc() = "papercuts C++ bindings";

    m.def("cut_all", &papercuts::cutAll,
        py::arg("tree"),
        py::arg("bitShrink") = false,
        py::arg("ternaryRemove") = false,
        py::arg("ifRemove") = false,
        "Cut a SyntaxTree into multiple trees based on mux types"
    );

    m.def("insert_muxes", &papercuts::insertMuxes,
        py::arg("tree"),
        py::arg("bitMux") = false,
        py::arg("ternaryMux") = false,
        py::arg("ifMux") = false,
        "Insert muxes into a SyntaxTree"
    );
}
