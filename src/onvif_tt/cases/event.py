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


# ---------------------------------------------------------------------------
# Basic Notification (NotificationProducer)
#
# The Subscribe operation needs a ConsumerReference — the URL the device
# POSTs Notify messages to. The lifecycle tests here only exercise
# Subscribe / Renew / Unsubscribe, so a throwaway URL is fine; delivery is
# tested further down against a real consumer (runtime/notify_consumer.py).
# ---------------------------------------------------------------------------

@register("EVENT-2-1-9", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "events"})
def test_basic_subscribe(dut: DUT, spec) -> None:
    """EVENT.html#tc.EVENT-2-1-9 — BASIC NOTIFICATION SUBSCRIBE.

    The device responds with a SubscribeResponse carrying a valid
    SubscriptionReference and TerminationTime > CurrentTime.
    """
    with dut.create_notify_subscription(initial_termination="PT15S") as sub:
        assert sub.subscription_url.startswith(("http://", "https://")), (
            f"subscription URL not a URI: {sub.subscription_url!r}"
        )
        assert sub.termination_time > sub.current_time, (
            f"Subscribe: TerminationTime {sub.termination_time} <= "
            f"CurrentTime {sub.current_time}"
        )


@register("EVENT-2-1-12", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "events"})
def test_basic_renew(dut: DUT, spec) -> None:
    """EVENT.html#tc.EVENT-2-1-12 — BASIC NOTIFICATION RENEW.

    Renew extends the termination strictly later than the original.
    """
    with dut.create_notify_subscription(initial_termination="PT15S") as sub:
        original_term = sub.termination_time
        resp = sub.renew("PT60S")
        new_term = resp.TerminationTime
        new_now = resp.CurrentTime
        assert new_term > new_now, (
            f"Renew: TerminationTime {new_term} <= CurrentTime {new_now}"
        )
        assert new_term > original_term, (
            f"Renew did not extend termination: {original_term} → {new_term}"
        )


@register("EVENT-2-1-28", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt", "events"},
          xfail_on=[
              {
                  "Manufacturer": "H264",
                  "reason": "Xiongmai stock firmware (Manufacturer=H264) "
                            "is expected to silently keep the subscription "
                            "alive after Unsubscribe (same pattern as "
                            "EVENT-3-1-36 for PullPoint).",
              },
          ])
def test_basic_unsubscribe(dut: DUT, spec) -> None:
    """EVENT.html#tc.EVENT-2-1-28 — BASIC NOTIFICATION UNSUBSCRIBE.

    Explicit Unsubscribe succeeds; a follow-up Renew on the destroyed
    subscription must fault.
    """
    sub = dut.create_notify_subscription(initial_termination="PT30S")
    sub.unsubscribe()
    with pytest.raises(Exception):  # ResourceUnknownFault or similar
        sub.renew("PT60S")


@register("EVENT-3-1-36", profiles={"S", "T"}, mandatory=True,
          requires_services={"devicemgmt", "events"},
          xfail_on=[
              {
                  "Manufacturer": "H264",
                  "reason": "Xiongmai stock firmware (Manufacturer=H264) "
                            "silently keeps a PullPoint subscription "
                            "active after Unsubscribe — PullMessages "
                            "keeps returning responses where spec demands "
                            "ResourceUnknownFault.",
              },
          ])
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


# ---------------------------------------------------------------------------
# Basic Notification delivery — a real consumer the DUT pushes to
#
# Everything above checks that a subscription can be made. An NVR that
# records on motion depends on the other half: the Notify actually arriving.
# SetSynchronizationPoint makes the test deterministic — a device with
# property events must restate them, so no motion has to be staged.
# ---------------------------------------------------------------------------

_TT = "http://www.onvif.org/ver10/schema"
_WSNT_NS = "http://docs.oasis-open.org/wsn/b-2"


def _property_ops(msgs) -> list[str]:
    return [m.get("PropertyOperation") or ""
            for nm in msgs for m in nm.iter(f"{{{_TT}}}Message")]


def _assert_wellformed_notification(nm) -> None:
    topic = nm.find(f"{{{_WSNT_NS}}}Topic")
    assert topic is not None and (topic.text or "").strip(), (
        "Notify carries a NotificationMessage with no Topic")
    assert topic.get("Dialect"), "Notify Topic has no Dialect"
    msg = nm.find(f"{{{_WSNT_NS}}}Message/{{{_TT}}}Message")
    assert msg is not None, "Notify NotificationMessage has no tt:Message"
    utc = msg.get("UtcTime") or ""
    assert utc.endswith("Z"), f"tt:Message UtcTime {utc!r} is not UTC"


@register("EVENT-2-1-17", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "events"})
def test_basic_notify_delivered(dut: DUT, spec) -> None:
    """EVENT.html#tc.EVENT-2-1-17 — BASIC NOTIFICATION INTERFACE - NOTIFY.

    Subscribe with a consumer we actually run, invoke SetSynchronizationPoint
    and require a well-formed Notify to arrive.
    """
    from ..runtime.notify_consumer import NotifyConsumer

    with NotifyConsumer(dut.config.host) as consumer:
        with dut.create_notify_subscription(
                consumer_reference=consumer.url,
                initial_termination="PT60S") as sub:
            sub.set_synchronization_point()
            msgs = consumer.wait_for(lambda m: len(m) > 0, timeout=10)
        assert msgs, (
            f"no Notify reached {consumer.url} within 10 s of "
            f"SetSynchronizationPoint ({len(consumer.bodies)} POSTs received)"
        )
        for nm in msgs:
            _assert_wellformed_notification(nm)


@register("EVENT-2-1-24", profiles={"S", "T"}, mandatory=False,
          requires_services={"devicemgmt", "events"})
def test_basic_notify_sync_point(dut: DUT, spec) -> None:
    """EVENT.html#tc.EVENT-2-1-24 — BASIC NOTIFICATION SET SYNCHRONIZATION
    POINT.

    A device with property topics sends their Initialized state on
    Subscribe, and sends it again after SetSynchronizationPoint.
    """
    from ..runtime.notify_consumer import NotifyConsumer

    props = dut.events.GetEventProperties()
    raw = dut.last_response or ""
    if 'IsProperty="true"' not in raw:
        pytest.skip("GetEventProperties advertises no property topic")
    del props

    def has_initialized(m):
        return "Initialized" in _property_ops(m)

    with NotifyConsumer(dut.config.host) as consumer:
        with dut.create_notify_subscription(
                consumer_reference=consumer.url,
                initial_termination="PT60S") as sub:
            first = consumer.wait_for(has_initialized, timeout=10)
            assert has_initialized(first), (
                "no Notify with PropertyOperation=Initialized after Subscribe "
                f"({len(consumer.bodies)} POSTs received)"
            )
            mark = len(consumer.bodies)
            sub.set_synchronization_point()
            again = consumer.wait_for(has_initialized, timeout=10, since=mark)
        assert has_initialized(again), (
            "no Initialized property Notify after SetSynchronizationPoint"
        )
