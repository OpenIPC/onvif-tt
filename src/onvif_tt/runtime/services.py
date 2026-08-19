"""The ONVIF service table — one source of truth.

Every service this tool can talk to is described exactly once, here:
its WSDL namespace (which is what ``GetServices`` reports), the URL of
the WSDL that defines it (a key into the vendored schema store), and
the SOAP binding to bind against.

This replaces three previously separate maps that had to be kept in
sync by hand and silently diverged:

* ``dut._SERVICE_FACTORIES`` — short name → ``ONVIFCamera.create_*_service``
* ``features._NS_TO_SHORT``  — namespace → short name
* ``onvif.definition.SERVICES`` — python-onvif-zeep's own table

The divergence was not theoretical. ``receiver`` had tests registered
against it (``cases/receiver.py``) but appeared in neither of the first
two maps, so a device advertising the receiver service got its tests
skipped as "not advertised" and ``dut.receiver`` would have raised a bare
``AttributeError``. ``media2`` was in one map but not the other. One table
makes that class of drift impossible: :func:`by_namespace` and
:func:`get` read the same rows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServiceDef:
    """How to bind one ONVIF service."""

    short: str
    """Short name used in ``requires_services={...}`` and ``dut.<short>``."""

    namespace: str
    """WSDL target namespace. This is the string ``GetServices`` reports
    in ``Service/Namespace``, and the ``{ns}`` half of ``binding_name``."""

    wsdl_url: str
    """Canonical URL of the defining WSDL — the key into the vendored
    schema store (``runtime.schema_store``), not something we fetch."""

    binding: str
    """Local name of the SOAP binding, e.g. ``Media2Binding``."""

    @property
    def binding_name(self) -> str:
        """Fully-qualified binding as zeep's ``create_service`` wants it."""
        return f"{{{self.namespace}}}{self.binding}"


_ONVIF = "http://www.onvif.org"

SERVICES: tuple[ServiceDef, ...] = (
    ServiceDef("devicemgmt", f"{_ONVIF}/ver10/device/wsdl",
               "https://www.onvif.org/ver10/device/wsdl/devicemgmt.wsdl",
               "DeviceBinding"),
    ServiceDef("media", f"{_ONVIF}/ver10/media/wsdl",
               "https://www.onvif.org/ver10/media/wsdl/media.wsdl",
               "MediaBinding"),
    ServiceDef("media2", f"{_ONVIF}/ver20/media/wsdl",
               "https://www.onvif.org/ver20/media/wsdl/media.wsdl",
               "Media2Binding"),
    ServiceDef("events", f"{_ONVIF}/ver10/events/wsdl",
               "https://www.onvif.org/ver10/events/wsdl/event.wsdl",
               "EventBinding"),
    ServiceDef("imaging", f"{_ONVIF}/ver20/imaging/wsdl",
               "https://www.onvif.org/ver20/imaging/wsdl/imaging.wsdl",
               "ImagingBinding"),
    ServiceDef("ptz", f"{_ONVIF}/ver20/ptz/wsdl",
               "https://www.onvif.org/ver20/ptz/wsdl/ptz.wsdl",
               "PTZBinding"),
    ServiceDef("analytics", f"{_ONVIF}/ver20/analytics/wsdl",
               "https://www.onvif.org/ver20/analytics/wsdl/analytics.wsdl",
               "AnalyticsEngineBinding"),
    ServiceDef("recording", f"{_ONVIF}/ver10/recording/wsdl",
               "https://www.onvif.org/ver10/recording.wsdl",
               "RecordingBinding"),
    ServiceDef("search", f"{_ONVIF}/ver10/search/wsdl",
               "https://www.onvif.org/ver10/search.wsdl",
               "SearchBinding"),
    ServiceDef("replay", f"{_ONVIF}/ver10/replay/wsdl",
               "https://www.onvif.org/ver10/replay.wsdl",
               "ReplayBinding"),
    ServiceDef("deviceio", f"{_ONVIF}/ver10/deviceIO/wsdl",
               "https://www.onvif.org/ver10/deviceio.wsdl",
               "DeviceIOBinding"),
    ServiceDef("receiver", f"{_ONVIF}/ver10/receiver/wsdl",
               "https://www.onvif.org/ver10/receiver.wsdl",
               "ReceiverBinding"),
    # Profile A (Access Control) + Profile D (Door Control).
    ServiceDef("accesscontrol", f"{_ONVIF}/ver10/accesscontrol/wsdl",
               "https://www.onvif.org/ver10/pacs/accesscontrol.wsdl",
               "PACSBinding"),
    ServiceDef("doorcontrol", f"{_ONVIF}/ver10/doorcontrol/wsdl",
               "https://www.onvif.org/ver10/pacs/doorcontrol.wsdl",
               "DoorControlBinding"),
)

# The event WSDL carries several bindings rooted at a *subscription* URL
# rather than the service XAddr, so they aren't services in their own
# right — PullPointHandle / NotifyHandle bind them against the URL the
# device hands back from CreatePullPointSubscription / Subscribe.
EVENT_BINDINGS: dict[str, str] = {
    "pullpoint": "PullPointSubscriptionBinding",
    "subscription_manager": "SubscriptionManagerBinding",
    "notification_producer": "NotificationProducerBinding",
}

_BY_SHORT: dict[str, ServiceDef] = {s.short: s for s in SERVICES}
_BY_NS: dict[str, ServiceDef] = {s.namespace: s for s in SERVICES}


def get(short: str) -> ServiceDef | None:
    """Look a service up by short name, or ``None`` if we don't know it."""
    return _BY_SHORT.get(short)


def by_namespace(namespace: str) -> ServiceDef | None:
    """Look a service up by the namespace ``GetServices`` reported."""
    return _BY_NS.get(namespace)


def event_binding_name(key: str) -> str:
    """Fully-qualified name of one of the subscription-rooted bindings."""
    events = _BY_SHORT["events"]
    return f"{{{events.namespace}}}{EVENT_BINDINGS[key]}"
