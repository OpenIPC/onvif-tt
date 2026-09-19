"""Focused unit tests for Events TopicSet extraction."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from lxml import etree

from onvif_tt.cases.event import (
    _topic_roots,
    test_event_get_event_properties as check_event_get_event_properties,
)


def _topic_set(*children: bytes) -> etree._Element:
    root = etree.fromstring(
        b'<wstop:TopicSet '
        b'xmlns:wstop="http://docs.oasis-open.org/wsn/t-1" '
        b'xmlns:tns="http://www.onvif.org/ver10/topics"/>'
    )
    for child in children:
        root.append(etree.fromstring(child))
    return root


def test_topic_roots_from_direct_topic_set_wrapper():
    topic_set = _topic_set(
        b'<tns:RuleEngine '
        b'xmlns:tns="http://www.onvif.org/ver10/topics"/>'
    )

    roots = _topic_roots(topic_set)

    assert [etree.QName(root).localname for root in roots] == ["RuleEngine"]


def test_topic_roots_accepts_an_already_unwrapped_topic():
    topic = etree.fromstring(
        b'<tns:RuleEngine '
        b'xmlns:tns="http://www.onvif.org/ver10/topics"/>'
    )

    assert _topic_roots(topic) == [topic]


def test_topic_roots_from_zeep_wildcard_value():
    topic_set = _topic_set(
        b'<tns:RuleEngine '
        b'xmlns:tns="http://www.onvif.org/ver10/topics"/>'
    )
    zeep_value = SimpleNamespace(_value_1=list(topic_set))

    roots = _topic_roots(zeep_value)

    assert [etree.QName(root).localname for root in roots] == ["RuleEngine"]


def test_topic_roots_ignores_non_element_wildcard_metadata():
    topic = etree.fromstring(
        b'<tns:RuleEngine '
        b'xmlns:tns="http://www.onvif.org/ver10/topics"/>'
    )
    zeep_value = SimpleNamespace(_value_1=["metadata", 1, None, topic])

    assert _topic_roots(zeep_value) == [topic]


def test_topic_roots_returns_empty_for_no_usable_topics():
    topic_set = _topic_set()
    topic_set.append(etree.Comment("not a topic"))

    assert _topic_roots(topic_set) == []
    assert _topic_roots(SimpleNamespace(_value_1=[])) == []
    assert _topic_roots(None) == []


def test_topic_roots_ignores_ws_topics_documentation():
    topic_set = _topic_set(
        b'<wstop:documentation '
        b'xmlns:wstop="http://docs.oasis-open.org/wsn/t-1">'
        b'descriptive metadata</wstop:documentation>'
    )

    assert _topic_roots(topic_set) == []


def test_topic_roots_terminates_on_cyclic_supported_values():
    items = []
    items.append(items)

    first = SimpleNamespace()
    second = SimpleNamespace()
    first._value_1 = second
    second._value_1 = first

    assert _topic_roots(items) == []
    assert _topic_roots(first) == []


def test_event_properties_rejects_a_topic_set_without_usable_topics():
    props = SimpleNamespace(
        TopicNamespaceLocation=["https://example.invalid/topics"],
        TopicSet=_topic_set(),
    )
    dut = SimpleNamespace(
        events=SimpleNamespace(GetEventProperties=lambda: props)
    )

    with pytest.raises(AssertionError, match="contains no usable topic nodes"):
        check_event_get_event_properties(dut, spec=None)
