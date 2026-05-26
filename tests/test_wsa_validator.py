"""Unit tests for runtime.wsa_validator — verifies the plugin records
the expected violation codes against synthetic envelopes. No DUT
required; runs on every CI matrix entry.
"""

from __future__ import annotations

from lxml import etree

from onvif_tt.runtime.wsa_validator import WSAValidator, WSAViolation


SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"
WSA = "http://www.w3.org/2005/08/addressing"


class _FakeOp:
    def __init__(self, name: str) -> None:
        self.name = name


def _build_envelope(soap_ns: str, *, action: str | None) -> etree._Element:
    nsmap = {"s": soap_ns, "wsa": WSA}
    env = etree.SubElement(
        etree.Element(f"{{{soap_ns}}}root", nsmap=nsmap),
        f"{{{soap_ns}}}Envelope", nsmap=nsmap,
    )
    header = etree.SubElement(env, f"{{{soap_ns}}}Header")
    if action is not None:
        a = etree.SubElement(header, f"{{{WSA}}}Action")
        a.text = action
    etree.SubElement(env, f"{{{soap_ns}}}Body")
    return env


def test_wsa_clean_envelope_passes():
    sink: list[WSAViolation] = []
    v = WSAValidator()
    v.attach(sink)
    env = _build_envelope(SOAP12, action="http://www.onvif.org/ver10/device/wsdl/GetDeviceInformationResponse")
    v.ingress(env, {}, _FakeOp("GetDeviceInformation"))
    assert sink == []


def test_wsa_missing_action_recorded():
    sink: list[WSAViolation] = []
    v = WSAValidator()
    v.attach(sink)
    env = _build_envelope(SOAP12, action=None)
    v.ingress(env, {}, _FakeOp("GetCapabilities"))
    codes = {x.code for x in sink}
    assert "missing-action" in codes


def test_wsa_empty_action_recorded():
    sink: list[WSAViolation] = []
    v = WSAValidator()
    v.attach(sink)
    env = _build_envelope(SOAP12, action="   ")
    v.ingress(env, {}, _FakeOp("GetServices"))
    assert any(x.code == "missing-action" for x in sink)


def test_wsa_wrong_action_suffix_recorded():
    sink: list[WSAViolation] = []
    v = WSAValidator()
    v.attach(sink)
    # Action that's the *request* URI rather than the response — common
    # vendor bug.
    env = _build_envelope(SOAP12, action="http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation")
    v.ingress(env, {}, _FakeOp("GetDeviceInformation"))
    assert any(x.code == "wrong-action-suffix" for x in sink)


def test_wsa_fault_action_accepted():
    sink: list[WSAViolation] = []
    v = WSAValidator()
    v.attach(sink)
    env = _build_envelope(SOAP12, action="http://www.w3.org/2005/08/addressing/soap/fault")
    v.ingress(env, {}, _FakeOp("GetCapabilities"))
    # Fault Actions are fine — no wrong-suffix violation.
    assert not any(x.code == "wrong-action-suffix" for x in sink)


def test_wsa_soap11_envelope_recorded():
    sink: list[WSAViolation] = []
    v = WSAValidator()
    v.attach(sink)
    env = _build_envelope(SOAP11, action="http://www.onvif.org/ver10/device/wsdl/GetCapabilitiesResponse")
    v.ingress(env, {}, _FakeOp("GetCapabilities"))
    assert any(x.code == "soap11-envelope" for x in sink)


def _build_fault_envelope(*, with_code: bool = True, code_value: str = "env:Sender",
                           with_reason: bool = True, reason_text: str = "Bad input"):
    """Build a SOAP 1.2 envelope containing a Fault. Knobs let tests
    exercise the various structural-violation paths."""
    nsmap = {"s": SOAP12, "wsa": WSA}
    env = etree.Element(f"{{{SOAP12}}}Envelope", nsmap=nsmap)
    header = etree.SubElement(env, f"{{{SOAP12}}}Header")
    a = etree.SubElement(header, f"{{{WSA}}}Action")
    a.text = "http://www.w3.org/2005/08/addressing/soap/fault"
    body = etree.SubElement(env, f"{{{SOAP12}}}Body")
    fault = etree.SubElement(body, f"{{{SOAP12}}}Fault")
    if with_code:
        code = etree.SubElement(fault, f"{{{SOAP12}}}Code")
        if code_value:
            value = etree.SubElement(code, f"{{{SOAP12}}}Value")
            value.text = code_value
    if with_reason:
        reason = etree.SubElement(fault, f"{{{SOAP12}}}Reason")
        if reason_text:
            text = etree.SubElement(reason, f"{{{SOAP12}}}Text")
            text.text = reason_text
    return env


def test_wsa_well_formed_fault_passes():
    sink: list[WSAViolation] = []
    v = WSAValidator()
    v.attach(sink)
    env = _build_fault_envelope()
    v.ingress(env, {}, _FakeOp("GetCapabilities"))
    fault_codes = {x.code for x in sink if x.code.startswith("fault-")}
    assert fault_codes == set(), f"unexpected fault violations: {fault_codes}"


def test_wsa_fault_missing_code_recorded():
    sink: list[WSAViolation] = []
    v = WSAValidator()
    v.attach(sink)
    env = _build_fault_envelope(with_code=False)
    v.ingress(env, {}, _FakeOp("GetCapabilities"))
    assert any(x.code == "fault-missing-code" for x in sink)


def test_wsa_fault_code_missing_value_recorded():
    sink: list[WSAViolation] = []
    v = WSAValidator()
    v.attach(sink)
    env = _build_fault_envelope(code_value="")
    v.ingress(env, {}, _FakeOp("GetCapabilities"))
    assert any(x.code == "fault-code-missing-value" for x in sink)


def test_wsa_fault_missing_reason_recorded():
    sink: list[WSAViolation] = []
    v = WSAValidator()
    v.attach(sink)
    env = _build_fault_envelope(with_reason=False)
    v.ingress(env, {}, _FakeOp("GetCapabilities"))
    assert any(x.code == "fault-missing-reason" for x in sink)


def test_wsa_fault_reason_missing_text_recorded():
    sink: list[WSAViolation] = []
    v = WSAValidator()
    v.attach(sink)
    env = _build_fault_envelope(reason_text="")
    v.ingress(env, {}, _FakeOp("GetCapabilities"))
    assert any(x.code == "fault-reason-missing-text" for x in sink)


def test_wsa_unattached_plugin_is_silent():
    """If attach() was never called, the plugin must NOT crash on
    ingress — it just no-ops. This is the default state right after
    construction."""
    v = WSAValidator()
    # No attach()!
    env = _build_envelope(SOAP12, action=None)
    # Should return without raising; sink is None internally.
    out_env, out_h = v.ingress(env, {}, _FakeOp("GetX"))
    assert out_env is env
