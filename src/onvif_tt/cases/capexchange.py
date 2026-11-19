"""Capability exchange: GetServices(IncludeCapability) against each service's
own GetServiceCapabilities.

A client that follows ONVIF Core's "use GetServices" guidance reads a
service's capabilities from the ``tds:Capabilities`` block GetServices embeds
when ``IncludeCapability`` is true. That block is the *same* element the
service's GetServiceCapabilities answers with; when it is missing, the client
concludes the device has no such capabilities. For the event service that
means no subscription is ever attempted (OpenIPC/majestic#379).

The comparison is structural on the raw XML — element names, attributes with
xs:boolean values canonicalised, and children — because the embedded block is
an ``xs:any`` that zeep hands over as an lxml element while the direct answer
is a typed object.
"""

from __future__ import annotations

from lxml import etree

from ..registry import register
from ..runtime.dut import DUT

_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
_MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"
_EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"
_IMAGING_NS = "http://www.onvif.org/ver20/imaging/wsdl"

_XS_BOOLEAN = {"true": "true", "1": "true", "false": "false", "0": "false"}


def _canon(el: etree._Element) -> tuple:
    """Order-insensitive structural form of a capabilities element."""
    attrs = tuple(sorted(
        (k, _XS_BOOLEAN.get(v.strip(), v.strip())) for k, v in el.attrib.items()
    ))
    text = (el.text or "").strip()
    text = _XS_BOOLEAN.get(text, text)
    kids = tuple(sorted(
        (_canon(c) for c in el if isinstance(c.tag, str)), key=repr))
    return (etree.QName(el).localname, attrs, text, kids)


def _embedded_caps(raw: str | bytes, ns: str) -> etree._Element | None:
    """The element inside ``tds:Service/tds:Capabilities`` for ``ns``.

    Read from the raw response: zeep resolves some embedded blocks (the
    device service's own ``tds:Capabilities``) into typed objects and leaves
    others as lxml elements, and only the XML compares like for like.
    """
    root = etree.fromstring(raw.encode() if isinstance(raw, str) else raw)
    for svc in root.xpath("//*[local-name()='Service']"):
        nss = svc.xpath("./*[local-name()='Namespace']/text()")
        if not nss or nss[0].strip() != ns:
            continue
        wrap = svc.xpath("./*[local-name()='Capabilities']")
        if not wrap:
            return None
        kids = [c for c in wrap[0] if isinstance(c.tag, str)]
        return kids[0] if kids else None
    raise AssertionError(f"GetServices does not list {ns}")


def _direct_caps(dut: DUT, svc_name: str) -> etree._Element:
    """Call ``svc_name``.GetServiceCapabilities and return the raw element."""
    getattr(dut, svc_name).GetServiceCapabilities()
    raw = dut.last_response
    assert raw, "no response captured for GetServiceCapabilities"
    root = etree.fromstring(raw.encode() if isinstance(raw, str) else raw)
    resp = root.xpath(
        "//*[local-name()='GetServiceCapabilitiesResponse']")
    assert resp, "response is not a GetServiceCapabilitiesResponse"
    kids = [c for c in resp[0] if isinstance(c.tag, str)]
    assert kids and etree.QName(kids[0]).localname == "Capabilities", (
        "GetServiceCapabilitiesResponse has no Capabilities element "
        "(it is mandatory in every service WSDL)"
    )
    return kids[0]


def _assert_consistent(dut: DUT, ns: str, svc_name: str) -> None:
    dut.devicemgmt.GetServices(True)
    raw = dut.last_response
    assert raw, "no response captured for GetServices"
    embedded = _embedded_caps(raw, ns)
    assert embedded is not None, (
        f"GetServices(IncludeCapability=true) carries no Capabilities for "
        f"{ns}; a client reading capabilities the modern way sees none"
    )
    direct = _direct_caps(dut, svc_name)
    assert _canon(embedded) == _canon(direct), (
        f"{ns}: GetServices embeds {etree.tostring(embedded).decode()} but "
        f"GetServiceCapabilities answers {etree.tostring(direct).decode()}"
    )


@register("DEVICE-1-1-19", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"})
def test_device_services_caps_consistency(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-19 — device service capabilities agree."""
    _assert_consistent(dut, _DEVICE_NS, "devicemgmt")


@register("EVENT-5-1-2", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt", "events"})
def test_event_services_caps_consistency(dut: DUT, spec) -> None:
    """EVENT.html#tc.EVENT-5-1-2 — event service capabilities agree."""
    _assert_consistent(dut, _EVENTS_NS, "events")


@register("MEDIA2-7-1-2", profiles={"T"}, mandatory=True,
          requires_services={"devicemgmt", "media2"})
def test_media2_services_caps_consistency(dut: DUT, spec) -> None:
    """MEDIA2.html#tc.MEDIA2-7-1-2 — Media2 service capabilities agree."""
    _assert_consistent(dut, _MEDIA2_NS, "media2")


@register("IMAGING-3-1-2", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "imaging"})
def test_imaging_services_caps_consistency(dut: DUT, spec) -> None:
    """IMAGING.html#tc.IMAGING-3-1-2 — imaging service capabilities agree."""
    _assert_consistent(dut, _IMAGING_NS, "imaging")


@register("LOCAL-SERVICES-CAPS-ON-REQUEST", profiles={"S", "T"},
          mandatory=True, requires_services={"devicemgmt"},
          tags={"local", "consistency"})
def test_services_capabilities_follow_include_flag(dut: DUT, spec) -> None:
    """IncludeCapability decides whether Capabilities appear at all.

    ONVIF Core §8.1.2.1: with IncludeCapability=true every service carries
    its Capabilities; with false none does. DEVICE-1-1-14 checks the false
    case for Media only; this checks every service, both ways.
    """
    with_caps = dut.devicemgmt.GetServices(True) or []
    missing = [s.Namespace for s in with_caps
               if getattr(s, "Capabilities", None) is None]
    assert not missing, (
        f"GetServices(IncludeCapability=true) omits Capabilities for: "
        f"{', '.join(missing)}"
    )
    without = dut.devicemgmt.GetServices(False) or []
    extra = [s.Namespace for s in without
             if getattr(s, "Capabilities", None) is not None]
    assert not extra, (
        f"GetServices(IncludeCapability=false) still carries Capabilities "
        f"for: {', '.join(extra)}"
    )
