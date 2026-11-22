"""Listing the camera's cards and downloading files from them.

The protocol is in docs/protocol.md ("Listing and downloading the card's
contents"): a UPnP Browse on port 60606 names the files, a plain HTTP GET on
port 50001 fetches them, and `Range` resumes a broken download.

One worker thread does all of that traffic, one request at a time. Whether
the camera serves two files at once is unmeasured, so it is never asked to.
The state query of the controller keeps running beside it as the keepalive.

A file that is being fetched is called `<name>.<size>.part` in the download
folder and is renamed when it is complete, so a file with its final name is
always whole. A download that stopped for any reason (cancelled, the camera
gone, the container restarted) continues from the part file.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

from .camera import DESCRIPTOR_PORT, LumixError

log = logging.getLogger("lumixremote.media")

CDS_PATH = "/Server0/CDS_control"
SOAP_ACTION = "urn:schemas-upnp-org:service:ContentDirectory:1#Browse"
DLNA_AGENT = "Panasonic MIL DLNA CP UPnP/1.0"
DOWNLOAD_AGENT = "LUMIX Sync"

THUMBNAIL_KINDS = ("CAM_TN", "CAM_LRGTN")
# An unmapped VOLUME is an anonymous Docker volume: it lives in Docker's own
# storage and would fill it with video.
ANONYMOUS_VOLUME = re.compile(r"/volumes/[0-9a-f]{64}/_data$")


# ------------------------------------------------------------- parsing ---

def browse_envelope(object_id: str, start: int, count: int) -> str:
    """The Browse request exactly as the phone app sends it."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1" '
        'xmlns:pana="urn:schemas-panasonic-com:pana">'
        f"<ObjectID>{object_id}</ObjectID>"
        "<BrowseFlag>BrowseDirectChildren</BrowseFlag><Filter>*</Filter>"
        f"<StartingIndex>{start}</StartingIndex>"
        f"<RequestedCount>{count}</RequestedCount>"
        "<SortCriteria></SortCriteria>"
        "<pana:X_FromCP>LumixLink2.0</pana:X_FromCP>"
        "</u:Browse></s:Body></s:Envelope>"
    )


def _local(tag: str) -> str:
    """Tag name without its namespace."""
    return tag.rsplit("}", 1)[-1]


@dataclass
class Resource:
    """One <res> of a listed item: an address and what it leads to."""

    url: str
    mime: str = ""
    kind: str = ""           # PANASONIC.COM_PN, e.g. CAM_ORG
    filename: str = ""       # OriginalFileName
    size: str = ""
    duration: str = ""


@dataclass
class Entry:
    """One listed <item> or <container>."""

    id: str
    is_container: bool
    title: str = ""
    date: str = ""
    upnp_class: str = ""
    resources: list[Resource] = field(default_factory=list)


@dataclass
class BrowseResult:
    entries: list[Entry]
    returned: int
    total: int


def parse_protocol_info(value: str) -> tuple[str, str, str]:
    """Split a protocolInfo into (MIME type, kind, original file name).

    The form is `http-get:*:<mime>:<key>=<value>;<key>=<value>`, with the keys
    PANASONIC.COM_PN and OriginalFileName among them.
    """
    parts = value.split(":", 3)
    mime = parts[2] if len(parts) > 2 else ""
    extras = parts[3] if len(parts) > 3 else ""
    fields = dict(p.split("=", 1) for p in extras.split(";") if "=" in p)
    name = fields.get("OriginalFileName", "").strip("'\"")
    return mime, fields.get("PANASONIC.COM_PN", ""), name


