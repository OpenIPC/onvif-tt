"""Feature discovery on a DUT.

We call ``GetServices(IncludeCapability=true)`` once per session and
cache the resulting ``{service_namespace: xaddr}`` mapping. Tests
declare ``requires_services={"media2"}`` and the runner skips them
if the DUT doesn't advertise that service.
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
}


def discover_services(dut: DUT) -> dict[str, str]:
    """Populate ``dut.session.services`` if empty; return it either way."""
    if dut.session.services:
        return dut.session.services
    try:
        # python-onvif-zeep's wrappers take positional WSDL parameters.
        resp = dut.devicemgmt.GetServices(False)
    except Exception as exc:
        log.warning("GetServices failed on %s: %s", dut.config.host, exc)
        return dut.session.services
    for s in resp or []:
        short = _NS_TO_SHORT.get(s.Namespace, s.Namespace)
        dut.session.services[short] = s.XAddr
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
