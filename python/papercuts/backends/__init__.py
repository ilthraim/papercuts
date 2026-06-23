from __future__ import annotations

from importlib.metadata import entry_points

from papercuts.backends.base import ECBackend
from papercuts.backends.jasper import JasperBackend

#: Entry-point group out-of-tree packages use to register EC backends.
ENTRY_POINT_GROUP = "papercuts.backends"

_BUILTINS: dict[str, type[ECBackend]] = {JasperBackend.name: JasperBackend}


def discover_backends() -> dict[str, type[ECBackend]]:
    """Return all available EC backends keyed by name (built-ins + plugins).

    Plugins are loaded from the ``papercuts.backends`` entry-point group, so an
    installed package (e.g. an environment-specific tool wrapper) can contribute
    backends. Plugins override built-ins of the same name.
    """
    backends: dict[str, type[ECBackend]] = dict(_BUILTINS)
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        backends[ep.name] = ep.load()
    return backends


def get_backend(name: str) -> type[ECBackend]:
    """Look up a backend class by name, or exit with a helpful message."""
    backends = discover_backends()
    if name not in backends:
        avail = ", ".join(sorted(backends)) or "(none)"
        raise SystemExit(f"Unknown EC backend '{name}'. Available: {avail}")
    return backends[name]


__all__ = [
    "ECBackend",
    "JasperBackend",
    "ENTRY_POINT_GROUP",
    "discover_backends",
    "get_backend",
]
