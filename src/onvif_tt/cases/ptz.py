"""Profile S PTZ implementations from PTZ.html.

Read-only smoke tests — no movement commands (those need explicit opt-in
via ``--allow-writes`` once we add that flag).
"""

from __future__ import annotations

import pytest
import zeep.exceptions

from ..registry import register
from ..runtime.dut import DUT


@register("PTZ-1-1-1", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_get_nodes(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-1-1-1 — PTZ NODES.

    GetNodes must return at least one PTZNode entry with a token.
    """
    nodes = dut.ptz.GetNodes()
    assert nodes, "PTZ.GetNodes returned empty"
    for n in nodes:
        assert getattr(n, "token", None), "PTZNode missing token"


@register("PTZ-1-1-2", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_get_node(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-1-1-2 — PTZ NODE.

    GetNode for the first node token returned by GetNodes must match.
    """
    nodes = dut.ptz.GetNodes()
    assert nodes, "no nodes — cannot exercise GetNode"
    first_token = nodes[0].token
    node = dut.ptz.GetNode(first_token)
    assert node is not None
    assert node.token == first_token, (
        f"GetNode token mismatch: asked {first_token!r}, got {node.token!r}"
    )


@register("PTZ-1-1-4", profiles={"S"}, mandatory=False,
          requires_services={"devicemgmt", "ptz"})
def test_ptz_soap_fault_invalid_node(dut: DUT, spec) -> None:
    """PTZ.html#tc.PTZ-1-1-4 — PTZ SOAP FAULT MESSAGE.

    Querying a bogus PTZ node token must return a SOAP fault, not silently
    succeed with empty data.
    """
    try:
        dut.ptz.GetNode("__definitely_not_a_real_token__")
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(f"expected SOAP Fault, got {type(exc).__name__}: {exc}")
    pytest.fail("DUT did not fault on invalid PTZ node token")
