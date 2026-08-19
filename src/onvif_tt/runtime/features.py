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

from . import services as service_table
from .dut import DUT

log = logging.getLogger(__name__)

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
        sd = service_table.by_namespace(s.Namespace)
        if sd is None:
            # Don't file it under its raw namespace URI: that produces a key
            # no `requires_services` entry can ever match, so every test
            # needing the service skips as "not advertised" and the run stays
            # green. Record it instead — LOCAL-CLIENT-SERVICES-BINDABLE fails
            # on it and names the namespace to add to runtime/services.py.
            dut.session.unknown_namespaces.add(s.Namespace)
            continue
        if s.XAddr:
            dut.session.services[sd.short] = s.XAddr
        else:
            # Advertised with no endpoint. Silently dropping it would make
            # the service indistinguishable from one the DUT doesn't
            # implement, and its tests would skip as "not applicable" —
            # the exact silent-green failure this module is guarding.
            dut.session.advertised_without_xaddr.add(sd.short)

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
                # The legacy envelope supplied an endpoint GetServices left
                # blank, so the service is reachable after all.
                dut.session.advertised_without_xaddr.discard(short)

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
