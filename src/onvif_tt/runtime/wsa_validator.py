"""WS-Addressing validation plugin.

Every ONVIF response is required by the WS-Addressing 1.0 SOAP Binding
to carry an ``<wsa:Action>`` element in the SOAP Header, whose value
is the well-known URI for that operation's response (typically the
operation's input Action URI with a ``Response`` suffix, or the value
declared in the WSDL's ``wsaw:Action`` attribute on the output message).

The DTT validates this on every test — it's one of the most common
"vendor accidentally drops a header" findings. ``onvif-tt`` was
missing this entirely; this plugin closes the gap without adding any
new test IDs (every existing test gains the check for free).

Failures are *recorded*, not raised. We collect them on the DUT session
and the dispatch+reporter pipeline surfaces them in ``results.json``
under ``wsa_violations`` for each test. A vendor with broken WS-A
headers will see this without ANY test failing on its own merits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from lxml import etree
from zeep import Plugin

log = logging.getLogger(__name__)


# Namespaces we care about. SOAP 1.2 is the only envelope ONVIF accepts.
_SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
_SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/"  # legacy, reject
_WSA_NS    = "http://www.w3.org/2005/08/addressing"


@dataclass(slots=True)
class WSAViolation:
    """One observation. ``code`` is a short stable token for filtering."""

    operation: str
    code: str           # "missing-action" | "wrong-action-suffix" | "soap11-envelope"
    detail: str


class WSAValidator(Plugin):
    """zeep plugin that runs on every ingress envelope."""

    def __init__(self) -> None:
        self._sink: list[WSAViolation] | None = None

    def attach(self, sink: list[WSAViolation]) -> None:
        """Bind this plugin to a violation sink. The dispatch fixture
        attaches the DUT session's list; tests inherit transparently."""
        self._sink = sink

    def ingress(self, envelope, http_headers, operation):  # noqa: D401
        if self._sink is None:
            return envelope, http_headers
        if envelope is None:
            return envelope, http_headers

        op_name = getattr(operation, "name", "<unknown>") or "<unknown>"

        # Envelope namespace check — ONVIF mandates SOAP 1.2.
        root_ns = etree.QName(envelope.tag).namespace
        if root_ns == _SOAP11_NS:
            self._sink.append(WSAViolation(
                operation=op_name,
                code="soap11-envelope",
                detail=f"response uses SOAP 1.1 envelope namespace ({_SOAP11_NS}); "
                       "ONVIF mandates SOAP 1.2",
            ))
        elif root_ns != _SOAP12_NS:
            self._sink.append(WSAViolation(
                operation=op_name,
                code="non-soap12-envelope",
                detail=f"response root namespace is {root_ns!r}, expected {_SOAP12_NS!r}",
            ))

        # wsa:Action must be present in <soap:Header>.
        action_elem = envelope.find(
            f"{{{_SOAP12_NS}}}Header/{{{_WSA_NS}}}Action"
        )
        if action_elem is None:
            # Some servers put wsa:Action under a SOAP 1.1 header; check.
            action_elem = envelope.find(
                f"{{{_SOAP11_NS}}}Header/{{{_WSA_NS}}}Action"
            )
        if action_elem is None or not (action_elem.text or "").strip():
            self._sink.append(WSAViolation(
                operation=op_name,
                code="missing-action",
                detail="<wsa:Action> header is missing or empty in the response",
            ))
            return envelope, http_headers

        action = (action_elem.text or "").strip()
        # ONVIF response Actions almost always end in 'Response' (the
        # output-message convention). A response Action that doesn't is
        # usually a vendor bug — they returned the *request* Action.
        # We accept Fault-style actions too.
        if not (action.endswith("Response")
                or action.endswith("fault")
                or "Fault" in action):
            self._sink.append(WSAViolation(
                operation=op_name,
                code="wrong-action-suffix",
                detail=f"<wsa:Action>={action!r} (expected something ending in "
                       "'Response' or a Fault URI)",
            ))

        # If this is a Fault response, validate the SOAP 1.2 Fault
        # structure. Spec mandates env:Code → env:Value and env:Reason
        # → env:Text under the Fault element. Vendors sometimes ship
        # half-built faults that look fault-like to the eye but don't
        # satisfy the schema.
        if "Fault" in action or action.endswith("fault"):
            body = envelope.find(f"{{{_SOAP12_NS}}}Body")
            if body is not None:
                fault = body.find(f"{{{_SOAP12_NS}}}Fault")
                if fault is not None:
                    self._validate_fault_structure(fault, op_name)

        return envelope, http_headers

    def _validate_fault_structure(self, fault, op_name: str) -> None:
        """SOAP 1.2 Fault must contain env:Code/env:Value + env:Reason/env:Text."""
        assert self._sink is not None
        code = fault.find(f"{{{_SOAP12_NS}}}Code")
        if code is None:
            self._sink.append(WSAViolation(
                operation=op_name,
                code="fault-missing-code",
                detail="SOAP Fault envelope missing required <env:Code> child",
            ))
        else:
            value = code.find(f"{{{_SOAP12_NS}}}Value")
            if value is None or not (value.text or "").strip():
                self._sink.append(WSAViolation(
                    operation=op_name,
                    code="fault-code-missing-value",
                    detail="<env:Code> missing required non-empty <env:Value>",
                ))
        reason = fault.find(f"{{{_SOAP12_NS}}}Reason")
        if reason is None:
            self._sink.append(WSAViolation(
                operation=op_name,
                code="fault-missing-reason",
                detail="SOAP Fault envelope missing required <env:Reason> child",
            ))
        else:
            text = reason.find(f"{{{_SOAP12_NS}}}Text")
            if text is None or not (text.text or "").strip():
                self._sink.append(WSAViolation(
                    operation=op_name,
                    code="fault-reason-missing-text",
                    detail="<env:Reason> missing required non-empty <env:Text>",
                ))
