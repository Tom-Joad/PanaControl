"""Second test run: what the phone app does during live view and recording.

mediaprobe.py covered the cards. This run measures the rest of what
docs/phone-app-notes.md took from the phone app, on a real camera and with
somebody standing at it:

  * the camera's events and every state field during three recordings:
    started and stopped by command, stopped on the camera, started on the
    camera
  * every setting the camera's menu definition names, read once
  * touch focus, AF/AE lock, focus pull and zoom by command
  * exposure compensation, white balance and shutter speed set and restored
  * live view datagrams next to known settings, for mapping the header
  * the second card slot in playback

It makes three short recordings, and nothing else on the cards is touched.
Photos are left out: the camera is tested in its video mode, and the mode
dial can't be turned by command. Settings it changes are put back, also when the run is
aborted. Only one remote session exists per camera: stop PanaControl first.

Usage:
    python3 -m lumixremote.appprobe --ip 192.0.2.10
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter

from .camera import LumixError
from .mediaprobe import Probe, datagram_layout

log = logging.getLogger("appprobe")

# Settings read on every run even if the menu definition doesn't name them.
ALWAYS_READ = ("iso", "shtrspeed", "exposure", "whitebalance", "focusmode",
               "afmode", "photostyle", "drivemode", "quality", "liveviewsize",
               "peaking", "videoquality", "current_sd")


def menu_settings(allmenu: str) -> dict[str, list[str]]:
    """The settings the camera's menu offers, with their allowed values."""
    found: dict[str, list[str]] = {}
    try:
        root = ET.fromstring(allmenu)
    except ET.ParseError:
        return found
    for item in root.iter("item"):
        if item.get("cmd_mode") != "setsetting":
            continue
        name, value = item.get("cmd_type"), item.get("cmd_value")
        if not name:
            continue
        values = found.setdefault(name, [])
        if value and value not in values:
            values.append(value)
    return found


def setting_value(reply: str, name: str) -> str | None:
    """The value from a getsetting reply; None when the camera refused."""
    try:
        root = ET.fromstring(reply)
    except ET.ParseError:
        return None
    if root.findtext("result") != "ok":
        return None
    node = root.find("settingvalue")
    return None if node is None else node.get(name)


def state_fields(reply: str) -> dict[str, str]:
    try:
        state = ET.fromstring(reply).find("state")
    except ET.ParseError:
        return {}
    return {} if state is None else {c.tag: (c.text or "") for c in state}


def result_of(reply: str) -> str:
    """Short form of a cam.cgi reply for the report."""
    try:
        return ET.fromstring(reply).findtext("result") or reply[:80]
    except ET.ParseError:
        return " ".join(reply.split())[:80]


def focus_position(reply: str) -> int | None:
    """The lens position from a focus reply (`ok,<position>,<maximum>,...`)."""
    parts = [p.strip() for p in reply.split(",")]
    if len(parts) < 2 or parts[0] != "ok":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


