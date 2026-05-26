"""Profile D — Door Control Service tests.

DOORCONTROL-* covers door state queries (read-only) and the various
door-actuation commands (AccessDoor, LockDoor, UnlockDoor, BlockDoor,
DoubleLockDoor, LockDownDoor, LockOpenDoor). The actuation tests are
gated behind ``requires_writes`` — opt in with ``--allow-writes``.

The "command not supported" family (DOORCONTROL-3-1-19..27) is included
read-only: we send the command with a fresh fake token and accept
either a ``MethodNotSupported``-style fault OR a successful response,
depending on whether the DUT actually supports that command. Spec says
both are conformant outcomes for unsupported commands.
"""

from __future__ import annotations

import pytest
import zeep.exceptions

from ..registry import register
from ..runtime.dut import DUT


_DOORCONTROL_NS = "http://www.onvif.org/ver10/doorcontrol/wsdl"

_INVALID_TOKEN = "__definitely_not_a_real_door_token__"


# ---------------------------------------------------------------------------
# DOORCONTROL-1-1-* — capabilities + GetServices consistency
# ---------------------------------------------------------------------------

@register("DOORCONTROL-1-1-1", profiles={"D"}, mandatory=True,
          requires_services={"devicemgmt", "doorcontrol"})
def test_dc_service_capabilities(dut: DUT, spec) -> None:
    """DOORCONTROL.html#tc.DOORCONTROL-1-1-1 — DOOR CONTROL SERVICE
    CAPABILITIES.
    """
    caps = dut.doorcontrol.GetServiceCapabilities()
    assert caps is not None, "GetServiceCapabilities returned None"
    assert getattr(caps, "MaxLimit", None) is not None, "MaxLimit missing"
    assert int(caps.MaxLimit) >= 1, f"MaxLimit must be >= 1, got {caps.MaxLimit}"


@register("DOORCONTROL-1-1-2", profiles={"D"}, mandatory=True,
          requires_services={"devicemgmt", "doorcontrol"})
def test_dc_get_services_consistency(dut: DUT, spec) -> None:
    """DOORCONTROL.html#tc.DOORCONTROL-1-1-2 — GetServices(True) must
    include doorcontrol with populated Capabilities.
    """
    services = dut.devicemgmt.GetServices(True) or []
    dc_entries = [s for s in services if s.Namespace == _DOORCONTROL_NS]
    assert dc_entries, "Door Control service missing from GetServices"
    dc = dc_entries[0]
    assert dc.XAddr, "Door Control XAddr empty"
    assert getattr(dc, "Capabilities", None) is not None, (
        "GetServices(True) did not include Capabilities for doorcontrol"
    )


# ---------------------------------------------------------------------------
# DOORCONTROL-2-1-* — door info + state (read-only)
# ---------------------------------------------------------------------------

def _first_door_token(dut: DUT) -> str:
    listing = dut.doorcontrol.GetDoorInfoList()
    items = getattr(listing, "DoorInfo", None) or []
    if not items:
        pytest.skip("DUT has no doors configured")
    return items[0].token


@register("DOORCONTROL-2-1-1", profiles={"D"}, mandatory=True,
          requires_services={"devicemgmt", "doorcontrol"})
def test_dc_get_door_state(dut: DUT, spec) -> None:
    """DOORCONTROL.html#tc.DOORCONTROL-2-1-1 — GET DOOR STATE.

    Returns the runtime state of a door (DoorMode, DoorPhysicalState,
    LockPhysicalState, …). We assert presence of the DoorMode field
    which the spec marks mandatory.
    """
    token = _first_door_token(dut)
    state = dut.doorcontrol.GetDoorState(token)
    assert state is not None, "GetDoorState returned None"
    assert getattr(state, "DoorMode", None), "DoorState.DoorMode missing"


@register("DOORCONTROL-2-1-2", profiles={"D"}, mandatory=True,
          requires_services={"devicemgmt", "doorcontrol"})
def test_dc_get_door_state_invalid_token(dut: DUT, spec) -> None:
    """DOORCONTROL.html#tc.DOORCONTROL-2-1-2 — GetDoorState fault on
    invalid token.
    """
    try:
        dut.doorcontrol.GetDoorState(_INVALID_TOKEN)
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(f"expected SOAP Fault, got {type(exc).__name__}: {exc}")
    pytest.fail("DUT did not fault on invalid door token")


@register("DOORCONTROL-2-1-3", profiles={"D"}, mandatory=True,
          requires_services={"devicemgmt", "doorcontrol"})
def test_dc_get_door_info(dut: DUT, spec) -> None:
    """DOORCONTROL.html#tc.DOORCONTROL-2-1-3 — GET DOOR INFO."""
    token = _first_door_token(dut)
    detail = dut.doorcontrol.GetDoorInfo([token])
    items = getattr(detail, "DoorInfo", None) or []
    assert items, "GetDoorInfo returned no items"
    assert items[0].token == token, (
        f"GetDoorInfo echoed wrong token: asked {token!r}, "
        f"got {items[0].token!r}"
    )


@register("DOORCONTROL-2-1-4", profiles={"D"}, mandatory=True,
          requires_services={"devicemgmt", "doorcontrol"})
