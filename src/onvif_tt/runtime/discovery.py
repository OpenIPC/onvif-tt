"""WS-Discovery probe helper.

Sends one multicast WS-Discovery Probe (NetworkVideoTransmitter type) and
returns the discovered services as ``DiscoveredDevice`` records.

Used both by ``cases/discovery.py`` for spec-driven assertions and as a
session-bootstrap helper in the runner when a DUT host isn't pre-known.
The implementation deliberately uses
:class:`wsdiscovery.ThreadedWSDiscovery` (not the legacy ``WSDiscovery``)
because the legacy class blocks the calling thread for the full search
interval; we want a bounded ``timeout`` argument.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

log = logging.getLogger(__name__)

_NVT_TYPE = "dn:NetworkVideoTransmitter"
_NVT_NAMESPACE = "http://www.onvif.org/ver10/network/wsdl"


@dataclass(slots=True)
class DiscoveredDevice:
    """One Probe response."""

    epr: str  # EndpointReferenceAddress (a uuid: URN)
    xaddrs: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)

    def has_xaddr(self) -> bool:
        return bool(self.xaddrs)


def probe(timeout: float = 3.0, target_addr: str | None = None) -> list[DiscoveredDevice]:
    """Send one WS-Discovery Probe and return matching services.

    Parameters
    ----------
    timeout
        How long to wait for responses, in seconds.
    target_addr
        Bind to a specific source interface address. ``None`` lets the
        OS pick (which works for multicast on most setups). Useful when
        the DUT is on a specific subnet and the host has more than one.
    """
    from wsdiscovery import QName
    from wsdiscovery.discovery import ThreadedWSDiscovery

    wsd = ThreadedWSDiscovery()
    try:
        wsd.start()
        type_qname = QName(_NVT_NAMESPACE, "NetworkVideoTransmitter")
        services = wsd.searchServices(
            types=[type_qname],
            timeout=int(max(1, round(timeout))),
        )
    except Exception as exc:  # pragma: no cover — network-dependent
        log.warning("WS-Discovery probe failed: %s", exc)
        return []
    finally:
        try:
            wsd.stop()
        except Exception:
            pass

    return [_to_record(s) for s in (services or [])]


def _to_record(svc) -> DiscoveredDevice:
    """Convert a wsdiscovery Service object to our flat dataclass."""
    return DiscoveredDevice(
        epr=str(getattr(svc, "getEPR", lambda: "")() or ""),
        xaddrs=[str(x) for x in (svc.getXAddrs() or [])],
        types=[str(t) for t in (svc.getTypes() or [])],
        scopes=[str(s) for s in (svc.getScopes() or [])],
    )


def has_onvif_target(devices: Iterable[DiscoveredDevice], host: str) -> bool:
    """True if any discovered device's xaddr references ``host``."""
    for d in devices:
        for x in d.xaddrs:
            if host in x:
                return True
    return False
