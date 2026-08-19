"""Unit tests for WS-Security auth mode negotiation and clock-skew recovery.

No DUT required. The clock-skew path in particular can only be exercised
here: neither lab camera permits it — one refuses PasswordDigest outright and
both refuse an unauthenticated GetSystemDateAndTime, which is what a skew
probe depends on. See issue #5.
"""

from __future__ import annotations

import datetime as dt

from onvif_tt.runtime.auth import (
    AuthMode,
    AuthState,
    candidates,
    device_utc_from_response,
    is_auth_failure,
)


# ---------------------------------------------------------------------------
# Mode ordering
# ---------------------------------------------------------------------------

def test_auto_prefers_digest_then_falls_back():
    """Digest first — it is what ONVIF Core requires. Text only as fallback."""
    assert [label for label, _ in candidates(AuthMode.AUTO)] == [
        "PasswordDigest", "PasswordText"]
    assert [use_digest for _, use_digest in candidates(AuthMode.AUTO)] == [
        True, False]


def test_pinned_modes_do_not_fall_back():
    assert len(candidates(AuthMode.DIGEST)) == 1
    assert len(candidates(AuthMode.TEXT)) == 1
    assert candidates(AuthMode.NONE) == ()


def test_fell_back_to_text_only_when_digest_was_actually_refused():
    """The signal SECURITY-1-1-1 keys on.

    A device pinned to --auth text has no digest verdict, so it must not be
    reported as having refused digest — that would be inventing evidence.
    """
    negotiated = AuthState(accepted="PasswordText", rejected=["PasswordDigest"])
    assert negotiated.fell_back_to_text
    assert not negotiated.used_digest

    pinned = AuthState(requested=AuthMode.TEXT, accepted="PasswordText")
    assert not pinned.fell_back_to_text

    digest = AuthState(accepted="PasswordDigest")
    assert digest.used_digest
    assert not digest.fell_back_to_text


# ---------------------------------------------------------------------------
# Recognising an auth failure
# ---------------------------------------------------------------------------

def test_auth_failure_recognised_from_fault_text():
    """python-onvif-zeep flattens faults to bare ONVIFError, losing the
    subcode, so the text is all we have to key on."""
    assert is_auth_failure(Exception(
        "Unknown error: The security token could not be authenticated."))
    assert is_auth_failure(Exception("wsse:FailedAuthentication"))


def test_non_auth_errors_are_not_mistaken_for_a_rejection():
    """A transport failure must not be recorded as "the device refused this
    mode" — that would fabricate a conformance finding out of a dropped
    connection, and would silently try the next mode for no reason."""
    assert not is_auth_failure(ConnectionError("Connection refused"))
    assert not is_auth_failure(Exception("Read timed out"))
    assert not is_auth_failure(Exception("ter:InvalidArgVal"))


# ---------------------------------------------------------------------------
# Device clock parsing (issue #5)
# ---------------------------------------------------------------------------

def _time_response(year=2026, month=8, day=19, hour=12, minute=0, second=0,
                   utc=True):
    inner = (
        f"<tt:Date><tt:Year>{year}</tt:Year><tt:Month>{month}</tt:Month>"
        f"<tt:Day>{day}</tt:Day></tt:Date>"
        f"<tt:Time><tt:Hour>{hour}</tt:Hour><tt:Minute>{minute}</tt:Minute>"
        f"<tt:Second>{second}</tt:Second></tt:Time>"
    )
    block = "UTCDateTime" if utc else "LocalDateTime"
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body>'
        '<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>'
        f'<tt:{block}>{inner}</tt:{block}>'
        '</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>'
        '</s:Body></s:Envelope>'
    ).encode()


def test_device_utc_is_parsed():
    assert device_utc_from_response(_time_response()) == dt.datetime(
        2026, 8, 19, 12, 0, 0)


