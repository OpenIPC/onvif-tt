"""Authentication conformance tests.

The plan called out ``AUTH-1-1-*`` as a must-have family, but there is
no ``AUTH-*`` prefix in the published v20.12 corpus — the authentication
conformance bits are scattered across other test cases ("Pre-Requisite:
authentication required" etc.). These ``LOCAL-AUTH-*`` tests cover the
behaviour we actually care about for the OpenIPC library:

* anonymous access to a privileged operation is rejected;
* wrong credentials yield a SOAP fault (not a silent success);
* WS-UsernameToken authentication succeeds (implicitly verified by any
  other passing test, but called out explicitly here for the catalog).

The implementation deliberately builds *fresh* ``DUT`` instances with
mutated credentials rather than mutating the session-scoped one — we
must not leave the long-lived DUT in an unauthenticated state for the
tests that come after.
"""

from __future__ import annotations

import pytest
import zeep.exceptions

from ..registry import register
from ..runtime.dut import DUT, DUTConfig


def _dut_with_creds(template: DUT, user: str, password: str) -> DUT:
    """Build a fresh DUT sharing the template's host/port but with
    different credentials."""
    cfg = DUTConfig(
        host=template.config.host,
        port=template.config.port,
        user=user,
        password=password,
    )
    return DUT(cfg)


_PRIVILEGED_OPS = ("GetNetworkInterfaces", "GetSystemLog")


@register("LOCAL-AUTH-VALID-CREDENTIALS", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"},
          tags={"local", "auth"})
def test_auth_valid_credentials_accepted(dut: DUT, spec) -> None:
    """A privileged operation succeeds with the supplied credentials.

    Implicit smoke — but worth being explicit so the catalog shows
    "auth passes with the configured creds" as a green tick.
    """
    # GetNetworkInterfaces is User-level per the ONVIF Core Spec — the
    # cheapest privileged op available on every device.
    interfaces = dut.devicemgmt.GetNetworkInterfaces()
    assert interfaces, "GetNetworkInterfaces should succeed with valid creds"


@register("LOCAL-AUTH-WRONG-PASSWORD-REJECTED", profiles={"S", "T"},
          mandatory=True,
          requires_services={"devicemgmt"},
          tags={"local", "auth"},
          xfail_on=[{
              "Manufacturer": "H264",
              "reason": "Xiongmai stock firmware is documented to "
                        "ignore invalid SOAP input silently — same "
                        "pattern as DEVICE-1-1-9 / PTZ-1-1-4 etc. "
                        "Wrong password likely surfaces the same way.",
          }])
def test_auth_wrong_password_rejected(dut: DUT, spec) -> None:
    """A privileged operation invoked with a deliberately bad password
    must SOAP-fault.

    We don't accept "connection close" as conformant — the device must
    return an env:Sender/ter:NotAuthorized fault per ONVIF Core Spec.
    """
    if not dut.config.user:
        pytest.skip("anonymous run — no credentials to test")
    bad = _dut_with_creds(dut, dut.config.user, "definitely-not-the-password")
    try:
        bad.devicemgmt.GetNetworkInterfaces()
    except zeep.exceptions.Fault:
        return  # ✓ a SOAP fault is the correct rejection
    except Exception as exc:
        pytest.fail(
            f"expected SOAP Fault for wrong password, got "
            f"{type(exc).__name__}: {exc}"
        )
    pytest.fail("DUT accepted a privileged op with a wrong password")


@register("LOCAL-AUTH-ANONYMOUS-REJECTED", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt"},
          tags={"local", "auth"},
          xfail_on=[{
              "Manufacturer": "H264",
              "reason": "Xiongmai stock firmware does not reliably "
                        "enforce auth on every privileged op.",
          }])
def test_auth_anonymous_rejected_on_privileged_op(dut: DUT, spec) -> None:
    """An anonymous request to a privileged op must fault.

    Per ONVIF Core Specification §5.12 ("Security"), every operation
    above ``PRE_AUTH`` access level requires authentication. We test
    by spinning up a DUT with empty creds and invoking GetSystemLog
    (which is Administrator-level on every device).
    """
    if not dut.config.user:
        pytest.skip("test only meaningful when DUT actually requires auth")
    anon = _dut_with_creds(dut, "", "")
    try:
        anon.devicemgmt.GetSystemLog({"LogType": "System"})
    except zeep.exceptions.Fault:
        return
    except Exception as exc:
        pytest.fail(
            f"expected SOAP Fault for anonymous privileged op, got "
            f"{type(exc).__name__}: {exc}"
        )
    pytest.fail("DUT accepted a privileged op without authentication")
