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
import datetime
import logging
from dataclasses import dataclass, field
from typing import Any

from . import services as service_table
from .auth import (
    SYSTEM_DATE_TIME_ENVELOPE,
    AuthMode,
    AuthState,
    candidates,
    device_utc_from_response,
    is_auth_failure,
)
from .response_validator import ResponseValidator, SchemaViolation
from .schema_store import VendoredSchemaTransport, local_path
from .soap_trace import SoapTrace
from .wsa_validator import WSAValidator, WSAViolation

log = logging.getLogger(__name__)

#: Below this, host and device clocks agree closely enough that skew cannot
#: be what a device is objecting to, so there is nothing to retry.
_CLOCK_SKEW_TOLERANCE = datetime.timedelta(seconds=5)


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
    auth: AuthMode = AuthMode.AUTO
    """Which WS-Security UsernameToken password type to use. ``AUTO``
    negotiates: PasswordDigest first, PasswordText as fallback. See
    :mod:`.auth` for why the default is not simply PasswordDigest."""
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
    advertised_without_xaddr: set[str] = field(default_factory=set)
    """Services the DUT listed in ``GetServices`` with an empty or absent
    ``XAddr``. That's a spec violation on its own — ONVIF Core makes
    ``Service/XAddr`` mandatory — but the reason it's tracked separately is
    the same one as ``unknown_namespaces``: dropping the entry silently
    would leave the service looking un-advertised, so its tests would skip
    as "not applicable" and the run would stay green."""
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
    auth: AuthState = field(default_factory=AuthState)
    """What the DUT accepted for message-level security. SECURITY-1-1-1
    reads this rather than re-probing, so the verdict comes from the same
    exchange the rest of the suite authenticated with."""
    wsa_violations: list[WSAViolation] = field(default_factory=list)
    schema_violations: list[SchemaViolation] = field(default_factory=list)
    """Structural schema defects seen in responses during the current test.
    Cleared by the dispatch loop immediately before each test body, so a
    violation is attributed to the test whose call provoked it."""


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
        dut.attach_plugins(self._pull, self._mgr)
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
        dut.attach_plugins(self._mgr)
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
        self._schema = ResponseValidator(self)
        self._schema.attach(self.session.schema_violations)
        # One transport, shared by every service: it resolves WSDL/XSD
        # imports from the vendored store and carries the SOAP requests.
        # operation_timeout is deliberately left unset — PullMessages
        # legitimately blocks for the subscription's timeout (PT60S), which
        # config.timeout (a connect budget) must not cut short.
        self._transport = VendoredSchemaTransport(timeout=config.timeout)
        self.session.auth.requested = config.auth
        # First candidate for the requested mode; negotiation may move on to
        # the next one when the device refuses this. NONE means no
        # credentials, which python-onvif-zeep expresses as an empty user.
        cands = candidates(config.auth)
        self._auth_candidates = list(cands)
        self._camera = _make_camera(
            config.host,
            config.port,
            config.user if cands else "",
            config.password if cands else "",
            encrypt=cands[0][1] if cands else False,
            transport=self._transport,
        )
        if not cands:
            self.session.auth.accepted = "none"
        # Services are constructed on demand in __getattr__.
        self._services: dict[str, Any] = {}
        # Every zeep client we've built, including the subscription-rooted
        # bindings that never become entries in _services. The response
        # validator resolves QNames against this.
        self.zeep_clients: list[Any] = []
        self._negotiating = False
        self._clock_checked = False

    # -- authentication -------------------------------------------------------

    def _reset_clients(self) -> None:
        """Drop every bound client so the next access rebuilds it.

        Credentials are baked into each ``ONVIFService`` at construction, so
        changing the password type or the clock offset means rebuilding.
        """
        self._services.clear()
        self.zeep_clients.clear()

    def _try_auth(self, label: str, use_digest: bool) -> bool:
        """Attempt one password type. True if the DUT accepted it.

        Probes with ``GetDeviceInformation`` — universally implemented, and
        the same operation ``LOCAL-AUTH-*`` uses. A device that ignores
        credentials entirely will of course accept anything here; that is a
        different defect and ``LOCAL-AUTH-ANONYMOUS-REJECTED`` is what catches
        it.
        """
        self._camera.encrypt = use_digest
        self._reset_clients()
        try:
            self.devicemgmt.GetDeviceInformation()
        except Exception as exc:  # noqa: BLE001
            if not is_auth_failure(exc):
                raise  # transport failure, not a verdict on this mode
            log.debug("DUT refused %s: %s", label, exc)
            if label not in self.session.auth.rejected:
                self.session.auth.rejected.append(label)
            return False
        # A mode that works on a retry was never really refused; leaving it
        # in `rejected` would have SECURITY-1-1-1 report a digest-capable
        # device as digest-refusing.
        if label in self.session.auth.rejected:
            self.session.auth.rejected.remove(label)
        self.session.auth.accepted = label
        return True

    def _negotiate_auth(self) -> None:
        """Settle on a password type the DUT accepts, once per session."""
        if (self._negotiating or self.session.auth.accepted
                or not self._auth_candidates):
            return
        self._negotiating = True
        try:
            for label, use_digest in self._auth_candidates:
                if self._try_auth(label, use_digest):
                    return
                # Retry *this* candidate with the clock corrected before
                # falling back to a weaker one. Doing the skew check only
                # after every candidate had failed meant a digest-capable
                # device with a wrong clock silently ended up on cleartext,
                # recorded as having refused digest — a false verdict, and
                # the password on the wire for no reason.
                if (self._apply_clock_offset()
                        and self._try_auth(label, use_digest)):
                    return
        finally:
            self._negotiating = False

    def _probe_device_time(self) -> datetime.datetime | None:
        """The device's UTC clock, asked with no credentials at all.

        Posted raw rather than through zeep: python-onvif-zeep always attaches
        a UsernameToken, and an *empty* one is still a token — verified that a
        device refuses it just as readily as a wrong one, which would defeat
        the point of a pre-auth probe.
        """
        resp = self._transport.session.post(
            self._xaddr_for("devicemgmt"),
            data=SYSTEM_DATE_TIME_ENVELOPE,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            timeout=self.config.timeout,
        )
        self.session.soap_traces.append(
            ("response", resp.content.decode("utf-8", errors="replace")))
        return device_utc_from_response(resp.content)

    def _apply_clock_offset(self) -> bool:
        """A password type was refused — is the device's clock the reason?

        A ``PasswordDigest`` is computed over the ``Created`` timestamp we
        send, and devices that enforce a freshness window on it will reject an
        otherwise valid token from a host whose clock disagrees. This is why
        ONVIF Core leaves ``GetSystemDateAndTime`` unauthenticated: it is
        exactly the diagnostic to reach for once credentials have been
        refused. Retried once, only when the measured offset is big enough to
        plausibly be the cause, so a genuinely wrong password still fails
        fast and honestly. Issue #5.

        Returns True if an offset worth retrying for was found and applied.
        Probes at most once per session — a device whose clock we've already
        measured won't tell us anything new on the next refusal.
        """
        if self._clock_checked:
            return False
        self._clock_checked = True
        try:
            device_utc = self._probe_device_time()
        except Exception as exc:  # noqa: BLE001
            log.debug("clock probe failed, leaving auth as refused: %s", exc)
            return False
        if device_utc is None:
            self.session.auth.clock_probe_refused = True
            log.warning(
                "DUT would not answer an unauthenticated GetSystemDateAndTime, "
                "so clock skew cannot be ruled out as the cause of the "
                "authentication failure")
            return False
        offset = device_utc - datetime.datetime.utcnow()
        self.session.auth.clock_offset = offset
        if abs(offset) <= _CLOCK_SKEW_TOLERANCE:
            return False  # clocks agree; the credentials are genuinely refused
        log.warning(
            "DUT clock differs from host by %s — retrying authentication with "
            "the offset applied to wsu:Created", offset)
        self._camera.dt_diff = offset
        return True

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

    def attach_plugins(self, *svcs: Any) -> None:
        """Attach every observing plugin to freshly-built ONVIFServices.

        One place, because there are four construction sites (services,
        PullPoint's two bindings, NotifyHandle, NotificationProducer) and
        adding a plugin to three of four is a silent hole — the observer
        simply never sees those responses.

        Also records the zeep client on :attr:`zeep_clients`, which is what
        the response validator searches to resolve a response's QName. The
        subscription-rooted bindings never land in ``_services``, so without
        this their responses would be silently unvalidatable.
        """
        for svc in svcs:
            try:
                plugins = svc.zeep_client.plugins
            except AttributeError:
                # Older onvif-zeep — plugin list may live elsewhere.
                log.debug("Could not attach plugins to %r", svc)
                continue
            plugins.append(self._trace)
            plugins.append(self._wsa)
            plugins.append(self._schema)
            # --auth none means *no* wsse:Security header. Clearing the
            # credentials isn't enough: python-onvif-zeep still builds a
            # UsernameToken, and an empty token is still a token that a
            # device will refuse — the same trap the pre-auth clock probe
            # hits. zeep only applies wsse when client.wsse is truthy.
            if self.config.auth is AuthMode.NONE:
                svc.zeep_client.wsse = None
            if svc.zeep_client not in self.zeep_clients:
                self.zeep_clients.append(svc.zeep_client)

    def can_reach(self, name: str) -> bool:
        """Whether a *working* proxy can be built — schema **and** endpoint.

        :meth:`can_bind` answers "do we have the schema", which is about this
        client. This adds "did the device give us somewhere to send the
        request", which is about the DUT: a service advertised in
        ``GetServices`` with an empty ``XAddr`` is unusable no matter how
        good our schema set is, and must not be mistaken for absent.
        """
        return self.can_bind(name) and name not in (
            self.session.advertised_without_xaddr
        )

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
        # Settle the password type before handing out any client, so every
        # service is built with credentials the device has actually accepted.
        # Guarded against re-entry: the negotiation probe itself goes through
        # here to reach devicemgmt.
        self._negotiate_auth()
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
            # Cache before attaching: the response validator resolves QNames
            # against the bound clients, and this one must be reachable by
            # the time its own first response comes back.
            self._services[name] = svc
            self.attach_plugins(svc)
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
        self.attach_plugins(np)
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