def parse_browse(text: str) -> BrowseResult:
    """Read a Browse reply: the envelope and the DIDL-Lite document in it."""
    root = ET.fromstring(text)
    values = {_local(el.tag): (el.text or "") for el in root.iter()}
    didl = values.get("Result", "").strip()
    entries: list[Entry] = []
    if didl:
        for node in ET.fromstring(didl):
            kind = _local(node.tag)
            if kind not in ("item", "container"):
                continue
            entry = Entry(id=node.get("id", ""), is_container=kind == "container")
            for child in node:
                name = _local(child.tag)
                if name == "title":
                    entry.title = child.text or ""
                elif name == "date":
                    entry.date = child.text or ""
                elif name == "class":
                    entry.upnp_class = child.text or ""
                elif name == "res":
                    mime, pn, filename = parse_protocol_info(child.get("protocolInfo", ""))
                    entry.resources.append(Resource(
                        url=(child.text or "").strip(), mime=mime, kind=pn,
                        filename=filename, size=child.get("size", ""),
                        duration=child.get("duration", "")))
            entries.append(entry)

    def number(name: str) -> int:
        try:
            return int(values.get(name, ""))
        except ValueError:
            return -1

    return BrowseResult(entries, number("NumberReturned"), number("TotalMatches"))


def safe_name(name: str, fallback: str) -> str:
    """A file name from the camera, reduced to what is safe in a folder."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name or fallback


def folder_problem(path: pathlib.Path | None,
                   mountinfo: pathlib.Path = pathlib.Path("/proc/self/mountinfo")) -> str:
    """Why nothing can be saved to this folder; empty when it is fine."""
    if path is None:
        return "No download folder is configured (LUMIX_DOWNLOAD_DIR)"
    if not path.is_dir():
        return f"The download folder {path} does not exist"
    try:
        for line in mountinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split(" ")
            if len(fields) > 4 and fields[4] == str(path) and ANONYMOUS_VOLUME.search(fields[3]):
                return (f"No folder is mapped to {path}: add a path mapping for "
                        "it in the container's settings")
    except OSError:
        pass   # not Linux, or no /proc: nothing to check
    if not os.access(path, os.W_OK | os.X_OK):
        return f"The download folder {path} is not writable for the app"
    return ""


# ---------------------------------------------------------------- files ---

@dataclass
class MediaFile:
    """One file to fetch: the original behind a listed item."""

    url: str
    name: str
    size: int
    kind: str


@dataclass
class MediaItem:
    """One entry of the listing as the interface shows it."""

    id: str
    title: str
    files: list[MediaFile]
    # Card slot the item is on ("sd1", "sd2"); empty on a one-slot camera.
    slot: str = ""
    thumb_url: str = ""
    duration: str = ""
    video: bool = False
    # "", "queued", "active", "done" or "failed"
    state: str = ""
    done: int = 0
    error: str = ""
    # The revision in which this item last changed for the interface.
    rev: int = 0

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def name(self) -> str:
        return ", ".join(f.name for f in self.files) or self.title


def item_from_entry(entry: Entry) -> MediaItem | None:
    """Turn a listed entry into an item; None for folders and empty entries."""
    if entry.is_container:
        return None
    files: list[MediaFile] = []
    thumb = ""
    duration = ""
    for res in entry.resources:
        if not res.url.startswith("http"):
            continue
        if res.kind in THUMBNAIL_KINDS:
            if res.kind == "CAM_TN" or not thumb:
                thumb = res.url
            continue
        fallback = res.url.rsplit("/", 1)[-1] or f"{entry.id}.bin"
        try:
            size = int(res.size)
        except ValueError:
            size = 0
        files.append(MediaFile(url=res.url, name=safe_name(res.filename, safe_name(fallback, entry.id)),
                               size=size, kind=res.kind or res.mime))
        duration = duration or res.duration
    if not files:
        return None
    return MediaItem(id=entry.id, title=entry.title, files=files, thumb_url=thumb,
                     duration=duration, video="videoItem" in entry.upnp_class)


class Session(Protocol):
    """What the downloader needs from the controller."""

    @property
    def media_wanted(self) -> bool: ...
    @property
    def media_ready(self) -> bool: ...
    def media_headers(self) -> dict[str, str]: ...
    def content_count(self) -> int: ...
    def card_slots(self) -> list[str]: ...
    def select_slot(self, slot: str) -> None: ...


class NoSpace(LumixError):
    """The download folder is too full for this file; others may still fit."""


class Cancelled(Exception):
    """The running download was cancelled or the media view was closed."""


class MediaManager:
    """Lists the cards and works through the download queue."""

    PAGE = 50                # entries per Browse call, as the phone app
    CHUNK = 256 * 1024
    RETRY_WAIT = 5.0         # between two attempts on the same file
    GIVE_UP_AFTER = 900.0    # without a single new byte
    INDEX_WAIT = 60.0        # for the camera to build its index
    THUMB_CACHE = 2000
    RESERVE = 64 * 1024 * 1024   # free space kept on the target
    # The listing and the file addresses refer to the active card slot. After
    # a change the listing was read two seconds later on the device.
    SLOT_SETTLE = 2.0
    # A run started by a script: how long the camera may take to reach
    # playback, and how often the listing may fail, before the run gives up.
    SYNC_READY_WAIT = 120.0
    SYNC_LIST_TRIES = 5

    def __init__(self, session: Session, camera_ip: str,
                 folder: pathlib.Path | None,
                 transport: httpx.BaseTransport | None = None):
        self.session = session
        self.camera_ip = camera_ip
        self.folder = folder
        self._http = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0), transport=transport)
        self._cond = threading.Condition()
        self._stop = False
        self._items: dict[str, MediaItem] = {}
        self._listed = False
        self._listing = False
        self._list_error = ""
        self._relist = False
        self._queue: deque[str] = deque()
        self._cancel = False
        # Set while the view is being closed: whatever the worker is doing
        # ends, and nothing new starts.
        self._closed = False
        self._working = False
        # Counts every change the interface has to show (not the progress of
        # a download). Each item remembers the revision of its last change,
        # so a poll gets only the items that changed since the one it knows;
        # `_list_rev` is the last revision in which the list itself changed.
        self._rev = 0
        self._list_rev = 0
        # Counts the resets, so a listing that was under way during one
        # knows that its result is nobody's any more.
        self._generation = 0
        self._folder_checked = 0.0
        # Since when the camera has not been ready during a run's listing.
        self._sync_unready_since: float | None = None
        self._sync_list_failures = 0
        # The run a script started, or the last one: see sync_state().
        self._sync: dict = {"status": "idle", "step": "", "error": "",
                            "started": None, "finished": None, "files": []}
        self._sync_leave: Callable[[], None] | None = None
        self._sync_present: set[str] = set()
        self._folder_state: tuple[str, int | None] = ("", None)
        self._thumbs: dict[str, bytes] = {}
        # The slot this thread last made active; None when unknown.
        self._slot: str | None = None
        self._rate: deque[tuple[float, int]] = deque(maxlen=64)
        self._thread = threading.Thread(target=self._run, name="lumix-media", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ #
    # Called from the web layer
    # ------------------------------------------------------------------ #

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        # Short: the controller's stopstream comes after this. A request
        # that still hangs ends when its connection is closed.
        self._thread.join(timeout=2.0)
        self._http.close()

    def refresh(self) -> None:
        with self._cond:
            self._relist = True
            self._cond.notify_all()

    def close(self, leave: Callable[[], None], wait: float = 3.0) -> None:
        """Close the media view: stop the download, forget the listing, give
        the worker a moment to let go of the camera, and only then call
        `leave`, which takes the camera out of the media mode."""
        deadline = time.monotonic() + wait
        with self._cond:
            self._closed = True
            if self._sync["status"] == "running":
                self._end_sync_locked("cancelled", "The cards were closed")
            self._reset_locked()
            self._cond.notify_all()
            while self._working and time.monotonic() < deadline:
                self._cond.wait(0.1)
        try:
            leave()
        finally:
            with self._cond:
                self._closed = False
                self._cond.notify_all()

    # ------------------------------------------------------------------ #
    # A whole run for a script: open the cards, save everything that is
    # not in the folder yet, close the cards again.
    # ------------------------------------------------------------------ #

    def start_sync(self, enter: Callable[[], None], leave: Callable[[], None]) -> dict:
        """Start a run. `enter` puts the camera into the media mode (and
        refuses while it records), `leave` takes it out again at the end."""
        problem = folder_problem(self.folder)
        if problem:
            raise LumixError(problem)
        with self._cond:
            if self._sync["status"] == "running":
                raise LumixError("A run is already under way")
            if self._queue:
                raise LumixError("A download is already under way")
        enter()
        with self._cond:
            self._sync = {"status": "running", "step": "listing", "error": "",
                          "started": time.time(), "finished": None, "files": []}
            self._sync_unready_since = None
            self._sync_list_failures = 0
            self._sync_leave = leave
            self._relist = True
            self._cond.notify_all()
        log.info("media_sync_started")
        return self.sync_state()

    def sync_state(self) -> dict:
        """The running or the last run, with the progress of a running one."""
        with self._cond:
            state = dict(self._sync)
            if state["status"] == "running":
                state["files"] = self._sync_files_locked()
            pending = [i for i in self._items.values() if i.state in ("queued", "active")]
            state["remaining"] = sum(max(i.size - i.done, 0) for i in pending)
            state["rate"] = self._rate_locked()
            state["folder"] = str(self.folder) if self.folder else ""
            return state

    def _sync_files_locked(self) -> list[dict]:
        """One entry per file on the cards: saved now, there before, or failed."""
        files = []
        for item in self._items.values():
            if item.state == "done":
                result = "present" if item.id in self._sync_present else "saved"
            else:
                result = item.state or "pending"
            files.append({"name": item.name, "card": item.slot, "size": item.size,
                          "result": result, "error": item.error})
        return files

    def _end_sync_locked(self, status: str, error: str = "") -> None:
        self._sync.update(status=status, step="", error=error, finished=time.time(),
                          files=self._sync_files_locked())
        self._sync_leave = None
        log.info("media_sync_ended", extra={"status": status, "error": error})

    def _sync_job_locked(self):
        """What the run needs done next, if anything; called by the worker."""
        if self._sync["status"] != "running":
            return None
        if not self.session.media_wanted:
            # The controller left the media mode by itself.
            self._end_sync_locked("failed", "The cards were closed: a recording started")
            return None
        if self._sync["step"] == "downloading" and not self._queue:
            # Decided before asking whether the camera is ready: a download
            # that gave up on a silent camera has to end the run too.
            failed = [i for i in self._items.values() if i.state == "failed"]
            # A failure that is not about space empties the queue; what was
            # left then counts as not saved either.
            unsaved = [i for i in self._items.values() if i.state != "done"]
            if failed or unsaved:
                reason = failed[0].error if failed else "not every file was saved"
                return lambda: self._finish_sync("failed", reason)
            return lambda: self._finish_sync("done")
        if not self.session.media_ready:
            now = time.monotonic()
            if self._sync_unready_since is None:
                self._sync_unready_since = now
            if (self._sync["step"] == "listing"
                    and now - self._sync_unready_since > self.SYNC_READY_WAIT):
                return lambda: self._finish_sync("failed", "The camera did not reach playback")
            return None
        self._sync_unready_since = None
        if self._sync["step"] == "listing" and self._listed and not self._relist:
            self._sync_present = {i.id for i in self._items.values() if i.state == "done"}
            for item in self._items.values():
                if item.state != "done":
                    item.state, item.error = "queued", ""
                    self._changed_locked(item)
                    self._queue.append(item.id)
            self._sync["step"] = "downloading"
        if self._sync["step"] == "listing" and self._sync_list_failures >= self.SYNC_LIST_TRIES:
            return lambda: self._finish_sync("failed", self._list_error)
        return None

    def _finish_sync(self, status: str, error: str = "") -> None:
        """End the run and give the camera back; runs in the worker."""
        with self._cond:
            leave = self._sync_leave
            self._end_sync_locked(status, error)
            self._reset_locked()
        if leave:
            try:
                leave()
            except LumixError as exc:
                log.warning("media_sync_leave_failed", extra={"error": str(exc)})

    def _reset_locked(self) -> None:
        self._cancel_locked()
        self._listed = False
        self._list_error = ""
        self._items = {}
        self._thumbs = {}
        self._slot = None
        self._rev += 1
        self._list_rev = self._rev
        self._generation += 1

    def _changed_locked(self, item: MediaItem) -> None:
        """Note that an item looks different now."""
        self._rev += 1
        item.rev = self._rev

    def enqueue(self, ids: list[str] | None) -> int:
        """Queue the given items, or all that are not done yet. Returns how many."""
        problem = folder_problem(self.folder)
        if problem:
            raise LumixError(problem)
        with self._cond:
            if not self._listed:
                raise LumixError("The card has not been listed yet")
            wanted = list(self._items) if ids is None else ids
            added = 0
            for item_id in wanted:
                item = self._items.get(item_id)
                if item is None or item.state in ("queued", "active", "done"):
                    continue
                item.state, item.error = "queued", ""
                self._changed_locked(item)
                self._queue.append(item_id)
                added += 1
            self._cond.notify_all()
            return added

    def cancel(self) -> None:
        with self._cond:
            if self._sync["status"] == "running":
                # Somebody is at the interface: the run ends, the view stays.
                self._end_sync_locked("cancelled", "Cancelled in the interface")
            self._cancel_locked()
            self._cond.notify_all()

    def _cancel_locked(self) -> None:
        self._cancel = True
        for item_id in self._queue:
            item = self._items.get(item_id)
            if item and item.state == "queued":
                item.state = ""
                self._changed_locked(item)
        self._queue.clear()

    def thumbnail(self, item_id: str) -> bytes | None:
        with self._cond:
            return self._thumbs.get(item_id)

    @property
    def busy(self) -> bool:
        """Is a download running or waiting?"""
        with self._cond:
            return bool(self._queue) or any(i.state == "active" for i in self._items.values())

    def _folder(self) -> tuple[str, int | None]:
        """(why nothing can be saved, free bytes), looked up every few seconds:
        every viewer asks once a second, and the folder may be a network share."""
        now = time.monotonic()
        if now - self._folder_checked >= 5.0 or not self._folder_checked:
            problem = folder_problem(self.folder)
            free = None
            if not problem:
                try:
                    free = shutil.disk_usage(self.folder).free
                except OSError:
                    pass
            self._folder_state, self._folder_checked = (problem, free), now
        return self._folder_state

    def snapshot(self, known_rev: int | None = None) -> dict:
        """The state for the interface. `known_rev` is the revision of the
        reply the caller already has: it then gets `changed`, only the items
        that look different since, instead of the whole list in `items`. A
        card can hold thousands of files, and a download changes one."""
        problem, free = self._folder()

        def row(i: MediaItem) -> dict:
            return {"id": i.id, "name": i.name, "title": i.title, "size": i.size,
                    "duration": i.duration, "video": i.video, "state": i.state,
                    "slot": i.slot, "done": i.done, "error": i.error,
                    "thumb": bool(self._thumbs.get(i.id))}

        with self._cond:
            items = changed = None
            if known_rev is None or not self._list_rev <= known_rev <= self._rev:
                items = [row(i) for i in self._items.values()]
            else:
                changed = [row(i) for i in self._items.values() if i.rev > known_rev]
            pending = [i for i in self._items.values() if i.state in ("queued", "active")]
            remaining = sum(max(i.size - i.done, 0) for i in pending)
            rate = self._rate_locked()
            return {
                "rev": self._rev,
                "changed": changed,
                "progress": {i.id: i.done for i in pending if i.state == "active"},
                "queued_size": sum(i.size for i in pending),
                "wanted": self.session.media_wanted,
                "ready": self.session.media_ready,
                "listed": self._listed,
                "listing": self._listing,
                "list_error": self._list_error,
                "folder_problem": problem,
                "free": free,
                "items": items,
                "active": any(i.state == "active" for i in pending),
                "queued": len(pending),
                "remaining": remaining,
                "rate": rate,
                "eta": round(remaining / rate) if rate > 0 and remaining else None,
            }

    def _rate_locked(self) -> float:
        """Bytes per second over the last seconds of the running download."""
        if len(self._rate) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self._rate[0], self._rate[-1]
        if t1 - t0 < 0.5 or time.monotonic() - t1 > 10:
            return 0.0
        return max(b1 - b0, 0) / (t1 - t0)

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stop:
                    return
                job = self._next_job_locked()
                if job is None:
                    self._cond.wait(1.0)
                    continue
                self._working = True
            try:
                job()
            except Cancelled:
                pass
            except Exception:
                # A bug here must not end the thread: the view would hang.
                log.exception("media_worker_error")
                time.sleep(self.RETRY_WAIT)
            finally:
                with self._cond:
                    self._working = False
                    self._cond.notify_all()

    def _next_job_locked(self):
        if self._closed:
            return None
        job = self._sync_job_locked()
        if job is not None:
            return job
        if not self.session.media_wanted:
            # The media mode is over, also when the controller ended it by
            # itself (a recording started on the camera): the next view
            # starts fresh.
            if self._listed:
                self._reset_locked()
            return None
        if not self.session.media_ready:
            return None
        if self._relist or not self._listed:
            self._relist = False
            if not self._queue:     # never relist under a running download
                self._listing = True
                return self._list
        if self._queue:
            return self._download_next
        if len(self._thumbs) < self.THUMB_CACHE:
            missing = [i for i in self._items.values()
                       if i.thumb_url and i.id not in self._thumbs]
            # Those on the active slot first: every change of slot costs time.
            missing.sort(key=lambda i: i.slot != self._slot)
            if missing:
                return lambda item=missing[0]: self._fetch_thumbnail(item)
        return None

    # -- listing --

    def _list(self) -> None:
        error = ""
        items: dict[str, MediaItem] = {}
        generation = self._generation
        try:
            self._slot = None
            for slot in self.session.card_slots() or [""]:
                self._use_slot(slot)
                total = self._wait_for_index()
                start = 0
                while start < total:
                    result = self._browse("0", start, min(self.PAGE, total - start))
                    if not result.entries:
                        break
                    for entry in result.entries:
                        item = item_from_entry(entry)
                        if item is not None:
                            # The same number can exist on both cards.
                            item.slot = slot
                            item.id = f"{slot}:{item.id}" if slot else item.id
                            items[item.id] = item
                    start += len(result.entries)
                    if result.total >= 0:
                        total = result.total
                log.info("media_listed", extra={"slot": slot or "-", "items": total})
            self._mark_present(items)
        except (LumixError, httpx.HTTPError, ET.ParseError) as exc:
            error = f"The cards could not be listed: {exc}"
            log.warning("media_list_failed", extra={"error": str(exc)})
        finally:
            with self._cond:
                self._listing = False
        with self._cond:
            if generation != self._generation:
                return      # closed meanwhile: this listing is nobody's
            self._list_error = error
            if not error:
                self._items = items
                self._listed = True
                self._rev += 1
                self._list_rev = self._rev
                # Kept across a refresh; a thumbnail that failed is asked again.
                self._thumbs = {k: v for k, v in self._thumbs.items() if k in items and v}
                self._sync_list_failures = 0
            else:
                self._sync_list_failures += 1
        if error:
            time.sleep(self.RETRY_WAIT)

    def _wait_for_index(self) -> int:
        """Number of files; the camera reports -1 while it builds its index."""
        deadline = time.monotonic() + self.INDEX_WAIT
        while True:
            count = self.session.content_count()
            if count >= 0:
                return count
            if time.monotonic() >= deadline:
                raise LumixError("the camera is still building its index")
            time.sleep(1.0)

    def _browse(self, object_id: str, start: int, count: int) -> BrowseResult:
        url = f"http://{self.camera_ip}:{DESCRIPTOR_PORT}{CDS_PATH}"
        r = self._http.post(url, content=browse_envelope(object_id, start, count).encode(),
                            headers={"Content-Type": 'text/xml; charset="utf-8"',
                                     "SOAPAction": SOAP_ACTION, "User-Agent": DLNA_AGENT})
        if r.status_code != 200:
            raise LumixError(f"listing returned HTTP {r.status_code}")
        return parse_browse(r.text)

    def _mark_present(self, items: dict[str, MediaItem]) -> None:
        """Files that are already whole in the folder count as done."""
        if folder_problem(self.folder):
            return
        for item in items.values():
            if all(self._present(f) for f in item.files):
                item.state, item.done = "done", item.size

    def _present(self, file: MediaFile) -> bool:
        try:
            return file.size > 0 and (self.folder / file.name).stat().st_size == file.size
        except OSError:
            return False

    def _use_slot(self, slot: str) -> None:
        """Make the card slot active that the next requests refer to."""
        if not slot or slot == self._slot:
            return
        with self._cond:
            if self._gone_locked():
                raise Cancelled()
        self.session.select_slot(slot)
        time.sleep(self.SLOT_SETTLE)
        self._slot = slot

    def _fetch_thumbnail(self, item: MediaItem) -> None:
        data = b""
        try:
            self._use_slot(item.slot)
            r = self._http.get(item.thumb_url, headers={
                "User-Agent": DOWNLOAD_AGENT, **self.session.media_headers()})
            if r.status_code == 200:
                data = r.content
        except (httpx.HTTPError, LumixError, Cancelled) as exc:
            log.debug("thumbnail_failed", extra={"error": str(exc)})
        with self._cond:
            if not self._closed and item.id in self._items:
                # An empty entry keeps a broken thumbnail from being asked
                # again before the next refresh.
                self._thumbs[item.id] = data
                self._changed_locked(item)

    # -- download --

    def _download_next(self) -> None:
        with self._cond:
            if not self._queue:
                return
            item = self._items.get(self._queue.popleft())
            if item is None:
                return
            item.state, item.done, item.error = "active", 0, ""
            self._cancel = False
            self._rate.clear()
            self._changed_locked(item)
        started = time.monotonic()
        try:
            base = 0
            for file in item.files:
                self._download_file(item, file, base)
                base += file.size
            with self._cond:
                item.state, item.done = "done", item.size
                self._changed_locked(item)
            log.info("media_downloaded", extra={
                "file": item.name, "bytes": item.size,
                "seconds": round(time.monotonic() - started)})
        except Cancelled:
            with self._cond:
                item.state = ""
                self._changed_locked(item)
            log.info("media_download_cancelled", extra={"file": item.name})
        except (LumixError, OSError) as exc:
            with self._cond:
                item.state, item.error = "failed", str(exc)
                self._changed_locked(item)
                if not isinstance(exc, NoSpace):
                    # What comes next would fail the same way.
                    self._cancel_locked()
            log.warning("media_download_failed", extra={"file": item.name, "error": str(exc)})

    def _target(self, file: MediaFile) -> pathlib.Path:
        """Where the file goes. A different file of the same name is kept."""
        target = self.folder / file.name
        if target.exists() and target.stat().st_size != file.size:
            stem, dot, ext = file.name.rpartition(".")
            for n in range(2, 1000):
                other = self.folder / (f"{stem}-{n}.{ext}" if dot else f"{file.name}-{n}")
                if not other.exists() or other.stat().st_size == file.size:
                    return other
        return target

    def _download_file(self, item: MediaItem, file: MediaFile, base: int) -> None:
        problem = folder_problem(self.folder)
        if problem:
            raise LumixError(problem)
        target = self._target(file)
        if target.exists() and file.size and target.stat().st_size == file.size:
            self._progress(item, base + file.size)
            return
        part = target.with_name(f"{target.name}.{file.size}.part")
        last_progress = time.monotonic()

        while True:
            self._check_cancel()
            offset = part.stat().st_size if part.exists() else 0
            if file.size and offset > file.size:
                part.unlink()
                offset = 0
            if file.size and offset == file.size:
                break
            free = shutil.disk_usage(self.folder).free
            if file.size and free < file.size - offset + self.RESERVE:
                raise NoSpace(
                    f"not enough space in the download folder for {file.name}")
            try:
                try:
                    self._use_slot(item.slot)
                except LumixError as exc:
                    # The session may just be coming back after a dropout;
                    # that is an interruption, not the end of the download.
                    raise httpx.TransportError(f"card slot not selected: {exc}") from exc
                got = self._fetch(item, file, part, offset, base)
            except httpx.HTTPError as exc:
                got = part.stat().st_size - offset if part.exists() else 0
                log.info("media_download_interrupted", extra={
                    "file": file.name, "at": offset + max(got, 0), "error": str(exc)})
            else:
                if not file.size:
                    break       # size unknown: the reply's end is the file's end
            if got > 0:
                last_progress = time.monotonic()
            elif time.monotonic() - last_progress > self.GIVE_UP_AFTER:
                raise LumixError(
                    f"the camera sent nothing for {self.GIVE_UP_AFTER / 60:.0f} minutes")
            if part.exists() and file.size and part.stat().st_size == file.size:
                break
            self._wait_for_camera()

        os.replace(part, target)
        self._progress(item, base + (file.size or target.stat().st_size))

    def _fetch(self, item: MediaItem, file: MediaFile, part: pathlib.Path,
               offset: int, base: int) -> int:
        """One request; appends to the part file. Returns the bytes received."""
        headers = {"User-Agent": DOWNLOAD_AGENT, **self.session.media_headers()}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        received = 0
        with self._http.stream("GET", file.url, headers=headers) as r:
            if offset and r.status_code == 416:
                # The part file reaches past the file's real end: the size in
                # the listing was wrong. Start over; the reply then says it.
                part.unlink(missing_ok=True)
                return 0
            actual = self._size_from(r)
            if actual and actual != file.size:
                # The reply knows the size better than the listing, where
                # placeholders were seen (for the preview pictures).
                log.info("media_size_corrected", extra={
                    "file": file.name, "listed": file.size, "actual": actual})
                with self._cond:
                    file.size = actual
                    self._changed_locked(item)
            if offset and r.status_code == 200:
                offset = 0      # the camera ignored Range: start over
            elif offset and (r.status_code != 206 or not r.headers.get(
                    "Content-Range", "").startswith(f"bytes {offset}-")):
                raise LumixError(f"{file.name}: the camera answered a resumed "
                                 f"download with HTTP {r.status_code}")
            elif not offset and r.status_code != 200:
                raise LumixError(f"{file.name}: HTTP {r.status_code}")
            with part.open("ab" if offset else "wb") as out:
                for chunk in r.iter_bytes(self.CHUNK):
                    out.write(chunk)
                    received += len(chunk)
                    self._progress(item, base + offset + received)
                    self._check_cancel()
                out.flush()
                os.fsync(out.fileno())
        return received

    @staticmethod
    def _size_from(r: httpx.Response) -> int:
        """The file's whole size as the reply states it; 0 when it doesn't."""
        if r.status_code == 206:
            # Only Content-Range is known to name the whole file here.
            total = r.headers.get("Content-Range", "").rpartition("/")[2]
        else:
            total = r.headers.get("X-FILE_SIZE", "")
        try:
            return int(total)
        except ValueError:
            return 0

    def _progress(self, item: MediaItem, done: int) -> None:
        with self._cond:
            item.done = done
            self._rate.append((time.monotonic(), done))

    def _gone_locked(self) -> bool:
        """Is there nobody left to work for?"""
        return self._stop or self._closed or not self.session.media_wanted

    def _check_cancel(self) -> None:
        with self._cond:
            if self._cancel or self._gone_locked():
                raise Cancelled()

    def _wait_for_camera(self) -> None:
        """Pause before the next attempt, and until the camera is back."""
        self._slot = None       # a camera that was away may have changed it
        deadline = time.monotonic() + self.RETRY_WAIT
        while time.monotonic() < deadline or not self.session.media_ready:
            self._check_cancel()
            if time.monotonic() > deadline + self.GIVE_UP_AFTER:
                raise LumixError("the camera did not come back")
            time.sleep(0.5)
