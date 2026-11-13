# Changelog

All notable changes to this project are listed here. Versions follow
[semantic versioning](https://semver.org/). A change that needs action when
upgrading (a renamed or removed setting, a changed network or volume setup)
only comes with a new major version.

## [3.3.1] - 2026-11-13

### Fixed
- **A recording is no longer started by itself after a long outage.** The
  automatic restart is armed only if the camera was silent for less than ten
  minutes. A camera that was switched off during a recording and on again
  hours later stays idle. A recording that the camera reported as stopped
  while it answered (for example stopped in the LUMIX app) is not started
  again either.
- **One unreadable reply no longer costs the session.** A broken or
  non-200 reply to the state poll is tolerated up to three times in a row;
  before, one such reply dropped the session, and the re-registration ended a
  running recording.
- **Many open live views no longer block the buttons.** Frames are handed to
  the viewers by the server's event loop instead of one worker thread per
  viewer, so with 40 or more browsers open the stop button and the page still
  answer.
- **A broken live-view receiver is reported.** An occupied UDP port or a
  socket that stops is logged (`udp_bind_failed`, `udp_receiver_failed`),
  shown as `stream_error` in `/api/status`, retried at the next stream start,
  and the container's health check (now `/api/health`) turns unhealthy.
- **The camera's identifier stays out of errors.** Error messages (shown in
  the interface and written to the log) no longer contain the registration
  values (the camera's UDN and the device name); `probe` no longer prints the
  session ID.
- **Invalid settings stop the start with `config_invalid`** instead of a
  traceback: a `config.toml` that is not valid TOML, a section that is not a
  table, and values of the wrong type.
- **A home-screen app picks up an update.** The page and `/static` files are
  sent with `Cache-Control: no-cache` (revalidated with their ETag).

### Docs
- The 3.3.0 entry "A recording you stop on purpose stays stopped" is
  imprecise: registering alone does not arm the restart. It is armed by a
  recording that was running when the session was lost, or one that is seen
  running at the first state poll after a registration, and then applies
  only within 90 seconds of that registration.
- `unraid/README-UNRAID.md` describes installing the template by copying it to
  `/boot/config/plugins/dockerMan/templates-user/` (Unraid no longer has
  template repositories), lists `LUMIX_ALLOWED_HOSTS`, warns that a missing
  `br0` makes Unraid show the network as "none", and no longer shows an old
  start line. The template category is now a valid one.

### Tests
- Tests for the controller loop, the live view, `Controller.stop()`,
  `set_iso`, the settings and the health route.

## [3.3.0] - 2026-11-10

### Added
- **`/api/status` answers `HEAD` and reports the version.** Scripts and
  monitoring can tell which release is running.

### Fixed
- **A recording you stop on purpose stays stopped.** The automatic restart
  now applies only to a recording that was running when the connection was
  lost or when the app registered with the camera (for example after a
  restart of the container), and only for that registration. A recording
  started later, or one stopped on the camera, is no longer started again
  within 90 seconds of a registration. A stop in the app can no longer be
  undone by a status reply that was already on its way.
- **The restart after a second connection loss waits long enough.** The
  timing of the previous registration no longer carries over, so the restart
  waits for the camera's own drop into playback as intended.
- **A timeout is no longer reported as "Mode change refused".** The message
  names the camera's reply only when the camera actually answered.
- **Stop works while the camera records outside record mode.** The button
  stays enabled and the overlay says the recording continues.
- **The live view accepts frames only from the camera.** While the camera
  name can't be resolved, all frames are dropped (before, every sender on
  the network was accepted), and the name is resolved again at each new
  registration.
- **`allowed_hosts` in `config.toml` may be a single name,** and entries
  with a port (`cam.example.com:8443`) match like the host name does.
  Anything else stops the start with `config_invalid`.
- The "Connection lost" text says that the app retries with growing gaps.

### Docs
- The README tag table uses `X.Y.Z` examples; the `3.2.0` changelog heading
  links to its comparison.

## [3.2.0] - 2026-11-09

### Added
- **Full screen for the live view.** The button in the corner of the
  picture, the V key or a double-click or double-tap switch it on and off;
  Esc leaves it. The bar disappears, the red frame and REC stay. The iPhone
  allows real full screen only for videos, so there the bar is hidden.
- **Home screen.** Added to the home screen, the page opens as an app
  without the browser's bars, with the PanaControl icon.
- **The screen stays on in full screen.** Over plain HTTP the browser
  offers no Wake Lock, so a short, silent, muted video plays while full
  screen is on; over HTTPS the Wake Lock is used.

## [3.1.2] - 2026-11-09

### Changed
- Updated dependencies:
  - linuxserver/baseimage-alpine from `d07ad0e` to `7315b19`

## [3.1.1] - 2026-11-03

