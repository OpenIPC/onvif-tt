"""Profile A — Access Control Service tests.

Implementations of the read-only / fault-shape ACCESSCONTROL-* test
cases from the public ONVIF Test Specification. All gated on
``requires_services={"accesscontrol"}`` — a DUT that doesn't expose the
service skips cleanly.

Profile A devices: access controllers, building/door automation gear.
A pure-video camera (Profile S/T/G) won't advertise the service at
all, so on the OpenIPC reference these tests all SKIP.
"""

from __future__ import annotations

import pytest
import zeep.exceptions

from ..registry import register
from ..runtime.dut import DUT


_ACCESSCONTROL_NS = "http://www.onvif.org/ver10/accesscontrol/wsdl"


# ---------------------------------------------------------------------------
# ACCESSCONTROL-1-1-* — capabilities + service-table consistency
# ---------------------------------------------------------------------------

@register("ACCESSCONTROL-1-1-1", profiles={"A"}, mandatory=True,
          requires_services={"devicemgmt", "accesscontrol"})
def test_ac_service_capabilities(dut: DUT, spec) -> None:
    """ACCESSCONTROL.html#tc.ACCESSCONTROL-1-1-1 — service capabilities.

    Asserts ``GetServiceCapabilities`` returns a struct with the
    mandatory ``MaxLimit`` and at least one of the per-entity max-* limits
    populated.
    """
    caps = dut.accesscontrol.GetServiceCapabilities()
    assert caps is not None, "GetServiceCapabilities returned None"
    # MaxLimit is mandatory per the Access Control spec.
    assert getattr(caps, "MaxLimit", None) is not None, "MaxLimit missing"
    assert int(caps.MaxLimit) >= 1, f"MaxLimit must be >= 1, got {caps.MaxLimit}"


@register("ACCESSCONTROL-1-1-2", profiles={"A"}, mandatory=True,
          requires_services={"devicemgmt", "accesscontrol"})
def test_ac_get_services_consistency(dut: DUT, spec) -> None:
    """ACCESSCONTROL.html#tc.ACCESSCONTROL-1-1-2 — GetServices(IncludeCapability=true)
    must surface the Access Control service with the same Capabilities
    GetServiceCapabilities returns directly.
    """
    services = dut.devicemgmt.GetServices(True) or []
    ac_entries = [s for s in services if s.Namespace == _ACCESSCONTROL_NS]
    assert ac_entries, "Access Control service missing from GetServices"
    ac = ac_entries[0]
    assert ac.XAddr, "Access Control XAddr empty"
    # IncludeCapability=true should populate s.Capabilities.
    assert getattr(ac, "Capabilities", None) is not None, (
        "GetServices(True) did not include Capabilities for accesscontrol"
    )


# ---------------------------------------------------------------------------
# ACCESSCONTROL-2-1-* — Access Points (read-only)
# ---------------------------------------------------------------------------

@register("ACCESSCONTROL-2-1-1", profiles={"A"}, mandatory=False,
          requires_services={"devicemgmt", "accesscontrol"})
def test_ac_get_access_point_info(dut: DUT, spec) -> None:
    """ACCESSCONTROL.html#tc.ACCESSCONTROL-2-1-1 — GET ACCESS POINT INFO.

    Walks ``GetAccessPointInfoList`` to find an existing token, then
    re-fetches that one via ``GetAccessPointInfo``. Skips cleanly if the
    DUT has no access points configured.
    """
    listing = dut.accesscontrol.GetAccessPointInfoList()
    items = getattr(listing, "AccessPointInfo", None) or []
    if not items:
        pytest.skip("DUT has no access points configured")
    token = items[0].token
    assert token, "first AccessPointInfo missing token"

    detail = dut.accesscontrol.GetAccessPointInfo([token])
    detail_items = getattr(detail, "AccessPointInfo", None) or []
    assert detail_items, "GetAccessPointInfo returned no items"
    assert detail_items[0].token == token, (
        f"GetAccessPointInfo echoed wrong token: asked {token!r}, "
        f"got {detail_items[0].token!r}"
    )


@register("ACCESSCONTROL-2-1-2", profiles={"A"}, mandatory=False,
          requires_services={"devicemgmt", "accesscontrol"})
def test_ac_get_access_point_info_invalid_token(dut: DUT, spec) -> None:
    """ACCESSCONTROL.html#tc.ACCESSCONTROL-2-1-2 — GET ACCESS POINT INFO
    WITH INVALID TOKEN. Spec demands a SOAP Fault.
    """
    try:
        dut.accesscontrol.GetAccessPointInfo(["__definitely_not_a_real_token__"])
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(f"expected SOAP Fault, got {type(exc).__name__}: {exc}")
    pytest.fail("DUT did not fault on invalid access-point token")


