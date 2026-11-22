"""Read-only test of what the phone app does and PanaControl doesn't yet.

docs/media-transfer.md and docs/phone-app-notes.md were read from the
decompiled phone app. This run checks them on a real camera and answers:

  * Does the camera push events (recording ended and why, mode changes)?
  * Is `camcmd&value=playmode` accepted?
  * Which folders does the camera name, and how many files are there?
  * What does the listing look like, and which kinds of files does it offer?
  * Can a file be downloaded, how fast, and does `Range` work?
  * Which fields does `getstate` carry on this model?
  * Does the JPEG in a live view datagram start at 32 + (u16 at offset 30)?

Nothing on the camera is changed, deleted or recorded. The camera is switched
to playback for the listing and back to record mode afterwards. The run
refuses to start while a recording is running.

Only one remote session exists per camera: stop PanaControl before the run.

Usage:
    python3 -m lumixremote.mediaprobe --ip 192.0.2.10
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import pathlib
import socket
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter

import httpx

from .camera import DESCRIPTOR_PATH, DESCRIPTOR_PORT, Camera, LumixError
from .events import event_url, local_ip_towards  # noqa: F401
from .media import (CDS_PATH, DLNA_AGENT, DOWNLOAD_AGENT, SOAP_ACTION,  # noqa: F401
                    BrowseResult, Entry, Resource, _local, browse_envelope,
                    parse_browse, parse_protocol_info)
from .stream import SOI

log = logging.getLogger("mediaprobe")


# The fixed part of a live view datagram, as the phone app reads it.
DATAGRAM_FIXED = 32
INFO_LENGTH_AT = 30


# ------------------------------------------------------------- parsing ---

def parse_folders(capability_xml: str) -> list[dict[str, str]]:
    """The listable folders from `getinfo&type=capability`.

    They are `<dir object_id=...>` elements below `<media detail="sd">`.
    """
    folders: list[dict[str, str]] = []
    try:
        root = ET.fromstring(capability_xml)
    except ET.ParseError:
        return folders
    for media in root.iter("media"):
        for node in media.iter("dir"):
            object_id = node.get("object_id")
            if object_id:
                folders.append({
                    "media": media.get("detail", "") or media.get("id", ""),
                    "id": node.get("id", ""),
                    "detail": node.get("detail", ""),
                    "object_id": object_id,
                })
    return folders


def datagram_layout(datagram: bytes) -> dict[str, int | bool]:
    """Compare the phone app's layout with where the JPEG really starts."""
    found = datagram.find(SOI)
    if len(datagram) < DATAGRAM_FIXED + 2:
        return {"length": len(datagram), "soi_at": found, "matches": False}
    info = struct.unpack_from(">H", datagram, INFO_LENGTH_AT)[0]
    computed = DATAGRAM_FIXED + info
    return {
        "length": len(datagram),
        "declared_length": struct.unpack_from(">H", datagram, 0)[0],
        "number": struct.unpack_from(">I", datagram, 4)[0],
        "info_length": info,
        "info_type": struct.unpack_from(">H", datagram, DATAGRAM_FIXED)[0],
        "computed_jpeg_at": computed,
        "soi_at": found,
        "matches": computed == found,
    }


# -------------------------------------------------------------- events ---