def test_dc_get_door_info_invalid_token(dut: DUT, spec) -> None:
    """DOORCONTROL.html#tc.DOORCONTROL-2-1-4 — GetDoorInfo fault on
    invalid token.
    """
    try:
        dut.doorcontrol.GetDoorInfo([_INVALID_TOKEN])
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(f"expected SOAP Fault, got {type(exc).__name__}: {exc}")
    pytest.fail("DUT did not fault on invalid door token")


@register("DOORCONTROL-2-1-8", profiles={"D"}, mandatory=True,
          requires_services={"devicemgmt", "doorcontrol"})
def test_dc_get_door_info_list_no_limit(dut: DUT, spec) -> None:
    """DOORCONTROL.html#tc.DOORCONTROL-2-1-8 — GET DOOR INFO LIST – NO
    LIMIT.
    """
    listing = dut.doorcontrol.GetDoorInfoList()
    assert listing is not None, "GetDoorInfoList returned None"
    items = getattr(listing, "DoorInfo", None) or []
    caps = dut.doorcontrol.GetServiceCapabilities()
    max_limit = int(getattr(caps, "MaxLimit", 0) or 0)
    if max_limit:
        assert len(items) <= max_limit, (
            f"DUT returned {len(items)} items, exceeding MaxLimit={max_limit}"
        )


# ---------------------------------------------------------------------------
# DOORCONTROL-3-1-* — door commands, invalid-token (negative) variants
#
# These exercise the eight door-actuation operations with a bogus token
# and assert a SOAP Fault. None of them actually modify state (the
# token doesn't resolve to anything), so they're safe without
# --allow-writes. Each is one line different from the next, so we
# parametrise via a registry helper.
# ---------------------------------------------------------------------------

_DOOR_COMMANDS = (
    ("DOORCONTROL-3-1-10", "AccessDoor"),
    ("DOORCONTROL-3-1-11", "BlockDoor"),
    ("DOORCONTROL-3-1-12", "DoubleLockDoor"),
    ("DOORCONTROL-3-1-13", "LockDoor"),
    ("DOORCONTROL-3-1-14", "UnlockDoor"),
    ("DOORCONTROL-3-1-15", "LockDownDoor"),
    ("DOORCONTROL-3-1-17", "LockOpenDoor"),
)


def _make_invalid_token_test(op_name: str):
    def _test(dut: DUT, spec) -> None:
        op = getattr(dut.doorcontrol, op_name)
        try:
            op(_INVALID_TOKEN)
        except zeep.exceptions.Fault:
            return
        except Exception as exc:
            pytest.fail(
                f"expected SOAP Fault for {op_name}(invalid token), "
                f"got {type(exc).__name__}: {exc}"
            )
        pytest.fail(f"DUT did not fault on {op_name}(invalid token)")
    _test.__doc__ = (
        f"DOORCONTROL.html — {op_name} with invalid token must SOAP-fault."
    )
    return _test


for _tid, _op in _DOOR_COMMANDS:
    register(
        _tid,
        profiles={"D"}, mandatory=True,
        requires_services={"devicemgmt", "doorcontrol"},
    )(_make_invalid_token_test(_op))


# ---------------------------------------------------------------------------
# DOORCONTROL-3-1-19..24 — "COMMAND NOT SUPPORTED" tests
#
# The spec accepts EITHER outcome as conformant: if the DUT supports the
# operation, a normal response; if it doesn't, an ActionNotSupported /
# MethodNotSupported SOAP Fault. So we assert "no random exception",
# not "must fault" or "must succeed".
# ---------------------------------------------------------------------------

_COMMAND_NOT_SUPPORTED = (
    ("DOORCONTROL-3-1-19", "AccessDoor"),
    ("DOORCONTROL-3-1-20", "BlockDoor"),
    ("DOORCONTROL-3-1-21", "DoubleLockDoor"),
    ("DOORCONTROL-3-1-22", "LockDoor"),
    ("DOORCONTROL-3-1-23", "UnlockDoor"),
    ("DOORCONTROL-3-1-24", "LockDownDoor"),
)


def _make_command_supported_or_fault_test(op_name: str):
    def _test(dut: DUT, spec) -> None:
        token = _first_door_token(dut)
        op = getattr(dut.doorcontrol, op_name)
        try:
            op(token)
            # Successful → DUT supports this op. Fine.
        except zeep.exceptions.Fault as fault:
            # Acceptable per spec — DUT signals unsupported via fault.
            msg = str(fault).lower()
            assert any(k in msg for k in (
                "actionnotsupported", "methodnotsupported",
                "notsupported", "operation",
            )), (
                f"{op_name} returned a fault, but the fault code/string "
                f"doesn't look like an unsupported-command signal: {fault}"
            )
    _test.__doc__ = (
        f"DOORCONTROL — {op_name} either succeeds or returns a "
        "'command not supported' SOAP Fault (both are conformant)."
    )
    return _test


for _tid, _op in _COMMAND_NOT_SUPPORTED:
    register(
        _tid,
        profiles={"D"}, mandatory=False,
        requires_services={"devicemgmt", "doorcontrol"},
        requires_writes=True,
    )(_make_command_supported_or_fault_test(_op))
