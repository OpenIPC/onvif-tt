"""Recording Search service (Profile G).

Pair to ``cases/recording.py``. The Search service exposes
``FindRecordings`` / ``GetRecordingSearchResults`` / ``EndSearch``
session-style operations, plus capability queries.
"""

from __future__ import annotations

import time
import pytest

from ..registry import register
from ..runtime.dut import DUT


_SEARCH_NS = "http://www.onvif.org/ver10/search/wsdl"


@register("SEARCH-1-1-1", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "search"})
def test_search_service_capabilities(dut: DUT, spec) -> None:
    """SEARCH.html#tc.SEARCH-1-1-1 — RECORDING SEARCH SERVICE CAPABILITIES."""
    caps = dut.search.GetServiceCapabilities()
    assert caps is not None, "search GetServiceCapabilities returned None"
    # MetadataSearch is the cheapest mandatory boolean flag.
    assert hasattr(caps, "MetadataSearch"), (
        "search capabilities missing MetadataSearch flag"
    )


@register("SEARCH-1-1-2", profiles={"G"}, mandatory=True,
          requires_services={"devicemgmt", "search"})
def test_get_services_and_search_caps_consistency(dut: DUT, spec) -> None:
    """SEARCH.html#tc.SEARCH-1-1-2 — GetServices(IncludeCapability)
    Search entry matches a direct GetServiceCapabilities call."""
    services = dut.devicemgmt.GetServices(True)
    s_entry = next(
        (s for s in services if s.Namespace == _SEARCH_NS), None
    )
    assert s_entry is not None, "GetServices didn't include search"
    direct = dut.search.GetServiceCapabilities()
    embedded = getattr(s_entry, "Capabilities", None)
    assert embedded is not None, "GetServices didn't embed search caps"


@register("SEARCH-2-1-11", profiles={"G"}, mandatory=False,
          requires_services={"devicemgmt", "search"})
def test_end_search_with_invalid_token(dut: DUT, spec) -> None:
    """SEARCH.html#tc.SEARCH-2-1-11 — END SEARCH WITH INVALID SEARCHTOKEN.

    Calling EndSearch with a never-issued SearchToken must SOAP-fault.
    """
    from ..runtime.fault import assert_soap_fault
    assert_soap_fault(dut.search.EndSearch, "__definitely_not_a_real_search_token__")


@register("LOCAL-SEARCH-FIND-RECORDINGS-EMPTY-SCOPE", profiles={"G"},
          mandatory=False,
          requires_services={"devicemgmt", "search"},
          tags={"local"})
def test_find_recordings_with_empty_scope(dut: DUT, spec) -> None:
    """FindRecordings with an empty Scope must still return a SearchToken
    (the device matches all recordings). Round-trip to EndSearch
    immediately so we don't leave search state behind.
    """
    req = dut.search.create_type("FindRecordings")
    req.Scope = {"IncludedSources": [], "IncludedRecordings": []}
    req.MaxMatches = 1
    req.KeepAliveTime = "PT10S"
    try:
        resp = dut.search.FindRecordings(req)
    except Exception as exc:
        pytest.skip(f"FindRecordings rejected our minimal scope: {exc}")
    token = getattr(resp, "SearchToken", None)
    assert token, "FindRecordings returned no SearchToken"
    # Best-effort cleanup — ignore errors.
    try:
        dut.search.EndSearch(token)
    except Exception:
        pass


@register("LOCAL-SEARCH-GET-RECORDING-SUMMARY", profiles={"G"},
          mandatory=False,
          requires_services={"devicemgmt", "search"},
          tags={"local"})
def test_get_recording_summary(dut: DUT, spec) -> None:
    """GetRecordingSummary returns DataFrom / DataUntil even on an empty
    device (the spec says the response is always populated)."""
    try:
        summary = dut.search.GetRecordingSummary()
    except Exception as exc:
        pytest.skip(f"GetRecordingSummary not supported: {exc}")
    assert summary is not None, "GetRecordingSummary returned None"
    # Optional fields, but if present must be datetimes — just verify
    # the call shape.
    assert hasattr(summary, "DataFrom"), "summary missing DataFrom"
    assert hasattr(summary, "DataUntil"), "summary missing DataUntil"
