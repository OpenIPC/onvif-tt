"""Feature discovery on a DUT.

We call ``GetServices(IncludeCapability=false)`` once per session and
cache the resulting ``{short_name: xaddr}`` mapping. Tests declare
``requires_services={"media2"}`` and the runner skips them if the
DUT doesn't advertise that service.

Discovery falls back to the legacy ``GetCapabilities`` envelope to
fill in gaps. Some firmwares (notably Xiongmai stock) advertise
services via ``GetCapabilities`` but omit them from ``GetServices``
— a real ONVIF Core §8.1.6 violation, caught separately by the
``LOCAL-SERVICES-CAPABILITIES-CONSISTENT`` test. We still want to
*exercise* the service the device actually implements rather than
skip past it on the strength of the buggier advertisement; the
consistency test surfaces the spec violation as its own signal.
"""

from __future__ import annotations

import logging

from .dut import DUT

log = logging.getLogger(__name__)

# Map ONVIF service WSDL namespace → short name we use everywhere else.
_NS_TO_SHORT = {
    "http://www.onvif.org/ver10/device/wsdl": "devicemgmt",
    "http://www.onvif.org/ver10/media/wsdl": "media",
    "http://www.onvif.org/ver20/media/wsdl": "media2",
    "http://www.onvif.org/ver10/events/wsdl": "events",
    "http://www.onvif.org/ver20/ptz/wsdl": "ptz",
    "http://www.onvif.org/ver20/imaging/wsdl": "imaging",
    "http://www.onvif.org/ver20/analytics/wsdl": "analytics",
    "http://www.onvif.org/ver10/recording/wsdl": "recording",
    "http://www.onvif.org/ver10/search/wsdl": "search",
    "http://www.onvif.org/ver10/replay/wsdl": "replay",
    "http://www.onvif.org/ver10/deviceIO/wsdl": "deviceio",
    "http://www.onvif.org/ver10/accesscontrol/wsdl": "accesscontrol",
    "http://www.onvif.org/ver10/doorcontrol/wsdl": "doorcontrol",
}

# Map short name → attribute on GetCapabilities("All") response. Only
# the categories the legacy envelope knows about — newer services
# (media2, accesscontrol, …) are only addressable via GetServices.
_CAPS_TO_SHORT = (
    ("devicemgmt", "Device"),
    ("media", "Media"),
    ("events", "Events"),
    ("ptz", "PTZ"),
    ("imaging", "Imaging"),
    ("analytics", "Analytics"),
)


def discover_services(dut: DUT) -> dict[str, str]:
    """Populate ``dut.session.services`` if empty; return it either way.

    Discovery order:

    1. ``GetServices(False)`` — preferred, returns every modern service.
    2. ``GetCapabilities("All")`` — fills in services some firmwares
       advertise only in the legacy envelope (Xiongmai stock omits
       analytics from GetServices despite implementing it). The
       inconsistency itself is a separate spec violation flagged by
       ``LOCAL-SERVICES-CAPABILITIES-CONSISTENT``.
    """
    if dut.session.services:
        return dut.session.services
    try:
        # python-onvif-zeep's wrappers take positional WSDL parameters.
        resp = dut.devicemgmt.GetServices(False)
    except Exception as exc:
        log.warning("GetServices failed on %s: %s", dut.config.host, exc)
        resp = None
    for s in resp or []:
        short = _NS_TO_SHORT.get(s.Namespace, s.Namespace)
        if s.XAddr:
            dut.session.services[short] = s.XAddr

    try:
        caps = dut.devicemgmt.GetCapabilities("All")
    except Exception as exc:
        log.warning("GetCapabilities failed on %s: %s", dut.config.host, exc)
        caps = None
    if caps is not None:
        for short, attr in _CAPS_TO_SHORT:
            sec = getattr(caps, attr, None)
            xaddr = getattr(sec, "XAddr", None) if sec is not None else None
            if xaddr and short not in dut.session.services:
                dut.session.services[short] = xaddr

    return dut.session.services


def discover_device_info(dut: DUT) -> dict[str, str]:
    """Populate ``dut.session.device_info`` from ``GetDeviceInformation``.

    Cached after the first call. Used by the ``xfail_on`` matcher to
    decide whether a test is expected to fail on this particular DUT.
    Returns an empty dict if the call fails (e.g. anonymous-disabled).
    """
    if dut.session.device_info:
        return dut.session.device_info
    try:
        resp = dut.devicemgmt.GetDeviceInformation()
    except Exception as exc:
        log.warning("GetDeviceInformation failed on %s: %s", dut.config.host, exc)
        return dut.session.device_info
    # zeep gives us a struct with attribute access; expose as plain dict.
    for k in ("Manufacturer", "Model", "FirmwareVersion",
              "SerialNumber", "HardwareId"):
        v = getattr(resp, k, None)
        if v is not None:
            dut.session.device_info[k] = str(v)
    return dut.session.device_info
