"""The camera's own reports: a recording started, ended, and why.

The camera accepts a UPnP event subscription and then sends NOTIFY requests
to an address of our choosing; see "Events" in docs/protocol.md. This module
keeps that subscription alive and hands every reported value on.

The events are an addition, never the only source: the state query keeps
running, and everything works without them (a blocked port, another model).
"""

from __future__ import annotations

import http.server
import logging
import socket
import threading
import xml.etree.ElementTree as ET
from typing import Callable
from urllib.parse import urljoin

import httpx

from .camera import DESCRIPTOR_PATH, DESCRIPTOR_PORT

log = logging.getLogger("lumixremote.events")

AGENT = "Panasonic MIL DLNA CP UPnP/1.0"
CALLBACK_PATH = "/Camera/event"
# The camera grants 300 seconds; renewed well before that.
TIMEOUT_HEADER = "Second-300"
MAX_BODY = 64 * 1024


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def event_url(descriptor_xml: str, base: str) -> str | None:
    """The eventSubURL of the ConnectionManager service, made absolute."""
    try:
        root = ET.fromstring(descriptor_xml)
    except ET.ParseError:
        return None
    for service in root.iter():
        if _local(service.tag) != "service":
            continue
        values = {_local(c.tag): (c.text or "").strip() for c in service}
        if "ConnectionManager" in values.get("serviceType", "") and values.get("eventSubURL"):
            return urljoin(base, values["eventSubURL"])
    return None


def parse_notify(body: str) -> dict[str, str]:
    """The properties of a NOTIFY body, e.g. {"X_Panasonic_Cam_VRec": "start"}."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {}
    found: dict[str, str] = {}
    for prop in root.iter():
        if _local(prop.tag) != "property":
            continue
        for child in prop:
            found[_local(child.tag)] = (child.text or "").strip()
    return found


def local_ip_towards(ip: str) -> str:
    """The address of this machine as the camera sees it."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((ip, 9))
        return probe.getsockname()[0]


class EventSubscriber:
    """Listens for the camera's NOTIFY requests and keeps the subscription."""

    RENEW_EVERY = 120.0     # seconds; the subscription lasts 300
    RETRY_EVERY = 30.0      # after a failed attempt

    def __init__(self, camera_ip: str, port: int,
                 on_event: Callable[[str, str], None],
                 transport: httpx.BaseTransport | None = None,
                 host: str = ""):
        self.camera_ip = camera_ip
        self.port = port
        # The address the camera is told to report to. Empty: this machine's
        # own address towards the camera, which is wrong behind Docker's NAT.
        self.host = host
        self.on_event = on_event
        self.error = ""
        self._http = httpx.Client(timeout=8.0, transport=transport)
        self._stop = threading.Event()
        self._sid = ""
        self._url = ""
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def subscribed(self) -> bool:
        return bool(self._sid)

    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if not self.port:
            log.info("events_disabled")
            return
        try:
            self._server = self._listen()
        except OSError as exc:
            # The app runs without events; the state query covers the rest.
            self.error = f"event port {self.port} not usable: {exc}"
            log.warning("events_port_unusable", extra={"port": self.port, "error": str(exc)})
            return
        threading.Thread(target=self._server.serve_forever,
                         name="lumix-events-http", daemon=True).start()
        self._thread = threading.Thread(target=self._run, name="lumix-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        if self._sid:
            try:
                # Short: this runs during shutdown.
                self._http.request("UNSUBSCRIBE", self._url, headers={"SID": self._sid},
                                   timeout=2.0)
            except httpx.HTTPError:
                pass    # it expires by itself
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        self._http.close()

    # ------------------------------------------------------------------ #

    def _listen(self) -> http.server.ThreadingHTTPServer:
        subscriber = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_NOTIFY(self):  # noqa: N802 - the method name is the HTTP verb
                # Only the camera may report; anyone else on the network could
                # otherwise fake the end of a recording.
                if not subscriber._from_camera(self.client_address[0]):
                    self.send_error(403)
                    return
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
                except ValueError:
                    length = 0
                body = self.rfile.read(length).decode("utf-8", "replace")
                for name, value in parse_notify(body).items():
                    subscriber._deliver(name, value)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):   # the app logs in its own format
                pass

        return http.server.ThreadingHTTPServer(("0.0.0.0", self.port), Handler)

    def _from_camera(self, address: str) -> bool:
        try:
            return address == socket.gethostbyname(self.camera_ip)
        except OSError:
            return False

    def _deliver(self, name: str, value: str) -> None:
        if not name.startswith("X_Panasonic_Cam_"):
            return      # the standard properties of the first NOTIFY
        log.info("camera_event", extra={"group": name.removeprefix("X_Panasonic_Cam_"),
                                        "value": value})
        try:
            self.on_event(name.removeprefix("X_Panasonic_Cam_"), value)
        except Exception:
            log.exception("event_handler_error")

    def _run(self) -> None:
        wait = 0.0
        while not self._stop.wait(wait):
            try:
                ok = self._renew() if self._sid else self._subscribe()
            except (httpx.HTTPError, OSError) as exc:
                # The camera is off or out of reach (OSError: no route to it
                # yet); an old subscription may still be good when it comes
                # back.
                log.debug("events_unreachable", extra={"error": str(exc)})
                ok = False
            wait = self.RENEW_EVERY if ok else self.RETRY_EVERY

    def _subscribe(self) -> bool:
        base = f"http://{self.camera_ip}:{DESCRIPTOR_PORT}{DESCRIPTOR_PATH}"
        url = event_url(self._http.get(base).text, base)
        if not url:
            log.info("events_not_offered")
            return False
        host = self.host or local_ip_towards(self.camera_ip)
        callback = f"<http://{host}:{self.port}{CALLBACK_PATH}>"
        r = self._http.request("SUBSCRIBE", url, headers={
            "CALLBACK": callback, "NT": "upnp:event",
            "TIMEOUT": TIMEOUT_HEADER, "User-Agent": AGENT})
        sid = r.headers.get("SID", "")
        if r.status_code != 200 or not sid:
            log.info("events_refused", extra={"status": r.status_code})
            return False
        self._url, self._sid = url, sid
        log.info("events_subscribed")
        return True

    def _renew(self) -> bool:
        r = self._http.request("SUBSCRIBE", self._url, headers={
            "SID": self._sid, "TIMEOUT": TIMEOUT_HEADER})
        if r.status_code == 200:
            return True
        # 412: the camera forgot the subscription (switched off and on).
        log.info("events_subscription_lost", extra={"status": r.status_code})
        self._sid = ""
        return self._subscribe()
