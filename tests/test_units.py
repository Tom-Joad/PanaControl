"""Unit tests for the parts that need no camera: the protocol parsing, the
exposure values, the stream statistics, the settings and the web layer's
guards. The device findings behind the expected values are in
docs/protocol.md."""

from __future__ import annotations

import pathlib
import re
import time

import pytest
from fastapi.testclient import TestClient

from lumixremote import app as appmod
from lumixremote.camera import Camera, CameraState, LumixError, _hexencode, _looks_like_session
from lumixremote.stream import LiveStream, StreamStats, extract_jpeg, jpeg_dimensions


# --------------------------------------------------------------- camera ---

def test_session_id_is_hex_not_the_word_open():
    assert _looks_like_session("0123456789abcdef")
    assert not _looks_like_session("open")
    assert not _looks_like_session("")


def test_registration_values_are_hex_encoded():
    assert _hexencode("lumix-remote") == "6c756d69782d72656d6f7465"


# ------------------------------------------------------------- exposure ---

@pytest.mark.parametrize("raw, marked", [(1280, "f/5.6"), (925, "f/3.5"), (2304, "f/22")])
def test_apex_aperture_snaps_to_the_marked_value(raw, marked):
    assert appmod.av_to_fnumber(raw) == marked


LENS = "ok,2304/256,925/256,3755/256,768/256,0,0,lens"


def test_aperture_is_read_after_the_lens_limits_in_the_header():
    anchor = appmod.lens_anchor(LENS)
    assert anchor == bytes.fromhex("09000100039d0100")
    header = b"\x00" * 40 + anchor + bytes.fromhex("05000100") + b"\x00" * 8
    assert appmod.aperture_from_header(header, anchor) == "f/5.6"


def test_aperture_is_empty_when_unclear():
    anchor = appmod.lens_anchor(LENS)
    assert appmod.aperture_from_header(b"\x00" * 64, anchor) == ""
    assert appmod.aperture_from_header(anchor, anchor) == ""   # value cut off
    assert appmod.lens_anchor("ok") is None


def test_iso_values_come_from_the_menu_without_duplicates():
    menu = """<menuset><item cmd_mode="setsetting" cmd_type="iso" cmd_value="auto"/>
        <item cmd_mode="setsetting" cmd_type="iso" cmd_value="200"/>
        <item cmd_mode="setsetting" cmd_type="iso" cmd_value="200"/>
        <item cmd_mode="setsetting" cmd_type="shtrspeed" cmd_value="1/50"/></menuset>"""
    assert appmod.iso_values_from_menu(menu) == ["auto", "200"]
    assert appmod.iso_values_from_menu("not xml") == []


def _camera_with_reply(monkeypatch, body: str | None, status: int = 200):
    """A Camera whose HTTP layer answers with a canned reply (or times out)."""
    import httpx

    def get(url, params=None, headers=None):
        if body is None:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(status, text=body)

    camera = Camera("192.0.2.10")
    camera.session_id = "0123456789abcdef"
    monkeypatch.setattr(camera._client, "get", get)
    return camera


def test_a_refusal_carries_the_cameras_reply(monkeypatch):
    camera = _camera_with_reply(monkeypatch, "<r><result>err_busy</result></r>")
    with pytest.raises(LumixError) as caught:
        camera.set_rec_mode()
    assert caught.value.reply == "err_busy"


@pytest.mark.parametrize("body, status", [(None, 200), ("", 503)])
def test_a_timeout_or_http_error_has_no_camera_reply(monkeypatch, body, status):
    # The request parameters in the message ('recmode') must not pass as a reply.
    camera = _camera_with_reply(monkeypatch, body, status)
    with pytest.raises(LumixError) as caught:
        camera.set_rec_mode()
    assert caught.value.reply == ""
    assert "recmode" in str(caught.value)


# --------------------------------------------------------------- stream ---

def jpeg(width: int, height: int) -> bytes:
    sof = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
    return b"\xff\xd8\xff\xe0\x00\x04ab" + sof + b"\x00" * 12 + b"\xff\xd9"


def test_the_jpeg_start_is_searched_not_computed():
    for header in (276, 324, 340):          # measured in one run
        offset, frame = extract_jpeg(b"\x01" * header + jpeg(640, 360))
        assert offset == header
        assert jpeg_dimensions(frame) == (640, 360)
    assert extract_jpeg(b"no picture") is None


def test_stream_stats_keep_a_sum_not_every_frame():
    stats = StreamStats()
    stats.frames, stats.frame_bytes = 4, 100
    assert stats.avg_frame_size == 25
    assert StreamStats().avg_frame_size == 0


def test_shown_frame_rate_forgets_a_pause():
    stats = StreamStats()
    assert stats.recent_fps == 0
    stats.first_frame_at = 0.0
    for i in range(300):                    # 10 s at 30 fps
        stats.frames += 1
        stats.last_frame_at = i / 30
        stats.mark_frame(i / 30)
    for i in range(300):                    # 40 s without frames, then 10 s more
        stats.frames += 1
        stats.last_frame_at = 50 + i / 30
        stats.mark_frame(50 + i / 30)
    assert round(stats.recent_fps, 1) == 30.0
    assert stats.fps < 11                   # the average still carries the pause
    assert len(stats.recent) <= 30 * 3 + 1


# ------------------------------------------------------------- settings ---

def test_config_takes_the_environment_first(monkeypatch, tmp_path):
    monkeypatch.setenv("LUMIX_CAMERA_IP", "192.0.2.10")
    monkeypatch.setenv("LUMIX_STREAM_PORT", "50000")
    cfg = appmod.Config.load(tmp_path / "missing.toml")
    assert (cfg.camera_ip, cfg.stream_port, cfg.web_port) == ("192.0.2.10", 50000, 8080)


@pytest.mark.parametrize("value", ["abc", "0", "70000"])
def test_a_bad_port_stops_the_start(monkeypatch, tmp_path, value):
    monkeypatch.setenv("LUMIX_CAMERA_IP", "192.0.2.10")
    monkeypatch.setenv("LUMIX_WEB_PORT", value)
    with pytest.raises(SystemExit, match="LUMIX_WEB_PORT"):
        appmod.Config.load(tmp_path / "missing.toml")


def test_no_camera_address_stops_the_start(monkeypatch, tmp_path):
    monkeypatch.delenv("LUMIX_CAMERA_IP", raising=False)
    with pytest.raises(SystemExit):
        appmod.Config.load(tmp_path / "missing.toml")


# ------------------------------------------------------------------ web ---
# Without the lifespan (no "with"), no controller exists: a command that
# passes the guards answers 503, one refused by them 403.

