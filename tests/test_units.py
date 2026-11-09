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
    controller.camera.connect = lambda: None
    controller.camera.describe = lambda: None
    controller.camera.stop_stream = lambda: None
    controller._read_capabilities = lambda: None
    controller._settle = lambda: None
    controller._establish()
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
