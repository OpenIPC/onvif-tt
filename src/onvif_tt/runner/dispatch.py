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

from ..registry import REGISTRY, discover, match_xfail
from ..runtime.auth import AuthMode
from ..runtime.dut import DUT, DUTConfig
from ..runtime.features import discover_device_info, discover_services
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
        auth=AuthMode(request.config.getoption("--auth")),
    )
    try:
        d = DUT(cfg)
    except Exception as exc:  # connection refused, WSDL fetch failure, ...
        pytest.skip(f"cannot construct DUT for {target}: {exc}")
        return  # unreachable but appeases type checkers
    yield d
    # Auth is negotiated lazily on first service access, so read it at
    # teardown when it has actually settled.
    from .plugin import record_auth_state
    record_auth_state(d.session.auth)
    # Belt-and-braces: any subscription a crashing test left behind gets
    # unsubscribed here so we don't pile up state on the device.
    d.teardown_subscriptions()


@pytest.fixture(scope="session")
def _services(dut):
    # Authentication is negotiated on first service access, i.e. inside
    # discover_services below. If it never succeeds, every service lookup
    # comes back empty and all 147 tests skip as "DUT does not advertise
    # services" — a green-looking run whose real cause is buried. Nothing
    # downstream can be trusted, so stop here and say why, once.
    try:
        services = discover_services(dut)
    except Exception:
        services = {}
    auth = dut.session.auth
    if dut.config.user and auth.accepted is None:
        pytest.exit(
            f"DUT refused every credential type tried "
            f"({', '.join(auth.rejected) or 'none attempted'}) for user "
            f"{dut.config.user!r}. No test below this point could produce a "
            f"meaningful verdict."
            + (f" The device also would not answer an unauthenticated "
               f"GetSystemDateAndTime, so clock skew could not be ruled out."
               if auth.clock_probe_refused else "")
            + (f" Requested --auth={auth.requested.value}; try --auth=auto."
               if auth.requested is not AuthMode.AUTO else ""),
            returncode=1,
        )
    return services


@pytest.fixture(scope="session")
def _device_info(dut):
    try:
        return discover_device_info(dut)
    except Exception:
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


def _gate_services(requires, advertised, can_reach):
    """Split a test's required services into (missing, unreachable).

    These are different verdicts and must not share one outcome. *Missing*
    means the DUT doesn't implement the service — legitimately not
    applicable, so skip. *Unreachable* means the DUT does advertise it and
    we still can't talk to it — we have no idea whether the device conforms,
    and reporting that as a skip is how twelve Media2 tests sat green
    without ever executing (issue #1).

    ``advertised`` must include services the DUT listed without a usable
    XAddr, or they fall back into ``missing`` and the skip is silent again.
    """
    missing = set(requires) - set(advertised)
    unreachable = {
        s for s in set(requires) & set(advertised) if not can_reach(s)
    }
    return missing, unreachable


def _fail_on_schema_violations(dut, validate: bool) -> None:
    """Fail the test if any response it provoked was structurally invalid.

    Deliberately after the body rather than in-band: raising from inside the
    zeep ingress hook would abort the SOAP call itself, which reads as a
    transport error and can strand a subscription mid-teardown.

    A test that ran on a response missing a mandatory element cannot claim a
    pass — that is the whole point of issue #3. When many tests share one
    device defect the reporter groups them, so the run reads as N defects
    rather than N failures.
    """
    if not validate:
        return
    violations = dut.session.schema_violations
    if not violations:
        return
    lines = "\n".join(
        f"  [{v.code}] {v.path}\n      {v.detail}"
        for v in violations
    )
    pytest.fail(
        f"{len(violations)} schema violation(s) in responses this test "
        f"received. The assertions may have passed, but they ran on data "
        f"the ONVIF schema does not permit:\n{lines}",
        pytrace=False,
    )


