"""A real WS-BaseNotification consumer: an HTTP endpoint the DUT pushes
``wsnt:Notify`` to.

The lifecycle tests subscribe with a throwaway ConsumerReference because they
only exercise Subscribe/Renew/Unsubscribe. Whether the device actually
*delivers* is the half an NVR depends on, and it needs somewhere to deliver
to: this binds an ephemeral port on the interface that routes to the DUT and
records every Notify it receives.

It listens on a reachable interface, so it trusts nothing it is sent: only
POSTs from the DUT's own address to an unguessable per-consumer path count
(so another host cannot forge a delivery), bodies are bounded in size and
number, and a sender that stalls mid-request times out instead of holding up
shutdown.
"""

from __future__ import annotations

import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lxml import etree

_WSNT = "http://docs.oasis-open.org/wsn/b-2"

MAX_BODY = 1 << 20      # a Notify is a few KiB; anything near this is not one
MAX_BODIES = 256        # retained deliveries
READ_TIMEOUT_S = 5.0


def local_address_towards(host: str, port: int = 80) -> str:
    """The local IP the kernel would use to reach ``host`` (no packet sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((socket.gethostbyname(host), port))
        return s.getsockname()[0]
    finally:
        s.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False  # a stalled handler must not hold up __exit__


class NotifyConsumer:
    """Context manager: ``with NotifyConsumer(dut_host) as c: c.url ...``."""

    def __init__(self, dut_host: str) -> None:
        self._host = local_address_towards(dut_host)
        self._dut_ip = socket.gethostbyname(dut_host)
        self._path = "/onvif-tt-notify/" + secrets.token_urlsafe(16)
        self._lock = threading.Condition()
        self.bodies: list[bytes] = []
        self.rejected = 0
        consumer = self

        class _Handler(BaseHTTPRequestHandler):
            timeout = READ_TIMEOUT_S

            def _reply(self, code: int) -> None:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802 — http.server API
                if (self.client_address[0] != consumer._dut_ip
                        or self.path != consumer._path):
                    with consumer._lock:
                        consumer.rejected += 1
                    self._reply(404)
                    return
                try:
                    n = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    n = -1
                if n < 0 or n > MAX_BODY:
                    self._reply(413 if n > MAX_BODY else 411)
                    return
                body = self.rfile.read(n)
                if len(body) != n:
                    return  # sender went away mid-body
                with consumer._lock:
                    consumer.bodies.append(body)
                    del consumer.bodies[:-MAX_BODIES]
                    consumer._lock.notify_all()
                self._reply(202)

            def log_message(self, *args) -> None:  # silence stderr
                pass

        self._srv = _Server((self._host, 0), _Handler)
        self._thread = threading.Thread(
            target=self._srv.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._srv.server_port}{self._path}"

    def __enter__(self) -> "NotifyConsumer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def count(self) -> int:
        with self._lock:
            return len(self.bodies)

    def notification_messages(self, since: int = 0) -> list[etree._Element]:
        """Every ``wsnt:NotificationMessage`` in bodies[since:]."""
        out: list[etree._Element] = []
        with self._lock:
            bodies = list(self.bodies[since:])
        for b in bodies:
            try:
                root = etree.fromstring(b)
            except etree.XMLSyntaxError:
                continue
            out.extend(root.iter(f"{{{_WSNT}}}NotificationMessage"))
        return out

    def wait_for(self, predicate, timeout: float, since: int = 0
                 ) -> list[etree._Element]:
        """Wait until ``predicate(messages)`` holds; return the messages."""
        deadline = time.monotonic() + timeout
        while True:
            msgs = self.notification_messages(since)
            if predicate(msgs):
                return msgs
            left = deadline - time.monotonic()
            if left <= 0:
                return msgs
            with self._lock:
                self._lock.wait(min(left, 0.5))

    def settle(self, quiet: float = 1.5, timeout: float = 10.0) -> int:
        """Wait until no Notify has arrived for ``quiet`` seconds; return the
        delivery count then. A burst split over several POSTs is over."""
        deadline = time.monotonic() + timeout
        last = self.count()
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            with self._lock:
                self._lock.wait(0.2)
            n = self.count()
            if n != last:
                last, last_change = n, time.monotonic()
            elif time.monotonic() - last_change >= quiet:
                break
        return last
