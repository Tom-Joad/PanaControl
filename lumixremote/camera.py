"""Connection and command layer for LUMIX cameras (cam.cgi protocol).

Tested against a DC-GH6 with firmware 3.01. Should work with every LUMIX model
that reports `CPRemoteView` in its DLNA descriptor.
"""

from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

UPNP_NS = "{urn:schemas-upnp-org:device-1-0}"
PANA_NS = "{urn:schemas-panasonic-com:pana}"

DESCRIPTOR_PORT = 60606
DESCRIPTOR_PATH = "/Lumix/Server0/ddd"


class LumixError(RuntimeError):
    """Error in the communication with the camera."""


class NotConnected(LumixError):
    """Command sent without a valid session."""


class NotReachable(LumixError):
    """The camera could not be reached: timeout or aborted connection.

    Deliberately kept apart from a refusal by the camera: a short WLAN dropout
    is something else than an err_busy. Treating both alike throws the session
    away on every lost packet and aborts a running recording with it.
    """


@dataclass
class DeviceInfo:
    friendly_name: str = ""
    model_number: str = ""
    firmware: str = ""
    udn: str = ""
    capabilities: str = ""

    @property
    def supports_remote_view(self) -> bool:
        return "CPRemoteView" in self.capabilities


@dataclass
class CameraState:
    """Extract of the getstate reply. Field names as the camera sends them."""

    raw: dict[str, str] = field(default_factory=dict)

    @property
    def recording(self) -> bool:
        return self.raw.get("rec", "off") == "on"

    @property
    def battery(self) -> str:
        return self.raw.get("batt", "unknown")

    @property
    def remaining_shots(self) -> str:
        return self.raw.get("remaincapacity", "unknown")

    @property
    def video_remaining(self) -> str:
        """Remaining video recording time in seconds, if reported."""
        return self.raw.get("video_remaincapacity", "unknown")


def _hexencode(value: str) -> str:
    """Panasonic expects the UDN and device name hex-encoded on first contact."""
    return value.encode("utf-8").hex()


def _looks_like_session(value: str) -> bool:
    """Does the field look like a session ID?

    Only sixteen-digit hex strings have been seen. The last field of the
    interim reply, on the other hand, is `open`; without this check it would
    pass as a session ID.
    """
    return len(value) >= 8 and all(c in "0123456789abcdefABCDEF" for c in value)