client = TestClient(appmod.app)


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
])
def test_commands_from_other_sites_are_refused(headers):
    assert client.post("/api/rec/start", headers=headers).status_code == 403


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://testserver"},
    {},                                       # curl and scripts
])
def test_own_and_scripted_commands_pass(headers):
    assert client.post("/api/rec/start", headers=headers).status_code == 503


def test_security_headers_and_no_api_pages():
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_the_interface_is_served():
    assert "PanaControl" in client.get("/").text
    assert pathlib.Path(appmod.STATIC_DIR, "css", "tokens.css").is_file()


def test_every_font_the_stylesheet_names_is_served():
    css = client.get("/static/css/tokens.css").text
    fonts = re.findall(r'url\("(fonts/[^"]+)"\)', css)
    # Latin and Latin Extended for both families.
    assert len(fonts) == 4
    for font in fonts:
        r = client.get("/static/css/" + font)
        assert r.status_code == 200, font
        assert r.content[:4] == b"wOF2", font


# -------------------------------------------------------------- logging ---

def _format(msg: str, exc_info=None, **extra) -> dict:
    import json
    import logging
    from lumixremote.logging_setup import JsonFormatter
    record = logging.LogRecord("lumixremote.app", logging.INFO, __file__, 1, msg, None, exc_info)
    record.__dict__.update(extra)
    return json.loads(JsonFormatter().format(record))


def test_log_lines_are_json_with_event_and_fields():
    line = _format("connected", model="DC-GH6", attempts=1)
    assert line["level"] == "INFO" and line["event"] == "connected"
    assert line["model"] == "DC-GH6" and line["attempts"] == 1
    assert line["ts"][:2] == "20" and "T" in line["ts"]


def test_an_exception_adds_only_its_last_line():
    import sys
    try:
        raise ValueError("unreadable descriptor")
    except ValueError:
        line = _format("keeper_error", exc_info=sys.exc_info())
    assert line["error"] == "ValueError: unreadable descriptor"


# ----------------------------------------------------------- host names ---

@pytest.mark.parametrize("host", ["attacker.example", "attacker.example:8080", "192.0.2.10.nip.io"])
def test_foreign_host_names_are_refused(host):
    # DNS rebinding: same-origin as far as the browser knows, but not a name of ours.
    assert client.get("/api/status", headers={"Host": host}).status_code == 403
    assert client.post("/api/rec/start", headers={"Host": host, "Sec-Fetch-Site": "same-origin"}).status_code == 403


@pytest.mark.parametrize("host", ["192.0.2.10:8080", "[2001:db8::1]:8080", "tower:8080", "tower.local", "localhost"])
def test_lan_host_names_pass(host):
    assert client.get("/", headers={"Host": host}).status_code == 200


def test_allowed_hosts_come_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LUMIX_CAMERA_IP", "192.0.2.10")
    monkeypatch.setenv("LUMIX_ALLOWED_HOSTS", "Cam.Example.com, other.example.com")
    cfg = appmod.Config.load(tmp_path / "missing.toml")
    assert cfg.allowed_hosts == ("cam.example.com", "other.example.com")
    assert appmod.host_allowed("cam.example.com:443", cfg.allowed_hosts)
    assert not appmod.host_allowed("third.example.com", cfg.allowed_hosts)


def _config(monkeypatch, tmp_path, toml: str | None = None, env: str | None = None):
    monkeypatch.setenv("LUMIX_CAMERA_IP", "192.0.2.10")
    monkeypatch.delenv("LUMIX_ALLOWED_HOSTS", raising=False)
    if env is not None:
        monkeypatch.setenv("LUMIX_ALLOWED_HOSTS", env)
    path = tmp_path / "config.toml"
    if toml is not None:
        path.write_text(toml)
    return appmod.Config.load(path)


def test_allowed_hosts_as_a_string_is_one_name(monkeypatch, tmp_path):
    cfg = _config(monkeypatch, tmp_path, '[web]\nallowed_hosts = "Cam.Example.com"\n')
    assert cfg.allowed_hosts == ("cam.example.com",)


def test_allowed_hosts_lose_their_port(monkeypatch, tmp_path):
    cfg = _config(monkeypatch, tmp_path, env="cam.example.com:8443, [::1]:80")
    assert cfg.allowed_hosts == ("cam.example.com", "::1")
    assert appmod.host_allowed("cam.example.com:8443", cfg.allowed_hosts)
    cfg = _config(monkeypatch, tmp_path, '[web]\nallowed_hosts = ["cam.example.com:8443"]\n')
    assert cfg.allowed_hosts == ("cam.example.com",)


@pytest.mark.parametrize("value", ["5", "[1, 2]", '{a = "b"}'])
def test_allowed_hosts_of_another_type_stop_the_start(monkeypatch, tmp_path, value):
    with pytest.raises(SystemExit, match="allowed_hosts"):
        _config(monkeypatch, tmp_path, f"[web]\nallowed_hosts = {value}\n")


# ------------------------------------------------------------- receiver ---

def test_frames_from_another_sender_are_dropped():
    import socket as so

    stream = LiveStream(port=0, source="192.0.2.10")
    stream.start()
    try:
        port = stream._sock.getsockname()[1]
        with so.socket(so.AF_INET, so.SOCK_DGRAM) as sender:
            sender.sendto(b"HDR" + b"\xff\xd8\xff" + b"fake" + b"\xff\xd9", ("127.0.0.1", port))
        deadline = time.monotonic() + 2
        while stream.stats.foreign == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stream.stop()
    assert stream.stats.foreign == 1
    assert stream.stats.frames == 0
    assert stream.latest_frame is None


def test_an_unresolved_camera_name_lets_nothing_pass_until_it_resolves(monkeypatch):
    import socket as so
    from lumixremote import stream as streammod

    answers = {"ip": None}

    def lookup(name):
        if answers["ip"] is None:
            raise so.gaierror("no such name")
        return answers["ip"]

    monkeypatch.setattr(streammod.socket, "gethostbyname", lookup)
    monkeypatch.setattr(streammod, "SOURCE_RETRY", 0.0)
    stream = LiveStream(port=0, source="cam.example.com")
    stream.start()
    try:
        port = stream._sock.getsockname()[1]
        packet = b"HDR" + b"\xff\xd8\xff" + b"fake" + b"\xff\xd9"
        with so.socket(so.AF_INET, so.SOCK_DGRAM) as sender:
            sender.sendto(packet, ("127.0.0.1", port))
            deadline = time.monotonic() + 2
            while stream.stats.foreign == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert stream.stats.foreign == 1 and stream.stats.frames == 0
            # The name resolves later (a new registration): frames pass.
            answers["ip"] = "127.0.0.1"
            stream.start()
            sender.sendto(packet, ("127.0.0.1", port))
            deadline = time.monotonic() + 2
            while stream.stats.frames == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
    finally:
        stream.stop()
    assert stream.stats.frames == 1


