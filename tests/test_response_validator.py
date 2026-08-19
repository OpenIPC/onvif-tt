"""Unit tests for structural response validation (issue #3).

No DUT required — every case is a synthetic envelope. Two of them are the
exact defects from the issue, so if the validator ever stops catching those,
these fail.
"""

from __future__ import annotations

import pytest
from lxml import etree
from zeep import Client, Settings

from onvif_tt.runtime import schema_store, services
from onvif_tt.runtime.response_validator import (
    enum_index,
    validate_body,
    validate_element,
)

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
TT = "http://www.onvif.org/ver10/schema"
TEV = "http://www.onvif.org/ver10/events/wsdl"
TRT = "http://www.onvif.org/ver10/media/wsdl"


@pytest.fixture(scope="module")
def finder():
    """Resolve a QName against the vendored schemas, as the DUT would."""
    settings = Settings(strict=False, xml_huge_tree=True)
    transport = schema_store.VendoredSchemaTransport()
    clients = [
        Client(wsdl=str(schema_store.local_path(sd.wsdl_url)),
               settings=settings, transport=transport)
        for sd in services.SERVICES
        if sd.short in ("devicemgmt", "media", "events")
    ]

    def find(qname):
        for c in clients:
            try:
                return c.get_element(qname)
            except Exception:  # noqa: BLE001
                continue
        return None

    return find


def _body(payload: str):
    env = (f'<s:Envelope xmlns:s="{SOAP12}" xmlns:tt="{TT}" '
           f'xmlns:tev="{TEV}" xmlns:trt="{TRT}" '
           f'xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" '
           f'xmlns:wstop="http://docs.oasis-open.org/wsn/t-1">'
           f'<s:Body>{payload}</s:Body></s:Envelope>')
    return etree.fromstring(env.encode()).find(f"{{{SOAP12}}}Body")


# ---------------------------------------------------------------------------
# Defect 1 from issue #3 — missing mandatory elements
# ---------------------------------------------------------------------------

_GET_EVENT_PROPERTIES_BROKEN = """
<tev:GetEventPropertiesResponse>
  <tev:TopicNamespaceLocation>http://www.onvif.org/onvif/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>
  <wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>
  <wstop:TopicSet/>
  <wsnt:TopicExpressionDialect>http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete</wsnt:TopicExpressionDialect>
</tev:GetEventPropertiesResponse>
"""


def test_missing_mandatory_elements_are_caught(finder):
    """The defect eight event tests passed over, EVENT-1-1-2 included.

    MessageContentFilterDialect and MessageContentSchemaLocation are both
    minOccurs=1 in the ver10 events WSDL. zeep substitutes an empty list for
    each rather than raising, which is why no assertion noticed.
    """
    found = validate_body(_body(_GET_EVENT_PROPERTIES_BROKEN), finder,
                          "GetEventProperties")
    missing = {v.path for v in found if v.code == "missing-element"}
    assert "GetEventPropertiesResponse/MessageContentFilterDialect" in missing
    assert "GetEventPropertiesResponse/MessageContentSchemaLocation" in missing


def test_conformant_response_produces_no_violations(finder):
    """The false-positive guard — the whole feature dies if this breaks."""
    payload = _GET_EVENT_PROPERTIES_BROKEN.replace(
        "</tev:GetEventPropertiesResponse>",
        "<tev:MessageContentFilterDialect>http://www.onvif.org/ver10/tev/"
        "messageContentFilter/ItemFilter</tev:MessageContentFilterDialect>"
        "<tev:MessageContentSchemaLocation>http://www.onvif.org/onvif/ver10/"
        "schema/onvif.xsd</tev:MessageContentSchemaLocation>"
        "</tev:GetEventPropertiesResponse>")
    assert validate_body(_body(payload), finder, "GetEventProperties") == []


# ---------------------------------------------------------------------------
# Defect 2 from issue #3 — a value outside a closed enumeration
# ---------------------------------------------------------------------------

def _profile(encoding: str) -> str:
    return f"""
<trt:GetProfilesResponse>
  <trt:Profiles token="P0" fixed="true">
    <tt:Name>P0</tt:Name>
    <tt:VideoEncoderConfiguration token="VEC0">
      <tt:Name>vec</tt:Name><tt:UseCount>1</tt:UseCount>
      <tt:Encoding>{encoding}</tt:Encoding>
      <tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
      <tt:Quality>5</tt:Quality>
      <tt:Multicast>
        <tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>
        <tt:Port>0</tt:Port><tt:TTL>0</tt:TTL><tt:AutoStart>false</tt:AutoStart>
      </tt:Multicast>
      <tt:SessionTimeout>PT60S</tt:SessionTimeout>
    </tt:VideoEncoderConfiguration>
  </trt:Profiles>
</trt:GetProfilesResponse>"""


def test_h265_in_ver10_encoding_is_caught(finder):
    """tt:VideoEncoding is a closed enumeration: JPEG, MPEG4, H264.

    H265 exists only as a tt:VideoEncodingMimeNames value — a Media2 concept
    — so this response is well-formed XML that no schema accepts. zeep hands
    the invalid enum through as a plain string, which is how the run stayed
    green until someone added an explicit assertion (issue #2).
    """
    found = validate_body(_body(_profile("H265")), finder, "GetProfiles")
    bad = [v for v in found if v.code == "bad-enum"]
    assert len(bad) == 1, found
    assert bad[0].path.endswith("/Encoding")
    assert "H265" in bad[0].detail


def test_valid_encoding_is_not_flagged(finder):
    found = validate_body(_body(_profile("H264")), finder, "GetProfiles")
    assert [v for v in found if v.code == "bad-enum"] == []


