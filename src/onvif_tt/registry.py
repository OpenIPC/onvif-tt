"""Global registry mapping ONVIF test IDs → Python implementation functions.

Each implementation is annotated with the profile(s) it satisfies and
its mandatory/optional flag. The pytest plugin in ``onvif_tt.runner``
discovers everything in this registry and parametrises a single test
node per registered ID.

Typical usage in a ``cases/*.py`` module::

    from onvif_tt.registry import register

    @register("DEVICE-1-1-2", profiles={"S", "T"}, mandatory=True)
    def test_get_device_information(dut, spec):
        resp = dut.devicemgmt.GetDeviceInformation()
        assert resp.Manufacturer
        ...
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Implementation:
    """A registered test-case implementation."""

    test_id: str
    func: Callable[..., Any]
    profiles: set[str] = field(default_factory=set)
    mandatory: bool = False
    requires_services: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)

    @property
    def qualname(self) -> str:
        return f"{self.func.__module__}.{self.func.__qualname__}"


# In-process registry; modules under ``onvif_tt.cases`` populate it at import.
REGISTRY: dict[str, Implementation] = {}


def register(
    test_id: str,
    *,
    profiles: set[str] | None = None,
    mandatory: bool = False,
    requires_services: set[str] | None = None,
    tags: set[str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register a callable as the implementation of ``test_id``.

    ``requires_services`` is the set of ONVIF service names (e.g.
    ``{"media", "media2"}``) the test depends on. The runner will skip
    tests whose required services aren't advertised by the DUT in
    ``GetServices``.
    """

    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        if test_id in REGISTRY:
            existing = REGISTRY[test_id]
            raise RuntimeError(
                f"Duplicate implementation for {test_id!r}: "
                f"{existing.qualname} vs {func.__module__}.{func.__qualname__}"
            )
        REGISTRY[test_id] = Implementation(
            test_id=test_id,
            func=func,
            profiles=set(profiles or ()),
            mandatory=mandatory,
            requires_services=set(requires_services or ()),
            tags=set(tags or ()),
        )
        return func

    return deco


def discover() -> None:
    """Import every module under ``onvif_tt.cases`` so their @register calls
    populate REGISTRY. Called once by the CLI / pytest plugin entrypoints."""
    import importlib
    import pkgutil

    from . import cases as _cases_pkg

    for m in pkgutil.iter_modules(_cases_pkg.__path__):
        importlib.import_module(f"{_cases_pkg.__name__}.{m.name}")
