"""Diagnostic run, made before the interface was built.

Answers in one pass:
  * Which frame rate and resolution does the camera really deliver?
  * Does the JPEG start always sit at the same byte position?
  * Does the stream hold without kicking, or does it dry up?
  * Does the session survive a minute without commands?

Usage:
    python3 -m lumixremote.probe --config config.toml
    python3 -m lumixremote.probe --ip 192.0.2.10 --duration 90 --test-commands
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import time
import tomllib

from .camera import Camera, LumixError
from .stream import LiveStream

log = logging.getLogger("probe")


def load_config(path: pathlib.Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"Configuration {path} is missing. Copy config.example.toml and set the IP."
        )
    with path.open("rb") as fh:
        return tomllib.load(fh)


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def run_probe(ip: str, device_name: str, port: int, duration: float,
              restart_on_stall: bool, test_commands: bool,
              outdir: pathlib.Path) -> int:
    """Run the diagnosis. However it ends (Ctrl+C, a refused command, an
    error), the session is released: without stopstream the camera stays
    locked until it is switched off and on."""
    cam = Camera(ip, device_name=device_name)
    try:
        return _measure(cam, ip, port, duration, restart_on_stall,
                        test_commands, outdir)
    finally:
        if cam.connected:
            cam.stop_stream()
        cam.close()


def _measure(cam: Camera, ip: str, port: int, duration: float,
             restart_on_stall: bool, test_commands: bool,
             outdir: pathlib.Path) -> int:
    print(f"\n[1/5] Fetching the descriptor from {ip}")
    info = cam.describe()
    print(f"      Model      : {info.model_number} ({info.friendly_name})")
    print(f"      Firmware   : {info.firmware}")
    print(f"      Capability : {info.capabilities or '-'}")
    if not info.supports_remote_view:
        print("      WARNING: no CPRemoteView reported")

    print("\n[2/5] Registering (confirm the prompt on the camera's display if shown)")
    session = cam.connect()
    print(f"      Session ID : {session or '(none)'}")

    print("\n[3/5] Setting record mode and reading the state")
    try:
        cam.set_rec_mode()
    except LumixError as exc:
        print(f"      recmode refused: {exc}")
    state = cam.get_state()
    print(f"      Battery    : {state.battery}")
    print(f"      Recording  : {'running' if state.recording else 'stopped'}")
    print(f"      Remaining  : {state.remaining_shots}")

    if test_commands:
        print("\n[3b] Command test: watch the lens and the recording indicator")
        for label, fn in (("Autofocus", cam.autofocus),
                          ("Record start", cam.rec_start)):
            try:
                fn()
                print(f"      {label:<15}: ok")
            except LumixError as exc:
                print(f"      {label:<15}: ERROR {exc}")
            time.sleep(2)
        try:
            cam.rec_stop()
            print(f"      {'Record stop':<15}: ok")
        except LumixError as exc:
            print(f"      {'Record stop':<15}: ERROR {exc}")

    print(f"\n[4/5] Live stream on UDP {port}, running {duration:.0f} s")
    print("      (On the first start the firewall asks about incoming")
    print("       connections for Python: allow them, or nothing arrives.)")

    stream = LiveStream(port=port, source=cam.ip)
    stream.start()
    try:
        return _stream_and_report(cam, stream, port, duration,
                                  restart_on_stall, outdir)
    finally:
        stream.stop()


def _stream_and_report(cam: Camera, stream: LiveStream, port: int,
                       duration: float, restart_on_stall: bool,
                       outdir: pathlib.Path) -> int:
    time.sleep(0.3)  # receiver surely up before the camera sends

    restarts = 0
    keepalives = 0
    session_lost = False

    try:
        cam.start_stream(port)
    except LumixError as exc:
        print(f"      startstream refused: {exc}")
        return 1

    deadline = time.monotonic() + duration
    next_report = time.monotonic() + 10
    last_restart = 0.0

    while time.monotonic() < deadline:
        time.sleep(0.25)
        now = time.monotonic()

        if stream.stalled and now - last_restart > 3.0:
            if restart_on_stall:
                try:
                    cam.start_stream(port)
                    restarts += 1
                    last_restart = now
                    print(f"      [{now - stream.stats.started_at:5.0f}s] "
                          f"stream kicked (#{restarts})")
                except LumixError as exc:
                    print(f"      Kick failed: {exc}")
                    session_lost = True
                    break
            else:
                print(f"      [{now - stream.stats.started_at:5.0f}s] "
                      f"stream stalled, not kicking (--no-restart)")
                last_restart = now

        if now >= next_report:
            s = stream.stats
            print(f"      [{s.elapsed:5.0f}s] {s.frames:5d} frames, "
                  f"{s.fps:4.1f} fps, {human_bytes(s.avg_frame_size)}/frame")
            next_report = now + 10
            # Keepalive test: query the state and log the reaction
            try:
                cam.get_state()
                keepalives += 1
            except LumixError as exc:
                print(f"      Session lost after {s.elapsed:.0f}s: {exc}")
                session_lost = True
                break

    stream.stop()
    try:
        cam.stop_stream()
    except LumixError:
        pass
    # Released; run_probe doesn't send stopstream a second time.
    cam.session_id = None

    # ------------------------------------------------------------------ #
    print("\n[5/5] Results")
    s = stream.stats

    if s.frames == 0:
        print("      Not a single frame received.")
        print("      Check: firewall, same subnet, port free?")
        return 1

    outdir.mkdir(parents=True, exist_ok=True)
    sample = outdir / "probe_frame.jpg"
    frame = stream.latest_frame
    if frame:
        sample.write_bytes(frame)

    offsets = s.header_offsets.most_common()
    dims = s.dimensions.most_common(1)

    print(f"      Datagrams       : {s.datagrams}")
    print(f"      Frames          : {s.frames}")
    print(f"      Frame rate      : {s.fps:.1f} fps")
    print(f"      Average frame   : {human_bytes(s.avg_frame_size)}")
    print(f"      Data rate       : {human_bytes(s.bytes_total / max(s.elapsed, 1))}/s")
    print(f"      Resolution      : {dims[0][0][0]}x{dims[0][0][1]}" if dims
          else "      Resolution      : not determinable")
    print(f"      Header offset   : {', '.join(f'{o} ({n}x)' for o, n in offsets[:5])}")
    print(f"      {'-> constant' if len(offsets) == 1 else '-> VARIABLE, search dynamically'}")
    print(f"      Without SOI     : {s.malformed}")
    print(f"      Truncated       : {s.truncated}")
    print(f"      Gaps > 2 s      : {s.gap_count}")
    print(f"      Kicked          : {restarts}")
    print(f"      Keepalives ok   : {keepalives}")
    print(f"      Session         : {'LOST' if session_lost else 'stable'}")
    if frame:
        print(f"      Sample frame    : {sample}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LUMIX diagnostic run")
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path("config.toml"))
    parser.add_argument("--ip", help="overrides the IP from the configuration")
    parser.add_argument("--name", help="connection name as shown on the camera")
    parser.add_argument("--port", type=int, help="local UDP port")
    parser.add_argument("--duration", type=float, default=60.0, help="measuring time in seconds")
    parser.add_argument("--no-restart", action="store_true",
                        help="do NOT kick the stream when it stalls (endurance test)")
    parser.add_argument("--test-commands", action="store_true",
                        help="also test AF and record start/stop")
    parser.add_argument("--outdir", type=pathlib.Path, default=pathlib.Path("out"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    cfg: dict = {}
    if args.ip is None:
        cfg = load_config(args.config)

    ip = args.ip or cfg.get("camera", {}).get("ip")
    name = args.name or cfg.get("camera", {}).get("device_name", "lumix-remote")
    port = args.port or cfg.get("stream", {}).get("port", 49199)

    if not ip:
        raise SystemExit("No camera address given.")

    try:
        return run_probe(ip, name, port, args.duration,
                         restart_on_stall=not args.no_restart,
                         test_commands=args.test_commands,
                         outdir=args.outdir)
    except LumixError as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
