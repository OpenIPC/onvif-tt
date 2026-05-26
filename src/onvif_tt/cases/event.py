"""Event service smoke tests from EVENT.html.

For now only the read-only ``GetEventProperties`` is implemented. The
PullPoint lifecycle (CreatePullPointSubscription / PullMessages /
Unsubscribe) requires a per-test subscription tracker — Phase 2.
"""

from __future__ import annotations

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
