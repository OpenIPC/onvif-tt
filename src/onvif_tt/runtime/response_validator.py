"""Structural validation of every response against the ONVIF schema.

The suite used to assert only on the fields a test happened to name, and zeep
does not enforce the schema, so **a response missing a mandatory element
passed**. That is structural rather than a gap in any one test: no amount of
"is this value sensible?" catches an element that is simply absent. zeep
substitutes an empty list for a missing mandatory element rather than raising,
and ``Settings(strict=True)`` does not change that — ``minOccurs`` is not
enforced either way. See issue #3.

Why this is not ``lxml.etree.XMLSchema``
----------------------------------------

The obvious implementation — compile the vendored schemas and call
``schema.validate(response)`` — is impossible, because **the published ONVIF
schemas are not valid XSD 1.0**. ``onvif.xsd`` alone contains 26+ Unique
Particle Attribution violations (``PTZStatus``, ``ColorDescriptor``,
``LensProjection``, …) and ``metadatastream.xsd`` adds more, all from the
house style of trailing ``<xs:any>`` extension points after optional elements.
libxml2 enforces UPA and refuses to compile them; 13 of our 14 service schemas
fail to build. (.NET exposes ``XmlSchemaSet.CompilationSettings.EnableUpaCheck
= false``, which is presumably how ONVIF's own tooling copes. libxml2 has no
equivalent switch.)

So this validates structurally, from two sources that are available and
correct:

* **zeep's type model** — which parses these schemas quite happily, since its
  XSD engine doesn't enforce UPA. It gives us ``min_occurs`` for every declared
  child at arbitrary depth, and ``required`` for attributes.
* **the vendored XSDs parsed as plain XML** — for ``xs:enumeration`` facets,
  which zeep discards (``tt:VideoEncoding`` reaches us as bare ``str``).
  Parsing is not compiling, so the UPA problem doesn't arise.

Three checks, matching the two defects in issue #3 plus one generalisation:

``missing-element``
    A child declared ``minOccurs >= 1`` is absent. Catches the
    ``GetEventProperties`` without ``MessageContentFilterDialect`` /
    ``MessageContentSchemaLocation`` that eight event tests passed over.
``bad-enum``
    An element's text is outside its type's enumeration. Catches ``H265`` in a
    **ver10** ``VideoEncoderConfiguration``, where ``tt:VideoEncoding`` is a
    closed enumeration of ``JPEG``/``MPEG4``/``H264`` (issue #2).
``missing-attribute``
    A ``use="required"`` attribute is absent. Generalises the hand-written
    ``RuleDescription`` Name/Type assertions in ``cases/analytics.py``.

What it deliberately does **not** check: datatypes and formats (an ``xs:int``
holding ``"abc"``, a malformed ``xs:dateTime``), and undeclared elements —
ONVIF's ``xs:any`` extension points make the latter noisy. Don't read a clean
run as "this response is schema-valid"; read it as "these three classes are
clean".

Violations are *recorded*, never raised in-band: the plugin runs inside zeep's
ingress path, and raising there would abort the SOAP call itself — turning a
conformance finding into what looks like a transport error, and potentially
stranding a PullPoint subscription mid-teardown. ``runner/dispatch`` fails the
calling test afterwards.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

from lxml import etree
from zeep import Plugin

from . import schema_store, services

log = logging.getLogger(__name__)

_XSD_NS = "http://www.w3.org/2001/XMLSchema"
_WSDL_NS = "http://schemas.xmlsoap.org/wsdl/"
_SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"

# zeep's name for an xs:any wildcard particle. Not a real declared child, and
# its absence means nothing.
_ANY = "_value_1"


@dataclass(slots=True)
class SchemaViolation:
    """One observation. ``code`` is a short stable token for filtering."""

    operation: str
    code: str           # "missing-element" | "bad-enum" | "missing-attribute"
    path: str           # "GetEventProperties/MessageContentFilterDialect"
    detail: str

    def key(self) -> tuple[str, str, str]:
        """Identity for de-duplication across tests.

        One device defect is typically hit by many tests — on the reference
        camera a single malformed ``GetProfiles`` reddens 13 of them. The
        reporter groups on this so the run reads as N defects rather than N
        failures.
        """
        return (self.code, self.path, self.detail)


# ---------------------------------------------------------------------------
# Enumeration facets — read straight out of the vendored XSDs
# ---------------------------------------------------------------------------

_ENUMS: dict[str, list[str]] | None = None


def enum_index() -> dict[str, list[str]]:
    """``{"{ns}TypeName": [allowed, values]}`` for every enumerated simple type.

    Built once from every vendored document: the standalone ``.xsd`` files and
    the inline ``<xs:schema>`` inside each WSDL. Parsing only — see the module
    docstring for why we can't compile.
    """
    global _ENUMS
    if _ENUMS is not None:
        return _ENUMS

    index: dict[str, list[str]] = {}
    for entry in schema_store.manifest().values():
        path = schema_store.SCHEMA_ROOT / entry["path"]
        try:
            root = etree.parse(str(path)).getroot()
        except (OSError, etree.XMLSyntaxError) as exc:
            log.warning("cannot parse vendored schema %s: %s", path, exc)
            continue
        # A WSDL carries its schema inline; an .xsd is one already.
        if root.tag == f"{{{_WSDL_NS}}}definitions":
            roots = root.findall(f"{{{_WSDL_NS}}}types/{{{_XSD_NS}}}schema")
        elif root.tag == f"{{{_XSD_NS}}}schema":
            roots = [root]
        else:
            continue
        for schema in roots:
            tns = schema.get("targetNamespace")
            if not tns:
                continue
            for st in schema.iter(f"{{{_XSD_NS}}}simpleType"):
                name = st.get("name")
                if not name:
                    continue  # anonymous, not addressable by QName
                values = [e.get("value")
                          for e in st.iter(f"{{{_XSD_NS}}}enumeration")]
                if values:
                    index[f"{{{tns}}}{name}"] = values
    _ENUMS = index
    return index


# ---------------------------------------------------------------------------
# The walker
# ---------------------------------------------------------------------------

def validate_element(node, zeep_type, operation: str, path: str,
                     out: list[SchemaViolation]) -> None:
    """Check ``node`` against ``zeep_type``, recursing into declared children.

    Appends to ``out``; never raises. An unrecognised child is skipped rather
    than reported — ONVIF's extension points make undeclared elements normal.

    Matching is on the **full QName**, not the local name. ONVIF responses mix
    namespaces freely (the wrapper is in the service namespace, its contents in
    ``tt:``), and matching on local names alone would let a vendor-extension
    element impersonate a required ONVIF child — suppressing the
    ``missing-element`` it should have raised, and then being validated against
    the wrong declaration.
    """
    enums = enum_index()
    elements = getattr(zeep_type, "elements", None) or []

    declared: dict[str, object] = {}
    for name, sub in elements:
        if name == _ANY:
            continue
        qname = str(getattr(sub, "qname", "") or "")
        if qname:
            declared[qname] = sub

    counts = Counter(child.tag for child in node
                     if isinstance(child.tag, str))
    for name, sub in elements:
        if name == _ANY:
            continue
        qname = str(getattr(sub, "qname", "") or "")
        min_occurs = getattr(sub, "min_occurs", 0) or 0
        # Without a QName we cannot tell presence from absence, so say nothing
        # rather than risk a false alarm.
        if not qname or not min_occurs:
            continue
        seen = counts.get(qname, 0)
        if seen < min_occurs:
            out.append(SchemaViolation(
                operation=operation,
                code="missing-element",
                path=f"{path}/{name}",
                detail=(
                    f"mandatory element (minOccurs={min_occurs}) is absent "
                    f"from the response"
                    if seen == 0 else
                    f"declared minOccurs={min_occurs} but only {seen} "
                    f"occurrence(s) present"
                ),
            ))

    for name, attr in (getattr(zeep_type, "attributes", None) or []):
        if getattr(attr, "required", False) and node.get(name) is None:
            out.append(SchemaViolation(
                operation=operation,
                code="missing-attribute",
                path=f"{path}/@{name}",
                detail="attribute is declared use=\"required\" but absent",
            ))

    for child in node:
        if not isinstance(child.tag, str):
            continue  # comment / processing instruction
        sub = declared.get(child.tag)
        if sub is None:
            continue
        name = etree.QName(child).localname
        qname = str(getattr(sub.type, "qname", "") or "")
        allowed = enums.get(qname)
        text = (child.text or "").strip()
        if allowed and text and text not in allowed:
            out.append(SchemaViolation(
                operation=operation,
                code="bad-enum",
                path=f"{path}/{name}",
                detail=(f"{text!r} is not a value of {qname} — "
                        f"permitted: {', '.join(allowed)}"),
            ))
        validate_element(child, sub.type, operation, f"{path}/{name}", out)


def validate_body(body, find_element, operation: str) -> list[SchemaViolation]:
    """Validate every child of a SOAP ``<Body>``.

    ``find_element`` maps a ``"{ns}Local"`` QName to a zeep element
    declaration, or ``None`` when we hold no schema for it (a fault, or a
    service we don't model) — in which case that child is skipped.
    """
    out: list[SchemaViolation] = []
    if body is None:
        return out
    for child in body:
        if etree.QName(child).localname == "Fault":
            continue  # faults are their own signal; tests assert on them
        try:
            element = find_element(str(etree.QName(child)))
        except Exception:  # noqa: BLE001 — lookup must never break a run
            element = None
        if element is None:
            continue
        validate_element(child, element.type, operation,
                         etree.QName(child).localname, out)
    return out


# ---------------------------------------------------------------------------
# zeep plugin
# ---------------------------------------------------------------------------

class ResponseValidator(Plugin):
    """Validates every ingress envelope, recording what it finds.

    Mirrors :class:`~.wsa_validator.WSAValidator` — same ``attach`` protocol,
    same record-don't-raise stance, same reporting path.
    """

    def __init__(self, dut) -> None:
        self._dut = dut
        self._sink: list[SchemaViolation] | None = None
        self.enabled = True

    def attach(self, sink: list[SchemaViolation]) -> None:
        self._sink = sink

    def _find_element(self, qname: str):
        """Resolve a QName against every zeep client bound on this DUT.

        Searches ``dut.zeep_clients``, not ``dut._services``: the
        subscription-rooted bindings (PullPoint, SubscriptionManager,
        NotificationProducer) are never cached as services, so a
        ``_services``-only search silently skips every Subscribe / PullMessages
        response — validation that looked enabled but wasn't.
        """
        for client in list(self._dut.zeep_clients):
            try:
                return client.get_element(qname)
            except Exception:  # noqa: BLE001 — wrong client, try the next
                continue
        return None

    def ingress(self, envelope, http_headers, operation):  # noqa: D401
        if self._sink is None or not self.enabled or envelope is None:
            return envelope, http_headers
        op_name = getattr(operation, "name", "<unknown>") or "<unknown>"
        try:
            body = envelope.find(f"{{{_SOAP12_NS}}}Body")
            self._sink.extend(validate_body(body, self._find_element, op_name))
        except Exception:  # noqa: BLE001
            # A validator bug must never take down a conformance run.
            log.exception("response validation failed for %s", op_name)
        return envelope, http_headers


def element_finder_for_services(shorts):
    """Standalone QName resolver for callers without a bound DUT.

    Used by the raw-SOAP path, which bypasses zeep entirely and so has no
    client of its own. Builds (and caches) a client per named service.
    """
    from zeep import Client, Settings

    cache: dict[str, object] = {}
    settings = Settings(strict=False, xml_huge_tree=True)
    transport = schema_store.VendoredSchemaTransport()

    def find(qname: str):
        for short in shorts:
            client = cache.get(short)
            if client is None:
                sd = services.get(short)
                path = schema_store.local_path(sd.wsdl_url) if sd else None
                if path is None:
                    continue
                try:
                    client = cache[short] = Client(
                        wsdl=str(path), settings=settings, transport=transport)
                except Exception:  # noqa: BLE001
                    continue
            try:
                return client.get_element(qname)
            except Exception:  # noqa: BLE001
                continue
        return None

    return find
