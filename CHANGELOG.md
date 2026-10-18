# Changelog

All notable changes to this project are listed here. Versions follow
[semantic versioning](https://semver.org/). A change that needs action when
upgrading (a renamed or removed setting, a changed network or volume setup)
only comes with a new major version.

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

[2.1.0]: https://github.com/Tom-Joad/PanaControl/compare/v2.0.1...v2.1.0
[2.0.1]: https://github.com/Tom-Joad/PanaControl/releases/tag/v2.0.1
