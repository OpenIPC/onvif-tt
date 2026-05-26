"""Device-Under-Test wrapper.

Thin layer over ``python-onvif-zeep``'s ``ONVIFCamera`` that:

* lazy-binds service proxies on first access (``dut.devicemgmt``,
  ``dut.media``, ``dut.media2``, ``dut.events``, ``dut.ptz``,
  ``dut.imaging``);
* attaches a SOAP-trace zeep plugin so the runner can capture the last
  request/response per service for failure reporting;
* caches the result of ``GetServices`` so feature-gating queries cost
  nothing after the first call.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from typing import Any

from .soap_trace import SoapTrace

log = logging.getLogger(__name__)

# ONVIF service short-name → method on ONVIFCamera for create_*_service()
_SERVICE_FACTORIES: dict[str, str] = {
    "devicemgmt": "create_devicemgmt_service",
    "media": "create_media_service",
    "media2": "create_media2_service",
    "events": "create_events_service",
    "ptz": "create_ptz_service",
    "imaging": "create_imaging_service",
    "analytics": "create_analytics_service",
    "recording": "create_recording_service",
    "search": "create_search_service",
    "replay": "create_replay_service",
    "deviceio": "create_deviceio_service",
}


@dataclass(slots=True)
class DUTConfig:
    host: str
    port: int = 80
    user: str = ""
    password: str = ""
    wsdl_dir: str | None = None  # let python-onvif pick its bundled dir
    timeout: float = 10.0


@dataclass(slots=True)
class DUTSession:
    """Per-run cache that lives alongside a DUT.

    Empty by default; the feature-discovery module fills ``services`` from
    ``GetServices`` once and the runner reuses it across tests.

    ``subscriptions`` is the list of live PullPointHandle objects opened
    during this session — fixture teardown walks it and unsubscribes
    anything still alive, so a test crash never leaves dangling
    subscriptions on the device.
    """

    services: dict[str, str] = field(default_factory=dict)  # name → xaddr
    capabilities: Any | None = None
    device_info: dict[str, Any] = field(default_factory=dict)
    soap_traces: collections.deque[tuple[str, str]] = field(
        default_factory=lambda: collections.deque(maxlen=64)
    )
    subscriptions: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# PullPoint subscription handle
# ---------------------------------------------------------------------------

class PullPointHandle:
    """Wraps a CreatePullPointSubscription response with the two ONVIF
    bindings rooted at the subscription URL: PullPointSubscription
    (for ``PullMessages`` / ``SetSynchronizationPoint``) and
    SubscriptionManager (for ``Renew`` / ``Unsubscribe``).

    Usage::

        with dut.create_pullpoint(initial_termination="PT60S") as pp:
            resp = pp.pull_messages(timeout="PT3S", limit=5)
            pp.set_synchronization_point()
            # auto-unsubscribe on __exit__

    The handle registers itself on ``dut.session.subscriptions`` so a
    session-scope teardown can clean up after a crashing test.
    """

    _EVENT_NS = "{http://www.onvif.org/ver10/events/wsdl}"

    def __init__(self, dut: "DUT", create_resp: Any) -> None:
        from onvif import ONVIFService  # python-onvif-zeep

        self._dut = dut
        self.subscription_url = create_resp.SubscriptionReference.Address._value_1
        self.current_time = create_resp.CurrentTime
        self.termination_time = create_resp.TerminationTime
        self._alive = True

        cam = dut._camera
        _xaddr, wsdl_file, _binding = cam.get_definition("events")
        common = dict(
            user=cam.user, passwd=cam.passwd, url=wsdl_file,
            encrypt=cam.encrypt, daemon=cam.daemon, no_cache=cam.no_cache,
            dt_diff=cam.dt_diff, transport=cam.transport,
        )
        # Two bindings, same subscription URL.
        self._pull = ONVIFService(
            xaddr=self.subscription_url,
            **common,
            portType="PullPointSubscription",
            binding_name=f"{self._EVENT_NS}PullPointSubscriptionBinding",
        )
        self._mgr = ONVIFService(
            xaddr=self.subscription_url,
            **common,
            portType="SubscriptionManager",
            binding_name=f"{self._EVENT_NS}SubscriptionManagerBinding",
        )
        # Attach SOAP trace plugin to both.
        for svc in (self._pull, self._mgr):
            try:
                svc.zeep_client.plugins.append(dut._trace)
            except AttributeError:
                pass

        dut.session.subscriptions.append(self)

    # ------------------------------------------------------------------ ops

    def pull_messages(self, timeout: str = "PT3S", limit: int = 5) -> Any:
        return self._pull.PullMessages({"Timeout": timeout, "MessageLimit": limit})

    def set_synchronization_point(self) -> Any:
        return self._pull.SetSynchronizationPoint()

    def renew(self, termination_time: str = "PT60S") -> Any:
        return self._mgr.Renew({"TerminationTime": termination_time})

    def unsubscribe(self) -> Any | None:
        if not self._alive:
            return None
        try:
            return self._mgr.Unsubscribe()
        except Exception:  # device gone, expired, etc.
            return None
        finally:
            self._alive = False
            try:
                self._dut.session.subscriptions.remove(self)
            except ValueError:
                pass

    # ------------------------------------------------------------------ ctx

    def __enter__(self) -> "PullPointHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.unsubscribe()


class NotifyHandle:
    """Wraps a basic-notification Subscribe response.

    Unlike :class:`PullPointHandle` this doesn't expose Pull/Sync — those
    aren't part of the Notification Producer pattern. It only owns the
    SubscriptionManager binding (Renew, Unsubscribe).

    Use a throwaway ``ConsumerReference`` URL when calling
    :meth:`DUT.create_notify_subscription` — for round-trip tests we
    don't actually receive the Notify messages, only assert the
    Subscribe/Unsubscribe lifecycle.
    """

    _EVENT_NS = "{http://www.onvif.org/ver10/events/wsdl}"

    def __init__(self, dut: "DUT", subscribe_resp: Any) -> None:
        from onvif import ONVIFService

        self._dut = dut
        self.subscription_url = subscribe_resp.SubscriptionReference.Address._value_1
        self.current_time = subscribe_resp.CurrentTime
        self.termination_time = subscribe_resp.TerminationTime
        self._alive = True

        cam = dut._camera
        _xaddr, wsdl_file, _binding = cam.get_definition("events")
        self._mgr = ONVIFService(
            xaddr=self.subscription_url,
            user=cam.user, passwd=cam.passwd, url=wsdl_file,
            encrypt=cam.encrypt, daemon=cam.daemon, no_cache=cam.no_cache,
            dt_diff=cam.dt_diff, transport=cam.transport,
            portType="SubscriptionManager",
            binding_name=f"{self._EVENT_NS}SubscriptionManagerBinding",
        )
        try:
            self._mgr.zeep_client.plugins.append(dut._trace)
        except AttributeError:
            pass
        dut.session.subscriptions.append(self)

    def renew(self, termination_time: str = "PT60S") -> Any:
        return self._mgr.Renew({"TerminationTime": termination_time})

    def unsubscribe(self) -> Any | None:
        if not self._alive:
            return None
        try:
            return self._mgr.Unsubscribe()
        except Exception:
            return None
        finally:
            self._alive = False
            try:
                self._dut.session.subscriptions.remove(self)
            except ValueError:
                pass

    def __enter__(self) -> "NotifyHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.unsubscribe()


class DUT:
    """A lazy-loaded ONVIF device handle."""

    def __init__(self, config: DUTConfig) -> None:
        from onvif import ONVIFCamera  # python-onvif-zeep

        self.config = config
        self.session = DUTSession()
        self._trace = SoapTrace(self.session.soap_traces)
        # We pass our zeep plugin through ONVIFCamera's transport kwargs.
        kwargs: dict[str, Any] = {}
        if config.wsdl_dir:
            kwargs["wsdl_dir"] = config.wsdl_dir
        self._camera = ONVIFCamera(
            config.host,
            config.port,
            config.user,
            config.password,
            **kwargs,
        )
        # zeep clients are created on demand by ONVIFCamera; we attach our
        # plugin lazily in __getattr__ when each service is first accessed.
        self._services: dict[str, Any] = {}

    # -- service accessors ----------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        if name in _SERVICE_FACTORIES:
            svc = self._services.get(name)
            if svc is None:
                factory = getattr(self._camera, _SERVICE_FACTORIES[name])
                svc = factory()
                # Inject our trace plugin into the underlying zeep client.
                try:
                    svc.zeep_client.plugins.append(self._trace)
                except AttributeError:
                    # Older onvif-zeep — plugin list may live elsewhere.
                    log.debug("Could not attach SoapTrace to %s", name)
                self._services[name] = svc
            return svc
        raise AttributeError(name)

    # -- helpers --------------------------------------------------------------

    @property
    def last_request(self) -> str | None:
        for direction, envelope in reversed(self.session.soap_traces):
            if direction == "request":
                return envelope
        return None

    @property
    def last_response(self) -> str | None:
        for direction, envelope in reversed(self.session.soap_traces):
            if direction == "response":
                return envelope
        return None

    # ------------------------------------------------------------------

    def create_pullpoint(self, initial_termination: str = "PT60S") -> PullPointHandle:
        """Call CreatePullPointSubscription and wrap the result.

        The returned handle is a context manager that auto-unsubscribes
        on ``__exit__``. It's also registered on
        ``self.session.subscriptions`` so the session-scope fixture
        teardown can clean up after a crashing test.
        """
        resp = self.events.CreatePullPointSubscription(
            {"InitialTerminationTime": initial_termination}
        )
        return PullPointHandle(self, resp)

    def create_notify_subscription(
        self,
        consumer_reference: str = "http://127.0.0.1:1/onvif-tt-throwaway-consumer",
        initial_termination: str = "PT30S",
    ) -> "NotifyHandle":
        """Basic-notification Subscribe with a throwaway ConsumerReference.

        We never set up an actual notification receiver; the test exercises
        only the Subscribe/Renew/Unsubscribe lifecycle. Any Notify the
        device tries to push to the consumer URL will fail at the device
        with no impact on our side.
        """
        from onvif import ONVIFService

        cam = self._camera
        _xaddr, wsdl_file, _binding = cam.get_definition("events")
        np = ONVIFService(
            xaddr=self.session.services.get("events") or _xaddr,
            user=cam.user, passwd=cam.passwd, url=wsdl_file,
            encrypt=cam.encrypt, daemon=cam.daemon, no_cache=cam.no_cache,
            dt_diff=cam.dt_diff, transport=cam.transport,
            portType="NotificationProducer",
            binding_name="{http://www.onvif.org/ver10/events/wsdl}NotificationProducerBinding",
        )
        try:
            np.zeep_client.plugins.append(self._trace)
        except AttributeError:
            pass
        resp = np.Subscribe({
            "ConsumerReference": {"Address": {"_value_1": consumer_reference}},
            "InitialTerminationTime": initial_termination,
        })
        return NotifyHandle(self, resp)

    def teardown_subscriptions(self) -> None:
        """Unsubscribe everything still on session.subscriptions.

        Called once at end of pytest session — protects against test
        crashes that bypassed ``with`` blocks.
        """
        for handle in list(self.session.subscriptions):
            handle.unsubscribe()