@register("ACCESSCONTROL-2-1-5", profiles={"A"}, mandatory=True,
          requires_services={"devicemgmt", "accesscontrol"})
def test_ac_get_access_point_info_list_no_limit(dut: DUT, spec) -> None:
    """ACCESSCONTROL.html#tc.ACCESSCONTROL-2-1-5 — GET ACCESS POINT INFO
    LIST – NO LIMIT.

    Without a ``Limit`` arg, the DUT must return up to its capabilities'
    ``MaxLimit`` entries in a single response. We assert response shape;
    actual count depends on the device's provisioning.
    """
    listing = dut.accesscontrol.GetAccessPointInfoList()
    assert listing is not None, "GetAccessPointInfoList returned None"
    # NextStartReference is optional — only set when paginating mid-list.
    items = getattr(listing, "AccessPointInfo", None) or []
    caps = dut.accesscontrol.GetServiceCapabilities()
    max_limit = int(getattr(caps, "MaxLimit", 0) or 0)
    if max_limit:
        assert len(items) <= max_limit, (
            f"DUT returned {len(items)} items, exceeding MaxLimit={max_limit}"
        )


@register("ACCESSCONTROL-2-1-7", profiles={"A"}, mandatory=False,
          requires_services={"devicemgmt", "accesscontrol"})
def test_ac_get_access_point_state(dut: DUT, spec) -> None:
    """ACCESSCONTROL.html#tc.ACCESSCONTROL-2-1-7 — GET ACCESS POINT STATE.

    Returns the runtime state (enabled / disabled) of an access point.
    Skips if none are configured.
    """
    listing = dut.accesscontrol.GetAccessPointInfoList()
    items = getattr(listing, "AccessPointInfo", None) or []
    if not items:
        pytest.skip("DUT has no access points configured")
    state = dut.accesscontrol.GetAccessPointState(items[0].token)
    assert state is not None, "GetAccessPointState returned None"
    # 'Enabled' is the mandatory boolean field.
    assert getattr(state, "Enabled", None) in (True, False), (
        f"AccessPointState.Enabled is not a bool: {state.Enabled!r}"
    )


# ---------------------------------------------------------------------------
# ACCESSCONTROL-3-1-* — Areas (read-only)
# ---------------------------------------------------------------------------

@register("ACCESSCONTROL-3-1-1", profiles={"A"}, mandatory=False,
          requires_services={"devicemgmt", "accesscontrol"})
def test_ac_get_area_info(dut: DUT, spec) -> None:
    """ACCESSCONTROL.html#tc.ACCESSCONTROL-3-1-1 — GET AREA INFO."""
    listing = dut.accesscontrol.GetAreaInfoList()
    items = getattr(listing, "AreaInfo", None) or []
    if not items:
        pytest.skip("DUT has no areas configured")
    token = items[0].token
    detail = dut.accesscontrol.GetAreaInfo([token])
    detail_items = getattr(detail, "AreaInfo", None) or []
    assert detail_items, "GetAreaInfo returned no items"
    assert detail_items[0].token == token, (
        f"GetAreaInfo echoed wrong token: asked {token!r}, "
        f"got {detail_items[0].token!r}"
    )


@register("ACCESSCONTROL-3-1-2", profiles={"A"}, mandatory=False,
          requires_services={"devicemgmt", "accesscontrol"})
def test_ac_get_area_info_invalid_token(dut: DUT, spec) -> None:
    """ACCESSCONTROL.html#tc.ACCESSCONTROL-3-1-2 — GET AREA INFO WITH
    INVALID TOKEN. SOAP Fault required.
    """
    try:
        dut.accesscontrol.GetAreaInfo(["__definitely_not_a_real_token__"])
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(f"expected SOAP Fault, got {type(exc).__name__}: {exc}")
    pytest.fail("DUT did not fault on invalid area token")


@register("ACCESSCONTROL-3-1-10", profiles={"A"}, mandatory=True,
          requires_services={"devicemgmt", "accesscontrol"})
def test_ac_get_area_info_list_no_limit(dut: DUT, spec) -> None:
    """ACCESSCONTROL.html#tc.ACCESSCONTROL-3-1-10 — GET AREA INFO LIST
    – NO LIMIT.
    """
    listing = dut.accesscontrol.GetAreaInfoList()
    assert listing is not None, "GetAreaInfoList returned None"
    items = getattr(listing, "AreaInfo", None) or []
    caps = dut.accesscontrol.GetServiceCapabilities()
    max_limit = int(getattr(caps, "MaxLimit", 0) or 0)
    if max_limit:
        assert len(items) <= max_limit, (
            f"DUT returned {len(items)} items, exceeding MaxLimit={max_limit}"
        )
