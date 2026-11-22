# PanaControl

Remote control and live view for LUMIX cameras over the cam.cgi protocol:
live view in the browser, autofocus, start and stop video recording, ISO,
the current aperture as a reading, and downloading what is on the cards. Developed against a DC-GH6 (firmware
3.01) and kept platform-neutral. The container is built on linuxserver.io's
Alpine image with its conventions (`PUID`, `PGID`, `UMASK`, `TZ`).

Status: usable and verified on the device. Live view at 29.9 fps, autofocus,
start and stop recording.

**Always stop it in order** (Ctrl+C, `docker stop`, Unraid's `Stop`). Only
then the app sends `stopstream`. A process killed hard leaves the camera's
remote session occupied, and the camera then answers every command with
`err_busy` until it is switched off and on again.

## Requirements

* Camera and computer (or container) in the same subnet
* Camera in its waiting state: `[Wi-Fi] -> [Wi-Fi Function] -> [Control With Smartphone]`,
  switch to the Wi-Fi connection with the DISP. button, then `[Via Network]`
* For the native start: Python 3.11 or newer (`tomllib` from the standard
  library)

## Supported architectures and tags

| Architecture | Available |
| :---: | :---: |
| x86-64 | yes |
| arm64 | no |

Only x86-64 is built: PanaControl runs on an x86-64 server, and no ARM host
is in use.

| Tag | Meaning |
|---|---|
| `latest` | The latest release |
| `X.Y.Z`, `X.Y` | A specific release / the latest patch of a minor version |
| `sha-<commit>` | A specific build |
| `dev` | Test build from a branch, made by a manual run; not for regular use |

Images are built by GitHub Actions for every version tag, published to
`ghcr.io` and signed keylessly with Sigstore. They are public: no login
is needed to pull them.

## Usage

### docker compose

```bash
cp .env.example .env      # set the camera's address
docker compose up -d
```

See [`docker-compose.yml`](docker-compose.yml). It publishes the ports
through Docker's NAT, which can drop live view frames; on a Linux server an
own address on the LAN (macvlan/ipvlan) or `network_mode: host` is better,
see [Network](#network).

### docker run

```bash
docker run -d \
  --name panacontrol \
  --network <MACVLAN-NETWORK> --ip <STATIC-IP> \
  --stop-timeout 30 \
  --security-opt no-new-privileges \
  -e PUID=1000 -e PGID=1000 -e TZ=Europe/Berlin \
  -e LUMIX_CAMERA_IP=<CAMERA-IP> \
  -v /path/to/downloads:/downloads \
  --restart unless-stopped \
  ghcr.io/tom-joad/panacontrol:latest
```

### Unraid

Install **PanaControl** from Community Applications; the guide is
[`unraid/README-UNRAID.md`](unraid/README-UNRAID.md). The template runs the
container on `br0` with a fixed IP.

## In the browser

| Key or gesture | Action |
|---|---|
| Space | Start a recording |
| F | Autofocus |
| V, the button in the corner of the picture, or a double-click or double-tap on it | Full screen on and off |
| Esc | Leave full screen |

No key stops a recording: only a click or tap on **Stop** does.

**Full screen** shows only the live view; the red frame and REC stay. On a
computer, Android and the iPad the page goes into the browser's real full
screen. The iPhone allows that only for videos, so there the bar is hidden.
For the whole display, add the page to the home screen (Safari: Share ->
Add to Home Screen): it then opens without the browser's bars.

While full screen is on, the screen stays on. Over HTTPS the browser's Wake
Lock does this; over plain HTTP, as on the LAN, the page plays a short,
silent, muted video instead, which keeps phones and tablets from locking.
If a device locks anyway, set its auto-lock to never.

### The cards

**Cards** lists what is on the camera's cards, with a preview picture, the
length and the size of each file. **Download** saves one file into the
download folder, **Download all** everything that is not there yet.

* The camera has to be in playback for this, so there is no live view and
  no recording while the cards are open; **Back to live view** switches
  back. The cards can't be opened while a recording runs.
* A file is called `<name>.<size>.part` until it is complete. A download
  that was cancelled or interrupted (the camera out of reach, the container
  restarted) continues from there the next time, it does not start over.
* A file that is already in the folder with the same size counts as saved.
  A different file of the same name is kept; the new one gets `-2` in its
  name.
* Video takes long over WLAN: about 45 Mbit/s were measured on the DC-GH6,
  which is some 20 GB per hour. Keep the camera on power for large clips.
* Verified on the DC-GH6 with MOV clips. Photos and RAW files go the same
  way but were not on the cards when this was measured.

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `--network` | -- | An own address on the LAN (macvlan/ipvlan, `br0` on Unraid) or `host`; see [Network](#network) |
| `-p 8080:8080` | -- | Web interface (only with port mapping) |
| `-p 49199:49199/udp` | -- | Live view; the same number on both sides (only with port mapping) |
| `-e PUID` | `911` | User the app runs as (Unraid: `99`), not `0` |
| `-e PGID` | `911` | Group the app runs as (Unraid: `100`), not `0` |
| `-v /downloads` | -- | Folder for the files downloaded from the cards. Without it the download is switched off |
| `-e UMASK` | `022` | File mode mask for the downloaded files |
| `-e TZ` | `Etc/UTC` | Time zone of the log time stamps, e.g. `Europe/Berlin` |
| `-e LUMIX_CAMERA_IP` | -- (required) | The camera's address |
| `-e LUMIX_DEVICE_NAME` | `lumix-remote` | Name shown on the camera's display during registration |
| `-e LUMIX_STREAM_PORT` | `49199` | UDP port for the live view |
| `-e LUMIX_WEB_HOST` | `0.0.0.0` in the image | Where the interface listens |
| `-e LUMIX_WEB_PORT` | `8080` | Port of the interface |
| `-e LUMIX_LOG_LEVEL` | `INFO` | `DEBUG` for troubleshooting |
| `-e LUMIX_ALLOWED_HOSTS` | -- | Further host names for the interface, comma-separated (e.g. behind a reverse proxy); see [Security](#security) |
| `--stop-timeout 30` | -- | Time for the ordered shutdown, see above |

In the container the configuration comes from environment variables, so no
`config.toml` has to be mounted. They take precedence over `config.toml`.

There is no `/config` volume: the app keeps no state of its own, and an
interrupted download is continued from the part file in the download folder.
`/downloads` only receives what you download from the cards. If no folder is
mapped there, the app refuses to download instead of filling Docker's own
storage.

## Network

The camera sends the frames by UDP to the address `startstream` came from,
and to exactly the port it was told. A frame of about 23.5 KB is one UDP
datagram in about 17 IP fragments, at 30 frames per second. Docker's NAT
(bridge with port mapping) has to reassemble each of them and drops whole
frames under load: the live view then has gaps of 10 to 40 seconds while the
control keeps working.

So give the container its own address on the LAN (macvlan or ipvlan; on
Unraid `br0` with a fixed IP) or use `network_mode: host`. If you must use
port mapping, the UDP port needs the same number on both sides.

Docker Desktop on macOS runs the host namespace in a VM: the published UDP
port works there, `network_mode: host` doesn't. Day to day on a Mac, start
it natively.

## Upgrading from 2.x

3.0.0 only answers to the host names listed under [Security](#security). If
you open the interface by IP address (the Unraid default), as `tower` or as a
`.local` name, nothing changes. If you use another name, for example through
a reverse proxy, add it to `LUMIX_ALLOWED_HOSTS` before updating.

## Upgrading from 1.x

2.0.0 moves from `python:3.14-alpine` with its own entrypoint to
linuxserver.io's base image. Its init needs a writable root file system and
the usual capabilities, so **remove the 1.x hardening options**:
`--read-only`, `--cap-drop ALL`, `--cap-add SETUID`, `--cap-add SETGID`
(compose: `read_only`, `cap_drop`, `cap_add`). Keep `--stop-timeout 30` and
`--security-opt no-new-privileges`. Settings, ports and the network stay the
same. On Unraid, see [Upgrading from 1.x to 2.0.0](unraid/README-UNRAID.md#upgrading-from-1x-to-200).

## Native start

```bash
git clone https://github.com/Tom-Joad/PanaControl.git && cd PanaControl
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml   # set the IP
python3 -m lumixremote.app
```

On Windows, `run.bat` does these steps.

Then open the interface at the address set in `config.toml`, by default
`http://127.0.0.1:8080`. The app keeps the registration alive, polls the
state every five seconds and kicks the stream when it stalls. Several
browsers may watch at the same time; there is still only one connection to
the camera.

| Route | Purpose |
|---|---|
| `GET /` | Interface |
| `GET /stream.mjpg` | Live view as `multipart/x-mixed-replace` |
| `GET /api/status` | Connection state, battery, recording, frame rate |
| `GET /api/health` | `200`, or `503` while the UDP receiver for the live view is broken (the container's health check) |
| `POST /api/af` | Autofocus |
| `POST /api/rec/start` | Start recording |
| `POST /api/rec/stop` | Stop recording |
| `POST /api/iso` | Set the sensitivity, JSON `{"value": "800"}` |
| `GET /api/media` | The cards' contents, the download queue and its progress |
| `POST /api/media/open`, `/close` | Open the cards (camera to playback) and close them again |
| `POST /api/media/download` | Queue files, JSON `{"ids": ["..."]}` or `{"all": true}` |
| `POST /api/media/cancel` | Stop the running download and empty the queue |
| `POST /api/media/refresh` | Read the cards again |
| `GET /api/media/thumb/<id>` | Preview picture of a file |

The interface takes the ISO values from the camera's menu definition; they
are not in the program. Set values are read back, because the camera snaps
to the value allowed in the current mode.

The **aperture can only be read**. The DC-GH6 with firmware 3.01 offers no
call to set it remotely; the reasoning and the candidates tested are in
[`docs/protocol.md`](docs/protocol.md). The current value is read from the
header of the live view datagram and shown.

## Security

The interface has no login: whoever reaches it controls the camera, sees
the live view and can download the cards' contents into the download folder. Keep it on a trusted network, or put a reverse proxy with
authentication in front. Commands that a browser sends from another web
site are refused, so a page open in the same browser can't start or stop a
recording; scripts such as `curl` still work.

The interface also only answers to host names a web site can't take over:
IP addresses, `localhost`, single words such as `tower`, and names ending in
`.local`, `.lan`, `.home.arpa` or `.internal`. Without this, a page whose own
name was made to point at the app (DNS rebinding) would count as the
interface itself and could watch the live view and start a recording. Any
other name, e.g. a reverse proxy's, goes into `LUMIX_ALLOWED_HOSTS` (or
`allowed_hosts = [...]` under `[web]` in `config.toml`); others get `403`.

The live view only takes frames from the camera's address; datagrams from
anyone else on the network are dropped.

Reporting vulnerabilities: [SECURITY.md](SECURITY.md).

## Troubleshooting

```bash
docker logs -f panacontrol
```

The log starts with the TomJoad Images banner and the user IDs. The app
then writes one JSON object per line (`ts`, `level`, `event` and the
details); the first one is `"event":"starting"` with the version and the
settings that matter. The state
is at `http://<address>:8080/api/status`. `docker ps` shows the container
as unhealthy if the live view's UDP port can't be opened (taken by another
program) or the receiver stopped; the log says why (`udp_bind_failed`,
`udp_receiver_failed`). More symptoms and causes are in
[`unraid/README-UNRAID.md`](unraid/README-UNRAID.md#when-something-is-stuck).

Verify the signature:

```bash
cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/Tom-Joad/PanaControl/\.github/workflows/build-and-push\.yml@refs/tags/v' \
  ghcr.io/tom-joad/panacontrol:latest
```

## Building and testing

```bash
docker build -t panacontrol:test .
tests/smoke.sh panacontrol:test
```

`python -m pytest` runs the unit tests (`pip install -r
requirements-dev.txt` first). The smoke test needs no camera. It checks the settings, the switch to
`PUID:PGID`, `TZ`, the start banner, the interface and that `docker stop`
ends the app in order. GitHub Actions runs it on every push.

### Diagnostic run

```bash
python3 -m lumixremote.probe
```

| Switch | Effect |
|---|---|
| `--ip 192.0.2.10` | work without a configuration file |
| `--duration 120` | measuring time in seconds |
| `--test-commands` | also test autofocus and record start/stop |
| `--no-restart` | don't kick the stream when it stalls (endurance test) |
| `-v` | verbose log |

On the first run, the firewall asks about incoming connections for Python.
If that is refused, the control works but no picture arrives. The run saves
a sample frame to `out/probe_frame.jpg`. It answers: frame rate and
resolution in practice, whether the header before the JPEG has a constant
length, whether the stream holds without kicking, and whether the session
survives a longer pause without commands.

### Test of listing, download and events

```bash
python3 -m lumixremote.mediaprobe --ip 192.0.2.10
```

Checks on a real camera what `docs/media-transfer.md` and
`docs/phone-app-notes.md` took from the phone app: the camera's events,
listing the cards, downloading a sample of each kind of file, the fields of
the state query and the layout of the live view datagram. It changes nothing
on the cards and records nothing, switches the camera to playback and back,
and refuses to run while a recording is running. Stop PanaControl first: the
camera holds one remote session. The replies land in `out/mediaprobe/`; they
contain the camera's identifiers and stay out of the repository.

| Switch | Effect |
|---|---|
| `--list-max 200` | entries to list per folder at most |
| `--download-mb 32` | megabytes to fetch per sample file at most |

## Layout

| File | Content |
|---|---|
| `lumixremote/camera.py` | DLNA probe, registration, commands, reconnect |
| `lumixremote/stream.py` | UDP receiver, JPEG extraction, statistics |
| `lumixremote/app.py` | Connection keeper, routes, MJPEG output |
| `lumixremote/media.py` | Listing the cards, download queue with resume |
| `lumixremote/static/` | Interface without a build step, stylesheets and fonts; `app/` holds the home-screen manifest, icons and the keep-awake video |
| `lumixremote/probe.py` | Measuring run and results |
| `lumixremote/mediaprobe.py` | Test of listing, download, events and state |
| `docs/protocol.md` | What was verified on the device |
| `unraid/README-UNRAID.md` | Running on an Unraid server |
| `unraid/panacontrol.xml` | Container template for Unraid |
| `root/etc/s6-overlay/` | s6 services: settings check, app as `abc`, start banner |
| `tests/smoke.sh` | Smoke test of the image |
| `.github/workflows/build-and-push.yml` | Checks; for tags build, publish, sign |

`config.toml` is excluded on purpose. IP, device name, UDN and serial number
don't belong in the repository; the client reads the UDN from the DLNA
descriptor at runtime.

Changes are listed in the [CHANGELOG](CHANGELOG.md).