# ------------------------------------------------------- resume recording ---

class FakeCamera:
    def __init__(self):
        self.calls: list[str] = []
        self.session_id = None

    def rec_start(self):
        self.calls.append("rec_start")

    def rec_stop(self):
        self.calls.append("rec_stop")


@pytest.fixture
def keeper(monkeypatch):
    """A controller at second 1000, registered just now, with a fake camera."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(appmod.time, "monotonic", lambda: clock["now"])
    controller = appmod.Controller(appmod.Config(camera_ip="192.0.2.10"))
    controller.camera = FakeCamera()
    controller._connected_at = clock["now"]
    # The course of a lost session: a recording was running, the camera was
    # registered again just now.
    controller._resume_armed = True

    def poll(after: float, recording: bool, mode: str = "rec") -> list[str]:
        clock["now"] += after
        controller._follow_recording(recording, mode)
        return controller.camera.calls

    poll.clock = clock
    return controller, poll


def test_a_recording_ended_by_the_registration_is_started_again(keeper):
    # The course from the device log: still recording right after the
    # registration, off five seconds later, then playback, recmode, record.
    _, poll = keeper
    poll(0.5, recording=True)
    assert poll(5, recording=False) == []
    assert poll(5, recording=False, mode="play") == []
    assert poll(1, recording=False) == []
    assert poll(5, recording=False) == ["rec_start"]


def test_a_recording_ending_later_stays_stopped(keeper):
    controller, poll = keeper
    poll(0.5, recording=True)
    poll(600, recording=False)
    assert poll(5, recording=False) == []
    assert controller._rec_wanted is False


def test_the_start_waits_for_the_drop_into_playback(keeper):
    # 17:38 on the device: off with 'rec' still reported, a start a few
    # seconds later was refused with err_critical, then came 'play'.
    _, poll = keeper
    poll(0.5, recording=True)
    poll(5, recording=False)
    assert poll(5, recording=False) == []
    poll(1, recording=False, mode="play")
    poll(1, recording=False)
    assert poll(5, recording=False) == ["rec_start"]


def test_without_a_drop_the_start_comes_later(keeper):
    _, poll = keeper
    poll(0.5, recording=True)
    poll(5, recording=False)
    for _ in range(3):
        assert poll(5, recording=False) == []
    assert poll(5, recording=False) == ["rec_start"]


def test_a_stop_through_the_app_is_not_undone(keeper):
    controller, poll = keeper
    poll(0.5, recording=True)
    controller.rec_stop()
    poll(5, recording=False)
    assert poll(5, recording=False) == ["rec_stop"]


def test_nothing_is_started_that_was_not_recording(keeper):
    _, poll = keeper
    poll(0.5, recording=False)
    assert poll(5, recording=False) == []


def test_resuming_gives_up_after_a_few_tries(keeper):
    _, poll = keeper
    poll(0.5, recording=True)
    poll(5, recording=False, mode="play")
    for _ in range(10):
        poll(5, recording=False)
    assert poll(5, recording=False) == ["rec_start"] * appmod.Controller.RESUME_TRIES


def test_a_recording_started_after_the_registration_is_never_resumed(keeper):
    # Registration at 0 s, recording started in the app at 15 s, stopped on
    # the camera body at 50 s (the camera stays in 'rec').
    controller, poll = keeper
    controller._resume_armed = False
    poll(15, recording=True)
    poll(35, recording=True)
    for _ in range(10):
        assert poll(5, recording=False) == []
    assert controller._rec_wanted is False


def test_a_start_through_the_app_disarms_the_resume(keeper):
    controller, poll = keeper
    controller.rec_start()
    poll(5, recording=False)
    assert poll(30, recording=False) == ["rec_start"]   # the app's own start only


def test_a_loss_while_recording_arms_the_resume(keeper):
    controller, _ = keeper
    controller._resume_armed = False
    controller._rec_wanted = True
    controller._fail(LumixError("lost"))
    assert controller._resume_armed is True


def _register(controller):
    controller.camera.connect = lambda: None
    controller.camera.describe = lambda: None
    controller.camera.stop_stream = lambda: None
    controller._read_capabilities = lambda: None
    controller._settle = lambda: None
    controller._establish()


def test_a_recording_running_at_the_first_poll_arms_the_resume(keeper):
    # The app was restarted while the camera recorded: no session was lost
    # in this process, but the registration ends the recording all the same.
    controller, poll = keeper
    controller._resume_armed = False
    _register(controller)
    poll(0.5, recording=True)
    assert controller._resume_armed is True
    assert poll(5, recording=False) == []
    assert poll(5, recording=False, mode="play") == []
    assert poll(1, recording=False) == []
    assert poll(5, recording=False) == ["rec_start"]


def test_a_recording_seen_after_the_first_poll_arms_nothing(keeper):
    controller, poll = keeper
    controller._resume_armed = False
    _register(controller)
    poll(0.5, recording=False)
    poll(5, recording=True)
    assert controller._resume_armed is False


def test_a_loss_without_a_recording_arms_nothing(keeper):
    controller, _ = keeper
    controller._resume_armed = False
    controller._fail(LumixError("lost"))
    assert controller._resume_armed is False


class PollingCamera(FakeCamera):
    """Answers getstate with a script; a hook can run while the poll is 'in flight'."""

    def __init__(self):
        super().__init__()
        self.replies: list[dict[str, str]] = []
        self.in_flight = None

    def get_state(self):
        reply = self.replies.pop(0)
        if self.in_flight:
            self.in_flight()
        return CameraState(raw=reply)

    def get_setting(self, name):
        return ""


def test_a_poll_in_flight_during_a_stop_does_not_undo_it(keeper):
    controller, poll = keeper
    camera = controller.camera = PollingCamera()
    poll(5, recording=True)
    # rec_stop() runs while the next poll waits for its reply, which still
    # says 'rec=on'.
    camera.in_flight = controller.rec_stop
    camera.replies = [{"rec": "on"}]
    controller._read_state()
    camera.in_flight = None
    assert controller._rec_wanted is False
    camera.replies = [{"rec": "off"}]
    controller._read_state()
    poll(30, recording=False)
    assert camera.calls == ["rec_stop"]


def test_resume_timing_does_not_carry_over_to_a_new_registration(keeper):
    # A recording lost in one registration, no success, session lost again:
    # the next registration waits for the camera's own drop like the first.
    controller, poll = keeper
    poll(0.5, recording=True)
    poll(5, recording=False, mode="play")      # the drop is seen
    assert controller._rec_dropped is True
    controller.camera = FakeCamera()
    _register(controller)
    assert controller._rec_off_since is None
    assert controller._rec_dropped is False
    # 'rec' without a drop: nothing is sent after the short settle time.
    poll(0.5, recording=True)
    assert poll(6, recording=False) == []


def test_status_answers_head_and_names_the_version():
    from lumixremote import __version__

    app = appmod.app
    app.state.controller = appmod.Controller(appmod.Config(camera_ip="192.0.2.10"))
    try:
        c = TestClient(app)
        assert c.get("/api/status").json()["version"] == __version__
        head = c.head("/api/status")
        assert head.status_code == 200
    finally:
        del app.state.controller


# ------------------------------------------- outage limit for the resume ---

def _lose_and_register_again(controller, poll, outage: float):
    """A recording runs, the camera falls silent for `outage` seconds, the
    session is refused afterwards and the app registers again."""
    camera = controller.camera = PollingCamera()
    controller._resume_armed = False
    camera.replies = [{"rec": "on"}]
    controller._read_state()
    poll.clock["now"] += outage
    controller._fail(LumixError("session refused"))
    _register(controller)
    return camera


def test_a_recording_is_resumed_after_a_short_outage(keeper):
    controller, poll = keeper
    camera = _lose_and_register_again(controller, poll, outage=120)
    poll(0.5, recording=True)
    poll(5, recording=False)
    poll(5, recording=False)
    assert poll(30, recording=False) == ["rec_start"]


def test_a_recording_is_not_started_after_a_long_outage(keeper):
    # The camera was switched off while recording and on again hours later:
    # nobody wants a recording to start by itself then.
    controller, poll = keeper
    outage = appmod.Controller.RESUME_MAX_OUTAGE + 60
    _lose_and_register_again(controller, poll, outage=outage)
    assert controller._resume_armed is False
    for _ in range(12):
        assert poll(5, recording=False) == []
    assert controller._rec_wanted is False


def test_a_recording_stopped_while_the_camera_answered_is_not_started(keeper):
    # Stopped through the LUMIX app: the next poll shows 'off' while the
    # session still holds, then the session is taken over.
    controller, poll = keeper
    camera = controller.camera = PollingCamera()
    controller._resume_armed = False
    camera.replies = [{"rec": "on"}, {"rec": "off"}]
    controller._read_state()
    poll.clock["now"] += 5
    controller._read_state()
    controller._fail(LumixError("session refused"))
    _register(controller)
    for _ in range(12):
        assert poll(5, recording=False) == []
    assert camera.calls == []


# ----------------------------------------------------- many live viewers ---

class StreamOnlyController:
    """Just enough of a Controller for the stream and a stop command."""

    def __init__(self):
        self.stream = LiveStream(port=0)
        self.stopped = 0

    def rec_stop(self):
        self.stopped += 1


@pytest.fixture
def live_server():
    """The app on a real socket, with a stand-in controller (no lifespan)."""
    import socket
    import threading
    import uvicorn

    controller = StreamOnlyController()
    appmod.app.state.controller = controller
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        appmod.app, host="127.0.0.1", port=port, log_level="error",
        lifespan="off", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    yield controller, port
    server.should_exit = True
    thread.join(timeout=5)
    del appmod.app.state.controller


def _open_viewers(port: int, count: int):
    import socket
    viewers = []
    for _ in range(count):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(b"GET /stream.mjpg HTTP/1.1\r\nHost: localhost\r\n\r\n")
        viewers.append(sock)
    return viewers


def _post(port: int, path: str, timeout: float) -> int:
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", path)
        return conn.getresponse().status
    finally:
        conn.close()


def test_many_viewers_without_frames_do_not_block_commands(live_server):
    # 60 viewers waiting for a first frame used to take every worker thread
    # (one per viewer); the stop command and the page then waited.
    controller, port = live_server
    viewers = _open_viewers(port, 60)
    try:
        time.sleep(0.5)
        started = time.monotonic()
        assert _post(port, "/api/rec/stop", timeout=5) == 200
        assert time.monotonic() - started < 2
        assert controller.stopped == 1
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        assert conn.getresponse().status == 200
    finally:
        for sock in viewers:
            sock.close()


def test_every_viewer_gets_the_frames(live_server):
    controller, port = live_server
    viewers = _open_viewers(port, 3)
    try:
        time.sleep(0.5)
        controller.stream.publish(jpeg(64, 48), b"")
        for sock in viewers:
            sock.settimeout(5)
            data = b""
            while b"\xff\xd9" not in data:
                data += sock.recv(65536)
            assert b"multipart/x-mixed-replace" in data
            assert b"Content-Type: image/jpeg" in data
    finally:
        for sock in viewers:
            sock.close()


# ----------------------------------------------- no environment in errors ---

@pytest.mark.parametrize("body, status", [(None, 200), ("<r><result>err_busy</result></r>", 200),
                                          ("not xml", 200), ("", 503)])
def test_the_registration_values_stay_out_of_errors(monkeypatch, body, status):
    camera = _camera_with_reply(monkeypatch, body, status)
    with pytest.raises(LumixError) as caught:
        camera._get_xml(mode="accctrl", type="req_acc_e", value="c0ffee11", value2="beef2222")
    assert "c0ffee11" not in str(caught.value)
    assert "beef2222" not in str(caught.value)
    assert "req_acc_e" in str(caught.value)


def test_the_device_name_stays_out_of_errors(monkeypatch):
    camera = _camera_with_reply(monkeypatch, None)
    with pytest.raises(LumixError) as caught:
        camera._get_xml(mode="setsetting", type="device_name", value="Personal Name")
    assert "Personal Name" not in str(caught.value)


def test_other_requests_stay_readable_in_errors(monkeypatch):
    camera = _camera_with_reply(monkeypatch, None)
    with pytest.raises(LumixError) as caught:
        camera.set_setting("iso", "800")
    assert "800" in str(caught.value)


# ---------------------------------------------------------- bad settings ---

def _load(monkeypatch, tmp_path, text: str | None, **env):
    for name in ("LUMIX_CAMERA_IP", "LUMIX_DEVICE_NAME", "LUMIX_WEB_HOST", "LUMIX_STREAM_PORT",
                 "LUMIX_WEB_PORT", "LUMIX_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / "config.toml"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    return appmod.Config.load(path)


@pytest.mark.parametrize("text", [
    "[camera\nip = ",                       # not TOML
    'camera = "192.0.2.10"',                # a table was expected
    "[camera]\nip = 5",                     # text was expected
    '[camera]\nip = "192.0.2.10"\ndevice_name = 7',
    '[camera]\nip = "192.0.2.10"\n[web]\nhost = ["a"]',
    '[camera]\nip = "192.0.2.10"\n[stream]\nport = true',
])
def test_invalid_settings_stop_the_start_without_a_traceback(monkeypatch, tmp_path, text):
    with pytest.raises(SystemExit) as caught:
        _load(monkeypatch, tmp_path, text)
    assert isinstance(caught.value.code, str) and caught.value.code


def test_invalid_settings_are_logged_as_config_invalid(monkeypatch, tmp_path, capsys):
    import logging
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    monkeypatch.setattr(appmod.logging_setup, "configure", lambda level: logging.basicConfig(
        handlers=[logging.StreamHandler(__import__("sys").stderr)], force=True))
    monkeypatch.setenv("LUMIX_CONFIG", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text("[camera\n", encoding="utf-8")
    monkeypatch.delenv("LUMIX_CAMERA_IP", raising=False)
    try:
        assert appmod.main() == 1
    finally:
        root.handlers[:], root.level = saved
    assert "config_invalid" in capsys.readouterr().err


def test_valid_settings_still_load(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, tmp_path, '[camera]\nip = "192.0.2.10"\n[web]\nhost = "0.0.0.0"')
    assert (cfg.camera_ip, cfg.web_host) == ("192.0.2.10", "0.0.0.0")


# ------------------------------------------------------------ UDP health ---

def test_an_occupied_stream_port_is_reported(caplog):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as taken:
        # Without SO_REUSEADDR on this socket, the receiver cannot share it.
        taken.bind(("0.0.0.0", 0))
        stream = LiveStream(port=taken.getsockname()[1])
        with pytest.raises(OSError):
            stream.start()
    assert str(stream.port) in stream.error
    assert not stream.running


def test_a_dying_receiver_says_so_and_can_start_again():
    import socket
    stream = LiveStream(port=0)
    # A free port number for the receiver.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as free:
        free.bind(("0.0.0.0", 0))
        stream.port = free.getsockname()[1]
    stream.start()
    try:
        assert stream.running and stream.error == ""
        stream._sock.close()                      # the socket breaks under it
        deadline = time.monotonic() + 3
        while stream.running and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not stream.running
        assert "UDP receiver stopped" in stream.error
        stream.start()                             # the keeper starts it again
        assert stream.running and stream.error == ""
    finally:
        stream.stop()


def test_health_follows_the_receiver():
    controller = StreamOnlyController()
    appmod.app.state.controller = controller
    try:
        ok = client.get("/api/health")
        assert ok.status_code == 200 and ok.json()["ok"] is True
        controller.stream.error = "cannot listen on UDP port 49199"
        bad = client.get("/api/health")
        assert bad.status_code == 503 and "49199" in bad.json()["error"]
        assert client.head("/api/health").status_code == 503
    finally:
        del appmod.app.state.controller


def test_health_is_not_ready_without_a_controller():
    assert client.get("/api/health").status_code == 503


# ---------------------------------------------------------------- caching ---

def test_the_page_and_its_files_are_revalidated():
    for path in ("/", "/static/css/tokens.css", "/static/app/manifest.json"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
    assert client.get("/static/css/tokens.css").headers.get("etag")
    assert client.get("/api/status").headers["cache-control"] == "no-store"


# ---------------------------------------------------- the controller loop ---

class ScriptCamera:
    """A camera that answers getstate from a script: a CameraState, or an
    exception to raise. Everything else works at once."""

    class Info:
        model_number = "DC-TEST"
        firmware = "0.0"

    def __init__(self, script=()):
        self.script = list(script)
        self.session_id = None
        self.info = self.Info()
        self.connects = 0
        self.polls = 0
        self.calls: list[str] = []
        self.settings = {"iso": "200", "focusmode": "AFS"}
        self.closed = False

    @property
    def connected(self):
        return self.session_id is not None

    def describe(self):
        return self.info

    def connect(self):
        self.connects += 1
        self.session_id = f"0123456789abcd{self.connects:02d}"
        return self.session_id

    def get_state(self):
        self.polls += 1
        step = self.script.pop(0) if self.script else CameraState(raw={"cammode": "rec"})
        if isinstance(step, Exception):
            raise step
        return step

    def get_info(self, name):
        return ""

    def get_setting(self, name):
        return self.settings.get(name, "")

    def set_setting(self, name, value):
        self.settings[name] = value

    def set_rec_mode(self):
        self.calls.append("set_rec_mode")

    def start_stream(self, port):
        self.calls.append("start_stream")

    def stop_stream(self):
        self.calls.append("stop_stream")

    def rec_start(self):
        self.calls.append("rec_start")

    def rec_stop(self):
        self.calls.append("rec_stop")

    def close(self):
        self.closed = True


class QuietStream:
    """Stands in for the UDP receiver: no socket, no frames."""

    error = ""
    running = True
    stalled = False
    latest_header = None

    class stats:
        recent_fps = 0.0
        frames = 0

    def start(self): pass
    def stop(self): pass


@pytest.fixture
def running_controller(monkeypatch):
    """A controller whose loop runs fast against a ScriptCamera."""
    for name, value in (("TICK", 0.01), ("POLL_INTERVAL", 0.0), ("RETRY_DELAY", 0.01),
                        ("RETRY_MAX", 0.02), ("SETTLE_POLL", 0.01), ("SETTLE_STABLE", 0.02),
                        ("RESTART_COOLDOWN", 0.0)):
        monkeypatch.setattr(appmod.Controller, name, value)
    monkeypatch.setattr(appmod.time, "sleep", lambda _: None)
    started: list[appmod.Controller] = []

    def make(script=()):
        controller = appmod.Controller(appmod.Config(camera_ip="192.0.2.10"))
        controller.camera = ScriptCamera(script)
        controller.stream = QuietStream()
        started.append(controller)
        controller.start()
        return controller

    yield make
    for controller in started:
        controller._stop.set()
        controller._thread.join(timeout=3)


def _until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def test_the_loop_registers_and_goes_online(running_controller):
    controller = running_controller()
    assert _until(lambda: controller.snapshot()["status"] == "online")
    assert controller.camera.connects == 1
    assert "start_stream" in controller.camera.calls
    assert controller.snapshot()["iso"] == "200"


def test_a_dropout_keeps_the_session(running_controller):
    from lumixremote.camera import NotReachable
    controller = running_controller()
    assert _until(lambda: controller.snapshot()["status"] == "online")
    session = controller.camera.session_id
    controller.camera.script = [NotReachable("gone")] * 5
    assert _until(lambda: controller.snapshot()["unreachable"])
    assert _until(lambda: not controller.snapshot()["unreachable"])
    assert controller.camera.session_id == session
    assert controller.camera.connects == 1


def test_a_refused_session_registers_again(running_controller):
    controller = running_controller()
    assert _until(lambda: controller.snapshot()["status"] == "online")
    controller.camera.script = [LumixError("refused", reply="err_critical")]
    assert _until(lambda: controller.camera.connects == 2)
    assert _until(lambda: controller.snapshot()["status"] == "online")


def test_one_unreadable_reply_does_not_cost_the_session(running_controller):
    # A garbled getstate reply used to drop the session, and the
    # re-registration ends a running recording.
    from lumixremote.camera import BadReply
    controller = running_controller()
    assert _until(lambda: controller.snapshot()["status"] == "online")
    polls = controller.camera.polls
    controller.camera.script = [BadReply("unreadable")]
    assert _until(lambda: controller.camera.polls > polls + 3)
    assert controller.camera.connects == 1
    assert controller.snapshot()["status"] == "online"


def test_repeated_unreadable_replies_drop_the_session(running_controller):
    from lumixremote.camera import BadReply
    controller = running_controller()
    assert _until(lambda: controller.snapshot()["status"] == "online")
    controller.camera.script = [BadReply("unreadable")] * appmod.Controller.BAD_REPLY_LIMIT
    assert _until(lambda: controller.camera.connects == 2)


def test_unreadable_replies_in_a_row_are_counted_from_zero_after_a_good_one(running_controller):
    from lumixremote.camera import BadReply
    controller = running_controller()
    assert _until(lambda: controller.snapshot()["status"] == "online")
    limit = appmod.Controller.BAD_REPLY_LIMIT
    for _ in range(3):
        polls = controller.camera.polls
        controller.camera.script = [BadReply("unreadable")] * (limit - 1)
        assert _until(lambda: controller.camera.polls > polls + limit + 1)
    assert controller.camera.connects == 1


def test_an_unexpected_error_does_not_end_the_loop(running_controller):
    controller = running_controller()
    assert _until(lambda: controller.snapshot()["status"] == "online")
    controller.camera.script = [RuntimeError("bug")]
    polls = controller.camera.polls
    assert _until(lambda: controller.camera.polls > polls + 3)
    assert controller._thread.is_alive()
    assert controller.camera.connects == 1


def test_a_dead_receiver_is_started_again(running_controller):
    controller = running_controller()
    assert _until(lambda: controller.snapshot()["status"] == "online")
    starts = controller.camera.calls.count("start_stream")
    controller.stream.running = False
    assert _until(lambda: controller.camera.calls.count("start_stream") > starts)


# ------------------------------------------------------------ the shutdown ---

def test_stop_releases_the_session():
    controller = appmod.Controller(appmod.Config(camera_ip="192.0.2.10"))
    controller.camera = ScriptCamera()
    controller.stream = QuietStream()
    controller.camera.session_id = "0123456789abcdef"
    controller.stop()
    assert controller.camera.calls == ["stop_stream"]
    assert controller.camera.closed


def test_stop_releases_a_session_marked_as_lost():
    # After an err_busy the session is dropped; the camera still holds it.
    controller = appmod.Controller(appmod.Config(camera_ip="192.0.2.10"))
    controller.camera = ScriptCamera()
    controller.stream = QuietStream()
    controller._stale_session = "0123456789abcdef"
    controller.stop()
    assert controller.camera.calls == ["stop_stream"]
    assert controller.camera.session_id == "0123456789abcdef"


def test_stop_without_any_session_sends_nothing():
    controller = appmod.Controller(appmod.Config(camera_ip="192.0.2.10"))
    controller.camera = ScriptCamera()
    controller.stream = QuietStream()
    controller.stop()
    assert controller.camera.calls == []
    assert controller.camera.closed


# ---------------------------------------------------------------- set_iso ---

@pytest.fixture
def iso_controller(monkeypatch):
    monkeypatch.setattr(appmod.time, "sleep", lambda _: None)
    controller = appmod.Controller(appmod.Config(camera_ip="192.0.2.10"))
    controller.camera = ScriptCamera()
    controller._iso_values = ["auto", "200", "800"]
    return controller


def test_set_iso_returns_what_the_camera_took(iso_controller):
    # Measured: 400 was acknowledged and came back as 800.
    camera = iso_controller.camera
    camera.set_setting = lambda name, value: camera.settings.update({name: "800"})
    assert iso_controller.set_iso("200") == "800"
    assert iso_controller.snapshot()["iso"] == "800"


def test_set_iso_refuses_a_value_the_camera_does_not_offer(iso_controller):
    with pytest.raises(LumixError):
        iso_controller.set_iso("12345")
    assert iso_controller.camera.settings["iso"] == "200"


def test_set_iso_without_a_known_list_passes_the_value_on(iso_controller):
    iso_controller._iso_values = []
    assert iso_controller.set_iso("3200") == "3200"


def test_the_iso_route_answers_with_the_adopted_value():
    class IsoController:
        def set_iso(self, value):
            if value == "bad":
                raise LumixError("not available")
            return "800"

    appmod.app.state.controller = IsoController()
    try:
        ok = client.post("/api/iso", json={"value": "400"})
        assert ok.status_code == 200 and ok.json() == {"ok": True, "iso": "800"}
        assert client.post("/api/iso", json={"value": "bad"}).status_code == 409
    finally:
        del appmod.app.state.controller


# ---------------------------------------------------------- mediaprobe ---

from lumixremote import mediaprobe  # noqa: E402


def test_browse_reply_is_read_with_its_escaped_listing():
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<container id="01000000" parentID="0"><dc:title>100_PANA</dc:title></container>'
        '<item id="01000001" parentID="0"><dc:title>P1000001</dc:title>'
        "<dc:date>2026-01-02T03:04:05</dc:date>"
        "<upnp:class>object.item.imageItem</upnp:class>"
        "<res protocolInfo=\"http-get:*:image/jpeg:PANASONIC.COM_PN=CAM_TN\">"
        "http://192.0.2.10:50001/DT1000001.JPG</res>"
        "<res protocolInfo=\"http-get:*:image/jpeg:PANASONIC.COM_PN=CAM_ORG;"
        "OriginalFileName='P1000001.JPG'\">http://192.0.2.10:50001/DO1000001.JPG</res>"
        "</item></DIDL-Lite>"
    )
    escaped = didl.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    reply = (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        '<u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        f"<Result>{escaped}</Result><NumberReturned>2</NumberReturned>"
        "<TotalMatches>7</TotalMatches></u:BrowseResponse></s:Body></s:Envelope>"
    )
    result = mediaprobe.parse_browse(reply)
    assert (result.returned, result.total) == (2, 7)
    folder, item = result.entries
    assert folder.is_container and folder.title == "100_PANA"
    assert item.date == "2026-01-02T03:04:05"
    assert [r.kind for r in item.resources] == ["CAM_TN", "CAM_ORG"]
    assert item.resources[1].filename == "P1000001.JPG"
    assert item.resources[1].url.endswith("/DO1000001.JPG")


def test_protocol_info_without_extras():
    assert mediaprobe.parse_protocol_info("http-get:*:video/mp4:*") == ("video/mp4", "", "")
    assert mediaprobe.parse_protocol_info("") == ("", "", "")


def test_folders_come_from_the_capability_reply():
    xml = (
        "<camrply><result>ok</result><contents_action_info>"
        '<media id="media_sd" type="sd" detail="sd">'
        '<dir id="dir_id_sd_all" type="all" detail="pict" object_id="0"/>'
        '<dir id="dir_id_sd_mp4_only" type="movie" detail="mp4" object_id="01020000"/>'
        "</media></contents_action_info></camrply>"
    )
    folders = mediaprobe.parse_folders(xml)
    assert [f["object_id"] for f in folders] == ["0", "01020000"]
    assert folders[1]["media"] == "sd"
    assert mediaprobe.parse_folders("not xml") == []


def test_event_url_is_taken_from_the_connection_manager():
    xml = (
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><device><serviceList>'
        "<service><serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>"
        "<eventSubURL>/Server0/CDS_event</eventSubURL></service>"
        "<service><serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>"
        "<eventSubURL>/Server0/CMS_event</eventSubURL></service>"
        "</serviceList></device></root>"
    )
    base = "http://192.0.2.10:60606/Lumix/Server0/ddd"
    assert mediaprobe.event_url(xml, base) == "http://192.0.2.10:60606/Server0/CMS_event"
    assert mediaprobe.event_url("<root/>", base) is None


def test_datagram_layout_compares_computed_and_found_jpeg_start():
    info = b"\x00\x01" + b"\x00" * 242              # type 1, 244 bytes in all
    head = bytearray(32)
    head[0:2] = (32 + len(info) + 6).to_bytes(2, "big")
    head[30:32] = len(info).to_bytes(2, "big")
    layout = mediaprobe.datagram_layout(bytes(head) + info + b"\xff\xd8\xff\xe0\xff\xd9")
    assert layout["computed_jpeg_at"] == layout["soi_at"] == 276
    assert layout["matches"] and layout["info_type"] == 1
    assert not mediaprobe.datagram_layout(b"\x00" * 40 + b"\xff\xd8\xff")["matches"]
    assert not mediaprobe.datagram_layout(b"short")["matches"]


# --------------------------------------------------------------- media ---

import httpx  # noqa: E402

from lumixremote import media as mediamod  # noqa: E402


class FakeSession:
    """Stands in for the controller: the media view is open and ready."""

    def __init__(self):
        self.media_wanted = True
        self.media_ready = True

    def media_headers(self):
        return {"X-SESSION_ID": "0123456789abcdef"}

    def content_count(self):
        return 1


CLIP = bytes(range(256)) * 40          # 10240 bytes


def _listing(size: int = len(CLIP)) -> str:
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0110001" parentID="0">'
        '<upnp:class name="videoItem">object.item.videoItem.movie</upnp:class>'
        '<res protocolInfo="http-get:*:application/octet-stream:PANASONIC.COM_PN='
        f"CAM_AVC_MOV_ORG;OriginalFileName='P1000001.MOV'\" duration=\"0:00:08\" size=\"{size}\">"
        "http://192.0.2.10:50001/DO0110001.MOV</res>"
        '<res protocolInfo="http-get:*:image/jpeg:PANASONIC.COM_PN=CAM_TN" size="5000">'
        "http://192.0.2.10:50001/DT0110001.JPG</res>"
        "</item></DIDL-Lite>"
    ).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (f"<e><b><r><Result>{didl}</Result><NumberReturned>1</NumberReturned>"
            "<TotalMatches>1</TotalMatches></r></b></e>")


class FakeCard:
    """The camera's ports 60606 and 50001 as far as the downloader uses them."""

    def __init__(self, cut_after: int | None = None):
        self.cut_after = cut_after      # bytes after which the first reply breaks
        self.ranges: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Server0/CDS_control":
            return httpx.Response(200, text=_listing())
        if request.url.path.endswith(".JPG"):
            return httpx.Response(200, content=b"\xff\xd8thumb\xff\xd9")
        self.headers.append(dict(request.headers))
        asked = request.headers.get("range", "")
        self.ranges.append(asked)
        if asked:
            start = int(asked.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=CLIP[start:], headers={
                "Content-Range": f"bytes {start}-{len(CLIP) - 1}/{len(CLIP)}"})
        if self.cut_after is not None:
            cut, self.cut_after = self.cut_after, None

            def broken():
                yield CLIP[:cut]
                raise httpx.ReadError("connection lost")

            return httpx.Response(200, content=broken())
        return httpx.Response(200, content=CLIP, headers={"X-FILE_SIZE": str(len(CLIP))})


