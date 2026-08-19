"""Raw SOAP 1.2 call helper.

When python-onvif-zeep's bundled WSDL is missing an ONVIF operation
we still want to exercise device-side conformance — silently skipping
moves the gap from "tool-side known" to "untested". This helper sends
a hand-rolled SOAP 1.2 envelope to a service XAddr, capturing both
ends into ``dut.session.soap_traces`` so the rest of the framework
(``dut.last_response``, failure reporting, JSON reporter) keeps
working without changes.

We do not reach for this casually: every call here is a workaround
for a real gap in the client library. Each call site should be
crisp about *why* it's bypassing zeep.
"""

from __future__ import annotations

from typing import Any

import requests
from lxml import etree

SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
TT_NS = "http://www.onvif.org/ver10/schema"
ANALYTICS_NS = "http://www.onvif.org/ver20/analytics/wsdl"


class SoapFaultError(RuntimeError):
    """Raised when the device returns a SOAP 1.2 Fault envelope."""

    def __init__(self, code: str | None, subcode: str | None, reason: str | None) -> None:
        self.code = code
        self.subcode = subcode
        self.reason = reason
        parts = [p for p in (
            f"Code={code}" if code else None,
            f"Subcode={subcode}" if subcode else None,
            f"Reason={reason}" if reason else None,
        ) if p]
        super().__init__("SOAP Fault — " + "; ".join(parts) if parts else "SOAP Fault")


class MalformedResponseError(AssertionError):
    """The device response is not well-formed XML.

    Derived from ``AssertionError`` so pytest treats it as a normal
    test failure (not an environment error) — a non-well-formed
    envelope is a device-side ONVIF conformance violation, the very
    thing this tool exists to surface.
    """


def _excerpt(resp_bytes: bytes, line: int, col: int, ctx: int = 60) -> str:
    """Return a short snippet around ``(line, col)`` in ``resp_bytes`` to
    make a parse-error message actionable for the firmware author."""
    try:
        lines = resp_bytes.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    if line < 1 or line > len(lines):
        return ""
    src = lines[line - 1]
    start = max(0, col - ctx)
    end = min(len(src), col + ctx)
    return ("…" if start else "") + src[start:end] + ("…" if end < len(src) else "")


def _sign_envelope(envelope_bytes: bytes, dut: Any) -> bytes:
    """Apply a WS-Security UsernameToken to an envelope.

    Delegates to python-onvif-zeep's ``UsernameDigestTokenDtDiff`` so
    we inherit:

    * its clock-skew correction (``dt_diff``, populated from
      ``GetSystemDateAndTime`` — reinventing the digest by hand fails
      on devices whose clock doesn't match the host's), and
    * the same PasswordText / PasswordDigest selection (``use_digest``
      = ``cam.encrypt``) that every other operation on this DUT
      already uses successfully.

    Forcing ``use_digest=True`` on a device that was set up with
    ``encrypt=False`` will be rejected as ``wsse:FailedAuthentication``
    — symmetric with what zeep would send for any other op.
    """
    from onvif.client import UsernameDigestTokenDtDiff

    cam = dut._camera
    token = UsernameDigestTokenDtDiff(
        cam.user, cam.passwd, dt_diff=cam.dt_diff, use_digest=cam.encrypt,
    )
    env_tree = etree.fromstring(envelope_bytes)
    env_tree, _ = token.apply(env_tree, {})
    return etree.tostring(env_tree, xml_declaration=True, encoding="utf-8")


def raw_soap_call(dut: Any, service: str, body_xml: bytes,
                  action: str = "") -> etree._Element:
    """POST a SOAP 1.2 envelope to ``dut``'s ``service`` XAddr.

    Parameters
    ----------
    dut
        The DUT handle. We read its session services (for the XAddr),
        its credentials (for WS-Security), and its zeep transport
        (for connection pooling / TLS / timeout settings).
    service
        Short service name (``"analytics"``, ``"devicemgmt"``, …) —
        must already be advertised by GetServices on this DUT.
    body_xml
        Raw bytes for the ``<s:Body>`` child element, e.g.
        ``b'<tan:GetSupportedRules xmlns:tan="..."><tan:ConfigurationToken>X</tan:ConfigurationToken></tan:GetSupportedRules>'``.
    action
        Optional SOAP 1.2 action URI; appended to Content-Type if
        supplied. Most ONVIF devices ignore the action and dispatch
        on the body's QName, so default ``""`` is safe.

    Returns
    -------
    lxml ``<s:Body>`` element of the response (the response's payload
    is the body's first child).

    Raises
    ------
    SoapFaultError
        On any SOAP 1.2 Fault envelope.
    RuntimeError
        On HTTP transport errors or malformed responses.
    """
    xaddr = dut.session.services.get(service)
    if not xaddr:
        raise RuntimeError(
            f"DUT does not advertise {service!r} service — "
            f"raw_soap_call cannot route without an XAddr"
        )

    envelope = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<s:Envelope xmlns:s="' + SOAP12_NS.encode() + b'">'
        b'<s:Header/>'
        b'<s:Body>' + body_xml + b'</s:Body>'
        b'</s:Envelope>'
    )
    if dut.config.user:
        envelope = _sign_envelope(envelope, dut)
    dut.session.soap_traces.append(
        ("request", envelope.decode("utf-8", errors="replace"))
    )

    content_type = "application/soap+xml; charset=utf-8"
    if action:
        content_type += f'; action="{action}"'
    # Reuse the DUT's shared zeep transport session so these raw calls get
    # the same connection pool, TLS config and timeouts as everything else.
    # Falls back to a bare requests.post for DUT stand-ins in unit tests.
    session = getattr(getattr(dut, "_transport", None), "session", requests)
    resp = session.post(
        xaddr,
        data=envelope,
        headers={"Content-Type": content_type},
        timeout=dut.config.timeout,
    )
    resp_bytes = resp.content
    dut.session.soap_traces.append(
        ("response", resp_bytes.decode("utf-8", errors="replace"))
    )

    # Strict parse. A device response that is not well-formed XML is
    # itself an ONVIF conformance violation — ONVIF Core Spec §5 ("XML
    # encoding") requires conformant XML 1.0 + Namespaces 1.0. Masking
    # those failures with ``recover=True`` would defeat the entire
    # point of this tool: surfacing real spec deviations to the user.
    try:
        root = etree.fromstring(resp_bytes)
    except etree.XMLSyntaxError as exc:
        pos = getattr(exc, "position", None) or (0, 0)
        excerpt = _excerpt(resp_bytes, pos[0], pos[1])
        raise MalformedResponseError(
            f"Device response from {service} XAddr is not well-formed "
            f"XML (ONVIF Core §5 requires conformant XML 1.0 + "
            f"Namespaces in XML 1.0): {exc}. Near: {excerpt!r}"
        ) from exc

    fault = root.find(f".//{{{SOAP12_NS}}}Fault")
    if fault is not None:
        def _text(path: str) -> str | None:
            el = fault.find(path)
            return el.text if el is not None else None
        raise SoapFaultError(
            code=_text(f"{{{SOAP12_NS}}}Code/{{{SOAP12_NS}}}Value"),
            subcode=_text(
                f"{{{SOAP12_NS}}}Code/{{{SOAP12_NS}}}Subcode/{{{SOAP12_NS}}}Value"
            ),
            reason=_text(f"{{{SOAP12_NS}}}Reason/{{{SOAP12_NS}}}Text"),
        )

    body = root.find(f"{{{SOAP12_NS}}}Body")
    if body is None:
        raise RuntimeError("Response envelope has no <s:Body>")
    return body
