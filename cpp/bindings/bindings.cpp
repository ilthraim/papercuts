#include <pybind11/pybind11.h>
#include <pybind11/stl.h>  // for std::vector, std::shared_ptr automatic conversion
#include <pybind11/typing.h>  // for py::list, py::dict, etc. automatic conversion
#include <pybind11/smart_holder.h> // for std::shared_ptr support
#include "papercuts/papercuts.h"

namespace py = pybind11;

PYBIND11_SMART_HOLDER_TYPE_CASTERS(slang::syntax::SyntaxTree)

PYBIND11_MODULE(pypercuts, m) {
    // Ensure pyslang types are registered first
    py::module_::import("pyslang");

    m.doc() = "papercuts C++ bindings";

    m.def("cut", &papercuts::cut,
        py::arg("tree"),
        py::arg("bitShrink") = false,
        py::arg("ternaryRemove") = false,
        py::arg("ifRemove") = false,
        py::return_value_policy::take_ownership,
        "Cut a SyntaxTree into multiple trees based on mux types"
    );

    m.def("insert_muxes", &papercuts::insertMuxes,
        py::arg("tree"),
        py::arg("bitMux") = false,
        py::arg("ternaryMux") = false,
        py::arg("ifMux") = false,
        py::return_value_policy::take_ownership,
        "Insert muxes into a SyntaxTree"
    );
}