### Security
- Security updates of the distribution packages, picked up by rebuilding the image:
  - `libexpat` 2.10.0-r0 (CVE-2026-117610, CVE-2026-118370)

## [3.1.0] - 2026-11-01

### Added
- **A recording cut short by a camera outage is started again.** The
  camera at times stops answering for a minute or two and then refuses the
  old session; registering again is the only way back, and it ends the
  running recording. If a recording ends within 90 seconds after such a
  registration, the app waits for the camera's own switch into playback,
  switches it back to record mode and starts the recording again (up to
  three tries). The footage continues in a new file; the log says
  `recording_resumed`. A recording stopped in the app, or one that ends
  later (stopped on the camera, card full, battery), stays stopped.

### Changed
- Updated dependencies:
  - fastapi from 0.161.0 to 0.162.0
  - linuxserver/baseimage-alpine from `52106d1` to `d07ad0e`

## [3.0.4] - 2026-10-28

### Security
- Security updates of the distribution packages, picked up by rebuilding the image:
  - `python3` 3.14.10-r1

## [3.0.3] - 2026-10-27

### Security
- Security updates of the distribution packages, picked up by rebuilding the image:
  - `python3` 3.14.10-r0 (CVE-2026-114583, CVE-2026-115557, CVE-2026-12345)

## [3.0.2] - 2026-10-26

### Changed
- Updated dependencies:
  - fastapi from 0.143.2 to 0.161.0
  - linuxserver/baseimage-alpine from `b661ece` to `52106d1`
  - httpx2 from 2.15.1 to 2.16.0

### Security
- Security updates of the distribution packages, picked up by rebuilding the image:
  - `curl` 8.23.0-r0 (CVE-2026-92392)
  - `libcurl` 8.23.0-r0 (CVE-2026-92392)

## [3.0.1] - 2026-10-22

### Security
- Security updates of the distribution packages, picked up by rebuilding the image:
  - `musl` 1.2.6-r3 (CVE-2026-107891)

## [3.0.0] - 2026-10-20

**Needs action when upgrading** if you open the interface under a host name
other than an IP address, a single word (`tower`) or a `.local`, `.lan`,
`.home.arpa` or `.internal` name: add it to `LUMIX_ALLOWED_HOSTS`.

### Security
- **The interface only answers to host names a web site can't take over.**
  A page whose own name was made to point at the app (DNS rebinding) counted
  as the interface itself: it passed the cross-site check, could read the
  status and the live view and start or stop a recording. Other names now
  get `403`; `LUMIX_ALLOWED_HOSTS` (or `allowed_hosts` in `config.toml`)
  adds names, e.g. for a reverse proxy.
- **The live view only takes frames from the camera's address.** Anyone on
  the network could send a JPEG to the stream port and have it shown as the
  live view, with a made-up aperture.

### Fixed
- The list of stream gaps keeps the last 100; it grew for as long as the app
  ran.

### Docs
- PanaControl is public: the image needs no `docker login`, the Unraid
  template is in Community Applications and updates itself through its
  `TemplateURL`, and the icon comes from there instead of a manual copy.

## [2.1.0] - 2026-10-19

### Changed
- **The log is JSON:** one object per line with `ts` (local time with its
  offset, per `TZ`), `level`, a short `event` name and its details as
  fields, instead of plain text. The first line is `starting` with the
  version and the settings that matter (camera address, device name,
  ports). uvicorn's own lines come out the same way. The camera's own
  name is no longer logged, only model and firmware.
