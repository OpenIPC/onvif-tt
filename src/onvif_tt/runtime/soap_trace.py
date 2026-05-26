"""zeep Plugin that captures SOAP request/response envelopes.

Captured envelopes are stored in a bounded deque so they can be attached
to failure reports without exploding memory on long runs.
"""

from __future__ import annotations

import collections
from typing import Any

from lxml import etree


class SoapTrace:
    """Implements the zeep Plugin protocol (``egress`` / ``ingress``)."""

    def __init__(
        self, sink: collections.deque[tuple[str, str]]
    ) -> None:
        self._sink = sink

    def egress(self, envelope, http_headers, operation, binding_options):  # noqa: D401
        self._sink.append(("request", _serialise(envelope)))
        return envelope, http_headers

    def ingress(self, envelope, http_headers, operation):  # noqa: D401
        self._sink.append(("response", _serialise(envelope)))
        return envelope, http_headers


def _serialise(envelope: Any) -> str:
    try:
        return etree.tostring(envelope, pretty_print=True, encoding="unicode")
    except Exception:  # pragma: no cover — defensive
        return str(envelope)
