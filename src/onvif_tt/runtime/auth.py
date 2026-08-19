"""WS-Security UsernameToken authentication mode.

ONVIF Core requires message-level security via the WS-UsernameToken profile
with **PasswordDigest**. ``SECURITY-1-1-1``'s failure criteria say so
explicitly — a device fails if it "accepts Username Token without password
type PasswordDigest", i.e. accepting ``PasswordText`` is itself the defect,
because it means the password crosses the wire in cleartext.

onvif-tt used to hardcode ``encrypt=False`` (PasswordText). That had two
consequences, neither good: against a conformant digest-only device it could
not authenticate at all, and against a non-conformant text-accepting device it
*depended* on the very defect ``SECURITY-1-1-1`` exists to catch — so the tool
could never report it.

Hence negotiation. :class:`AuthMode.AUTO` tries PasswordDigest first and falls
back to PasswordText, recording what the device actually accepted and what it
refused. The suite keeps running against real firmware; ``SECURITY-1-1-1``
reads the record and fails the device that needed the fallback. Working tool,
honest verdict — rather than picking one at the other's expense.

Both OpenIPC/majestic builds on the bench reject PasswordDigest outright and
accept only PasswordText. That was verified against a hand-computed
``Base64(SHA1(nonce + created + password))``, with both the ``Z`` and
``+00:00`` spellings of ``wsu:Created``, so it is the device's position rather
than a quirk of how we build the token.
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


class AuthMode(str, enum.Enum):
    """How to build the WS-Security UsernameToken."""

    AUTO = "auto"
    """PasswordDigest, falling back to PasswordText. The default."""

    DIGEST = "digest"
    """PasswordDigest only — what ONVIF Core requires. No fallback."""

    TEXT = "text"
    """PasswordText only. Sends the password in cleartext; for devices whose
    digest implementation is broken and where you accept that trade."""

    NONE = "none"
    """No credentials at all, for devices with authentication disabled."""


@dataclass(slots=True)
class AuthState:
    """What the DUT turned out to accept. Read by ``SECURITY-1-1-1``."""

    requested: AuthMode = AuthMode.AUTO
    accepted: str | None = None
    """``"PasswordDigest"`` / ``"PasswordText"`` / ``"none"``, or ``None``
    while nothing authenticated has been tried yet."""
    rejected: list[str] = field(default_factory=list)
    """Modes the device refused, in the order they were tried."""
    clock_offset: dt.timedelta | None = None
    """Device time minus host time, derived from ``GetSystemDateAndTime`` if
    an authentication failure prompted us to check. ``None`` means never
    measured; zero means measured and in agreement."""
    clock_probe_refused: bool = False
    """The device would not answer an unauthenticated ``GetSystemDateAndTime``.

    Recorded because it makes clock-skew recovery impossible in principle:
    the diagnostic is gated behind the very authentication it exists to
    diagnose. The ONVIF Core Specification puts this operation in the
    pre-authentication access class for exactly that reason — though note
    that document is not part of this repo's corpus, so the tool observes
    the refusal without asserting a verdict on it."""

    @property
    def used_digest(self) -> bool:
        return self.accepted == "PasswordDigest"

    @property
    def fell_back_to_text(self) -> bool:
        """The device refused digest and only accepted cleartext."""
        return self.accepted == "PasswordText" and "PasswordDigest" in self.rejected


#: Order each mode tries its candidates in. ``use_digest`` is what
#: ``UsernameDigestTokenDtDiff`` wants; the label is what we report.
_CANDIDATES: dict[AuthMode, tuple[tuple[str, bool], ...]] = {
    AuthMode.AUTO: (("PasswordDigest", True), ("PasswordText", False)),
    AuthMode.DIGEST: (("PasswordDigest", True),),
    AuthMode.TEXT: (("PasswordText", False),),
    AuthMode.NONE: (),
}


def candidates(mode: AuthMode) -> tuple[tuple[str, bool], ...]:
    return _CANDIDATES[mode]


def is_auth_failure(exc: BaseException) -> bool:
    """Whether ``exc`` looks like the device rejecting our credentials.

    Matched on the fault text because python-onvif-zeep flattens every SOAP
    fault into a bare ``ONVIFError``, losing the subcode we'd rather key on.
    Deliberately narrow: a broader match would let a transport error or a
    genuine authorisation failure trigger a pointless retry, or worse, be
    reported as "the device rejected this mode".
    """
    text = str(exc)
    return (
        "FailedAuthentication" in text
        or "security token could not be authenticated" in text.lower()
    )


#: Envelope asking only the device's time. Sent with **no** Security header:
#: ONVIF Core places GetSystemDateAndTime below the authenticated access
#: levels precisely so a client can ask before it can authenticate, which is
#: what breaks the chicken-and-egg when credentials have just been refused.
SYSTEM_DATE_TIME_ENVELOPE = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    b'<s:Body><GetSystemDateAndTime '
    b'xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body>'
    b'</s:Envelope>'
)

_TT = "{http://www.onvif.org/ver10/schema}"


def device_utc_from_response(xml: bytes) -> dt.datetime | None:
    """Parse ``UTCDateTime`` out of a GetSystemDateAndTimeResponse envelope.

    Parsed from the wire rather than through zeep, because the caller has to
    send this request unauthenticated and python-onvif-zeep always attaches a
    UsernameToken — an empty one is still a token, and a device will refuse it.

    Only ``UTCDateTime`` is used: a device that fills in ``LocalDateTime``
    alone gives us no zone to reason about, so we decline rather than guess.
    """
    from lxml import etree

    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError:
        return None
    utc = root.find(f".//{_TT}UTCDateTime")
    if utc is None:
        return None
    date, time = utc.find(f"{_TT}Date"), utc.find(f"{_TT}Time")
    if date is None or time is None:
        return None

    def part(parent, tag: str) -> int | None:
        el = parent.find(f"{_TT}{tag}")
        try:
            return int(el.text)
        except (AttributeError, TypeError, ValueError):
            return None

    fields = [part(date, "Year"), part(date, "Month"), part(date, "Day"),
              part(time, "Hour"), part(time, "Minute"), part(time, "Second")]
    if any(f is None for f in fields):
        return None
    try:
        return dt.datetime(*fields)
    except ValueError as exc:
        log.warning("GetSystemDateAndTime returned an impossible UTCDateTime: "
                    "%s", exc)
        return None
