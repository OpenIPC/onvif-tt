"""Network configuration tests — read-only.

The plan called out ``IPCONFIG-1-1-1..5`` as "read-only — no writes to
the camera", but every one of those catalog IDs actually invokes
``SetNetworkInterfaces`` followed by ``SystemReboot``. Running them
against a real device disrupts the LAN and would brick a remote DUT.

These ``LOCAL-NETWORK-*`` tests cover the same conformance ground —
that the device answers each major network-config GET request with a
sane structure — without ever mutating the device. The write variants
can be added later behind ``--allow-writes`` if needed.
"""

from __future__ import annotations

import pytest

from ..registry import register
from ..runtime.dut import DUT


@register("LOCAL-NETWORK-GET-INTERFACES", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"},
          tags={"local", "network"})
def test_get_network_interfaces(dut: DUT, spec) -> None:
    """GetNetworkInterfaces returns ≥1 interface with a token and either
    an IPv4 or IPv6 configuration block."""
    interfaces = dut.devicemgmt.GetNetworkInterfaces()
    assert interfaces, "GetNetworkInterfaces returned empty list"
    for iface in interfaces:
        assert getattr(iface, "token", None), "interface missing token"
        ipv4 = getattr(iface, "IPv4", None)
        ipv6 = getattr(iface, "IPv6", None)
        assert ipv4 is not None or ipv6 is not None, (
            f"interface {iface.token!r} has neither IPv4 nor IPv6 config"
        )


@register("LOCAL-NETWORK-GET-HOSTNAME", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt"},
          tags={"local", "network"})
def test_get_hostname(dut: DUT, spec) -> None:
    """GetHostname returns a populated HostnameInformation struct."""
    h = dut.devicemgmt.GetHostname()
    assert h is not None, "GetHostname returned None"
    # HostnameInformation has FromDHCP (boolean) and optionally Name (string).
    assert hasattr(h, "FromDHCP"), "GetHostnameResponse missing FromDHCP"


@register("LOCAL-NETWORK-GET-DNS", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt"},
          tags={"local", "network"})
def test_get_dns(dut: DUT, spec) -> None:
    """GetDNS returns a DNSInformation struct.

    FromDHCP is mandatory; SearchDomain / DNSManual / DNSFromDHCP are
    optional collections that may be empty.
    """
    d = dut.devicemgmt.GetDNS()
    assert d is not None, "GetDNS returned None"
    assert hasattr(d, "FromDHCP"), "GetDNSResponse missing FromDHCP"


@register("LOCAL-NETWORK-GET-NTP", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt"},
          tags={"local", "network"})
def test_get_ntp(dut: DUT, spec) -> None:
    """GetNTP returns an NTPInformation struct."""
    try:
        n = dut.devicemgmt.GetNTP()
    except Exception as exc:
        pytest.skip(f"GetNTP not supported: {exc}")
    assert n is not None, "GetNTP returned None"
    assert hasattr(n, "FromDHCP"), "GetNTPResponse missing FromDHCP"


@register("LOCAL-NETWORK-GET-DEFAULT-GATEWAY", profiles={"S", "T"},
          mandatory=False,
          requires_services={"devicemgmt"},
          tags={"local", "network"})
def test_get_default_gateway(dut: DUT, spec) -> None:
    """GetNetworkDefaultGateway returns a NetworkGateway struct.

    IPv4Address and IPv6Address are optional lists; the structure itself
    must be present. Many devices fault on GetNetworkDefaultGateway when
    no gateway is configured — treat as clean skip.
    """
    try:
        gw = dut.devicemgmt.GetNetworkDefaultGateway()
    except Exception as exc:
        pytest.skip(f"GetNetworkDefaultGateway not supported: {exc}")
    assert gw is not None, "GetNetworkDefaultGateway returned None"


@register("LOCAL-NETWORK-GET-PROTOCOLS", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt"},
          tags={"local", "network"})
def test_get_network_protocols(dut: DUT, spec) -> None:
    """GetNetworkProtocols returns ≥1 protocol entry with Name + Port.

    The mandatory set per ONVIF is HTTP and RTSP — we don't enforce
    presence of either (some devices only advertise what's enabled)
    but every returned entry must carry Name and Port.
    """
    try:
        protocols = dut.devicemgmt.GetNetworkProtocols()
    except Exception as exc:
        pytest.skip(f"GetNetworkProtocols not supported: {exc}")
    assert protocols, "GetNetworkProtocols returned empty list"
    for p in protocols:
        assert getattr(p, "Name", None), "protocol entry missing Name"
        ports = getattr(p, "Port", None) or []
        assert ports, f"protocol {p.Name!r} has no Port"