class EventListener:
    """Small HTTP server that takes the camera's NOTIFY requests."""

    def __init__(self, bind_ip: str):
        self.events: list[dict[str, str]] = []
        self._started = time.monotonic()
        listener = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_NOTIFY(self):  # noqa: N802 - the method name is the HTTP verb
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace")
                listener.events.append({
                    "at": f"{time.monotonic() - listener._started:.1f}",
                    "path": self.path,
                    "body": body,
                })
                print(f"      EVENT +{listener.events[-1]['at']}s: "
                      f"{' | '.join(body.split())[:200]}")
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # keep the report readable
                pass

        self._server = http.server.ThreadingHTTPServer((bind_ip, 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()



# ----------------------------------------------------------------- run ---

class Probe:
    def __init__(self, ip: str, name: str, port: int, outdir: pathlib.Path,
                 list_max: int, download_mb: int):
        self.ip = ip
        self.port = port
        self.outdir = outdir
        self.list_max = list_max
        self.download_limit = download_mb * 1024 * 1024
        # recmode ran into a five-second timeout on the device; see protocol.md
        self.cam = Camera(ip, device_name=name, timeout=15.0)
        self.http = httpx.Client(timeout=30.0)
        self.listener: EventListener | None = None
        self.subscription: tuple[str, str] | None = None   # (url, SID)
        self.report: dict = {}

    # -- helpers --

    def save(self, name: str, data: bytes | str) -> pathlib.Path:
        path = self.outdir / name
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)
        return path

    def raw(self, **params: str) -> str:
        """One cam.cgi call; a refusal or an error becomes the returned text."""
        try:
            return self.cam._get_raw(**params)
        except LumixError as exc:
            return f"ERROR: {exc}"

    def cammode(self) -> str:
        try:
            return self.cam.get_state().raw.get("cammode", "?")
        except LumixError as exc:
            return f"error ({exc})"

    def watch_mode(self, seconds: float, until: str | None = None,
                   hold: float = 0.0) -> str:
        """Print every change of cammode. With `until`, return once that mode
        has held for `hold` seconds."""
        start = time.monotonic()
        last = ""
        since = start
        while time.monotonic() - start < seconds:
            mode = self.cammode()
            now = time.monotonic()
            if mode != last:
                print(f"      +{now - start:4.1f}s cammode = {mode}")
                last, since = mode, now
            if until and mode == until and now - since >= hold:
                break
            time.sleep(0.5)
        return last

    # -- steps --

    def step_describe(self) -> None:
        print(f"\n[1/9] Device description from {self.ip}")
        info = self.cam.describe()
        print(f"      Model    : {info.model_number}, firmware {info.firmware}")
        base = f"http://{self.ip}:{DESCRIPTOR_PORT}{DESCRIPTOR_PATH}"
        text = self.http.get(base).text
        self.save("descriptor.xml", text)
        self.event_sub_url = event_url(text, base)
        print(f"      Event URL: {self.event_sub_url or 'none found'}")
        self.report["model"] = info.model_number
        self.report["firmware"] = info.firmware
        self.report["event_url_found"] = bool(self.event_sub_url)

    def step_subscribe(self) -> None:
        print("\n[2/9] Subscribing to the camera's events")
        if not self.event_sub_url:
            print("      skipped: the description names no event URL")
            return
        me = local_ip_towards(self.ip)
        self.listener = EventListener(me)
        callback = f"<http://{me}:{self.listener.port}/Camera/event>"
        try:
            r = self.http.request("SUBSCRIBE", self.event_sub_url, headers={
                "CALLBACK": callback, "NT": "upnp:event",
                "TIMEOUT": "Second-300", "User-Agent": DLNA_AGENT,
            })
        except httpx.HTTPError as exc:
            print(f"      SUBSCRIBE failed: {exc}")
            self.report["subscribe"] = f"failed: {exc}"
            return
        sid = r.headers.get("SID", "")
        print(f"      SUBSCRIBE -> HTTP {r.status_code}, "
              f"SID {'given' if sid else 'missing'}, TIMEOUT {r.headers.get('TIMEOUT', '-')}")
        print(f"      listening on {callback}")
        self.report["subscribe"] = {"status": r.status_code, "sid": bool(sid)}
        if sid:
            self.subscription = (self.event_sub_url, sid)

    def step_connect(self) -> bool:
        print("\n[3/9] Registering (confirm the prompt on the camera if one shows)")
        self.cam.connect()
        print("      Session obtained. Watching the mode for 10 s:")
        self.watch_mode(10)
        state = self.cam.get_state()
        if state.recording:
            print("      A recording is running: stopping here, nothing is changed.")
            return False
        return True

    def step_playmode(self) -> None:
        print("\n[4/9] Asking for playback")
        reply = self.raw(mode="camcmd", value="playmode")
        print(f"      playmode -> {' '.join(reply.split())[:160]}")
        self.report["playmode_reply"] = reply
        mode = self.watch_mode(8, until="play", hold=1.0)
        self.report["cammode_for_listing"] = mode

    def step_folders(self) -> list[dict[str, str]]:
        print("\n[5/9] Folders and number of files")
        capability = self.raw(mode="getinfo", type="capability")
        self.save("capability.xml", capability)
        folders = parse_folders(capability)
        for f in folders:
            print(f"      {f['media']:<6} {f['id']:<28} detail={f['detail']:<10} "
                  f"object_id={f['object_id']}")
        if not folders:
            print("      no <dir object_id=...> found, see capability.xml")
        self.report["folders"] = folders

        info = ""
        for attempt in range(15):
            info = self.raw(mode="get_content_info")
            if "<total_content_number>-1<" not in info:
                break
            if attempt == 0:
                print("      the camera is still building its index, waiting")
            time.sleep(1)
        self.save("content_info.xml", info)
        print(f"      get_content_info -> {' '.join(info.split())[:200]}")
        self.report["content_info"] = info
        return folders

    def browse(self, object_id: str, start: int, count: int) -> BrowseResult | None:
        url = f"http://{self.ip}:{DESCRIPTOR_PORT}{CDS_PATH}"
        try:
            r = self.http.post(url, content=browse_envelope(object_id, start, count).encode(),
                               headers={
                                   "Content-Type": 'text/xml; charset="utf-8"',
                                   "SOAPAction": SOAP_ACTION,
                                   "User-Agent": DLNA_AGENT,
                               })
        except httpx.HTTPError as exc:
            print(f"      Browse {object_id} @{start} failed: {exc}")
            return None
        safe = "".join(c if c.isalnum() else "_" for c in object_id)
        self.save(f"browse_{safe}_{start}.xml", r.text)
        if r.status_code != 200:
            print(f"      Browse {object_id} @{start} -> HTTP {r.status_code}")
            return None
        try:
            return parse_browse(r.text)
        except ET.ParseError as exc:
            print(f"      Browse {object_id} @{start}: unreadable reply ({exc})")
            return None

    def step_list(self, folders: list[dict[str, str]]) -> list[Entry]:
        print(f"\n[6/9] Listing (up to {self.list_max} entries per folder)")
        items: list[Entry] = []
        listing: dict[str, dict] = {}
        for object_id in ["0"] + [f["object_id"] for f in folders]:
            start, total, got = 0, -1, []
            while start < self.list_max:
                result = self.browse(object_id, start, min(50, self.list_max - start))
                if result is None or not result.entries:
                    break
                got.extend(result.entries)
                total = result.total
                start += len(result.entries)
                if 0 <= total <= start:
                    break
                self.cam.get_state()   # keepalive between the calls
            kinds = Counter(r.kind or r.mime or "?" for e in got for r in e.resources)
            containers = sum(1 for e in got if e.is_container)
            print(f"      ObjectID {object_id}: {len(got)} listed of {total} "
                  f"({containers} folders)")
            for kind, n in kinds.most_common():
                print(f"          {n:5d} x {kind}")
            listing[object_id] = {"listed": len(got), "total": total,
                                  "containers": containers, "kinds": dict(kinds)}
            items.extend(e for e in got if not e.is_container)
        self.report["listing"] = listing
        hosts = Counter(httpx.URL(r.url).netloc.decode() for e in items for r in e.resources
                        if r.url.startswith("http"))
        if hosts:
            print(f"      Files are served from: {', '.join(hosts)}")
        self.report["file_hosts"] = list(hosts)
        return items

    def fetch(self, label: str, res: Resource, dest: str) -> None:
        headers = {"User-Agent": DOWNLOAD_AGENT, **self.cam._headers()}
        result: dict = {"kind": res.kind, "mime": res.mime, "name": res.filename}
        try:
            # Streamed and closed after the headers: a camera that ignores
            # Range would otherwise send a whole multi-gigabyte clip here.
            with self.http.stream("GET", res.url,
                                  headers={**headers, "Range": "bytes=0-1023"}) as ranged:
                result["range_status"] = ranged.status_code
                result["content_range"] = ranged.headers.get("Content-Range", "")
            start = time.monotonic()
            received = 0
            with self.http.stream("GET", res.url, headers=headers) as r, \
                    (self.outdir / dest).open("wb") as out:
                result["status"] = r.status_code
                result["file_size"] = r.headers.get("X-FILE_SIZE", "")
                result["content_length"] = r.headers.get("Content-Length", "")
                for chunk in r.iter_bytes(64 * 1024):
                    out.write(chunk)
                    received += len(chunk)
                    if received >= self.download_limit:
                        result["cut_off"] = True
                        break
            seconds = max(time.monotonic() - start, 0.001)
            result["bytes"] = received
            result["mbit_per_s"] = round(received * 8 / seconds / 1e6, 1)
            print(f"      {label:<10} {res.kind or res.mime:<22} {res.filename or '-':<16} "
                  f"HTTP {result['status']}, {received / 1e6:.1f} MB"
                  f"{' (cut off)' if result.get('cut_off') else ''}, "
                  f"{result['mbit_per_s']} Mbit/s, X-FILE_SIZE={result['file_size'] or '-'}, "
                  f"Range -> {result['range_status']}")
        except httpx.HTTPError as exc:
            result["error"] = str(exc)
            print(f"      {label:<10} {res.kind or res.mime}: failed, {exc}")
        self.report.setdefault("downloads", []).append(result)

    def step_download(self, items: list[Entry]) -> None:
        print(f"\n[7/9] Downloading samples (at most "
              f"{self.download_limit // (1024 * 1024)} MB each)")

        def first(test) -> Resource | None:
            for entry in items:
                for res in entry.resources:
                    if res.url and test(res):
                        return res
            return None

        samples = (
            ("Thumbnail", "sample_thumbnail.jpg", lambda r: r.kind == "CAM_TN"),
            ("Photo", "sample_photo.jpg",
             lambda r: r.kind in ("CAM_ORG", "CAM_RAW_JPG", "CAM_HSP_JPG")),
            ("RAW", "sample_raw.bin", lambda r: r.kind in ("CAM_RAW", "CAM_HSP_RAW")),
            ("MP4", "sample_video_mp4.bin",
             lambda r: "MP4" in r.kind or r.mime == "video/mp4"),
            ("MOV", "sample_video_mov.bin", lambda r: "MOV" in r.kind),
        )
        for label, dest, test in samples:
            res = first(test)
            if res is None:
                print(f"      {label:<10} none in the listing")
                continue
            self.fetch(label, res, dest)
            self.cam.get_state()

    def step_back_to_rec(self) -> bool:
        print("\n[8/9] Back to record mode")
        for attempt in range(3):
            reply = self.raw(mode="camcmd", value="recmode")
            print(f"      recmode -> {' '.join(reply.split())[:120]}")
            if self.watch_mode(12, until="rec", hold=3.0) == "rec":
                return True
            time.sleep(8)   # protocol.md: gap between two recmode attempts
        print("      the camera did not settle in record mode")
        return False

    def step_state_and_stream(self) -> None:
        print("\n[9/9] State fields and live view datagrams")
        state = self.raw(mode="getstate")
        self.save("getstate.xml", state)
        try:
            fields = [c.tag for c in ET.fromstring(state).find("state")]
        except (ET.ParseError, TypeError):
            fields = []
        print(f"      getstate fields: {', '.join(fields) or 'unreadable, see getstate.xml'}")
        self.report["getstate_fields"] = fields

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(1.0)
        layouts = []
        try:
            self.cam.start_stream(self.port)
            deadline = time.monotonic() + 12
            while len(layouts) < 30 and time.monotonic() < deadline:
                try:
                    data, sender = sock.recvfrom(65536)
                except socket.timeout:
                    continue
                if sender[0] != self.ip:
                    continue
                if len(layouts) < 3:
                    self.save(f"datagram_{len(layouts)}.bin", data)
                layouts.append(datagram_layout(data))
        except LumixError as exc:
            print(f"      startstream refused: {exc}")
        finally:
            sock.close()
        if not layouts:
            print("      no datagram received (firewall? cammode?)")
        else:
            ok = sum(1 for d in layouts if d["matches"])
            offsets = Counter(d["soi_at"] for d in layouts)
            types = Counter(d.get("info_type") for d in layouts)
            print(f"      {len(layouts)} datagrams, JPEG at 32 + u16@30 in {ok} of them")
            print(f"      JPEG offsets seen: {dict(offsets)}, info block types: {dict(types)}")
        self.report["datagrams"] = layouts[:5]
        self.report["datagram_layout_matches"] = (
            f"{sum(1 for d in layouts if d['matches'])}/{len(layouts)}")

    def finish(self) -> None:
        """Release everything. Without stopstream the camera keeps the remote
        session until it is switched off and on."""
        if self.cam.connected:
            self.cam.stop_stream()
        if self.subscription:
            url, sid = self.subscription
            try:
                self.http.request("UNSUBSCRIBE", url, headers={"SID": sid})
            except httpx.HTTPError:
                pass
        if self.listener:
            self.report["events"] = self.listener.events
            self.listener.close()
        self.cam.close()
        self.http.close()
        if self.report:
            path = self.save("report.json", json.dumps(self.report, indent=2))
            print(f"\nEvents received: {len(self.report.get('events', []))}")
            print(f"Report and raw replies: {path.parent}/")

    def run(self) -> int:
        self.outdir.mkdir(parents=True, exist_ok=True)
        try:
            self.step_describe()
            self.step_subscribe()
            if not self.step_connect():
                return 1
            self.step_playmode()
            folders = self.step_folders()
            items = self.step_list(folders)
            self.step_download(items)
            if self.step_back_to_rec():
                self.step_state_and_stream()
            return 0
        finally:
            self.finish()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only test of listing, download, events and state")
    parser.add_argument("--ip", required=True, help="the camera's address")
    parser.add_argument("--name", default="lumix-remote",
                        help="connection name as shown on the camera")
    parser.add_argument("--port", type=int, default=49199, help="local UDP port")
    parser.add_argument("--list-max", type=int, default=200,
                        help="entries to list per folder at most")
    parser.add_argument("--download-mb", type=int, default=32,
                        help="megabytes to fetch per sample file at most")
    parser.add_argument("--outdir", type=pathlib.Path,
                        default=pathlib.Path("out/mediaprobe"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return Probe(args.ip, args.name, args.port, args.outdir,
                     args.list_max, args.download_mb).run()
    except LumixError as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
