"""Web interface for the LUMIX remote control.

Three strands run side by side:

  * a background thread keeps the registration alive, polls `getstate` every
    few seconds and kicks the stream when it stalls
  * `LiveStream` receives the UDP frames, unchanged
  * FastAPI serves the interface, the MJPEG output and the command routes

Only one connection to the camera exists, no matter how many browsers watch.

Start:
    python3 -m lumixremote.app
"""

from __future__ import annotations

import contextlib
import logging
import os
import pathlib
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__, logging_setup
from .camera import Camera, CameraState, LumixError, NotReachable
from .stream import LiveStream

log = logging.getLogger("lumixremote.app")

STATIC_DIR = pathlib.Path(__file__).parent / "static"

# Separator in the multipart stream. The header names it without dashes, the
# separator lines in the body carry them; that is what browsers expect.
BOUNDARY = "frame"

# The mode change via recmode takes longer on the device than the five
# seconds httpx uses by default. If it runs into the timeout, the camera does
# not stream and answers the next call with err_busy.
CAMERA_TIMEOUT = 15.0


# ---------------------------------------------------------------------- #
# Configuration
# ---------------------------------------------------------------------- #

@dataclass
class Config:
    """Settings from config.toml, each overridable by an environment variable.

    The environment takes precedence so that a container can run without a
    mounted configuration file.
    """

    camera_ip: str = ""
    device_name: str = "lumix-remote"
    stream_port: int = 49199
    web_host: str = "127.0.0.1"
    web_port: int = 8080

    @classmethod
    def load(cls, path: pathlib.Path | None = None) -> "Config":
        path = path or pathlib.Path(os.environ.get("LUMIX_CONFIG", "config.toml"))
        data: dict = {}
        if path.exists():
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        else:
            # Normal in the container, where the settings come from the
            # environment; "starting" stays the first line.
            log.debug("config_file_missing", extra={"path": str(path)})

        camera = data.get("camera", {})
        stream = data.get("stream", {})
        web = data.get("web", {})

        def port(env: str, value: object) -> int:
            raw = os.environ.get(env, value)
            try:
                number = int(raw)
            except (TypeError, ValueError):
                number = 0
            if not 1 <= number <= 65535:
                raise SystemExit(f"{env} must be a port number from 1 to 65535, got {raw!r}")
            return number

        cfg = cls(
            camera_ip=os.environ.get("LUMIX_CAMERA_IP", camera.get("ip", "")),
            device_name=os.environ.get(
                "LUMIX_DEVICE_NAME", camera.get("device_name", "lumix-remote")
            ),
            stream_port=port("LUMIX_STREAM_PORT", stream.get("port", 49199)),
            web_host=os.environ.get("LUMIX_WEB_HOST", web.get("host", "127.0.0.1")),
            web_port=port("LUMIX_WEB_PORT", web.get("port", 8080)),
        )
        if not cfg.camera_ip:
            raise SystemExit(
                "No camera address configured. Either copy config.example.toml "
                "to config.toml and set ip, or set LUMIX_CAMERA_IP."
            )
        return cfg


# ---------------------------------------------------------------------- #
# Exposure values
# ---------------------------------------------------------------------- #

# Standard aperture series in third stops. The computed value is always
# slightly off (1280/256 gives 5.66, the marking says f/5.6), so it is snapped
# to this series, as the camera does on its own display.
FNUMBER_SCALE = (
    1.0, 1.1, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.5, 2.8, 3.2, 3.5, 4.0, 4.5,
    5.0, 5.6, 6.3, 7.1, 8.0, 9.0, 10.0, 11.0, 13.0, 14.0, 16.0, 18.0, 20.0,
    22.0, 25.0, 29.0, 32.0, 36.0, 40.0, 45.0, 51.0, 57.0, 64.0,
)


