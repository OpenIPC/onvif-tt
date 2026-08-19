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


def test_gate_flags_advertised_but_unbindable():
    """The issue-#1 shape: the DUT has it, we can't reach it.

    Must land in ``unbindable`` (which dispatch turns into a failure), not
    in ``missing`` (which it skips).
    """
    missing, unbindable = _gate_services(
        {"devicemgmt", "media2"},
        {"devicemgmt", "media2"},
        lambda s: s != "media2",
    )
    assert missing == set()
    assert unbindable == {"media2"}


# ---------------------------------------------------------------------------
# Discovery must not bury an unrecognised namespace
# ---------------------------------------------------------------------------

class _FakeService:
    def __init__(self, namespace: str, xaddr: str) -> None:
        self.Namespace = namespace
        self.XAddr = xaddr


class _FakeDeviceMgmt:
    def GetServices(self, _include_capability):
        return [
            _FakeService("http://www.onvif.org/ver10/device/wsdl",
                         "http://cam/onvif/device_service"),
            _FakeService("http://www.onvif.org/ver20/media/wsdl",
                         "http://cam/onvif/media2_service"),
            _FakeService("http://vendor.example.com/private/wsdl",
                         "http://cam/onvif/private_service"),
        ]

    def GetCapabilities(self, _category):
        return None


class _FakeConfig:
    host = "cam"


class _FakeDUT:
    def __init__(self) -> None:
        self.session = DUTSession()
        self.config = _FakeConfig()
        self.devicemgmt = _FakeDeviceMgmt()


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
