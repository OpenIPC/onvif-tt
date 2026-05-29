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

    python-onvif-zeep's bundled analytics.wsdl is missing the
    GetSupportedRules operation, so we send a raw SOAP 1.2 envelope
    and parse the response with lxml. Device-side conformance is
    fully exercised — we just go around zeep's parsed proxy.
    """
    from ..runtime.raw_soap import raw_soap_call, ANALYTICS_NS, TT_NS

    token = _first_analytics_config_token(dut)
    body = (
        f'<tan:GetSupportedRules xmlns:tan="{ANALYTICS_NS}">'
        f'<tan:ConfigurationToken>{token}</tan:ConfigurationToken>'
        f'</tan:GetSupportedRules>'
    ).encode()
    resp_body = raw_soap_call(dut, "analytics", body)

    # RuleDescription elements live under SupportedRules in the
    # analytics namespace, but the elements themselves are in tt:
    # (they're typed by tt:ConfigDescription). Iterate by local
    # name to stay namespace-tolerant.
    rules = list(resp_body.iter(f"{{{TT_NS}}}RuleDescription"))
    # Empty list is legal — device exposes the op but ships no rule
    # types. Nothing to assert in that case.
    for r in rules:
        name = r.get("Name")
        typ = r.get("Type")
        assert name, (
            "RuleDescription element on the wire is missing the Name "
            "attribute (analytics.wsdl requires it)"
        )
        assert typ, (
            "RuleDescription element on the wire is missing the Type "
            "attribute (analytics.wsdl requires it)"
        )


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

    Returns the metadata fields the analytics engine can emit. The
    parameter ``Type`` is optional; omitting it requests metadata for
    every module type the device supports.

    python-onvif-zeep's bundled analytics.wsdl omits GetSupportedMetadata,
    so we POST the SOAP 1.2 envelope ourselves and assert structure
    on the parsed response.
    """
    from ..runtime.raw_soap import raw_soap_call, ANALYTICS_NS, TT_NS

    body = f'<tan:GetSupportedMetadata xmlns:tan="{ANALYTICS_NS}"/>'.encode()
    resp_body = raw_soap_call(dut, "analytics", body)

    response = resp_body.find(f"{{{ANALYTICS_NS}}}GetSupportedMetadataResponse")
    assert response is not None, (
        "Response body does not contain a GetSupportedMetadataResponse "
        "element in the analytics namespace"
    )
    # Each AnalyticsModule entry is typed by tt:MetadataInfo. The list
    # can legally be empty (device has no modules configured), but
    # every entry that does appear must carry a Type attribute per
    # MetadataInfo's XSD definition.
    modules = list(response.iter(f"{{{ANALYTICS_NS}}}AnalyticsModule"))
    for m in modules:
        typ = m.get("Type")
        assert typ, (
            "AnalyticsModule entry in GetSupportedMetadataResponse "
            "is missing the Type attribute"
        )
