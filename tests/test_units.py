"""Unit tests for the parts that need no camera: the protocol parsing, the
exposure values, the stream statistics, the settings and the web layer's
guards. The device findings behind the expected values are in
docs/protocol.md."""

from __future__ import annotations

import pathlib
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
        fps = 0.0
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