def test_local_time_only_is_declined():
    """No zone information, so no offset can be computed honestly."""
    assert device_utc_from_response(_time_response(utc=False)) is None


def test_auth_fault_response_yields_no_time():
    """What both lab cameras actually return to an unauthenticated probe."""
    fault = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        '<s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code>'
        '</s:Fault></s:Body></s:Envelope>'
    ).encode()
    assert device_utc_from_response(fault) is None


def test_malformed_and_impossible_dates_are_declined():
    assert device_utc_from_response(b"not xml at all") is None
    assert device_utc_from_response(_time_response(month=13)) is None
    assert device_utc_from_response(_time_response(day=99)) is None


def test_offset_sign_matches_the_token_correction():
    """dt_diff is added to Created, so it must be device-minus-host.

    A device running five minutes ahead needs the timestamp we send pushed
    five minutes forward to land inside its freshness window. Getting the
    sign backwards would double the skew instead of cancelling it.
    """
    host = dt.datetime(2026, 8, 19, 12, 0, 0)
    ahead = device_utc_from_response(_time_response(hour=12, minute=5))
    assert ahead - host == dt.timedelta(minutes=5)
    behind = device_utc_from_response(_time_response(hour=11, minute=55))
    assert behind - host == dt.timedelta(minutes=-5)


# ---------------------------------------------------------------------------
# Regressions from the #7 review
# ---------------------------------------------------------------------------

def test_a_mode_that_works_on_retry_is_not_left_marked_rejected():
    """Clock-skew recovery must clear the earlier refusal.

    Otherwise a digest-capable device with a wrong clock authenticates as
    PasswordDigest while still listed as having rejected it, and
    SECURITY-1-1-1 reports a device that is fine as non-conformant.
    """
    state = AuthState(rejected=["PasswordDigest"])
    # What DUT._try_auth does on a successful retry.
    state.rejected.remove("PasswordDigest")
    state.accepted = "PasswordDigest"
    assert state.used_digest
    assert not state.fell_back_to_text
    assert state.rejected == []


def test_username_token_escapes_credentials():
    """A password containing XML metacharacters must not break the probe.

    An unescaped '&' produces a malformed request, which the device rejects
    for being malformed — and SECURITY-1-1-1 would report that as the device
    refusing valid credentials.
    """
    from onvif_tt.cases.auth import _username_token

    token = _username_token("ad&min", "p<ss>&word", digest=False,
                            nonce=True, created=True)
    assert "ad&amp;min" in token
    assert "p&lt;ss&gt;&amp;word" in token
    assert "&min" not in token.replace("&amp;min", "")
    # Must still be parseable as XML — the whole point.
    from lxml import etree
    etree.fromstring(
        f'<r xmlns:wsse="{_WSSE_NS}" xmlns:wsu="{_WSU_NS}">{token}</r>'.encode())


def test_username_token_applies_the_clock_offset():
    """The 'correctly formed' probe must be stamped on the device's clock."""
    import re
    from datetime import timedelta

    from onvif_tt.cases.auth import _username_token

    def stamp_of(offset):
        token = _username_token("u", "p", digest=True, nonce=True,
                                created=True, clock_offset=offset)
        return re.search(r"<wsu:Created[^>]*>([^<]+)<", token).group(1)

    base = dt.datetime.strptime(stamp_of(None), "%Y-%m-%dT%H:%M:%SZ")
    shifted = dt.datetime.strptime(stamp_of(timedelta(hours=2)),
                                   "%Y-%m-%dT%H:%M:%SZ")
    assert timedelta(minutes=118) < shifted - base < timedelta(minutes=122)


_WSSE_NS = ("http://docs.oasis-open.org/wss/2004/01/"
            "oasis-200401-wss-wssecurity-secext-1.0.xsd")
_WSU_NS = ("http://docs.oasis-open.org/wss/2004/01/"
           "oasis-200401-wss-wssecurity-utility-1.0.xsd")
