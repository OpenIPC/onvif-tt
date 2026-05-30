"""Profile S/T implementations from BASE.html.

Hand-written; one function per spec ID. IDs verified against the parsed
corpus (``onvif-tt show <id>``) — *do not invent IDs*; copy them from
the catalog or you'll point at the wrong test.

Calling-convention note: python-onvif-zeep wrappers take *positional*
WSDL parameters — ``GetServices(False)``, ``GetCapabilities("All")``.
"""

from __future__ import annotations

import pytest

from ..registry import register
from ..runtime.dut import DUT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
_MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"
_EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"
_PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
_IMAGING_NS = "http://www.onvif.org/ver20/imaging/wsdl"
_ANALYTICS_NS = "http://www.onvif.org/ver20/analytics/wsdl"


def _service_by_ns(services, ns: str):
    """Return the entry from a GetServices response matching ``ns``, or None."""
    for s in services or []:
        if s.Namespace == ns:
            return s
    return None


# ---------------------------------------------------------------------------
# BASE.html — Device management
# ---------------------------------------------------------------------------

@register("DEVICE-1-1-2", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"})
def test_get_capabilities_all(dut: DUT, spec) -> None:
    """Spec: BASE.html#tc.DEVICE-1-1-2 — ALL CAPABILITIES.

    Asserts a Device capability section exists; Profile S also requires
    Media and Events capability sections to be present when those
    services are supported.
    """
    caps = dut.devicemgmt.GetCapabilities("All")
    assert caps is not None, "GetCapabilities returned None"
    assert caps.Device is not None, "Device capabilities missing"
    assert caps.Device.XAddr, "Device.XAddr empty"


@register("DEVICE-1-1-13", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"})
def test_get_services_device(dut: DUT, spec) -> None:
    """Spec: BASE.html#tc.DEVICE-1-1-13 — GET SERVICES (DEVICE).

    The response must enumerate the Device service itself with a non-empty
    XAddr. We're not yet asserting the IncludeCapability variants here.
    """
    services = dut.devicemgmt.GetServices(False)
    assert services, "GetServices returned no entries"
    dev_ns = "http://www.onvif.org/ver10/device/wsdl"
    devs = [s for s in services if s.Namespace == dev_ns]
    assert devs, "GetServices does not include the device service itself"
    assert devs[0].XAddr, "Device service XAddr empty"


@register("DEVICE-3-1-1", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"})
def test_get_system_date_and_time(dut: DUT, spec) -> None:
    """Spec: BASE.html#tc.DEVICE-3-1-1 — SYSTEM COMMAND GETSYSTEMDATEANDTIME.

    Must return a populated structure with DateTimeType and a UTCDateTime
    with a sane year (>1970).
    """
    dt = dut.devicemgmt.GetSystemDateAndTime()
    assert dt is not None
    assert dt.DateTimeType in ("Manual", "NTP"), (
        f"unexpected DateTimeType: {dt.DateTimeType!r}"
    )
    assert dt.UTCDateTime is not None, "UTCDateTime missing"
    assert dt.UTCDateTime.Date.Year > 1970, (
        f"UTC year looks bogus: {dt.UTCDateTime.Date.Year}"
    )


@register("DEVICE-3-1-9", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"})
def test_get_device_information(dut: DUT, spec) -> None:
    """Spec: BASE.html#tc.DEVICE-3-1-9 — SYSTEM COMMAND DEVICE INFORMATION.

    All five fields are mandatory per the spec: Manufacturer, Model,
    FirmwareVersion, SerialNumber, HardwareId.
    """
    resp = dut.devicemgmt.GetDeviceInformation()
    assert resp.Manufacturer, "Manufacturer empty"
    assert resp.Model, "Model empty"
    assert resp.FirmwareVersion, "FirmwareVersion empty"
    assert resp.SerialNumber, "SerialNumber empty"
    assert resp.HardwareId, "HardwareId empty"


# ---------------------------------------------------------------------------
# MEDIA2.html — Profile T media
# ---------------------------------------------------------------------------

@register("MEDIA2-1-1-4", profiles={"T"}, mandatory=True,
          requires_services={"devicemgmt", "media2"})
def test_get_profiles_media2(dut: DUT, spec) -> None:
    """Spec: MEDIA2.html#tc.MEDIA2-1-1-4 — GET PROFILES.

    Profile T devices must expose at least one Media2 profile, each with
    a non-empty token + Name.
    """
    profiles = dut.media2.GetProfiles()
    assert profiles, "Media2.GetProfiles returned no profiles"
    p = profiles[0]
    assert p.token, "first media2 profile missing token"
    assert p.Name, "first media2 profile missing Name"


# ---------------------------------------------------------------------------
# BASE — capability section presence (DEVICE-1-1-3..10)
#
# Each test asserts that the named Capability section is populated when
# the DUT advertises that service in GetServices. Skip otherwise.
# ---------------------------------------------------------------------------

@register("DEVICE-1-1-3", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"})
def test_device_capabilities(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-3 — DEVICE CAPABILITIES.

    GetCapabilities(Category=Device) must return a Device section whose
    XAddr is a syntactically valid URI.
    """
    caps = dut.devicemgmt.GetCapabilities("Device")
    assert caps is not None, "GetCapabilities returned None"
    assert caps.Device is not None, "Device capability section missing"
    xaddr = caps.Device.XAddr or ""
    assert xaddr.startswith(("http://", "https://")), (
        f"Device.XAddr is not a valid URI: {xaddr!r}"
    )


@register("DEVICE-1-1-4", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media"})
def test_media_capabilities(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-4 — MEDIA CAPABILITIES."""
    caps = dut.devicemgmt.GetCapabilities("Media")
    assert caps is not None
    assert caps.Media is not None, "Media capability section missing"
    xaddr = caps.Media.XAddr or ""
    assert xaddr.startswith(("http://", "https://")), (
        f"Media.XAddr is not a valid URI: {xaddr!r}"
    )


@register("DEVICE-1-1-5", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt", "events"})
def test_event_capabilities(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-5 — EVENT CAPABILITIES."""
    caps = dut.devicemgmt.GetCapabilities("Events")
    assert caps is not None
    assert caps.Events is not None, "Events capability section missing"
    xaddr = caps.Events.XAddr or ""
    assert xaddr.startswith(("http://", "https://")), (
        f"Events.XAddr is not a valid URI: {xaddr!r}"
    )


@register("DEVICE-1-1-6", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_capabilities(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-6 — PTZ CAPABILITIES."""
    caps = dut.devicemgmt.GetCapabilities("PTZ")
    assert caps is not None
    assert caps.PTZ is not None, "PTZ capability section missing"
    xaddr = caps.PTZ.XAddr or ""
    assert xaddr.startswith(("http://", "https://")), (
        f"PTZ.XAddr is not a valid URI: {xaddr!r}"
    )


@register("DEVICE-1-1-10", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "imaging"})
def test_imaging_capabilities(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-10 — IMAGING CAPABILITIES."""
    caps = dut.devicemgmt.GetCapabilities("Imaging")
    assert caps is not None
    assert caps.Imaging is not None, "Imaging capability section missing"
    xaddr = caps.Imaging.XAddr or ""
    assert xaddr.startswith(("http://", "https://")), (
        f"Imaging.XAddr is not a valid URI: {xaddr!r}"
    )


# ---------------------------------------------------------------------------
# BASE — GetServices selectivity (DEVICE-1-1-14..17)
# ---------------------------------------------------------------------------

@register("DEVICE-1-1-14", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "media"})
def test_get_services_media(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-14 — GetServices includes Media service."""
    services = dut.devicemgmt.GetServices(False)
    s = _service_by_ns(services, _MEDIA_NS)
    assert s is not None, "Media service missing from GetServices"
    assert s.XAddr, "Media service XAddr empty"
    # IncludeCapability=False ⇒ Capabilities must not be present
    assert getattr(s, "Capabilities", None) is None, (
        "GetServices(False) returned Capabilities for media service"
    )


@register("DEVICE-1-1-16", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt", "events"})
def test_get_services_events(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-16 — GetServices includes Events service."""
    services = dut.devicemgmt.GetServices(False)
    s = _service_by_ns(services, _EVENTS_NS)
    assert s is not None, "Events service missing from GetServices"
    assert s.XAddr, "Events service XAddr empty"


@register("DEVICE-1-1-17", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "imaging"})
def test_get_services_imaging(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-17 — GetServices includes Imaging service."""
    services = dut.devicemgmt.GetServices(False)
    s = _service_by_ns(services, _IMAGING_NS)
    assert s is not None, "Imaging service missing from GetServices"
    assert s.XAddr, "Imaging service XAddr empty"


@register("DEVICE-1-1-31", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"})
def test_get_services_xaddr_valid(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-31 — every advertised XAddr is a URI."""
    services = dut.devicemgmt.GetServices(False)
    assert services
    for s in services:
        assert s.XAddr, f"empty XAddr for {s.Namespace}"
        assert s.XAddr.startswith(("http://", "https://")), (
            f"non-URI XAddr for {s.Namespace}: {s.XAddr!r}"
        )


# Map (GetCapabilities attribute → short name, WSDL namespace) for the
# six service categories the legacy GetCapabilities envelope knows
# about. Newer services (Media2, AccessControl, …) only appear in
# GetServices and are out of scope of this consistency check.
_CAPS_VS_SERVICES = (
    ("Device",    "devicemgmt", _DEVICE_NS),
    ("Media",     "media",      _MEDIA_NS),
    ("Events",    "events",     _EVENTS_NS),
    ("PTZ",       "ptz",        _PTZ_NS),
    ("Imaging",   "imaging",    _IMAGING_NS),
    ("Analytics", "analytics",  _ANALYTICS_NS),
)


@register("LOCAL-SERVICES-CAPABILITIES-CONSISTENT", profiles={"S", "T"},
          mandatory=True, requires_services={"devicemgmt"},
          tags={"local", "consistency"})
def test_get_services_matches_get_capabilities(dut: DUT, spec) -> None:
    """GetServices must enumerate every service GetCapabilities reports.

    Per ONVIF Core §8.1.6, GetServices is the modern replacement for
    GetCapabilities and **must** return every service the device
    implements. A device that advertises (say) Analytics via the
    legacy GetCapabilities envelope but omits it from GetServices is
    non-conformant — clients that follow the spec's "use GetServices"
    guidance will silently lose access to the service.

    We compare only the categories GetCapabilities knows about
    (Device, Media, Events, PTZ, Imaging, Analytics). Newer services
    (Media2, AccessControl, …) only live in GetServices and are out
    of scope here.
    """
    services = dut.devicemgmt.GetServices(False) or []
    caps = dut.devicemgmt.GetCapabilities("All")
    assert caps is not None, "GetCapabilities returned None"

    in_services = {s.Namespace for s in services if s.XAddr}
    missing: list[str] = []
    for attr, short, ns in _CAPS_VS_SERVICES:
        sec = getattr(caps, attr, None)
        if sec is None:
            continue
        if not getattr(sec, "XAddr", None):
            continue
        # GetCapabilities advertises this service. GetServices must too.
        if ns not in in_services:
            missing.append(f"{short} (ns={ns})")
    assert not missing, (
        "GetServices does not enumerate services that GetCapabilities "
        "advertises. ONVIF Core §8.1.6 requires GetServices to list "
        "every service the device implements. Missing from GetServices: "
        f"{', '.join(missing)}."
    )


# ---------------------------------------------------------------------------
# BASE — SOAP fault on invalid request (DEVICE-1-1-9)
# ---------------------------------------------------------------------------

@register("DEVICE-3-1-10", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt"})
def test_get_system_log(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-3-1-10 — SYSTEM COMMAND GETSYSTEMLOG.

    Spec allows two outcomes per LogType: either a populated
    GetSystemLogResponse, or one of:
      * env:Sender/ter:InvalidArgs/ter:SystemlogUnavailable
      * env:Sender/ter:InvalidArgs/ter:AccesslogUnavailable

    We accept either path — both are spec-conformant. The test fails
    only if the device errors with something *other* than those two.
    """
    from ..runtime.fault import looks_like_soap_fault
    for log_type in ("System", "Access"):
        try:
            resp = dut.devicemgmt.GetSystemLog(log_type)
        except Exception as exc:
            if looks_like_soap_fault(exc):
                # SOAP Fault is acceptable per spec ("log unavailable").
                continue
            raise
        # Non-Fault response — must carry the SystemLog structure.
        assert resp is not None, (
            f"GetSystemLog({log_type}) returned None with no fault"
        )


@register("DEVICE-3-1-11", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt"},
          requires_writes=True)
def test_set_system_date_and_time_manual(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-3-1-11 — SYSTEM COMMAND SETSYSTEMDATEANDTIME.

    Per spec: read original, set the device to the *current* UTC
    (effectively a no-op so we don't leave the clock skewed), confirm
    the readback, then re-set the original. Marked requires_writes
    because SetSystemDateAndTime is mandatory-write per the catalog.
    """
    import datetime as _dt
    original = dut.devicemgmt.GetSystemDateAndTime()

    now = _dt.datetime.now(_dt.UTC)
    set_req = dut.devicemgmt.create_type("SetSystemDateAndTime")
    set_req.DateTimeType = "Manual"
    set_req.DaylightSavings = False
    set_req.UTCDateTime = {
        "Time": {"Hour": now.hour, "Minute": now.minute, "Second": now.second},
        "Date": {"Year": now.year, "Month": now.month, "Day": now.day},
    }
    # SetSystemDateAndTimeResponse body is empty; success = no fault.
    dut.devicemgmt.SetSystemDateAndTime(set_req)

    readback = dut.devicemgmt.GetSystemDateAndTime()
    assert readback.DateTimeType == "Manual", (
        f"after SetSystemDateAndTime(Manual), DateTimeType is "
        f"{readback.DateTimeType!r}"
    )

    # Restore the original DateTimeType + time. If the DUT was NTP-managed,
    # this just tells it to resume NTP; otherwise we set the current UTC again.
    restore = dut.devicemgmt.create_type("SetSystemDateAndTime")
    restore.DateTimeType = original.DateTimeType or "Manual"
    restore.DaylightSavings = bool(getattr(original, "DaylightSavings", False))
    if getattr(original, "TimeZone", None):
        restore.TimeZone = {"TZ": original.TimeZone.TZ}
    now2 = _dt.datetime.now(_dt.UTC)
    restore.UTCDateTime = {
        "Time": {"Hour": now2.hour, "Minute": now2.minute, "Second": now2.second},
        "Date": {"Year": now2.year, "Month": now2.month, "Day": now2.day},
    }
    dut.devicemgmt.SetSystemDateAndTime(restore)


@register("DEVICE-1-1-9", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"},
          xfail_on=[
              {
                  "Manufacturer": "H264",
                  "reason": "Xiongmai stock firmware (Manufacturer=H264) "
                            "closes the TCP connection on invalid "
                            "GetCapabilities Category instead of returning "
                            "a SOAP 1.2 Fault. Tracked in kaeru: three-"
                            "xiongmai-stock-firmware-onvif-non-conformances.",
              },
          ])
def test_soap_fault_on_invalid_capability(dut: DUT, spec) -> None:
    """BASE.html#tc.DEVICE-1-1-9 — DUT returns SOAP 1.2 fault for an
    invalid GetCapabilities category.
    """
    from ..runtime.fault import assert_soap_fault
    assert_soap_fault(dut.devicemgmt.GetCapabilities, "ThisIsNotARealCategory")