@pytest.fixture
def card(tmp_path, monkeypatch):
    """A manager on a fake card, with the waits shortened."""
    monkeypatch.setattr(mediamod.MediaManager, "RETRY_WAIT", 0.05)
    monkeypatch.setattr(mediamod.MediaManager, "RESERVE", 0)
    # Small chunks: what arrived before a break is in the part file.
    monkeypatch.setattr(mediamod.MediaManager, "CHUNK", 1000)
    managers = []

    def make(fake: FakeCard, session: FakeSession | None = None):
        manager = mediamod.MediaManager(session or FakeSession(), "192.0.2.10", tmp_path,
                                        transport=httpx.MockTransport(fake))
        managers.append(manager)
        return manager

    yield make
    for manager in managers:
        manager.stop()


def _wait(check, seconds: float = 5.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def _item(manager):
    items = manager.snapshot()["items"]
    return items[0] if items else {}


def test_the_card_is_listed_and_a_file_downloaded(card, tmp_path):
    fake = FakeCard()
    manager = card(fake)
    _wait(lambda: manager.snapshot()["listed"])
    item = _item(manager)
    assert (item["name"], item["size"], item["video"]) == ("P1000001.MOV", len(CLIP), True)
    _wait(lambda: _item(manager)["thumb"])
    assert manager.thumbnail("0110001").startswith(b"\xff\xd8")

    assert manager.enqueue(None) == 1
    _wait(lambda: _item(manager)["state"] == "done")
    assert (tmp_path / "P1000001.MOV").read_bytes() == CLIP
    assert not list(tmp_path.glob("*.part"))
    # The download carries the session, like the phone app's.
    assert fake.headers[0]["x-session_id"] == "0123456789abcdef"


def test_a_broken_download_continues_where_it_stopped(card, tmp_path):
    fake = FakeCard(cut_after=3000)
    manager = card(fake)
    _wait(lambda: manager.snapshot()["listed"])
    manager.enqueue(["0110001"])
    _wait(lambda: _item(manager)["state"] == "done")
    assert fake.ranges == ["", "bytes=3000-"]
    assert (tmp_path / "P1000001.MOV").read_bytes() == CLIP


def test_a_part_file_from_an_earlier_run_is_continued(card, tmp_path):
    (tmp_path / f"P1000001.MOV.{len(CLIP)}.part").write_bytes(CLIP[:4096])
    fake = FakeCard()
    manager = card(fake)
    _wait(lambda: manager.snapshot()["listed"])
    manager.enqueue(None)
    _wait(lambda: _item(manager)["state"] == "done")
    assert fake.ranges == ["bytes=4096-"]
    assert (tmp_path / "P1000001.MOV").read_bytes() == CLIP


def test_a_file_already_in_the_folder_counts_as_saved(card, tmp_path):
    (tmp_path / "P1000001.MOV").write_bytes(CLIP)
    fake = FakeCard()
    manager = card(fake)
    _wait(lambda: manager.snapshot()["listed"])
    assert _item(manager)["state"] == "done"
    assert manager.enqueue(None) == 0
    assert fake.ranges == []


def test_another_file_of_the_same_name_is_not_overwritten(card, tmp_path):
    (tmp_path / "P1000001.MOV").write_bytes(b"an older clip with the same number")
    manager = card(FakeCard())
    _wait(lambda: manager.snapshot()["listed"])
    manager.enqueue(None)
    _wait(lambda: _item(manager)["state"] == "done")
    assert (tmp_path / "P1000001.MOV").read_bytes().startswith(b"an older clip")
    assert (tmp_path / "P1000001-2.MOV").read_bytes() == CLIP


def test_closing_the_view_stops_the_download_and_keeps_the_part(card, tmp_path, monkeypatch):
    session = FakeSession()
    fake = FakeCard(cut_after=2000)
    monkeypatch.setattr(mediamod.MediaManager, "RETRY_WAIT", 0.5)
    manager = card(fake, session)
    _wait(lambda: manager.snapshot()["listed"])
    manager.enqueue(None)
    part = tmp_path / f"P1000001.MOV.{len(CLIP)}.part"
    _wait(lambda: part.exists() and part.stat().st_size == 2000)
    session.media_wanted = False
    manager.closed()
    _wait(lambda: not manager.busy)
    assert part.stat().st_size == 2000
    assert not (tmp_path / "P1000001.MOV").exists()


def test_nothing_is_queued_without_a_usable_folder(tmp_path):
    manager = mediamod.MediaManager(FakeSession(), "192.0.2.10", tmp_path / "missing",
                                    transport=httpx.MockTransport(FakeCard()))
    try:
        _wait(lambda: manager.snapshot()["listed"])
        with pytest.raises(LumixError, match="does not exist"):
            manager.enqueue(None)
        assert "does not exist" in manager.snapshot()["folder_problem"]
    finally:
        manager.stop()


def test_an_unmapped_volume_is_not_a_download_folder(tmp_path):
    mountinfo = tmp_path / "mountinfo"
    hexid = "ab" * 32
    mountinfo.write_text(
        f"100 90 8:1 /var/lib/docker/volumes/{hexid}/_data {tmp_path} rw - ext4 /dev/sda1 rw\n")
    assert "No folder is mapped" in mediamod.folder_problem(tmp_path, mountinfo)
    mountinfo.write_text(
        f"100 90 8:1 /var/lib/docker/volumes/clips/_data {tmp_path} rw - ext4 /dev/sda1 rw\n")
    assert mediamod.folder_problem(tmp_path, mountinfo) == ""
    assert "configured" in mediamod.folder_problem(None)


def test_file_names_from_the_camera_stay_inside_the_folder():
    assert mediamod.safe_name("../../etc/passwd", "x") == "passwd"
    assert mediamod.safe_name("..", "fallback.bin") == "fallback.bin"
    assert mediamod.safe_name("P1000001.MOV", "x") == "P1000001.MOV"


# ---------------------------------------------------------- media view ---

class PlaybackCamera(FakeCamera):
    connected = True

    def set_play_mode(self):
        self.calls.append("playmode")

    def set_rec_mode(self):
        self.calls.append("recmode")

    def stop_stream(self):
        self.calls.append("stopstream")

    def get_setting(self, name):
        return ""

    def get_state(self):
        return CameraState(raw={"cammode": self.mode, "rec": self.rec})


@pytest.fixture
def viewer(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(appmod.time, "monotonic", lambda: clock["now"])
    controller = appmod.Controller(appmod.Config(camera_ip="192.0.2.10"))
    camera = controller.camera = PlaybackCamera()
    camera.mode, camera.rec = "rec", "off"
    controller._read_state()
    return controller, camera, clock


def test_opening_the_cards_switches_to_playback_and_closing_back(viewer):
    controller, camera, clock = viewer
    controller._streaming = True
    controller.media_open()
    controller._read_state()
    assert camera.calls == ["stopstream", "playmode"]
    assert not controller.media_ready

    camera.mode = "play"
    controller._read_state()
    # No recmode while the view is open, unlike an ordinary drop into playback.
    assert camera.calls == ["stopstream", "playmode"]
    assert controller.media_ready and controller._status == "media"

    controller.media_close()
    controller._read_state()
    assert camera.calls[-1] == "recmode"


def test_playmode_is_not_repeated_every_poll(viewer):
    controller, camera, clock = viewer
    controller.media_open()
    controller._read_state()
    clock["now"] += 2
    controller._read_state()
    assert camera.calls.count("playmode") == 1
    clock["now"] += 8
    controller._read_state()
    assert camera.calls.count("playmode") == 2


def test_the_cards_and_a_recording_exclude_each_other(viewer):
    controller, camera, _ = viewer
    controller.media_open()
    with pytest.raises(LumixError, match="close them"):
        controller.rec_start()
    assert "rec_start" not in camera.calls

    controller.media_close()
    controller.rec_start()
    with pytest.raises(LumixError, match="recording is running"):
        controller.media_open()


def test_a_recording_started_on_the_camera_closes_the_cards(viewer):
    controller, camera, _ = viewer
    controller.media_open()
    camera.rec = "on"
    controller._read_state()
    assert not controller.media_wanted
    assert "playmode" not in camera.calls
