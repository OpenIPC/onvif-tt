"""First slice of Profile S implementations from BASE.html / MEDIA2.html.

Hand-written; one function per spec ID. IDs verified against the parsed
corpus (``onvif-tt show <id>``) — *do not invent IDs*; copy them from
the catalog or you'll point at the wrong test.
"""

from __future__ import annotations

from ..registry import register
from ..runtime.dut import DUT


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
