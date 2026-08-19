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
from ..runtime.fault import assert_soap_fault

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
        # Same negotiation policy as the session DUT, so a run pinned to one
        # password type doesn't have the negative tests probing the other.
        auth=template.config.auth,
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
    # python-onvif-zeep's DUT construction itself calls GetCapabilities
    # (via ONVIFCamera.update_xaddrs), so the auth fault surfaces at
    # the constructor on devices that gate every op behind auth. Wrap
    # the construction; either the construction faults (early-rejection)
    # or it succeeds and we then verify the actual privileged op faults.
    try:
        bad = _dut_with_creds(dut, dut.config.user, "definitely-not-the-password")
    except Exception as exc:
        from ..runtime.fault import looks_like_soap_fault
        if looks_like_soap_fault(exc):
            return  # device rejected wrong password at handshake — ✓
        raise
    assert_soap_fault(bad.devicemgmt.GetNetworkInterfaces)


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
    # Same construction-time-fault pattern as wrong-password.
    try:
        anon = _dut_with_creds(dut, "", "")
    except Exception as exc:
        from ..runtime.fault import looks_like_soap_fault
        if looks_like_soap_fault(exc):
            return  # device rejected anonymous client at handshake — ✓
        raise
    assert_soap_fault(lambda: anon.devicemgmt.GetSystemLog({"LogType": "System"}))


# ---------------------------------------------------------------------------
# SECURITY-1-1-1 — USER TOKEN PROFILE (real catalog ID)
# ---------------------------------------------------------------------------

_WSSE = ("http://docs.oasis-open.org/wss/2004/01/"
         "oasis-200401-wss-wssecurity-secext-1.0.xsd")
_WSU = ("http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-wssecurity-utility-1.0.xsd")
_PW_DIGEST = ("http://docs.oasis-open.org/wss/2004/01/"
              "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")
_PW_TEXT = ("http://docs.oasis-open.org/wss/2004/01/"
            "oasis-200401-wss-username-token-profile-1.0#PasswordText")
_SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
_TDS = "http://www.onvif.org/ver10/device/wsdl"


def _username_token(user: str, password: str, *, digest: bool,
                    nonce: bool, created: bool) -> str:
    """Build one UsernameToken variant, well-formed but selectively incomplete.

    The digest is computed over exactly the parts present, per the
    UsernameToken profile: ``Base64(SHA1(nonce + created + password))`` with
    absent parts contributing nothing. So a token missing its nonce is still
    internally consistent — the device has to reject it on the *policy*
    grounds that a nonce is required, not because the maths doesn't add up.
    """
    import base64
    import hashlib
    import os
    from datetime import datetime, timezone

    raw_nonce = os.urandom(16)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [
        f'<wsse:Username>{user}</wsse:Username>',
    ]
    if digest:
        material = (raw_nonce if nonce else b"") \
            + (stamp.encode() if created else b"") + password.encode()
        secret = base64.b64encode(hashlib.sha1(material).digest()).decode()
        parts.append(f'<wsse:Password Type="{_PW_DIGEST}">{secret}'
                     f'</wsse:Password>')
    else:
        parts.append(f'<wsse:Password Type="{_PW_TEXT}">{password}'
                     f'</wsse:Password>')
    if nonce:
        parts.append(
            f'<wsse:Nonce>{base64.b64encode(raw_nonce).decode()}</wsse:Nonce>')
    if created:
        parts.append(f'<wsu:Created xmlns:wsu="{_WSU}">{stamp}</wsu:Created>')
    return (f'<wsse:Security xmlns:wsse="{_WSSE}"><wsse:UsernameToken>'
            + "".join(parts) + '</wsse:UsernameToken></wsse:Security>')


def _accepts_token(dut: DUT, security_header: str) -> bool:
    """POST GetDeviceInformation with ``security_header``; True if accepted.

    Hand-rolled rather than routed through zeep, because the whole point is
    to send tokens zeep would refuse to build.
    """
    from lxml import etree

    envelope = (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="{_SOAP12}">'
        f'<s:Header>{security_header}</s:Header>'
        f'<s:Body><tds:GetDeviceInformation xmlns:tds="{_TDS}"/></s:Body>'
        f'</s:Envelope>'
    ).encode()
    session = getattr(dut._transport, "session", None)
    poster = session.post if session is not None else __import__("requests").post
    resp = poster(
        dut._xaddr_for("devicemgmt"),
        data=envelope,
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        timeout=dut.config.timeout,
    )
    try:
        root = etree.fromstring(resp.content)
    except etree.XMLSyntaxError:
        return False
    return root.find(f".//{{{_SOAP12}}}Fault") is None


@register("SECURITY-1-1-1", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt"},
          tags={"auth", "security"})
def test_user_token_profile(dut: DUT, spec) -> None:
    """BASE.html#tc.SECURITY-1-1-1 — USER TOKEN PROFILE.

    Message-level security via the WS-UsernameToken profile. The spec's
    failure criteria are the assertions here, near-verbatim: the DUT must
    reject a token without a nonce, without a timestamp, and without password
    type ``PasswordDigest``; and must accept a correctly formed one.

    That third criterion is the sharp one, and it is not a formality —
    accepting ``PasswordText`` means the device requires the password to
    cross the wire in cleartext. Both OpenIPC/majestic builds on the bench
    fail exactly there: they reject ``PasswordDigest`` outright and accept
    only ``PasswordText``, which is the inverse of what ONVIF requires.
    """
    if not dut.config.user:
        pytest.skip("no credentials configured — nothing to authenticate with")
    if dut.session.auth.accepted == "none":
        pytest.skip("--auth none: message-level security is not under test")

    user, password = dut.config.user, dut.config.password

    # If the device serves privileged data to anyone, none of the token
    # variants below mean anything. That's a real defect, but it belongs to
    # LOCAL-AUTH-ANONYMOUS-REJECTED; reporting it here as "accepts a bad
    # token" would misattribute it.
    if _accepts_token(dut, ""):
        pytest.skip(
            "DUT answers GetDeviceInformation with no Security header at all, "
            "so token validity cannot be assessed here — see "
            "LOCAL-AUTH-ANONYMOUS-REJECTED"
        )

    failures: list[str] = []

    if _accepts_token(dut, _username_token(user, password, digest=True,
                                           nonce=False, created=True)):
        failures.append("accepted a UsernameToken with no Nonce")
    if _accepts_token(dut, _username_token(user, password, digest=True,
                                           nonce=True, created=False)):
        failures.append("accepted a UsernameToken with no Created timestamp")
    if _accepts_token(dut, _username_token(user, password, digest=False,
                                           nonce=True, created=True)):
        failures.append(
            "accepted a UsernameToken with password type PasswordText — the "
            "password crosses the wire in cleartext, and ONVIF requires "
            "PasswordDigest"
        )
    if not _accepts_token(dut, _username_token(user, password, digest=True,
                                               nonce=True, created=True)):
        failures.append(
            "rejected a correctly formed UsernameToken (PasswordDigest with "
            "Nonce and Created) — the DUT does not support the User Token "
            "profile ONVIF Core requires"
        )

    assert not failures, (
        "WS-UsernameToken profile violations:\n  - " + "\n  - ".join(failures)
    )
