"""UDP receiver for the LUMIX live stream.

The camera sends one complete JPEG per datagram, preceded by a proprietary
header. Its length is undocumented and varies with firmware and picture mode,
so the code searches for the SOI marker instead of assuming a fixed offset.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

SOI = b"\xff\xd8\xff"   # Start of Image
EOI = b"\xff\xd9"       # End of Image
MAX_DATAGRAM = 65535

# SOF markers that carry the image dimensions. C4/C8/CC are Huffman/arithmetic.
SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def extract_jpeg(datagram: bytes) -> tuple[int, bytes] | None:
    """Cut off the header. Returns (offset, JPEG bytes) or None."""
    offset = datagram.find(SOI)
    if offset < 0:
        return None
    return offset, datagram[offset:]


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read width and height from the JPEG markers, without a decoder."""
    i = 2  # skip SOI
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in SOF_MARKERS:
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if seglen < 2:
            return None
        i += 2 + seglen
    return None


@dataclass
class StreamStats:
    datagrams: int = 0
    frames: int = 0
    malformed: int = 0
    bytes_total: int = 0
    started_at: float = field(default_factory=time.monotonic)
    first_frame_at: float | None = None
    last_frame_at: float | None = None
    header_offsets: Counter = field(default_factory=Counter)
    # A sum, not a list of sizes: the receiver runs for days, and a list grew
    # by 30 entries a second.
    frame_bytes: int = 0
    dimensions: Counter = field(default_factory=Counter)
    truncated: int = 0
    gaps: list[tuple[float, float]] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def fps(self) -> float:
        if self.first_frame_at is None or self.last_frame_at is None:
            return 0.0
        span = self.last_frame_at - self.first_frame_at
        return (self.frames - 1) / span if span > 0 else 0.0

    @property
    def avg_frame_size(self) -> float:
        return self.frame_bytes / self.frames if self.frames else 0.0


class LiveStream:
    """Listens on UDP and always holds the most recently received frame."""

    def __init__(self, port: int = 49199, stall_after: float = 2.0,
                 recv_buffer: int = 4 * 1024 * 1024):
        self.port = port
        self.stall_after = stall_after
        self.recv_buffer = recv_buffer
        self.stats = StreamStats()

        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._frame_lock = threading.Lock()
        self._frame_event = threading.Condition(self._frame_lock)
        self._latest: bytes | None = None
        # The header before the JPEG carries exposure values that can't be
        # queried anywhere else, so it is kept instead of thrown away.
        self._latest_header: bytes | None = None

    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._running.is_set():
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.recv_buffer)
        except OSError:
            # macOS caps it via kern.ipc.maxsockbuf; not critical.
            log.debug("rcvbuf_not_set")
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(0.5)

        self._sock = sock
        self._running.set()
        self.stats = StreamStats()
        self._thread = threading.Thread(target=self._loop, name="lumix-udp", daemon=True)
        self._thread.start()
        log.info("udp_listening", extra={"port": self.port})

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._sock:
            self._sock.close()
            self._sock = None

    # ------------------------------------------------------------------ #

    def _loop(self) -> None:
        assert self._sock is not None
        last_seen: float | None = None

        while self._running.is_set():
            try:
                datagram, _addr = self._sock.recvfrom(MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                break

            now = time.monotonic()
            self.stats.datagrams += 1
            self.stats.bytes_total += len(datagram)

            if last_seen is not None and now - last_seen > self.stall_after:
                self.stats.gaps.append((last_seen, now))
                log.warning("stream_gap", extra={"seconds": round(now - last_seen, 1)})
            last_seen = now

            result = extract_jpeg(datagram)
            if result is None:
                self.stats.malformed += 1
                continue

            offset, frame = result
            self.stats.header_offsets[offset] += 1
            self.stats.frames += 1
            self.stats.frame_bytes += len(frame)
            if not frame.endswith(EOI):
                self.stats.truncated += 1
            if self.stats.first_frame_at is None:
                self.stats.first_frame_at = now
                dims = jpeg_dimensions(frame)
                if dims:
                    self.stats.dimensions[dims] += 1
            elif self.stats.frames % 100 == 0:
                dims = jpeg_dimensions(frame)
                if dims:
                    self.stats.dimensions[dims] += 1
            self.stats.last_frame_at = now

            with self._frame_event:
                self._latest = frame
                self._latest_header = datagram[:offset]
                self._frame_event.notify_all()

    # ------------------------------------------------------------------ #

    @property
    def latest_frame(self) -> bytes | None:
        with self._frame_lock:
            return self._latest

    @property
    def latest_header(self) -> bytes | None:
        """Proprietary header of the most recently received datagram."""
        with self._frame_lock:
            return self._latest_header

    def wait_for_frame(self, timeout: float = 5.0) -> bytes | None:
        """Blocks until the next frame. For the MJPEG output."""
        with self._frame_event:
            if self._frame_event.wait(timeout):
                return self._latest
        return None

    @property
    def stalled(self) -> bool:
        """True if nothing has arrived for stall_after seconds."""
        if self.stats.last_frame_at is None:
            return self.stats.elapsed > self.stall_after
        return time.monotonic() - self.stats.last_frame_at > self.stall_after