class AppProbe(Probe):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started = time.monotonic()
        self.last_state: dict[str, str] = {}
        self.timeline: list[dict] = []
        self.restore: list[tuple[str, str]] = []
        self.menu: dict[str, list[str]] = {}
        self._sock: socket.socket | None = None
        self._latest: bytes | None = None
        self._receiving = False
        self._samples = 0

    # -- helpers --

    def at(self) -> str:
        return f"{time.monotonic() - self.started:6.1f}s"

    def call(self, label: str, **params: str) -> str:
        """One cam.cgi call, with its reply printed and kept for the report."""
        reply = self.raw(**params)
        short = result_of(reply)
        print(f"      {self.at()} {label:<34} -> {short}")
        self.timeline.append({"at": self.at().strip(), "call": params, "reply": short})
        return reply

    def poll_state(self, quiet: bool = False) -> dict[str, str]:
        """Read the state and print every field that changed since last time."""
        fields = state_fields(self.raw(mode="getstate"))
        if not fields:
            return self.last_state
        changed = {k: v for k, v in fields.items() if self.last_state.get(k) != v}
        if changed and self.last_state and not quiet:
            text = ", ".join(f"{k}={v}" for k, v in changed.items())
            print(f"      {self.at()} state: {text}")
            self.timeline.append({"at": self.at().strip(), "state": changed})
        self.last_state = fields
        return fields

    def watch(self, seconds: float, until=None) -> bool:
        """Poll once a second; True as soon as `until(state)` holds."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            fields = self.poll_state()
            if until and until(fields):
                return True
            time.sleep(1.0)
        return False

    def sample(self, label: str) -> None:
        """Keep the newest live view datagram, with what is known about it."""
        data = self._latest
        if not data:
            return
        self._samples += 1
        name = f"datagram_{self._samples:02d}_{label}.bin"
        self.save(name, data)
        known = {n: setting_value(self.raw(mode="getsetting", type=n), n)
                 for n in ("iso", "shtrspeed", "exposure", "whitebalance")}
        layout = datagram_layout(data)
        self.report.setdefault("samples", []).append(
            {"file": name, "label": label, "settings": known, "layout": layout,
             "rec": self.last_state.get("rec", "")})

    def _receive(self) -> None:
        while self._receiving:
            try:
                data, sender = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if sender[0] == self.ip:
                self._latest = data
                self._offsets[data.find(b"\xff\xd8\xff")] += 1

    # -- steps --

    def step_settle(self) -> bool:
        print("\n[4] Record mode")
        if not self.step_back_to_rec():
            return False
        state = self.poll_state(quiet=True)
        print(f"      state: {len(state)} fields, rec={state.get('rec')}, "
              f"cammode={state.get('cammode')}")
        self.save("getstate_idle.xml", self.raw(mode="getstate"))
        return True

    def step_inventory(self) -> None:
        print("\n[5] What the camera offers")
        for name in ("lens", "capability", "curmenu", "allmenu"):
            text = self.raw(mode="getinfo", type=name)
            self.save(f"{name}.xml" if name != "lens" else "lens.txt", text)
            if name == "allmenu":
                self.menu = menu_settings(text)
        print(f"      menu definition: {len(self.menu)} settings")
        names = list(dict.fromkeys(list(ALWAYS_READ) + sorted(self.menu)))
        values: dict[str, str | None] = {}
        for name in names:
            values[name] = setting_value(self.raw(mode="getsetting", type=name), name)
        readable = {k: v for k, v in values.items() if v is not None}
        print(f"      readable settings: {len(readable)} of {len(names)}")
        for name in ALWAYS_READ:
            print(f"          {name:<14} = {values.get(name)}")
        self.report["settings"] = values
        self.report["menu"] = self.menu

    def step_stream(self) -> None:
        print("\n[6] Live view")
        self._offsets: Counter = Counter()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", self.port))
        self._sock.settimeout(1.0)
        self._receiving = True
        threading.Thread(target=self._receive, daemon=True).start()
        self.call("startstream", mode="startstream", value=str(self.port))
        deadline = time.monotonic() + 10
        while self._latest is None and time.monotonic() < deadline:
            time.sleep(0.2)
        print(f"      datagrams arriving: {'yes' if self._latest else 'NO'}")
        self.sample("idle")

    def focus_now(self) -> int | None:
        """Where the lens stands; focus-stop moves nothing and reports it."""
        return focus_position(self.raw(mode="camctrl", type="focus", value="focus-stop"))

    def focus_back(self, target: int, tele_raises: bool) -> None:
        """Step the lens back to where it stood, in short pulses.

        The camera only knows "nearer" and "farther", not "go to", so this
        approaches the old position and stops at the closest it gets.
        """
        best = None
        for _ in range(60):
            now = self.focus_now()
            if now is None:
                break
            gap = target - now
            if best is not None and abs(gap) >= best:
                break       # no closer than the pulse before: one step is coarser
            best = abs(gap)
            if gap == 0:
                break
            towards_tele = (gap > 0) == tele_raises
            self.raw(mode="camctrl", type="focus",
                     value="tele-normal" if towards_tele else "wide-normal")
            time.sleep(0.12)
        final = self.focus_now()
        print(f"      {self.at()} focus put back: wanted {target}, now {final}")
        self.report["focus_restore"] = {"wanted": target, "final": final}

    def step_controls(self) -> None:
        print("\n[7] Focus and zoom by command (the lens moves and is put back)")
        focus_reply = self.call("focus-stop (position)", mode="camctrl", type="focus",
                                value="focus-stop")
        start = focus_position(focus_reply)
        print(f"      focus reply: {' '.join(focus_reply.split())[:80]}")
        self.report["focus_reply"] = focus_reply
        self.call("oneshot_af", mode="camcmd", value="oneshot_af")
        time.sleep(1.5)
        self.call("touch 500/500 on", mode="camctrl", type="touch", value="500/500", value2="on")
        time.sleep(1.5)
        self.sample("touch_centre")
        self.call("touch 250/250 on", mode="camctrl", type="touch", value="250/250", value2="on")
        time.sleep(1.5)
        self.sample("touch_upper_left")
        self.call("touch off", mode="camctrl", type="touch", value="0/0", value2="off")
        self.call("touch_af_cancel", mode="camcmd", value="touch_af_cancel")
        self.call("af_area_reset", mode="camcmd", value="af_area_reset")
        self.call("af_ae_lock on", mode="camctrl", type="af_ae_lock", value="on")
        time.sleep(1.0)
        self.call("af_ae_lock off", mode="camctrl", type="af_ae_lock", value="off")
        before = self.focus_now()
        reply = self.call("focus tele-normal", mode="camctrl", type="focus", value="tele-normal")
        time.sleep(0.6)
        after = self.focus_now()
        print(f"      focus: {before} -> {after} after tele-normal ({' '.join(reply.split())[:60]})")
        self.call("focus wide-normal", mode="camctrl", type="focus", value="wide-normal")
        time.sleep(0.6)
        self.call("focus focus-stop", mode="camctrl", type="focus", value="focus-stop")
        if start is None or before is None or after is None or after == before:
            print("      the camera reports no usable focus position: the lens "
                  "stays where the tests left it")
        else:
            self.focus_back(start, tele_raises=after > before)
        for value in ("tele-normal", "zoomstop", "wide-normal", "zoomstop"):
            self.call(f"zoom {value}", mode="camcmd", value=value)
            time.sleep(0.6)
        self.call("lcd_on", mode="camcmd", value="lcd_on")
        self.poll_state()

    def change_and_restore(self, name: str, candidates: list[str]) -> None:
        current = setting_value(self.raw(mode="getsetting", type=name), name)
        if current is None:
            print(f"      {name}: not readable, skipped")
            return
        other = next((v for v in candidates if v != current), None)
        if other is None:
            print(f"      {name}: no other value to try (now {current})")
            return
        self.restore.append((name, current))
        self.call(f"set {name}={other}", mode="setsetting", type=name, value=other)
        time.sleep(0.8)
        got = setting_value(self.raw(mode="getsetting", type=name), name)
        print(f"      {self.at()} {name}: was {current}, asked {other}, camera says {got}")
        self.sample(f"{name}_changed")
        self.call(f"restore {name}={current}", mode="setsetting", type=name, value=current)
        time.sleep(0.8)
        back = setting_value(self.raw(mode="getsetting", type=name), name)
        if back == current:
            self.restore.remove((name, current))
        else:
            print(f"      {name}: NOT restored, camera says {back}")
        self.report.setdefault("set_tests", {})[name] = {
            "was": current, "asked": other, "got": got, "restored": back}

    def step_settings(self) -> None:
        print("\n[8] Settings changed and put back")
        self.change_and_restore("exposure", self.menu.get("exposure", []) or ["1/3", "0/3"])
        self.change_and_restore("whitebalance", self.menu.get("whitebalance", []))
        self.change_and_restore("shtrspeed", ["1536/256", "1280/256"])
        self.sample("restored")

    def recording(self, label: str, start_by_command: bool, stop_by_command: bool,
                  wait: float) -> None:
        on = lambda s: s.get("rec") == "on"          # noqa: E731
        off = lambda s: s.get("rec") != "on"         # noqa: E731
        if self.poll_state().get("cammode") != "rec" and not self.step_back_to_rec():
            print("      not in record mode, skipped")
            return
        if start_by_command:
            reply = self.call("video_recstart", mode="camcmd", value="video_recstart")
            if result_of(reply) != "ok":
                print("      start refused, skipped")
                return
            if not self.watch(8, until=on):
                print("      the state never showed rec=on")
        else:
            print(f"      >>> START a recording ON THE CAMERA now (waiting {wait:.0f} s)")
            if not self.watch(wait, until=on):
                print("      no recording was started on the camera, skipped")
                return
        self.save(f"getstate_{label}.xml", self.raw(mode="getstate"))
        self.sample(f"rec_{label}")
        if stop_by_command:
            self.watch(6)
            self.call("video_recstop", mode="camcmd", value="video_recstop")
            self.watch(8, until=off)
        else:
            print(f"      >>> STOP the recording ON THE CAMERA now (waiting {wait:.0f} s)")
            if not self.watch(wait, until=off):
                print("      not stopped on the camera; stopping by command")
                self.call("video_recstop", mode="camcmd", value="video_recstop")
                self.watch(8, until=off)
        # What comes after a recording: late events, the drop into playback.
        self.watch(6)

    def step_recordings(self, wait: float) -> None:
        print("\n[9] Recording 1: started and stopped by command")
        self.recording("by_command", True, True, wait)
        print("\n[10] Recording 2: started by command, stopped on the camera")
        self.recording("stopped_on_camera", True, False, wait)
        print("\n[11] Recording 3: started on the camera, stopped by command")
        self.recording("started_on_camera", False, True, wait)

    def step_cards(self) -> None:
        print("\n[12] Both card slots in playback")
        self._receiving = False
        self.call("stopstream", mode="stopstream")
        self.call("playmode", mode="camcmd", value="playmode")
        self.watch(10, until=lambda s: s.get("cammode") == "play")
        current = self.last_state.get("current_sd", "")
        slots = {}
        for slot in dict.fromkeys([current or "sd1", "sd2" if current != "sd2" else "sd1"]):
            if slot != current:
                reply = self.call(f"current_sd={slot}", mode="setsetting",
                                  type="current_sd", value=slot)
                if result_of(reply) != "ok":
                    continue
                time.sleep(2.0)
            info = self.raw(mode="get_content_info")
            for _ in range(15):
                if "<total_content_number>-1<" not in info:
                    break
                time.sleep(1.0)
                info = self.raw(mode="get_content_info")
            result = self.browse("0", 0, 50)
            kinds = Counter(r.kind or r.mime for e in (result.entries if result else [])
                            for r in e.resources)
            total = result.total if result else -1
            print(f"      {slot}: {total} files, kinds on the first page: {dict(kinds)}")
            slots[slot] = {"content_info": result_of(info), "total": total, "kinds": dict(kinds),
                           "newest": [e.title for e in (result.entries if result else [])][-5:]}
        if current and self.poll_state(quiet=True).get("current_sd") != current:
            self.call(f"current_sd={current} (back)", mode="setsetting",
                      type="current_sd", value=current)
        self.report["slots"] = slots

    def finish(self) -> None:
        for name, value in reversed(self.restore):
            print(f"      restoring {name}={value}")
            self.raw(mode="setsetting", type=name, value=value)
        self._receiving = False
        if self._sock:
            self._sock.close()
        self.report["timeline"] = self.timeline
        self.report["jpeg_offsets"] = dict(getattr(self, "_offsets", {}))
        super().finish()

    def run_manual(self, wait: float) -> int:
        """Only the two recordings that need a hand on the camera."""
        self.outdir.mkdir(parents=True, exist_ok=True)
        try:
            self.step_describe()
            self.step_subscribe()
            if not self.step_connect():
                return 1
            if not self.step_settle():
                return 1
            self.step_stream()
            print("\n[A] Recording started by command, stopped on the camera")
            self.recording("stopped_on_camera", True, False, wait)
            print("\n[B] Recording started on the camera, stopped by command")
            self.recording("started_on_camera", False, True, wait)
            self._receiving = False
            return 0
        finally:
            if state_fields(self.raw(mode="getstate")).get("rec") == "on":
                print("      a recording is still running: stopping it")
                self.raw(mode="camcmd", value="video_recstop")
            self.finish()

    def run_all(self, wait: float) -> int:
        self.outdir.mkdir(parents=True, exist_ok=True)
        try:
            self.step_describe()
            self.step_subscribe()
            if not self.step_connect():
                return 1
            if not self.step_settle():
                return 1
            self.step_inventory()
            self.step_stream()
            self.step_controls()
            self.step_settings()
            self.step_recordings(wait)
            self.step_cards()
            self.step_back_to_rec()
            offsets = dict(self._offsets)
            print(f"\n      JPEG offsets seen over the run: {offsets}")
            return 0
        finally:
            # A recording must not be left running by an aborted run.
            if state_fields(self.raw(mode="getstate")).get("rec") == "on":
                print("      a recording is still running: stopping it")
                self.raw(mode="camcmd", value="video_recstop")
            self.finish()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Test of live view, recording, settings and events")
    parser.add_argument("--ip", required=True, help="the camera's address")
    parser.add_argument("--name", default="lumix-remote",
                        help="connection name as shown on the camera")
    parser.add_argument("--port", type=int, default=49199, help="local UDP port")
    parser.add_argument("--wait", type=float, default=90.0,
                        help="seconds to wait for a start or stop on the camera")
    parser.add_argument("--manual-only", action="store_true",
                        help="only the two recordings stopped and started on the camera")
    parser.add_argument("--outdir", type=pathlib.Path,
                        default=pathlib.Path("out/appprobe"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    try:
        probe = AppProbe(args.ip, args.name, args.port, args.outdir, 50, 1)
        return probe.run_manual(args.wait) if args.manual_only else probe.run_all(args.wait)
    except LumixError as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
