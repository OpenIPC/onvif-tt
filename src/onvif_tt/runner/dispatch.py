"""Pytest dispatch file: turns every entry in REGISTRY into a parametrised
``test_onvif_case[ID]`` test node.

``onvif-tt run`` invokes pytest with this file as the test target. The
plugin module ``onvif_tt.runner.plugin`` adds the ``--target`` / ``--user``
/ ``--password`` / ``--profile`` / ``--id-glob`` options.
"""

from __future__ import annotations

import fnmatch
import inspect

import pytest

from ..registry import REGISTRY, discover
from ..runtime.dut import DUT, DUTConfig
from ..runtime.features import discover_services
from ..specs.parser import parse_corpus

# Populate REGISTRY at collection time.
discover()


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _corpus_by_id(request):
    cd = request.config.getoption("--corpus-dir")
    if not cd:
        return {}
    return {c.id: c for c in parse_corpus(cd)}


@pytest.fixture(scope="session")
def dut(request):
    target = request.config.getoption("--target")
    if not target:
        pytest.skip("--target not given")
    host, _, port = target.partition(":")
    cfg = DUTConfig(
        host=host,
        port=int(port) if port else 80,
        user=request.config.getoption("--user") or "",
        password=request.config.getoption("--password") or "",
    )
    try:
        return DUT(cfg)
    except Exception as exc:  # connection refused, WSDL fetch failure, ...
        pytest.skip(f"cannot construct DUT for {target}: {exc}")


@pytest.fixture(scope="session")
def _services(dut):
    try:
        return discover_services(dut)
    except Exception as exc:
        # Network blip — treat as "no services advertised" so individual
        # tests skip with their `requires_services` reason rather than
        # erroring out at the fixture level.
        return {}


@pytest.fixture
def spec(request, _corpus_by_id):
    test_id = request.node.callspec.params["test_id"]
    return _corpus_by_id.get(test_id)


# ---------------------------------------------------------------------------
# Selection filtering happens in plugin.pytest_collection_modifyitems —
# we always parametrize over the full registry here.
# ---------------------------------------------------------------------------

def _all_ids():
    return sorted(REGISTRY.keys())


@pytest.mark.parametrize("test_id", _all_ids() or ["__no_tests_registered__"])
def test_onvif_case(test_id, dut, _services, spec, request):
    if test_id == "__no_tests_registered__":
        pytest.skip("No ONVIF tests registered in REGISTRY")
    impl = REGISTRY[test_id]
    missing = impl.requires_services - set(_services)
    if missing:
        pytest.skip(f"DUT does not advertise services: {sorted(missing)}")
    sig = inspect.signature(impl.func)
    kwargs = {}
    if "dut" in sig.parameters:
        kwargs["dut"] = dut
    if "spec" in sig.parameters:
        kwargs["spec"] = spec
    if "request" in sig.parameters:
        kwargs["request"] = request
    impl.func(**kwargs)


# Stash on each item so the plugin's reporter can find the test_id.
def pytest_collection_modifyitems(config, items):
    for item in items:
        if not item.name.startswith("test_onvif_case["):
            continue
        tid = item.callspec.params.get("test_id") if hasattr(item, "callspec") else None
        if tid is not None:
            item.user_properties.append(("onvif_id", tid))
