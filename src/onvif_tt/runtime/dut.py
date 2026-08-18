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
from .wsa_validator import WSAValidator, WSAViolation

log = logging.getLogger(__name__)

# ONVIF service short-name → method on ONVIFCamera for create_*_service()
class ServiceUnbindable(RuntimeError):
    """The DUT advertises a service this client cannot construct.

    Raised rather than skipped: the device may well be conformant, but we
    cannot say either way, and a skip would misreport that as "not
    applicable" while keeping the run green.
    """


_SERVICE_FACTORIES: dict[str, str] = {
    "devicemgmt": "create_devicemgmt_service",
    "media": "create_media_service",
    # python-onvif-zeep ships NO media2 WSDL and no media2 entry in its
    # SERVICES map, so there is nothing to bind: create_media2_service does
    # not exist, and the generic create_onvif_service path fails too because
    # the bundled onvif.xsd predates types the ver20 WSDL references (e.g.
    # tt:StringList). Every media2 test here has therefore never executed
    # against a real device — they skipped, because until now no DUT under
    # test advertised the service. Kept mapped so the skip is explicit and
    # the reason is stated, rather than surfacing as a bare AttributeError.
    "media2": "__unbindable:media2",
    "events": "create_events_service",
    "ptz": "create_ptz_service",
    "imaging": "create_imaging_service",
    "analytics": "create_analytics_service",
    "recording": "create_recording_service",
    "search": "create_search_service",
    "replay": "create_replay_service",
    "deviceio": "create_deviceio_service",
    # Profile A (Access Control) + Profile D (Door Control). python-onvif-zeep
    # ships these WSDLs but doesn't expose a dedicated factory helper, so we
    # go through the generic ``create_onvif_service(name)`` path.
    "accesscontrol": "__create_onvif_service:accesscontrol",
    "doorcontrol":   "__create_onvif_service:doorcontrol",
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
    wsa_violations: list[WSAViolation] = field(default_factory=list)


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
        # Attach SOAP trace + WS-Addressing validator plugins to both.
        for svc in (self._pull, self._mgr):
            try:
                svc.zeep_client.plugins.append(dut._trace)
                svc.zeep_client.plugins.append(dut._wsa)
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
        # SubscriptionReference + Address are mandatory per WS-BaseNotification.
        # A DUT that returns SubscribeResponse without them is non-conformant;
        # raise a precise error that the test layer can xfail / surface.
        ref = getattr(subscribe_resp, "SubscriptionReference", None)
        addr = getattr(ref, "Address", None) if ref is not None else None
        url = getattr(addr, "_value_1", None) if addr is not None else None
        if not url:
            raise ValueError(
                "Subscribe response has no SubscriptionReference.Address — "
                "spec violation: WS-BaseNotification §3.1 requires the "
                "subscription manager endpoint to be present"
            )
        self.subscription_url = url
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
            self._mgr.zeep_client.plugins.append(dut._wsa)
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
        self._wsa = WSAValidator()
        self._wsa.attach(self.session.wsa_violations)
        # We pass our zeep plugin through ONVIFCamera's transport kwargs.
        kwargs: dict[str, Any] = {}
        if config.wsdl_dir:
            kwargs["wsdl_dir"] = config.wsdl_dir
        self._camera = ONVIFCamera(
            config.host,
            config.port,
            config.user,
            config.password,
            encrypt=False,
            **kwargs,
        )
        # zeep clients are created on demand by ONVIFCamera; we attach our
        # plugin lazily in __getattr__ when each service is first accessed.
        self._services: dict[str, Any] = {}

    # -- service accessors ----------------------------------------------------

    def can_bind(self, name: str) -> bool:
        """Whether a client for ``name`` can actually be constructed.

        Distinct from "the DUT advertises it": a service can be advertised and
        still be unreachable because this client has no schema for it. Tests
        with a working alternative should branch on this; tests that genuinely
        require the service should just access it and let the error surface.
        """
        spec = _SERVICE_FACTORIES.get(name)
        return spec is not None and not spec.startswith("__unbindable:")

    def __getattr__(self, name: str) -> Any:
        if name in _SERVICE_FACTORIES:
            svc = self._services.get(name)
            if svc is None:
                spec = _SERVICE_FACTORIES[name]
                if spec.startswith("__unbindable:"):
                    # Deliberately an error, not a skip. The DUT advertises
                    # this service; we simply cannot construct a client for it.
                    # Skipping would report "not applicable" for a device that
                    # does support it, and leave the run green while a whole
                    # profile went unverified. Callers that have a working
                    # fallback should consult can_bind() first.
                    raise ServiceUnbindable(
                        f"{name}: the DUT advertises this service but "
                        f"python-onvif-zeep cannot bind it — it ships no WSDL "
                        f"for it, and its bundled onvif.xsd predates the "
                        f"ver20 types. This is a limitation of the client, "
                        f"not a verdict on the device. See "
                        f"https://github.com/OpenIPC/onvif-tt/issues/1"
                    )
                if spec.startswith("__create_onvif_service:"):
                    # Generic path for services without a dedicated factory.
                    wsdl_name = spec.split(":", 1)[1]
                    svc = self._camera.create_onvif_service(wsdl_name)
                else:
                    factory = getattr(self._camera, spec)
                    svc = factory()
                # Inject trace + WS-Addressing validator plugins.
                try:
                    svc.zeep_client.plugins.append(self._trace)
                    svc.zeep_client.plugins.append(self._wsa)
                except AttributeError:
                    # Older onvif-zeep — plugin list may live elsewhere.
                    log.debug("Could not attach plugins to %s", name)
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
            np.zeep_client.plugins.append(self._wsa)
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