def av_to_fnumber(raw: int) -> str:
    """Convert an APEX aperture value into a marked f-number.

    The camera keeps aperture and shutter speed as Av and Tv in steps of
    1/256, with N = 2^(Av/2). Checked on the device: Av 1280 is f/5.6, Av 925
    the f/3.5 of the mounted lens.
    """
    value = 2 ** (raw / 256 / 2)
    snapped = min(FNUMBER_SCALE, key=lambda f: abs(f - value))
    return f"f/{snapped:.1f}" if snapped < 10 else f"f/{snapped:.0f}"


def _camera_reply(message: str) -> str:
    """Extract the camera's bare reply from an error message.

    LumixError carries text ending in `: 'err_busy'`. For the interface only
    the `err_busy` counts.
    """
    if "'" in message:
        return message.rsplit("'", 2)[-2]
    return ""


def lens_anchor(lens_reply: str) -> bytes | None:
    """Build the search pattern the aperture follows in the stream header.

    The reply to `getinfo&type=lens` starts with the lens limits in the same
    1/256 encoding. Exactly these two numbers stand in the datagram header
    right before the current aperture, each followed by 0x0100. The header has
    no fixed length, so it is searched rather than computed, as with the JPEG
    start.
    """
    parts = lens_reply.split(",")
    values: list[int] = []
    for part in parts[1:5]:
        if "/" in part:
            try:
                values.append(int(part.split("/")[0]))
            except ValueError:
                return None
    if len(values) < 2:
        return None
    return (values[0].to_bytes(2, "big") + b"\x01\x00"
            + values[1].to_bytes(2, "big") + b"\x01\x00")


def aperture_from_header(header: bytes, anchor: bytes) -> str:
    """Read the current aperture from the stream header. Empty if unclear."""
    if not header or not anchor:
        return ""
    pos = header.find(anchor)
    if pos < 0 or pos + 10 > len(header):
        return ""
    return av_to_fnumber(int.from_bytes(header[pos + 8:pos + 10], "big"))


def iso_values_from_menu(allmenu: str) -> list[str]:
    """Extract the allowed ISO values from the camera's menu definition.

    The camera supplies the list itself, so the interface has nothing to guess
    and other models are covered too. Duplicates occur: the same sensitivity
    appears in several menu contexts.
    """
    try:
        root = ET.fromstring(allmenu)
    except ET.ParseError:
        return []
    seen: dict[str, None] = {}
    for item in root.iter("item"):
        if item.get("cmd_type") == "iso" and item.get("cmd_mode") == "setsetting":
            value = item.get("cmd_value")
            if value:
                seen.setdefault(value, None)
    return list(seen)


# ---------------------------------------------------------------------- #
# Connection keeper
# ---------------------------------------------------------------------- #