- A bad setting (no camera address, a port that isn't a number) is logged
  as `config_invalid` with the reason.
- The base image moved to the newest `baseimage-alpine:3.24` build, and
  `fastapi` to 0.143.2.

### Security
- Every response says `nosniff` and can't be framed by other pages; API
  replies are never cached. The generated API pages (`/docs`,
  `/openapi.json`) are gone; the routes are listed in the README.

### Fixed
- The diagnostic run (`lumixremote.probe`) releases the camera's session
  however it ends. Stopped with Ctrl+C during the measurement, it used to
  leave the camera locked until it was switched off and on.

### Changed
- `docker-compose.yml` uses the published image and names the container
  `panacontrol`, as the docs do. The service keeps its name, so an
  existing setup replaces its container.

### Docs
- The Unraid guide moved to `unraid/README-UNRAID.md`. `SECURITY.md` no
  longer claims a read-only root file system, which 2.0.0 dropped.

## [2.0.1] - 2026-10-18

### Security
- Commands (autofocus, start and stop recording, ISO) that a browser sends
  from another web site are refused. The app has no login, so any page open
  in the same browser could start or stop a recording. Scripts such as
  `curl` are not affected.

### Fixed
- The app's memory no longer grows while the live view runs: it kept the
  size of every frame received, about 30 a second.
- An unreadable reply from the camera or another unexpected error no longer
  ends the connection keeper; before, the app stayed up but never
  reconnected.
- A broken request body for the ISO setting is answered with 400 instead of
  500, and a port setting that isn't a number stops the start with a clear
  message.

## [2.0.0] - 2026-10-17

**Needs action when upgrading:** remove `--read-only`,
`--cap-drop ALL`, `--cap-add SETUID` and `--cap-add SETGID` from the Extra
Parameters (compose: `read_only`, `cap_drop`, `cap_add`), or the container
doesn't start. See "Upgrading from 1.x" in the README.

### Changed
- **Built on linuxserver.io's `baseimage-alpine`** instead of
  `python:3.14-alpine` with its own entrypoint. s6-overlay starts the app as
  `abc` with `PUID`/`PGID`; `UMASK` and docker mods work as in other
  linuxserver.io images. The log starts with the TomJoad Images banner.
- A missing `LUMIX_CAMERA_IP`, a non-numeric `PUID`/`PGID` or the ID `0`
  stop the container at start with an `ERROR:` line.
- s6 gives the app 20 seconds after `docker stop` to send `stopstream`
  before it would kill it.
- The Unraid template uses `br0` with a fixed IP instead of `host`, and the
  docs recommend an own address on the LAN.
- The web interface follows the Tom Joad Media design: Anton and Archivo,
  served by the app itself, square panels on ink, status as words.
- Interface, log lines, error messages and documentation are in English.
- Images are built for `linux/amd64` only; `arm64` is no longer built.
- No keyboard shortcut stops a recording any more: only a click or tap on
  STOP does. Space still starts a recording, F still focuses.

### Added
- `UMASK` setting.
- The app logs `shutting down` when it ends, so an ordered stop shows in
  the log.

## [1.0.1] - 2026-10-12

### Changed
- The image is based on `python:3.14-alpine` (was `python:3.12-alpine`),
  pinned by digest. CI checks the code on Python 3.14 as well.

## [1.0.0] - 2026-10-03

First versioned release. Existing containers keep working; on Unraid, add
the new settings and Extra Parameters from the template (see the Unraid
guide, `docs/unraid.md`).

### Changed
- The image is based on `python:3.12-alpine` (was `python:3.11-slim`),
  pinned by digest.
- **`PUID` and `PGID`** set the user and group the app runs as (Unraid:
  `99`/`100`). The container starts as root only to switch to them; `0` is
  refused. Before, the app ran as a fixed user with UID 10001.
- **`TZ`** sets the time zone of the log, and every log line now starts with
  date and time. The first line names the version
  (`PanaControl 1.0.0 startet`).
- The compose file and the Unraid template run the container hardened:
  read-only root file system, all capabilities dropped but `SETUID` and
  `SETGID`, `no-new-privileges`. Compose also rotates the container log.
- Images are built and published only for version tags (`v1.0.0`), no longer
  for every push to `main`. Tags: `1.0.0`, `1.0`, `latest`, `sha-...`.
- Dependencies are pinned (`requirements.txt`), Dependabot proposes updates.

### Added
- Every push and pull request runs an import check, `pip-audit`, shellcheck,
  a template check, an image smoke test (settings check, `PUID`/`PGID`, `TZ`,
  web interface, orderly shutdown) and a secret scan.
- Images come with provenance and an SBOM and are scanned with Trivy.
- `SECURITY.md`, `LICENSE` (MIT), issue templates.

## Before 1.0.0

Unversioned development: protocol layer, live view, autofocus, recording,
ISO, the container and the Unraid template.

[3.3.1]: https://github.com/Tom-Joad/PanaControl/compare/v3.3.0...v3.3.1
[3.3.0]: https://github.com/Tom-Joad/PanaControl/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/Tom-Joad/PanaControl/compare/v3.1.2...v3.2.0
[3.1.2]: https://github.com/Tom-Joad/PanaControl/compare/v3.1.1...v3.1.2
[3.1.1]: https://github.com/Tom-Joad/PanaControl/compare/v3.1.0...v3.1.1
[3.1.0]: https://github.com/Tom-Joad/PanaControl/compare/v3.0.4...v3.1.0
[3.0.4]: https://github.com/Tom-Joad/PanaControl/compare/v3.0.3...v3.0.4
[3.0.3]: https://github.com/Tom-Joad/PanaControl/compare/v3.0.2...v3.0.3
[3.0.2]: https://github.com/Tom-Joad/PanaControl/compare/v3.0.1...v3.0.2
[3.0.1]: https://github.com/Tom-Joad/PanaControl/compare/v3.0.0...v3.0.1
[3.0.0]: https://github.com/Tom-Joad/PanaControl/compare/v2.1.0...v3.0.0
[2.1.0]: https://github.com/Tom-Joad/PanaControl/compare/v2.0.1...v2.1.0
[2.0.1]: https://github.com/Tom-Joad/PanaControl/releases/tag/v2.0.1