def test_enum_index_knows_the_ver10_encoding_enumeration():
    idx = enum_index()
    assert idx[f"{{{TT}}}VideoEncoding"] == ["JPEG", "MPEG4", "H264"]
    # H265 is legal only in the Media2 mime-name enumeration.
    assert "H265" in idx[f"{{{TT}}}VideoEncodingMimeNames"]


# ---------------------------------------------------------------------------
# Required attributes
# ---------------------------------------------------------------------------

def test_missing_required_attribute_is_caught(finder):
    """tt:Config declares Name and Type use="required".

    Generalises the hand-written assertions in ANALYTICS-1-1-1, which check
    exactly this on RuleDescription because nothing else would.
    """
    payload = """
<trt:GetProfilesResponse>
  <trt:Profiles token="P0" fixed="true">
    <tt:Name>P0</tt:Name>
    <tt:VideoAnalyticsConfiguration token="VA0">
      <tt:Name>va</tt:Name><tt:UseCount>1</tt:UseCount>
      <tt:AnalyticsEngineConfiguration/>
      <tt:RuleEngineConfiguration>
        <tt:Rule><tt:Parameters/></tt:Rule>
      </tt:RuleEngineConfiguration>
    </tt:VideoAnalyticsConfiguration>
  </trt:Profiles>
</trt:GetProfilesResponse>"""
    found = validate_body(_body(payload), finder, "GetProfiles")
    attrs = {v.path for v in found if v.code == "missing-attribute"}
    assert any(p.endswith("/Rule/@Name") for p in attrs), found
    assert any(p.endswith("/Rule/@Type") for p in attrs), found


# ---------------------------------------------------------------------------
# False-positive guards
# ---------------------------------------------------------------------------

def test_choice_members_are_not_reported_missing(finder):
    """An xs:choice must not read as "all branches are mandatory".

    zeep models choice members with min_occurs=0, so taking one branch is
    fine — but this is the shape most likely to produce a false-positive
    flood if that ever changes, and a flood would make the whole check
    untrustworthy.
    """
    settings = Settings(strict=False, xml_huge_tree=True)
    client = Client(wsdl=str(schema_store.local_path(
        services.get("media").wsdl_url)), settings=settings,
        transport=schema_store.VendoredSchemaTransport())
    color_options = client.get_type(f"{{{TT}}}ColorOptions")
    node = etree.fromstring(
        f'<ColorOptions xmlns="{TT}"><ColorList/></ColorOptions>'.encode())
    out = []
    validate_element(node, color_options, "op", "ColorOptions", out)
    assert [v for v in out if v.code == "missing-element"] == []


def test_foreign_namespace_cannot_impersonate_a_required_child(finder):
    """Matching must be on the full QName, not the local name.

    ONVIF responses mix namespaces by design, and extension points mean
    vendor elements really do turn up. If presence were checked on local
    names, a `<vendor:Name>` would satisfy the requirement for `<tt:Name>`
    — suppressing the violation *and* then being validated against the
    wrong declaration.
    """
    payload = ('<trt:GetProfilesResponse><trt:Profiles token="P0" '
               'fixed="true"><vendor:Name xmlns:vendor="urn:vendor">x'
               '</vendor:Name></trt:Profiles></trt:GetProfilesResponse>')
    found = validate_body(_body(payload), finder, "GetProfiles")
    missing = {v.path for v in found if v.code == "missing-element"}
    assert "GetProfilesResponse/Profiles/Name" in missing, found


def test_repeated_minimum_is_enforced(finder):
    """A positive minOccurs is a count, not a boolean.

    tt:Polyline declares Point with minOccurs="2" (and tt:Merge/from,
    tt:Split/to likewise), so a single occurrence is non-conformant even
    though the element is present.
    """
    settings = Settings(strict=False, xml_huge_tree=True)
    client = Client(wsdl=str(schema_store.local_path(
        services.get("media").wsdl_url)), settings=settings,
        transport=schema_store.VendoredSchemaTransport())
    polyline = client.get_type(f"{{{TT}}}Polyline")

    def points(n):
        node = etree.fromstring(
            (f'<Polyline xmlns="{TT}">'
             + '<Point x="1" y="2"/>' * n
             + '</Polyline>').encode())
        out = []
        validate_element(node, polyline, "op", "Polyline", out)
        return [v for v in out if v.code == "missing-element"]

    assert points(2) == [], "two points satisfy minOccurs=2"
    one = points(1)
    assert len(one) == 1, one
    assert "only 1 occurrence" in one[0].detail
    assert len(points(0)) == 1


def test_unknown_element_is_skipped_not_reported(finder):
    """ONVIF extension points mean undeclared children are normal."""
    payload = ('<trt:GetProfilesResponse><trt:Profiles token="P0" '
               'fixed="true"><tt:Name>P0</tt:Name>'
               '<tt:SomethingVendorSpecific>x</tt:SomethingVendorSpecific>'
               '</trt:Profiles></trt:GetProfilesResponse>')
    found = validate_body(_body(payload), finder, "GetProfiles")
    assert all("SomethingVendorSpecific" not in v.path for v in found)


def test_soap_fault_body_is_not_validated(finder):
    """Faults are their own signal; tests assert on them directly."""
    payload = (f'<s:Fault xmlns:s="{SOAP12}"><s:Code><s:Value>s:Sender'
               f'</s:Value></s:Code></s:Fault>')
    assert validate_body(_body(payload), finder, "GetProfiles") == []


def test_unresolvable_element_is_skipped():
    """No schema for it → no opinion about it, rather than a false alarm."""
    payload = '<Whatever xmlns="urn:vendor:private"><X/></Whatever>'
    assert validate_body(_body(payload), lambda q: None, "op") == []
