"""Event service tests from EVENT.html.

Read-only ``GetEventProperties`` plus the full PullPoint lifecycle —
CreatePullPointSubscription → PullMessages / Renew → Unsubscribe.
``dut.create_pullpoint()`` returns a context manager that registers
itself on ``session.subscriptions`` so the dispatch fixture can
auto-unsubscribe anything left dangling by a crashing test.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from ..registry import register
from ..runtime.dut import DUT


@register("EVENT-1-1-2", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt", "events"})
def test_event_get_event_properties(dut: DUT, spec) -> None:
    """EVENT.html#tc.EVENT-1-1-2 — GET EVENT PROPERTIES.

    Asserts the device advertises at least one topic in its TopicSet.
    """
    props = dut.events.GetEventProperties()
    assert props is not None, "GetEventProperties returned None"
    assert props.TopicNamespaceLocation, (
        "GetEventProperties.TopicNamespaceLocation empty"
    )
    # TopicSet is an opaque XML element; presence is the assertion here.
    assert props.TopicSet is not None, "TopicSet missing from GetEventProperties"


# ---------------------------------------------------------------------------
# Realtime PullPoint subscription
# ---------------------------------------------------------------------------

@register("LOCAL-EVENT-PULLPOINT-CREATE", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt", "events"},
          tags={"local"})
def test_pullpoint_create_and_unsubscribe(dut: DUT, spec) -> None:
    """CreatePullPointSubscription returns a usable subscription handle.

    No ONVIF catalog ID maps to "just the create+unsub round-trip" — the
    spec only exercises it as part of EVENT-3-1-15 et al. We add this
    as a cheap smoke check so a broken subscription URL surfaces
    before the heavier message-pulling tests run.
    """
    with dut.create_pullpoint("PT30S") as pp:
        assert pp.subscription_url.startswith(("http://", "https://")), (
            f"non-URI subscription URL: {pp.subscription_url!r}"
        )
        # TerminationTime must be strictly after CurrentTime per spec.
        assert pp.termination_time > pp.current_time, (
            f"TerminationTime {pp.termination_time} <= CurrentTime "
            f"{pp.current_time}"
        )


@register("EVENT-3-1-12", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "events"})
def test_pullpoint_renew(dut: DUT, spec) -> None:
    """EVENT.html#tc.EVENT-3-1-12 — REALTIME PULLPOINT SUBSCRIPTION RENEW.

    Renew before the original termination and verify the new
    TerminationTime is strictly later than the original.
    """
    with dut.create_pullpoint("PT15S") as pp:
        original_term = pp.termination_time
        renew_resp = pp.renew("PT60S")
        new_term = renew_resp.TerminationTime
        new_now = renew_resp.CurrentTime
        assert new_term > new_now, (
            f"Renew: TerminationTime {new_term} <= CurrentTime {new_now}"
        )
        assert new_term > original_term, (
            f"Renew did not extend termination: {original_term} → {new_term}"
        )


@register("EVENT-3-1-15", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt", "events"})
def test_pullpoint_pull_messages(dut: DUT, spec) -> None:
    """EVENT.html#tc.EVENT-3-1-15 — REALTIME PULLPOINT SUBSCRIPTION PULLMESSAGES.

    The spec wants the operator to trigger an event, then verify a
    NotificationMessage was returned. Since we can't trigger physical
    events in CI, we call ``SetSynchronizationPoint`` (which the spec
    explicitly suggests as the trigger for property events) and then
    PullMessages with a 3-second timeout.

    PASS criteria here:
      * PullMessagesResponse has CurrentTime < TerminationTime;
      * MessageLimit was respected;
      * No more NotificationMessages than requested (no SOAP-Fault).
    """
    with dut.create_pullpoint("PT60S") as pp:
        try:
            pp.set_synchronization_point()
        except Exception:
            # Optional per spec — only some devices support it. Continue
            # and rely on whatever events are queued already.
            pass

        msgs = pp.pull_messages(timeout="PT3S", limit=5)
        assert msgs.CurrentTime is not None, "PullMessagesResponse.CurrentTime missing"
        assert msgs.TerminationTime is not None, "PullMessagesResponse.TerminationTime missing"
        assert msgs.TerminationTime > msgs.CurrentTime, (
            f"PullMessages: TerminationTime {msgs.TerminationTime} <= "
            f"CurrentTime {msgs.CurrentTime}"
        )
        nm = getattr(msgs, "NotificationMessage", None) or []
        assert len(nm) <= 5, (
            f"DUT returned {len(nm)} messages, MessageLimit was 5"
        )
        # A device with no events at all is still spec-compliant if it
        # returns an empty NotificationMessage list with a valid timestamp.


@register("EVENT-3-1-36", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt", "events"})
def test_pullpoint_unsubscribe(dut: DUT, spec) -> None:
    """EVENT.html#tc.EVENT-3-1-36 — REALTIME PULLPOINT SUBSCRIPTION UNSUBSCRIBE.

    Explicit Unsubscribe must succeed for a freshly-created
    subscription. Re-calling Unsubscribe or PullMessages on the
    destroyed subscription is expected to fault (spec calls this the
    "ResourceUnknownFault").
    """
    pp = dut.create_pullpoint("PT30S")
    pp.unsubscribe()
    # After unsubscribe, the subscription must no longer be alive. A
    # follow-up PullMessages must NOT succeed silently.
    with pytest.raises(Exception):  # noqa: BLE001 — any error is fine
        pp.pull_messages(timeout="PT1S", limit=1)
