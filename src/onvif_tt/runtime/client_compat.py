"""Helpers that paper over known python-onvif-zeep / zeep XSD gaps.

When the underlying client library can't decode something the device
actually sent correctly, the failure is on *our* side, not the
device's. These helpers turn "tool-side gap" failures into something
honest — a `skip` with the operation name and the gap kind cited.

If you find yourself reaching for one of these often, the next move
is to extend the client lib (or its WSDL/XSD bundle) — but until
that happens, surfacing the gap honestly beats failing the test
silently.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from lxml import etree


_TT_NS = "http://www.onvif.org/ver10/schema"
_NAME_ATTR = re.compile(r'\bName\s*=\s*"([^"]+)"')
_TYPE_ATTR = re.compile(r'\bType\s*=\s*"([^"]+)"')


def call_or_skip_on_missing_op(service: Any, op_name: str, *args, **kwargs) -> Any:
    """Resolve ``op_name`` on ``service`` and invoke it; skip the test
    with a clear reason if zeep raises ``AttributeError: Service has no
    operation '<X>'`` — that's a WSDL gap in python-onvif-zeep, not a
    DUT bug.

    Note: zeep raises the AttributeError on *attribute access*, before
    the call would happen. So we must do the ``getattr`` inside the
    try block, not in the caller's argument expression.
    """
    try:
        op = getattr(service, op_name)
        return op(*args, **kwargs)
    except AttributeError as exc:
        msg = str(exc)
        m = re.search(r"Service has no operation '([^']+)'", msg)
        if m:
            op_missing = m.group(1)
            pytest.skip(
                f"python-onvif-zeep WSDL bundle does not include operation "
                f"{op_missing!r}; cannot validate response on the client side. "
                f"This is a TOOL-side gap — server-side conformance is "
                f"unaffected and can be confirmed with raw curl."
            )
        raise


def name_type_from_envelope(last_response_xml: str, element_local: str
                            ) -> list[tuple[str | None, str | None]]:
    """Extract ``Name`` / ``Type`` attribute pairs from every element
    named ``element_local`` (any namespace) in the captured SOAP
    response XML.

    Used as a fallback when zeep's parsed Python objects drop XML
    attributes inherited from a derived-type's base — a known XSD-
    derivation gap for some ONVIF types like ``AnalyticsModuleDescription``.

    Returns ``[]`` if the input is empty or unparseable.
    """
    if not last_response_xml:
        return []
    try:
        root = etree.fromstring(last_response_xml.encode("utf-8"))
    except (etree.XMLSyntaxError, ValueError):
        # Fall back to a regex sweep if the captured envelope is
        # truncated or malformed at the source. We accept the
        # imprecision because this path only runs as a workaround.
        names = _NAME_ATTR.findall(last_response_xml)
        types = _TYPE_ATTR.findall(last_response_xml)
        # zip silently truncates if counts differ — that's fine for a
        # best-effort fallback.
        return list(zip(names + [None] * (len(types) - len(names)),
                        types + [None] * (len(names) - len(types))))

    pairs: list[tuple[str | None, str | None]] = []
    # iterate every element with the target local name regardless of NS.
    for el in root.iter():
        local = etree.QName(el.tag).localname
        if local != element_local:
            continue
        pairs.append((el.get("Name"), el.get("Type")))
    return pairs