@pytest.mark.parametrize("test_id", _all_ids() or ["__no_tests_registered__"])
def test_onvif_case(test_id, dut, _services, _device_info, spec, request):
    if test_id == "__no_tests_registered__":
        pytest.skip("No ONVIF tests registered in REGISTRY")
    impl = REGISTRY[test_id]
    advertised = set(_services) | dut.session.advertised_without_xaddr
    missing, unreachable = _gate_services(
        impl.requires_services, advertised, dut.can_reach
    )
    if unreachable:
        no_endpoint = sorted(unreachable & dut.session.advertised_without_xaddr)
        no_schema = sorted(unreachable - set(no_endpoint))
        why = []
        if no_schema:
            why.append(
                f"{no_schema}: this client has no schema for them — add a "
                f"ServiceDef in onvif_tt/runtime/services.py and run "
                f"`onvif-tt schemas refresh`"
            )
        if no_endpoint:
            why.append(
                f"{no_endpoint}: the DUT advertised them with an empty XAddr, "
                f"which ONVIF Core makes mandatory, so there is nowhere to "
                f"send the request"
            )
        pytest.fail(
            "The DUT advertises services this test needs but we cannot reach "
            "them, so it could not run. Not a skip: a skip would claim the "
            "service was not applicable, when in fact the device offers it. "
            + "; ".join(why)
        )
    if missing:
        pytest.skip(f"DUT does not advertise services: {sorted(missing)}")
    if impl.requires_writes and not request.config.getoption("--allow-writes"):
        pytest.skip(
            "test mutates DUT state — pass --allow-writes to enable"
        )
    if impl.requires_reboot and not request.config.getoption("--allow-reboot"):
        pytest.skip(
            "test reboots the DUT (30–120 s outage) — pass --allow-reboot "
            "to enable"
        )

    # Device-fingerprint-aware xfail. If a known-bad matcher hits this
    # DUT we wrap the test body in a try/except so:
    #   - real failure → XFAIL (CI green)
    #   - unexpected pass → XPASS, surfaced as a warning ("device fixed it!")
    xfail_reason = match_xfail(impl, _device_info)

    sig = inspect.signature(impl.func)
    kwargs = {}
    if "dut" in sig.parameters:
        kwargs["dut"] = dut
    if "spec" in sig.parameters:
        kwargs["spec"] = spec
    if "request" in sig.parameters:
        kwargs["request"] = request

    validate = not request.config.getoption("--no-schema-validation")
    dut._schema.enabled = validate
    # Clear immediately before the body, not after the test: session-scoped
    # fixtures (discover_services' GetServices/GetCapabilities) issue calls
    # of their own, and those responses must not be blamed on whichever test
    # happened to run first.
    dut.session.schema_violations.clear()

    def _run_body():
        impl.func(**kwargs)
        _fail_on_schema_violations(dut, validate)

    if xfail_reason is None:
        _run_body()
        return

    try:
        _run_body()
    except BaseException as exc:  # noqa: BLE001
        # Pytest's outcome exceptions (pytest.fail / pytest.skip) inherit
        # from BaseException, not Exception — so a plain `except Exception`
        # would miss them. We also re-raise truly fatal control-flow
        # exceptions (KeyboardInterrupt / SystemExit) so Ctrl-C still works.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        pytest.xfail(f"{xfail_reason} | {type(exc).__name__}: {exc}")
    # Reached only if the test passed. Surface as XPASS-style warning so
    # CI is alerted that the device might have been fixed.
    import warnings
    warnings.warn(
        f"XPASS for {impl.test_id}: expected failure on this DUT "
        f"({xfail_reason!r}) but the test passed.",
        stacklevel=2,
    )


# Stash on each item so the plugin's reporter can find the test_id.
def pytest_collection_modifyitems(config, items):
    for item in items:
        if not item.name.startswith("test_onvif_case["):
            continue
        tid = item.callspec.params.get("test_id") if hasattr(item, "callspec") else None
        if tid is not None:
            item.user_properties.append(("onvif_id", tid))
