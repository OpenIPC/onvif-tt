"""Analytics service tests — capabilities, supported-rules enumeration,
modules listing.

The Analytics service exposes the device's onboard video-analytics
modules: motion-region detectors, line crossing, tampering, face
detection, etc. We cover the read-only enumeration side and the
GetServices consistency check. Rule-creation tests (ANALYTICS-1-1-3 /
-4 and -4-1-5 / -6 / -7) are write ops; we add them behind
``requires_writes``.

A pure-video camera without analytics will not advertise the service,
so all these tests SKIP cleanly on devices that don't expose it.
"""

from __future__ import annotations

import pytest

from ..registry import register
from ..runtime.dut import DUT


_ANALYTICS_NS = "http://www.onvif.org/ver20/analytics/wsdl"


# ---------------------------------------------------------------------------
# Service capabilities + GetServices consistency
# ---------------------------------------------------------------------------

@register("ANALYTICS-3-1-2", profiles={"T", "M"}, mandatory=True,
          requires_services={"devicemgmt", "analytics"})
def test_analytics_service_capabilities(dut: DUT, spec) -> None:
    """ANALYTICS.html#tc.ANALYTICS-3-1-2 — service capabilities."""
    caps = dut.analytics.GetServiceCapabilities()
    assert caps is not None, "GetServiceCapabilities returned None"
    # Two top-level booleans the spec marks mandatory.
    for f in ("RuleSupport", "AnalyticsModuleSupport"):
        assert getattr(caps, f, None) in (True, False), (
            f"GetServiceCapabilities.{f} is not a bool"
        )


@register("ANALYTICS-3-1-1", profiles={"T", "M"}, mandatory=True,
          requires_services={"devicemgmt", "analytics"})
def test_analytics_get_services_consistency(dut: DUT, spec) -> None:
    """ANALYTICS.html#tc.ANALYTICS-3-1-1 — GetServices(true) must
    include the analytics service with populated Capabilities.
    """
    services = dut.devicemgmt.GetServices(True) or []
    analytics = [s for s in services if s.Namespace == _ANALYTICS_NS]
    assert analytics, "Analytics service missing from GetServices"
    a = analytics[0]
    assert a.XAddr, "Analytics service XAddr empty"
    assert getattr(a, "Capabilities", None) is not None, (
        "GetServices(True) did not include Capabilities for analytics"
    )


# ---------------------------------------------------------------------------
# Rule + module enumeration — read-only
# ---------------------------------------------------------------------------

def _first_analytics_config_token(dut: DUT) -> str:
    """Find a VideoAnalyticsConfiguration token via the Media service.
    Skip cleanly if none exists.
    """
    try:
        configs = dut.media.GetVideoAnalyticsConfigurations() or []
    except Exception as exc:
        pytest.skip(f"GetVideoAnalyticsConfigurations failed: {exc}")
    if not configs:
        pytest.skip("DUT has no VideoAnalyticsConfigurations")
    return configs[0].token


@register("ANALYTICS-1-1-1", profiles={"T", "M"}, mandatory=False,
          requires_services={"devicemgmt", "media", "analytics"})
def test_analytics_get_supported_rules(dut: DUT, spec) -> None:
    """ANALYTICS.html#tc.ANALYTICS-1-1-1 — GET SUPPORTED RULES.

    Returns the list of rule types the device implements (motion
    detector, line crossing, …). Each entry must carry a non-empty
    Name and Type qname.
    """
    from ..runtime.client_compat import (
        call_or_skip_on_missing_op, name_type_from_envelope,
    )
    token = _first_analytics_config_token(dut)
    rules = call_or_skip_on_missing_op(dut.analytics, "GetSupportedRules", token)
    desc = getattr(rules, "RuleDescription", None) or []
    # Try the structured form first.
    structured = [(getattr(r, "Name", None), getattr(r, "Type", None))
                  for r in desc]
    # Fall back to the raw envelope when zeep drops the Name/Type XML
    # attributes (XSD-derivation gap on RuleDescription).
    if structured and not any(n and t for n, t in structured):
        structured = name_type_from_envelope(
            dut.last_response or "", "RuleDescription",
        )
    for name, typ in structured:
        assert name, "RuleDescription missing Name (in both parsed object and raw envelope)"
        assert typ, "RuleDescription missing Type (in both parsed object and raw envelope)"


@register("ANALYTICS-4-1-1", profiles={"T", "M"}, mandatory=False,
          requires_services={"devicemgmt", "media", "analytics"})
def test_analytics_get_supported_modules(dut: DUT, spec) -> None:
    """ANALYTICS.html#tc.ANALYTICS-4-1-1 — GET SUPPORTED ANALYTICS
    MODULES.
    """
    from ..runtime.client_compat import (
        call_or_skip_on_missing_op, name_type_from_envelope,
    )
    token = _first_analytics_config_token(dut)
    modules = call_or_skip_on_missing_op(
        dut.analytics, "GetSupportedAnalyticsModules", token,
    )
    desc = getattr(modules, "AnalyticsModuleDescription", None) or []
    structured = [(getattr(m, "Name", None), getattr(m, "Type", None))
                  for m in desc]
    # AnalyticsModuleDescription extends ConfigDescription; the Name and
    # Type attributes come from the base type and zeep can drop them if
    # the derived-type wiring isn't in its registry. Fall back to the
    # raw envelope when that happens.
    if structured and not any(n and t for n, t in structured):
        structured = name_type_from_envelope(
            dut.last_response or "", "AnalyticsModuleDescription",
        )
    for name, typ in structured:
        assert name, "AnalyticsModuleDescription missing Name (parsed + raw)"
        assert typ, "AnalyticsModuleDescription missing Type (parsed + raw)"


@register("ANALYTICS-4-1-3", profiles={"T", "M"}, mandatory=False,
          requires_services={"devicemgmt", "media", "analytics"})
def test_analytics_get_modules(dut: DUT, spec) -> None:
    """ANALYTICS.html#tc.ANALYTICS-4-1-3 — GET ANALYTICS MODULES.

    Returns the currently-installed modules (vs supported types).
    Empty list is OK.
    """
    token = _first_analytics_config_token(dut)
    modules = dut.analytics.GetAnalyticsModules(token)
    items = getattr(modules, "AnalyticsModule", None) or []
    for m in items:
        assert getattr(m, "Name", None), "analytics module missing Name"
        assert getattr(m, "Type", None), "analytics module missing Type"


@register("ANALYTICS-4-1-4", profiles={"T", "M"}, mandatory=False,
          requires_services={"devicemgmt", "media", "analytics"})
def test_analytics_get_supported_metadata(dut: DUT, spec) -> None:
    """ANALYTICS.html#tc.ANALYTICS-4-1-4 — GET SUPPORTED METADATA.

    Returns the metadata streams the analytics engine can emit.
    """
    from ..runtime.client_compat import call_or_skip_on_missing_op
    token = _first_analytics_config_token(dut)
    meta = call_or_skip_on_missing_op(dut.analytics, "GetSupportedMetadata", token)
    assert meta is not None, "GetSupportedMetadata returned None"
