"""Unit tests for the ONVIF service table and the no-silent-skip guard.

No DUT required. The point of these is to make the failure mode behind
issue #1 structurally impossible: a service that tests require but the
client can't reach must never present as "the device doesn't have it".
"""

from __future__ import annotations

from onvif_tt.registry import REGISTRY, discover
from onvif_tt.runner.dispatch import _gate_services
from onvif_tt.runtime import schema_store, services
from onvif_tt.runtime.dut import DUTSession
from onvif_tt.runtime.features import discover_services


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

def test_table_is_internally_consistent():
    shorts = [s.short for s in services.SERVICES]
    namespaces = [s.namespace for s in services.SERVICES]
    assert len(shorts) == len(set(shorts)), "duplicate short name"
    assert len(namespaces) == len(set(namespaces)), "duplicate namespace"
    for sd in services.SERVICES:
        assert services.get(sd.short) is sd
        assert services.by_namespace(sd.namespace) is sd


def test_every_wsdl_is_vendored():
    missing = [
        sd.short for sd in services.SERVICES
        if schema_store.local_path(sd.wsdl_url) is None
    ]
    assert not missing, (
        f"services with no vendored WSDL: {missing} — run "
        f"`onvif-tt schemas refresh`"
    )


def test_every_required_service_has_a_table_entry():
    """The check that would have caught both halves of issue #1.

    ``media2`` had tests but no bindable entry; ``receiver`` had tests but
    no entry at all, in either the factory map or the namespace map, so a
    device advertising it got those tests skipped as "not advertised".
    """
    discover()
    required = {s for impl in REGISTRY.values() for s in impl.requires_services}
    unknown = sorted(s for s in required if services.get(s) is None)
    assert not unknown, (
        f"tests require services with no ServiceDef: {unknown}. Add them to "
        f"onvif_tt/runtime/services.py."
    )


def test_binding_name_is_fully_qualified():
    assert services.get("media2").binding_name == (
        "{http://www.onvif.org/ver20/media/wsdl}Media2Binding"
    )
    assert services.event_binding_name("pullpoint") == (
        "{http://www.onvif.org/ver10/events/wsdl}PullPointSubscriptionBinding"
    )


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

def test_gate_skips_only_what_the_dut_lacks():
    missing, unbindable = _gate_services(
        {"devicemgmt", "ptz"}, {"devicemgmt", "media"}, lambda s: True
    )
    assert missing == {"ptz"}
    assert unbindable == set()


def test_gate_flags_advertised_but_unreachable():
    """The issue-#1 shape: the DUT has it, we can't reach it.

    Must land in ``unreachable`` (which dispatch turns into a failure), not
    in ``missing`` (which it skips).
    """
    missing, unreachable = _gate_services(
        {"devicemgmt", "media2"},
        {"devicemgmt", "media2"},
        lambda s: s != "media2",
    )
    assert missing == set()
    assert unreachable == {"media2"}


# ---------------------------------------------------------------------------
# Discovery must not bury an unrecognised namespace
# ---------------------------------------------------------------------------

class _FakeService:
    def __init__(self, namespace: str, xaddr: str) -> None:
        self.Namespace = namespace
        self.XAddr = xaddr


class _FakeDeviceMgmt:
    def __init__(self, caps=None) -> None:
        self._caps = caps

    def GetServices(self, _include_capability):
        return [
            _FakeService("http://www.onvif.org/ver10/device/wsdl",
                         "http://cam/onvif/device_service"),
            _FakeService("http://www.onvif.org/ver20/media/wsdl",
                         "http://cam/onvif/media2_service"),
            _FakeService("http://vendor.example.com/private/wsdl",
                         "http://cam/onvif/private_service"),
            # Advertised, but with no endpoint to send anything to.
            _FakeService("http://www.onvif.org/ver20/ptz/wsdl", ""),
        ]

    def GetCapabilities(self, _category):
        return self._caps


class _FakeConfig:
    host = "cam"


class _FakeDUT:
    def __init__(self, caps=None) -> None:
        self.session = DUTSession()
        self.config = _FakeConfig()
        self.devicemgmt = _FakeDeviceMgmt(caps)


def test_discovery_maps_known_namespaces():
    dut = _FakeDUT()
    found = discover_services(dut)
    assert found["devicemgmt"] == "http://cam/onvif/device_service"
    assert found["media2"] == "http://cam/onvif/media2_service"


def test_discovery_quarantines_unknown_namespaces():
    """An unrecognised namespace must not become a service key.

    Filing it under its raw URI (which this code used to do) creates a key
    no ``requires_services`` entry can match, so every test needing the
    service skips as "not advertised" — green, and wrong.
    """
    dut = _FakeDUT()
    found = discover_services(dut)
    assert "http://vendor.example.com/private/wsdl" not in found
    assert dut.session.unknown_namespaces == {
        "http://vendor.example.com/private/wsdl"
    }


def test_discovery_flags_advertised_service_with_no_xaddr():
    """An empty XAddr must not read as "the DUT doesn't have it".

    ONVIF Core makes ``Service/XAddr`` mandatory, so this is a device fault
    — but the reason it's tracked is that dropping the entry would make the
    service look un-advertised, and its tests would skip as "not
    applicable". Same silent-green shape as the Media2 gap itself.
    """
    dut = _FakeDUT()
    found = discover_services(dut)
    assert "ptz" not in found, "a service with no endpoint is not usable"
    assert dut.session.advertised_without_xaddr == {"ptz"}


def test_service_with_no_xaddr_is_unreachable_not_missing():
    """It has to reach dispatch as unreachable (fail), not missing (skip)."""
    dut = _FakeDUT()
    services = discover_services(dut)
    advertised = set(services) | dut.session.advertised_without_xaddr

    def can_reach(name):
        return name not in dut.session.advertised_without_xaddr

    missing, unreachable = _gate_services({"ptz"}, advertised, can_reach)
    assert missing == set()
    assert unreachable == {"ptz"}


def test_getcapabilities_fallback_clears_the_no_xaddr_flag():
    """Legacy envelope supplying the endpoint makes the service usable.

    Some firmwares list a service in GetServices without an XAddr but do
    give one in GetCapabilities. That's still a GetServices violation
    (flagged by LOCAL-SERVICES-CAPABILITIES-CONSISTENT), but the service is
    reachable, so its tests must run rather than fail.
    """
    class _Section:
        XAddr = "http://cam/onvif/ptz_service"

    class _Caps:
        PTZ = _Section()

    dut = _FakeDUT(caps=_Caps())
    found = discover_services(dut)
    assert found["ptz"] == "http://cam/onvif/ptz_service"
    assert dut.session.advertised_without_xaddr == set()