class Controller:
    """Owns the single camera session and keeps it alive.

    Everything that talks to the camera happens either in this thread or under
    the camera's own lock, so command routes and keepalive cannot interleave.
    """

    TICK = 1.0              # base tick of the loop
    POLL_INTERVAL = 5.0     # getstate as keepalive
    RETRY_DELAY = 5.0       # wait after a failed connection attempt
    CHURN_WINDOW = 45.0     # sessions held this briefly count as a dispute
    CHURN_ALARM = 2         # warn after this many in a row
    RETRY_MAX = 60.0        # upper limit of the growing wait
    # Minimum gap between two startstream calls, and the grace period after
    # the first. Measured on the device: a second call three seconds after the
    # first is answered with err_busy and takes the session down with it. The
    # camera needs this time to start up.
    RESTART_COOLDOWN = 8.0

    # Settling after the registration. The camera drops into playback mode on
    # its own (measured at 2.3 s), and recmode brings it back. How long that
    # takes varies a lot: once it was ready after 3.3 s, another time recmode
    # was only acknowledged after more than five seconds. A fixed wait never
    # fits, so the code waits for the state, not for the clock.
    SETTLE_POLL = 0.5       # gap between state polls while settling
    SETTLE_STABLE = 2.0     # 'rec' must hold this long without a break
    SETTLE_TIMEOUT = 40.0   # after this, try anyway
    SETTLE_RETRY = 8.0      # gap between two recmode attempts


    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.camera = Camera(cfg.camera_ip, device_name=cfg.device_name,
                             timeout=CAMERA_TIMEOUT)
        self.stream = LiveStream(port=cfg.stream_port)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._state = CameraState()
        self._status = "connecting"
        self._error = ""
        self._cammode = ""
        self._restarts = 0
        self._last_poll = 0.0
        self._last_restart = 0.0
        self._streaming = False
        self._recmode_tried = False
        self._recmode_error = ""
        self._kick_suppressed = False
        self._registered_at = 0.0
        self._churn = 0
        self._unreachable_since: float | None = None
        self._iso = ""
        self._focusmode = ""
        self._iso_values: list[str] = []
        self._lens_anchor = b""
        # Last known session, so the shutdown can still send stopstream when
        # the connection counts as lost.
        self._stale_session: str | None = None

    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="lumix-controller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ordered shutdown. Must run on every path that ends the process.

        Without `stopstream` the camera keeps the remote session occupied and
        answers every later command with err_busy until it is power cycled.
        """
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        # A session marked as lost is released too. Seen on the device: after
        # an err_busy, session_id was None and stopstream was skipped, which
        # is exactly when the camera needs it.
        if not self.camera.connected and self._stale_session:
            self.camera.session_id = self._stale_session
        try:
            if self.camera.connected:
                log.info("session_releasing")
                self.camera.stop_stream()
        finally:
            self.stream.stop()
            self.camera.close()

    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.camera.connected:
                    self._maintain()
                    self._stop.wait(self.TICK)
                else:
                    self._establish()
            except NotReachable as exc:
                # Network briefly gone; the session stays.
                self._endure(exc)
            except LumixError as exc:
                # The camera answered and refused. That is different; only a
                # fresh attempt helps.
                self._fail(exc)
                self._stop.wait(self._backoff())
            except Exception:
                # A bug or an unexpected reply must not end this thread: the
                # app would stay up without ever reconnecting. The session is
                # left alone, as with a dropout, so a recording isn't at risk.
                log.exception("keeper_error")
                self._stop.wait(self.RETRY_DELAY)

    def _endure(self, exc: NotReachable) -> None:
        """Sit out a dropout. The session is never given up over silence.

        Seen on the device: a running recording survives the dropout and only
        dies at the re-registration afterwards. The session stays valid on the
        camera while the network is gone, so it is kept and polling simply
        continues.

        A time limit would gain nothing here either: re-registering is only
        possible when the camera answers. Once it does, the next reply shows
        where things stand: either it accepts the old session, or it refuses
        and the other branch takes over.
        """
        if not self.camera.connected:
            # No session to protect yet: the ordinary connection attempt
            # against a silent camera.
            self._set(status="offline", error=str(exc))
            self._stop.wait(self.RETRY_DELAY)
            return

        if self._unreachable_since is None:
            self._unreachable_since = time.monotonic()
            log.info("camera_unreachable", extra={"error": str(exc)})

        self._stop.wait(self.TICK)

    def _backoff(self) -> float:
        """Wait before the next attempt; grows with repeated losses."""
        return min(self.RETRY_DELAY * (2 ** self._churn), self.RETRY_MAX)

    def _establish(self) -> None:
        """Register only. Whether frames may flow is decided by _maintain."""
        # The last error stays until the attempt succeeds; otherwise the
        # reason would flicker away with every retry.
        self._set(status="connecting")
        self.camera.describe()
        # connect() waits for the confirmation on the camera's display. On
        # first contact that can take a while, afterwards it goes through.
        self.camera.connect()
        self._streaming = False
        self._registered_at = time.monotonic()
        log.info("registered")

        # A stream from a lost session keeps running on the camera and makes
        # the next startstream fail with err_busy; the log showed this after
        # every reconnect. So clean up first.
        self.camera.stop_stream()
        self._read_capabilities()
        self._settle()
        self._last_poll = 0.0  # forces an immediate state poll

    def _settle(self) -> None:
        """Wait until the camera has finished its automatic mode change.

        It drops into playback on its own after the registration. Requesting
        the stream before that gets an acknowledged startstream and still not
        a single datagram. So the code waits until 'rec' has held for a while
        without a break.
        """
        started = time.monotonic()
        deadline = started + self.SETTLE_TIMEOUT
        rec_since: float | None = None
        last_recmode = 0.0

        while time.monotonic() < deadline and not self._stop.is_set():
            state = self.camera.get_state()
            mode = state.raw.get("cammode", "rec")
            now = time.monotonic()

            if state.recording:
                # The camera is recording; nothing is switched here.
                log.info("settle_skipped_recording")
                self._set(cammode=mode, status="online", error="")
                return

            if mode == "rec":
                if rec_since is None:
                    rec_since = now
                elif now - rec_since >= self.SETTLE_STABLE:
                    log.info("camera_ready", extra={"after_s": round(now - started, 1)})
                    self._set(cammode=mode, status="online", error="")
                    return
            else:
                rec_since = None
                self._set(cammode=mode, status="standby")
                if now - last_recmode >= self.SETTLE_RETRY:
                    last_recmode = now
                    try:
                        self.camera.set_rec_mode()
                        log.info("recmode_sent", extra={"cammode": mode})
                    except LumixError as exc:
                        # err_critical here only means "already in record mode"
                        log.info("recmode_refused", extra={"error": str(exc)})

            self._stop.wait(self.SETTLE_POLL)

        log.warning("camera_not_ready", extra={"after_s": self.SETTLE_TIMEOUT})

    def _read_capabilities(self) -> None:
        """Fetch the lens limits and the ISO list, once per session each.

        The values come from the camera, so the interface has nothing to guess
        and other models are covered too. If this fails, the rest still runs;
        only the selection fields are missing then.
        """
        if not self._lens_anchor:
            try:
                self._lens_anchor = lens_anchor(self.camera.get_info("lens")) or b""
            except LumixError as exc:
                log.info("lens_unreadable", extra={"error": str(exc)})
        if not self._iso_values:
            # allmenu is about 380 KB and doesn't change, so a reconnect
            # doesn't pull it over the WLAN again every time.
            try:
                values = iso_values_from_menu(self.camera.get_info("allmenu"))
                if values:
                    self._iso_values = values
                    log.info("iso_values", extra={"count": len(values)})
            except LumixError as exc:
                log.info("menu_unreadable", extra={"error": str(exc)})

    def _maintain(self) -> None:
        now = time.monotonic()

        if now - self._last_poll >= self.POLL_INTERVAL:
            self._last_poll = now
            self._read_state()

        # In playback mode the camera has no live view to send. A startstream
        # is acknowledged but stays silent.
        if self._cammode != "rec":
            return

        if not self._streaming:
            # The first attempt keeps the minimum gap too; otherwise a refused
            # startstream would knock again every second.
            if now - self._last_restart >= self.RESTART_COOLDOWN:
                self._begin_stream()
            return

        if not (self.stream.stalled
                and now - self._last_restart >= self.RESTART_COOLDOWN):
            return

        if self._recording():
            # While recording, the stream is NOT kicked. Reproduced on the
            # device: the kick was refused with err_busy, that counted as a
            # session loss, the code re-registered, and the re-registration
            # aborts the recording. A refusal no longer costs the session, but
            # not knocking at all is the safer way. A missing preview costs
            # less than the footage.
            if not self._kick_suppressed:
                self._kick_suppressed = True
                log.warning("stream_stalled_recording")
            return

        self._kick_suppressed = False
        self._registered_at = 0.0
        self._churn = 0
        self._last_restart = now
        self._restarts += 1
        log.warning("stream_kick", extra={"count": self._restarts})
        try:
            self.camera.start_stream(self.cfg.stream_port)
        except LumixError as exc:
            # A refused kick is NOT a session loss. The camera answered, so
            # the session lives; err_busy only means "busy right now". Seen on
            # the device: treating it as a loss re-registers and aborts the
            # running recording. Only the getstate keepalive decides whether
            # the session is really gone.
            log.warning("stream_kick_refused", extra={"error": str(exc)})

    def _recording(self) -> bool:
        """Is the camera recording, according to the last state read?"""
        with self._lock:
            return self._state.recording

    def _read_state(self) -> None:
        """Poll the camera and decide whether it can stream at all."""
        state = self.camera.get_state()
        # It answered, so a running dropout is over.
        self._unreachable_since = None
        # If a firmware reports no cammode, assume it is ready to record.
        mode = state.raw.get("cammode", "rec")
        self._set(state=state, cammode=mode)

        if mode == "rec":
            self._recmode_tried = False
            self._recmode_error = ""
            for name in ("iso", "focusmode"):
                try:
                    self._set(**{name: self.camera.get_setting(name)})
                except LumixError as exc:
                    log.debug("setting_unreadable", extra={"setting": name, "error": str(exc)})
            self._set(status="online", error="")
            return

        self._streaming = False
        if state.recording:
            # A mode change while recording would abort it. Better to run on
            # without live view than to lose the footage.
            self._set(status="standby",
                      error="Recording, but the camera reports "
                            f"'{mode}'. No mode change while it "
                            "records")
            return

        if not self._recmode_tried:
            # Try once, then leave it: on the DC-GH6 with firmware 3.01,
            # recmode from playback is refused with err_critical, and retries
            # only drive it into err_busy.
            self._recmode_tried = True
            try:
                self.camera.set_rec_mode()
                # The change takes effect at once. Check right away instead
                # of wasting a whole poll interval.
                self._last_poll = 0.0
                self._recmode_error = ""
                log.info("recmode_sent", extra={"cammode": mode})
            except LumixError as exc:
                self._recmode_error = _camera_reply(str(exc))
                log.info("recmode_refused", extra={"error": str(exc)})

        reason = f"Camera is in '{mode}' instead of record mode"
        if self._recmode_error == "err_busy":
            # Measured: err_busy doesn't clear by itself. Say so instead of
            # leaving the user guessing.
            reason += ". The mode change is refused with err_busy: switch the "
            reason += "camera off and on again"
        elif self._recmode_error:
            reason += f". Mode change refused: {self._recmode_error}"
        self._set(status="standby", error=reason)

    def _begin_stream(self) -> None:
        # The receiver must listen before the camera starts sending.
        self.stream.start()
        time.sleep(0.3)
        self._last_restart = time.monotonic()
        try:
            self.camera.start_stream(self.cfg.stream_port)
        except LumixError as exc:
            # Here too: refused is not lost. The next attempt comes after
            # RESTART_COOLDOWN, the session stays.
            log.warning("startstream_refused", extra={"error": str(exc)})
            return
        self._streaming = True
        log.info("stream_requested", extra={"port": self.cfg.stream_port})

    def _fail(self, exc: LumixError) -> None:
        """Drop the session so the next pass re-registers from scratch.

        If the session keeps dropping shortly after it was set up, something
        else is fighting over the same camera. It holds exactly one remote
        session; a second application takes it over and invalidates ours,
        after which getstate answers with err_critical. Quick retries don't
        help then, they only keep the dispute going, so the wait grows and the
        message says plainly what is happening.
        """
        held = time.monotonic() - self._registered_at if self._registered_at else 0.0
        if self._registered_at and held < self.CHURN_WINDOW:
            self._churn += 1
        else:
            self._churn = 0
        self._registered_at = 0.0

        if self._churn >= self.CHURN_ALARM:
            log.warning("session_churn", extra={
                "count": self._churn + 1, "held_s": round(held),
                "hint": "the camera holds only one remote session: is a second "
                        "instance or the LUMIX app running against it?",
            })
        log.warning("connection_lost", extra={"error": str(exc)})
        self._stale_session = self.camera.session_id or self._stale_session
        self.camera.session_id = None
        self._streaming = False

        detail = str(exc)
        if self._churn >= self.CHURN_ALARM:
            detail += (". The session keeps dropping shortly after it was set "
                       "up. The camera holds only one remote session: is a "
                       "second instance or the LUMIX app running against it?")
        self._set(status="offline", error=detail)

    def _set(self, *, state: CameraState | None = None,
             status: str | None = None, error: str | None = None,
             cammode: str | None = None, iso: str | None = None,
             focusmode: str | None = None) -> None:
        with self._lock:
            if state is not None:
                self._state = state
            if status is not None:
                self._status = status
            if error is not None:
                self._error = error
            if cammode is not None:
                self._cammode = cammode
            if iso is not None:
                self._iso = iso
            if focusmode is not None:
                self._focusmode = focusmode

    # ------------------------------------------------------------------ #

    def set_iso(self, value: str) -> str:
        """Set the sensitivity and report back what the camera actually took.

        An acknowledged ok doesn't mean the value applies: measured was ISO
        400, which the camera adopted as 800. So the value is read back and
        the read value is returned.
        """
        if self._iso_values and value not in self._iso_values:
            raise LumixError(f"ISO {value!r} is not available on this camera")
        self.camera.set_setting("iso", value)
        time.sleep(0.4)
        actual = self.camera.get_setting("iso")
        self._set(iso=actual)
        return actual


    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict:
        """Current view of the world, safe to serialise at any time."""
        with self._lock:
            state, status, error = self._state, self._status, self._error
            cammode, iso, iso_values = self._cammode, self._iso, self._iso_values
            focusmode = self._focusmode
            anchor = self._lens_anchor
        stats = self.stream.stats
        return {
            "status": status,
            "error": error,
            "cammode": cammode,
            "iso": iso,
            "iso_values": iso_values,
            "focusmode": focusmode,
            # The aperture is in no query, only in the datagram header. It
            # can therefore be read, but not set.
            "aperture": aperture_from_header(self.stream.latest_header or b"", anchor),
            "recording": state.recording,
            "battery": state.battery,
            "remaining_shots": state.remaining_shots,
            "video_remaining": state.video_remaining,
            "model": self.camera.info.model_number,
            "firmware": self.camera.info.firmware,
            "fps": round(stats.fps, 1),
            "frames": stats.frames,
            "stalled": self.stream.stalled,
            # During a dropout the interface's firewall hint would be wrong,
            # so this case is told apart.
            "unreachable": self._unreachable_since is not None,
            "restarts": self._restarts,
            "stream_port": self.cfg.stream_port,
        }


# ---------------------------------------------------------------------- #
# Web application
# ---------------------------------------------------------------------- #

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: Config = getattr(app.state, "config", None) or Config.load()
    app.state.config = cfg
    controller = Controller(cfg)
    app.state.controller = controller
    controller.start()
    try:
        yield
    finally:
        # Runs on Ctrl+C and on SIGTERM from the container too.
        log.info("shutting_down")
        await run_in_threadpool(controller.stop)


class SecurityHeaders:
    """Headers on every response, the live view included.

    A plain ASGI middleware: Starlette's BaseHTTPMiddleware would get in the
    way of the endless MJPEG response. No sniffing a reply into HTML, no
    framing by other pages (a hidden Record button under someone else's
    page), no paths in the Referer, and nothing cached from /api/.
    """

    HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"content-security-policy", b"frame-ancestors 'none'"),
        (b"referrer-policy", b"same-origin"),
    ]

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        api = scope["path"].startswith("/api/")

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", [])) + self.HEADERS
                if api:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


# No generated API pages: the routes are listed in the README.
app = FastAPI(title="lumix-remote", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SecurityHeaders)
# Stylesheets and fonts of the interface. The fonts are served from here, not
# from a CDN, so no viewer's address goes to a third party.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def same_origin(request: Request) -> None:
    """Refuse commands a browser sends from another web site.

    The app has no login, so without this any page open in the same browser
    could start or stop a recording. Browsers name the source of a request
    (Sec-Fetch-Site, Origin); scripts such as curl send neither and pass.
    """
    site = request.headers.get("sec-fetch-site", "")
    if site:
        allowed = site in ("same-origin", "none")
    else:
        origin = request.headers.get("origin", "")
        hosts = {request.headers.get("host", ""), request.headers.get("x-forwarded-host", "")}
        allowed = not origin or urlsplit(origin).netloc.lower() in {h.lower() for h in hosts if h}
    if not allowed:
        raise HTTPException(status_code=403, detail="Cross-site request refused")


COMMAND = {"dependencies": [Depends(same_origin)]}


def _controller(request: Request) -> Controller:
    controller: Controller | None = getattr(request.app.state, "controller", None)
    if controller is None:
        raise HTTPException(status_code=503, detail="Control not ready")
    return controller


async def _command(fn: Callable[[], None]) -> dict:
    """Run a blocking camera call off the event loop and map its errors."""
    try:
        await run_in_threadpool(fn)
    except LumixError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status(request: Request) -> dict:
    return _controller(request).snapshot()


@app.post("/api/af", **COMMAND)
async def autofocus(request: Request) -> dict:
    return await _command(_controller(request).camera.autofocus)


@app.post("/api/rec/start", **COMMAND)
async def rec_start(request: Request) -> dict:
    return await _command(_controller(request).camera.rec_start)


@app.post("/api/rec/stop", **COMMAND)
async def rec_stop(request: Request) -> dict:
    return await _command(_controller(request).camera.rec_stop)


@app.post("/api/iso", **COMMAND)
async def set_iso(request: Request) -> dict:
    """Set the sensitivity. Reports the value the camera really adopted."""
    controller = _controller(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not JSON") from None
    value = str(payload.get("value", "")).strip() if isinstance(payload, dict) else ""
    if not value:
        raise HTTPException(status_code=400, detail="No ISO value given")
    try:
        actual = await run_in_threadpool(controller.set_iso, value)
    except LumixError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "iso": actual}



@app.get("/stream.mjpg")
async def stream_mjpeg(request: Request) -> StreamingResponse:
    """Hand the received frames on as multipart/x-mixed-replace.

    Waits on the receiver's condition variable instead of polling, so an idle
    viewer costs nothing. Several browsers may watch at the same time.
    """
    controller = _controller(request)

    async def frames() -> AsyncIterator[bytes]:
        while not await request.is_disconnected():
            frame = await run_in_threadpool(controller.stream.wait_for_frame, 5.0)
            if frame is None:
                # The stream stays open: the interface reports the stall via
                # /api/status, and the picture returns by itself once the
                # watchdog has kicked the stream.
                continue
            yield (
                f"--{BOUNDARY}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(frame)}\r\n\r\n"
            ).encode("ascii") + frame + b"\r\n"

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ---------------------------------------------------------------------- #

def main() -> int:
    import uvicorn

    level = os.environ.get("LUMIX_LOG_LEVEL", "INFO").upper()
    logging_setup.configure(level)
    # httpx logs every single request at INFO. During the registration those
    # are dozens of identical lines that bury the camera's actual reply. Only
    # wanted at DEBUG.
    if level != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = Config.load()
    except SystemExit as exc:
        log.error("config_invalid", extra={"reason": str(exc)})
        return 1
    app.state.config = cfg
    # The first line: the version and the settings that matter.
    log.info("starting", extra={
        "version": __version__, "camera": cfg.camera_ip,
        "device_name": cfg.device_name, "stream_port": cfg.stream_port,
        "web": f"http://{cfg.web_host}:{cfg.web_port}", "log_level": level,
    })
    # The MJPEG stream never ends by itself. Without a deadline, uvicorn would
    # wait for every watching browser on shutdown, and a SIGKILL after that
    # leaves the camera's remote session occupied.
    uvicorn.run(
        app,
        host=cfg.web_host,
        port=cfg.web_port,
        log_level="warning",
        # uvicorn's own lines go through the JSON handler too.
        log_config=None,
        timeout_graceful_shutdown=3,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
