"""Device-Under-Test wrapper.

Thin layer over ``python-onvif-zeep``'s ``ONVIFCamera`` that:

* lazy-binds service proxies on first access (``dut.devicemgmt``,
  ``dut.media``, ``dut.media2``, ``dut.events``, ``dut.ptz``,
  ``dut.imaging``);
* attaches a SOAP-trace zeep plugin so the runner can capture the last
  request/response per service for failure reporting;
* caches the result of ``GetServices`` so feature-gating queries cost
  nothing after the first call.

Services are constructed here rather than through ``ONVIFCamera``'s
``create_*_service`` factories. Those read ``onvif.definition.SERVICES``,
which has no Media2 entry, and take their XAddrs from ver10
``GetCapabilities``, whose ``tt:Capabilities`` has no Media2 slot — so
Media2 was unreachable by construction. We instead build each
``ONVIFService`` against a WSDL from the vendored schema store
(:mod:`.schema_store`) at the XAddr the device reported in ``GetServices``,
per the table in :mod:`.services`. This is the same shape
:class:`PullPointHandle` has always used for the subscription bindings.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from typing import Any

from . import services as service_table
from .schema_store import VendoredSchemaTransport, local_path
from .soap_trace import SoapTrace
from .wsa_validator import WSAValidator, WSAViolation

log = logging.getLogger(__name__)


class ServiceUnbindable(RuntimeError):
    """The DUT advertises a service this client cannot construct.

    Raised rather than skipped: the device may well be conformant, but we
    cannot say either way, and a skip would misreport that as "not
    applicable" while keeping the run green.
    """


@dataclass(slots=True)
class DUTConfig:
    host: str
    port: int = 80
    user: str = ""
    password: str = ""
    timeout: float = 10.0
    """Connect/read budget for a single HTTP exchange. Deliberately not
    applied as zeep's ``operation_timeout``: ``PullMessages`` blocks for
    the subscription's own timeout (up to PT60S) by design."""


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
    unknown_namespaces: set[str] = field(default_factory=set)
    """Service namespaces the DUT advertised that :mod:`.services` has no
    row for. Kept out of ``services`` on purpose: filing them there under
    their raw URI (as this code used to) produced a key no
    ``requires_services`` entry could ever match, so every test needing
    that service skipped as "not advertised" — silently green. The
    ``LOCAL-CLIENT-SERVICES-BINDABLE`` test fails on a non-empty set."""
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

    def __init__(self, dut: "DUT", create_resp: Any) -> None:
        from onvif import ONVIFService  # python-onvif-zeep

        self._dut = dut
        self.subscription_url = create_resp.SubscriptionReference.Address._value_1
        self.current_time = create_resp.CurrentTime
        self.termination_time = create_resp.TerminationTime
        self._alive = True

        common = dut._service_kwargs("events")
        # Two bindings, same subscription URL.
        self._pull = ONVIFService(
            xaddr=self.subscription_url,
            **common,
            binding_name=service_table.event_binding_name("pullpoint"),
        )
        self._mgr = ONVIFService(
            xaddr=self.subscription_url,
            **common,
            binding_name=service_table.event_binding_name(
                "subscription_manager"),
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

        self._mgr = ONVIFService(
            xaddr=self.subscription_url,
            **dut._service_kwargs("events"),
            binding_name=service_table.event_binding_name(
                "subscription_manager"),
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


_CAMERA_CLASS = None


def _make_camera(*args, **kwargs):
    """Build the ``ONVIFCamera`` subclass lazily, once.

    ``onvif`` is imported inside functions throughout this module so the
    package stays importable (for ``onvif-tt list``, the parser tests, …)
    without the SOAP stack being present.
    """
    global _CAMERA_CLASS
    if _CAMERA_CLASS is not None:
        return _CAMERA_CLASS(*args, **kwargs)

    from onvif import ONVIFCamera  # python-onvif-zeep

    class _Camera(ONVIFCamera):
        """``ONVIFCamera`` with its implicit discovery disabled.

        Stock ``update_xaddrs()`` runs from ``__init__`` and does two things
        we do not want. It populates ``self.xaddrs`` from ver10
        ``GetCapabilities``, which cannot see Media2 at all — we take XAddrs
        from ``GetServices`` instead. And it opens a real PullPoint
        subscription on the device (``self.event.CreatePullPointSubscription()``
        inside a bare ``try/except``) purely to learn a URL, then never
        unsubscribes it. That leaked one subscription per run, before the
        trace plugins were attached, so it never even showed up in the report.

        We keep the class for its credential/transport bookkeeping only;
        service construction happens in :meth:`DUT.__getattr__`.
        """

        def update_xaddrs(self):
            self.dt_diff = None
            self.xaddrs = {}

    _CAMERA_CLASS = _Camera
    return _CAMERA_CLASS(*args, **kwargs)


class DUT:
    """A lazy-loaded ONVIF device handle."""

    def __init__(self, config: DUTConfig) -> None:
        self.config = config
        self.session = DUTSession()
        self._trace = SoapTrace(self.session.soap_traces)
        self._wsa = WSAValidator()
        self._wsa.attach(self.session.wsa_violations)
        # One transport, shared by every service: it resolves WSDL/XSD
        # imports from the vendored store and carries the SOAP requests.
        # operation_timeout is deliberately left unset — PullMessages
        # legitimately blocks for the subscription's timeout (PT60S), which
        # config.timeout (a connect budget) must not cut short.
        self._transport = VendoredSchemaTransport(timeout=config.timeout)
        self._camera = _make_camera(
            config.host,
            config.port,
            config.user,
            config.password,
            encrypt=False,
            transport=self._transport,
        )
        # Services are constructed on demand in __getattr__.
        self._services: dict[str, Any] = {}

    # -- service accessors ----------------------------------------------------

    def can_bind(self, name: str) -> bool:
        """Whether a client for ``name`` can actually be constructed.

        Distinct from "the DUT advertises it": a service can be advertised and
        still be unreachable because this client has no schema for it. Tests
        with a working alternative should branch on this; tests that genuinely
        require the service should just access it and let the error surface.
        """
        svc = service_table.get(name)
        return svc is not None and local_path(svc.wsdl_url) is not None

    def _service_kwargs(self, name: str) -> dict[str, Any]:
        """Constructor arguments shared by every ``ONVIFService`` we build.

        Covers credentials, the vendored WSDL path and the shared transport —
        everything except ``xaddr`` and ``binding_name``, which differ between
        a service proxy and a subscription-rooted binding.
        """
        sd = service_table.get(name)
        if sd is None:
            raise ServiceUnbindable(
                f"{name}: no entry in the ONVIF service table. Add a "
                f"ServiceDef to onvif_tt/runtime/services.py."
            )
        wsdl = local_path(sd.wsdl_url)
        if wsdl is None:
            raise ServiceUnbindable(
                f"{name}: {sd.wsdl_url} is not in the vendored schema store. "
                f"Run `onvif-tt schemas refresh`. This is a limitation of the "
                f"client, not a verdict on the device."
            )
        cam = self._camera
        return {
            "user": cam.user,
            "passwd": cam.passwd,
            "url": str(wsdl),
            "encrypt": cam.encrypt,
            "daemon": cam.daemon,
            "no_cache": cam.no_cache,
            "dt_diff": cam.dt_diff,
            "transport": self._transport,
        }

    def _xaddr_for(self, name: str) -> str:
        """Where to send this service's requests.

        The device management service lives at the fixed entry point ONVIF
        Core mandates. Everything else comes from ``GetServices`` — which for
        Media2 is the *only* source, since ver10 ``GetCapabilities`` has no
        slot for it.
        """
        if name == "devicemgmt":
            host = self.config.host
            if not host.startswith(("http://", "https://")):
                host = f"http://{host}"
            return f"{host}:{self.config.port}/onvif/device_service"

        if not self.session.services:
            # Local import: features imports this module.
            from .features import discover_services
            discover_services(self)
        xaddr = self.session.services.get(name)
        if not xaddr:
            raise ServiceUnbindable(
                f"{name}: the DUT did not report an XAddr for this service "
                f"in GetServices or GetCapabilities."
            )
        return xaddr

    def __getattr__(self, name: str) -> Any:
        if service_table.get(name) is None:
            raise AttributeError(name)
        svc = self._services.get(name)
        if svc is None:
            from onvif import ONVIFService  # python-onvif-zeep

            sd = service_table.get(name)
            assert sd is not None  # narrowed by the guard above
            svc = ONVIFService(
                xaddr=self._xaddr_for(name),
                **self._service_kwargs(name),
                binding_name=sd.binding_name,
            )
            # Inject trace + WS-Addressing validator plugins.
            try:
                svc.zeep_client.plugins.append(self._trace)
                svc.zeep_client.plugins.append(self._wsa)
            except AttributeError:
                # Older onvif-zeep — plugin list may live elsewhere.
                log.debug("Could not attach plugins to %s", name)
            self._services[name] = svc
        return svc

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

        np = ONVIFService(
            xaddr=self._xaddr_for("events"),
            **self._service_kwargs("events"),
            binding_name=service_table.event_binding_name(
                "notification_producer"),
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