class Camera:
    """Thin client for the cam.cgi protocol.

    Thread-safe: all HTTP calls run under a lock, so the keepalive thread and
    commands from the web interface don't get in each other's way.
    """

    def __init__(self, ip: str, device_name: str = "lumix-remote", timeout: float = 5.0):
        self.ip = ip
        self.device_name = device_name
        self.info = DeviceInfo()
        self.session_id: str | None = None
        self._lock = threading.RLock()
        self._client = httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------ #
    # Basics
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self._client.close()

    @property
    def connected(self) -> bool:
        return self.session_id is not None

    def _headers(self) -> dict[str, str]:
        return {"X-SESSION_ID": self.session_id} if self.session_id else {}

    def _get_raw(self, **params: str) -> str:
        """One cam.cgi call, reply as raw text."""
        url = f"http://{self.ip}/cam.cgi"
        with self._lock:
            try:
                r = self._client.get(url, params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                raise NotReachable(f"camera not reachable for {params}: {exc}") from exc
        if r.status_code != 200:
            raise LumixError(f"HTTP {r.status_code} for {params}")
        return r.text.strip()

    def _get_xml(self, **params: str) -> ET.Element:
        """One cam.cgi call with an XML reply. Checks <result>ok</result>."""
        text = self._get_raw(**params)
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise LumixError(f"unreadable reply to {params}: {text!r}") from exc
        result = root.findtext("result", default="")
        if result != "ok":
            raise LumixError(f"camera refused {params}: {result!r}")
        return root

    # ------------------------------------------------------------------ #
    # Connecting
    # ------------------------------------------------------------------ #

    def describe(self) -> DeviceInfo:
        """Fetch the DLNA descriptor. No side effects, works without a session."""
        url = f"http://{self.ip}:{DESCRIPTOR_PORT}{DESCRIPTOR_PATH}"
        try:
            r = self._client.get(url)
        except httpx.HTTPError as exc:
            raise NotReachable(f"camera at {self.ip} not reachable: {exc}") from exc
        if r.status_code != 200:
            raise LumixError(f"descriptor returned HTTP {r.status_code}")

        try:
            root = ET.fromstring(r.text)
        except ET.ParseError as exc:
            raise LumixError(f"unreadable descriptor: {exc}") from exc
        dev = root.find(f"{UPNP_NS}device")
        if dev is None:
            raise LumixError("descriptor without a <device> element")

        udn = dev.findtext(f"{UPNP_NS}UDN", default="")
        self.info = DeviceInfo(
            friendly_name=dev.findtext(f"{UPNP_NS}friendlyName", default=""),
            model_number=dev.findtext(f"{UPNP_NS}modelNumber", default=""),
            firmware=dev.findtext(f"{PANA_NS}X_FirmVersion", default=""),
            udn=udn.removeprefix("uuid:"),
            capabilities=dev.findtext(f"{PANA_NS}X_AdditionalFunction", default=""),
        )
        return self.info

    def connect(self, confirm_timeout: float = 60.0, poll_interval: float = 3.0) -> str:
        """Register and obtain a session ID.

        The camera answers `req_acc_e` in several forms, all with HTTP 200:

            ok_under_research_no_msg,<model>,remote,open       still working
            ok_under_research,<model>,remote,open              likewise
            ok,<model>,remote,open,<session ID>                done

        The interim reply comes in several spellings, so the code checks for
        exactly `ok` and also requires a session ID, instead of excluding
        prefixes.

        The gap between attempts is crucial. At half-second intervals the
        camera stays stuck in `ok_under_research_no_msg`: twelve attempts in a
        row without success, after which it stopped answering at all. With
        three seconds between attempts, the second one succeeded. So this asks
        slowly on purpose.

        On first contact, a confirmation on the display is added; until it is
        acknowledged, the reply also stays the interim one.
        """
        if not self.info.udn:
            self.describe()
        if not self.info.supports_remote_view:
            log.warning(
                "camera reports no CPRemoteView capability (%r), trying anyway",
                self.info.capabilities,
            )

        self._get_raw(mode="accctrl", type="req_acc_g")

        udn_hex = _hexencode(self.info.udn)
        name_hex = _hexencode(self.device_name)
        deadline = time.monotonic() + confirm_timeout
        last = ""
        reported = ""
        attempts = 0

        while time.monotonic() < deadline:
            attempts += 1
            last = self._get_raw(
                mode="accctrl", type="req_acc_e", value=udn_hex, value2=name_hex
            )
            parts = [p.strip() for p in last.split(",")]
            session = parts[-1] if len(parts) > 1 else ""

            # Success means: first field exactly 'ok' AND a field that looks
            # like a session ID. The interim reply carries the word 'open' in
            # the same place, which could not be told apart otherwise.
            if parts[0] == "ok" and _looks_like_session(session):
                self.session_id = session
                log.info(
                    "connected to %s (%s, firmware %s) after %d attempt(s)",
                    self.info.model_number,
                    self.info.friendly_name,
                    self.info.firmware,
                    attempts,
                )
                self._announce_name()
                return self.session_id

            # Report the interim reply once, not on every attempt; otherwise
            # the log drowns and the cause can't be seen.
            if last != reported:
                reported = last
                log.info("camera not ready yet, reply: %s", last)

            time.sleep(poll_interval)

        raise LumixError(
            f"registration not completed after {attempts} attempts "
            f"(last reply: {last!r}). If the camera's display shows a prompt, "
            "confirm it. If it keeps answering err_critical, the remote "
            "session is occupied and the camera must be switched off and on "
            "again."
        )

    def _announce_name(self) -> None:
        """Send the plain-text name afterwards. Fails on some firmware versions."""
        try:
            self._get_xml(mode="setsetting", type="device_name", value=self.device_name)
        except LumixError as exc:
            log.debug("device_name not set: %s", exc)

    def ensure_connected(self) -> None:
        """Register again after the camera was in standby or lost power."""
        try:
            self.get_state()
            return
        except LumixError:
            log.info("session lost, reconnecting")
        self.session_id = None
        self.describe()
        self.connect()

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    def _require_session(self) -> None:
        if not self.connected:
            raise NotConnected("no session context: call connect() first")

    def get_state(self) -> CameraState:
        """State query. Doubles as the keepalive."""
        self._require_session()
        root = self._get_xml(mode="getstate")
        state = root.find("state")
        raw = {child.tag: (child.text or "") for child in state} if state is not None else {}
        return CameraState(raw=raw)

    def set_rec_mode(self) -> None:
        """Force the camera into record mode (needed from playback)."""
        self._require_session()
        self._get_xml(mode="camcmd", value="recmode")

    def autofocus(self) -> None:
        self._require_session()
        self._get_xml(mode="camcmd", value="oneshot_af")

    def rec_start(self) -> None:
        self._require_session()
        self._get_xml(mode="camcmd", value="video_recstart")

    def rec_stop(self) -> None:
        self._require_session()
        self._get_xml(mode="camcmd", value="video_recstop")

    def capture(self) -> None:
        """Take a photo. Not a core function, but free."""
        self._require_session()
        self._get_xml(mode="camcmd", value="capture")

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #

    def get_setting(self, name: str) -> str:
        """Read a setting, such as `iso` or `shtrspeed`.

        The camera answers with `<settingvalue NAME="value"/>`; only the value
        is returned. Unknown names are answered with `err_critical`, which
        arrives here as a LumixError.
        """
        self._require_session()
        root = self._get_xml(mode="getsetting", type=name)
        node = root.find("settingvalue")
        if node is None:
            raise LumixError(f"reply to {name!r} without settingvalue")
        value = node.get(name)
        if value is None:
            raise LumixError(f"settingvalue without attribute {name!r}")
        return value

    def set_setting(self, name: str, value: str) -> None:
        """Change a setting.

        Note: an acknowledged `ok` doesn't mean the value applies. The camera
        snaps to the value allowed in the current mode; measured was ISO 400,
        which came back as 800. So the caller reads it back.
        """
        self._require_session()
        self._get_xml(mode="setsetting", type=name, value=value)

    def get_info(self, name: str) -> str:
        """Raw information from the camera, such as `lens` or `allmenu`."""
        self._require_session()
        return self._get_raw(mode="getinfo", type=name)

    # ------------------------------------------------------------------ #
    # Live stream
    # ------------------------------------------------------------------ #

    def start_stream(self, port: int) -> None:
        """From now on the camera sends JPEG frames by UDP to the asking host.

        Important: the target is always the IP this call came from. So the UDP
        receiver must listen on the same machine, and it should already run
        before this call is sent.
        """
        self._require_session()
        self._get_xml(mode="startstream", value=str(port))

    def stop_stream(self) -> None:
        self._require_session()
        try:
            self._get_xml(mode="stopstream")
        except LumixError as exc:
            log.debug("stopstream not acknowledged: %s", exc)
