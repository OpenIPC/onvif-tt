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
    """

    services: dict[str, str] = field(default_factory=dict)  # name → xaddr
    capabilities: Any | None = None
    soap_traces: collections.deque[tuple[str, str]] = field(
        default_factory=lambda: collections.deque(maxlen=64)
    )


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
