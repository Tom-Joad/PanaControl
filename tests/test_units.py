"""Unit tests for the parts that need no camera: the protocol parsing, the
exposure values, the stream statistics, the settings and the web layer's
guards. The device findings behind the expected values are in
docs/protocol.md."""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from lumixremote import app as appmod
from lumixremote.camera import _hexencode, _looks_like_session
from lumixremote.stream import StreamStats, extract_jpeg, jpeg_dimensions


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


def test_camera_reply_is_taken_from_the_error_text():
    assert appmod._camera_reply("camera refused {...}: 'err_busy'") == "err_busy"
    assert appmod._camera_reply("timeout") == ""


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
