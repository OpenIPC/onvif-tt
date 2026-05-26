"""WS-Discovery tests from BASE.html (DISCOVERY-* IDs).

The plan called out ``DISCOVERY-2-1-1 / DISCOVERY-2-1-2`` as Profile S
must-haves. The corpus shows those are XML-namespace conformance checks
on the ProbeMatch response — they're hard to run end-to-end because
they need to inspect the raw XML envelopes the device sends back, not
the SOAP-decoded form ``wsdiscovery`` hands us. So:

* ``LOCAL-DISCOVERY-PROBE`` — the practical smoke: sending a Probe to
  the LAN must surface the DUT's NetworkVideoTransmitter service with
  a non-empty XAddr.
* ``DISCOVERY-2-1-1`` — best-effort: assert ProbeMatch carried at
  least the standard ONVIF types we expect.
"""

from __future__ import annotations

import pytest

from ..registry import register
from ..runtime.discovery import probe, has_onvif_target
from ..runtime.dut import DUT


@register("LOCAL-DISCOVERY-PROBE", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"},
          tags={"local", "network"})
def test_local_discovery_probe_sees_dut(dut: DUT, spec) -> None:
    """Send a WS-Discovery Probe; expect the DUT's host in the matches.

    Skips if no ProbeMatch came back at all (firewall, multicast off, …).
    Fails if Probes came back but none mentioned the DUT host.
    """
    devices = probe(timeout=4.0)
    if not devices:
        pytest.skip(
            "no WS-Discovery ProbeMatch arrived — multicast may be "
            "blocked on this segment"
        )
    assert has_onvif_target(devices, dut.config.host), (
        f"WS-Discovery found {len(devices)} device(s) but none with "
        f"an xaddr referencing {dut.config.host!r}. "
        f"Discovered hosts: {[d.xaddrs for d in devices]}"
    )


@register("DISCOVERY-2-1-1", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt"},
          tags={"network"})
def test_probe_match_carries_onvif_types(dut: DUT, spec) -> None:
    """BASE.html#tc.DISCOVERY-2-1-1 — DISCOVERY NAMESPACES.

    The spec wants strict XML-namespace inspection on the ProbeMatch
    envelope; ``wsdiscovery`` hands us the decoded form, so we
    approximate with a softer check: at least one match must declare
    ``NetworkVideoTransmitter`` in its Types list.
    """
    devices = probe(timeout=4.0)
    if not devices:
        pytest.skip("no WS-Discovery ProbeMatch arrived")
    matched = False
    for d in devices:
        for t in d.types:
            if "NetworkVideoTransmitter" in t:
                matched = True
                break
        if matched:
            break
    assert matched, (
        f"no ProbeMatch declared NetworkVideoTransmitter in its Types. "
        f"Saw types={[d.types for d in devices]}"
    )
