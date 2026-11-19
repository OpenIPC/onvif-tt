"""A real WS-BaseNotification consumer: an HTTP endpoint the DUT pushes
``wsnt:Notify`` to.

The lifecycle tests subscribe with a throwaway ConsumerReference because they
only exercise Subscribe/Renew/Unsubscribe. Whether the device actually
*delivers* is the half an NVR depends on, and it needs somewhere to deliver
to: this binds an ephemeral port on the interface that routes to the DUT and
records every POST body it receives.
"""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lxml import etree

_WSNT = "http://docs.oasis-open.org/wsn/b-2"


def local_address_towards(host: str, port: int = 80) -> str:
    """The local IP the kernel would use to reach ``host`` (no packet sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((socket.gethostbyname(host), port))
        return s.getsockname()[0]
    finally:
        s.close()


class NotifyConsumer:
    """Context manager: ``with NotifyConsumer(dut_host) as c: c.url ...``."""

    def __init__(self, dut_host: str) -> None:
        self._host = local_address_towards(dut_host)
        self._lock = threading.Condition()
        self.bodies: list[bytes] = []
        self.paths: list[str] = []
        consumer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server API
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                with consumer._lock:
                    consumer.bodies.append(body)
                    consumer.paths.append(self.path)
                    consumer._lock.notify_all()
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args) -> None:  # silence stderr
                pass

        self._srv = ThreadingHTTPServer((self._host, 0), _Handler)
        self._thread = threading.Thread(
            target=self._srv.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._srv.server_port}/onvif-tt-notify"

    def __enter__(self) -> "NotifyConsumer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()

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
