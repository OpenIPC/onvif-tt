"""Smoke tests for ``onvif_tt.specs.parser``.

Run with: ``pytest tests/test_parser.py``.

Lives outside the runner plugin so a normal pytest run picks it up
without needing a DUT.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from onvif_tt.specs.parser import parse_corpus, parse_file, cases_to_json


REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "corpus" / "html"


@pytest.fixture(scope="module")
def base_cases():
    return parse_file(CORPUS / "BASE.html")


def test_base_has_many_cases(base_cases):
    # BASE.html should have well over 100 top-level test sections.
    assert len(base_cases) > 100, f"only got {len(base_cases)} BASE cases"


def test_ids_are_parsable(base_cases):
    pat = re.compile(r"^[A-Z][A-Z0-9_]+-[0-9]+-[0-9]+-[0-9]+(-v[0-9.]+)?$")
    for c in base_cases:
        assert pat.match(c.id), f"bad id shape: {c.id!r}"


def test_known_test_metadata(base_cases):
    by_id = {c.id: c for c in base_cases}
    c = by_id.get("IPCONFIG-1-1-3")
    assert c is not None, "IPCONFIG-1-1-3 missing from BASE.html"
    assert c.title == "IPV4 DHCP"
    assert c.wsdl_reference == "devicemgmt.wsdl"
    assert c.test_purpose.startswith("To test IPv4 DHCP")
    assert "Network Configuration" in c.prerequisite
    assert any(
        op == "GetNetworkInterfaces" for op in c.operations
    ), "GetNetworkInterfaces not extracted as an operation"
    assert len(c.procedure) > 5, "procedure parsing collapsed unexpectedly"


def test_full_corpus_parse_is_deterministic():
    """Re-parsing produces byte-identical JSON. Without this we can't
    commit corpus/parsed.json safely."""
    cases1 = parse_corpus(CORPUS)
    cases2 = parse_corpus(CORPUS)
    j1 = json.dumps(cases_to_json(cases1), sort_keys=True)
    j2 = json.dumps(cases_to_json(cases2), sort_keys=True)
    assert j1 == j2, "non-deterministic corpus parse"
    assert len(cases1) > 1000, f"unexpectedly few test cases: {len(cases1)}"


def test_committed_cache_matches_live_parse():
    """If `corpus/parsed.json` is committed, it must match what the parser
    produces today — otherwise CI is stale."""
    cache = REPO / "corpus" / "parsed.json"
    if not cache.exists():
        pytest.skip("no corpus cache committed yet")
    live = json.dumps(
        cases_to_json(parse_corpus(CORPUS)), sort_keys=True
    )
    committed = json.dumps(json.load(open(cache)), sort_keys=True)
    assert live == committed, (
        "corpus/parsed.json is out of date — run `onvif-tt corpus refresh`."
    )


# ---------------------------------------------------------------------------
# Registry / xfail_on matcher
# ---------------------------------------------------------------------------

def test_match_xfail_literal_match():
    from onvif_tt.registry import Implementation, match_xfail

    impl = Implementation(
        test_id="T", func=lambda: None,
        xfail_on=[{"Manufacturer": "H264", "reason": "ok"}],
    )
    assert match_xfail(impl, {"Manufacturer": "H264"}) == "ok"
    assert match_xfail(impl, {"Manufacturer": "Axis"}) is None
    assert match_xfail(impl, {}) is None


def test_match_xfail_multi_key_and():
    from onvif_tt.registry import Implementation, match_xfail

    impl = Implementation(
        test_id="T", func=lambda: None,
        xfail_on=[{
            "Manufacturer": "H264",
            "Model": "HI3516EV300_85H50AI",
            "reason": "narrow",
        }],
    )
    info = {"Manufacturer": "H264", "Model": "HI3516EV300_85H50AI"}
    assert match_xfail(impl, info) == "narrow"
    # Model wrong → no match.
    assert match_xfail(impl, {**info, "Model": "OTHER"}) is None


def test_match_xfail_callable_predicate():
    from onvif_tt.registry import Implementation, match_xfail

    impl = Implementation(
        test_id="T", func=lambda: None,
        xfail_on=[{
            "Model": lambda v: v and v.startswith("HI3516"),
            "reason": "any-hi3516",
        }],
    )
    assert match_xfail(impl, {"Model": "HI3516EV300_FOO"}) == "any-hi3516"
    assert match_xfail(impl, {"Model": "Bullet42"}) is None


def test_match_xfail_multiple_matchers_or():
    from onvif_tt.registry import Implementation, match_xfail

    impl = Implementation(
        test_id="T", func=lambda: None,
        xfail_on=[
            {"Manufacturer": "H264", "reason": "Xiongmai"},
            {"Manufacturer": "BuggyBrand", "reason": "Other vendor"},
        ],
    )
    assert match_xfail(impl, {"Manufacturer": "H264"}) == "Xiongmai"
    assert match_xfail(impl, {"Manufacturer": "BuggyBrand"}) == "Other vendor"
    assert match_xfail(impl, {"Manufacturer": "Axis"}) is None
