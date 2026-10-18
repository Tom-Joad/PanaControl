# PanaControl

Remote control and live view for LUMIX cameras over the cam.cgi protocol:
live view in the browser, autofocus, start and stop video recording, ISO,
and the current aperture as a reading. Developed against a DC-GH6 (firmware
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
| x86-64 | ✅ |
| arm64 | ❌ |

Only x86-64 is built: PanaControl runs on an x86-64 server, and no ARM host
is in use.

| Tag | Meaning |
|---|---|
| `latest` | The latest release |
| `2.0.1`, `2.0` | A specific release / the latest patch of a minor version |
| `sha-<commit>` | A specific build |
| `dev` | Test build from a branch, made by a manual run; not for regular use |

Images are built by GitHub Actions for every version tag, published to
`ghcr.io` and signed keylessly with Sigstore. The image inherits the
repository's visibility, so it is private: pulling it needs a token with
`read:packages` (`docker login ghcr.io`).

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
  --restart unless-stopped \
  ghcr.io/tom-joad/panacontrol:latest
```

### Unraid

A template and its own guide: [`docs/unraid.md`](docs/unraid.md). The
template runs the container on `br0` with a fixed IP.

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `--network` | -- | An own address on the LAN (macvlan/ipvlan, `br0` on Unraid) or `host`; see [Network](#network) |
| `-p 8080:8080` | -- | Web interface (only with port mapping) |
| `-p 49199:49199/udp` | -- | Live view; the same number on both sides (only with port mapping) |
| `-e PUID` | `911` | User the app runs as (Unraid: `99`), not `0` |
| `-e PGID` | `911` | Group the app runs as (Unraid: `100`), not `0` |
| `-e UMASK` | `022` | File mode mask; the app writes no files |
| `-e TZ` | `Etc/UTC` | Time zone of the log time stamps, e.g. `Europe/Berlin` |
| `-e LUMIX_CAMERA_IP` | -- (required) | The camera's address |
| `-e LUMIX_DEVICE_NAME` | `lumix-remote` | Name shown on the camera's display during registration |
| `-e LUMIX_STREAM_PORT` | `49199` | UDP port for the live view |
| `-e LUMIX_WEB_HOST` | `0.0.0.0` in the image | Where the interface listens |
| `-e LUMIX_WEB_PORT` | `8080` | Port of the interface |
| `-e LUMIX_LOG_LEVEL` | `INFO` | `DEBUG` for troubleshooting |
| `--stop-timeout 30` | -- | Time for the ordered shutdown, see above |

In the container the configuration comes from environment variables, so no
`config.toml` has to be mounted. They take precedence over `config.toml`.

There are no volumes: the app keeps no state and writes no files. This
deviates from the usual `/config` volume on purpose.

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

## Upgrading from 1.x

2.0.0 moves from `python:3.14-alpine` with its own entrypoint to
linuxserver.io's base image. Its init needs a writable root file system and
the usual capabilities, so **remove the 1.x hardening options**:
`--read-only`, `--cap-drop ALL`, `--cap-add SETUID`, `--cap-add SETGID`
(compose: `read_only`, `cap_drop`, `cap_add`). Keep `--stop-timeout 30` and
`--security-opt no-new-privileges`. Settings, ports and the network stay the
same. On Unraid, see [Upgrading from 1.x to 2.0.0](docs/unraid.md#upgrading-from-1x-to-200).

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
| `POST /api/af` | Autofocus |
| `POST /api/rec/start` | Start recording |
| `POST /api/rec/stop` | Stop recording |
| `POST /api/iso` | Set the sensitivity, JSON `{"value": "800"}` |

The interface takes the ISO values from the camera's menu definition; they
are not in the program. Set values are read back, because the camera snaps
to the value allowed in the current mode.

The **aperture can only be read**. The DC-GH6 with firmware 3.01 offers no
call to set it remotely; the reasoning and the candidates tested are in
[`docs/protocol.md`](docs/protocol.md). The current value is read from the
header of the live view datagram and shown.

## Security

The interface has no login: whoever reaches it controls the camera and sees
the live view. Keep it on a trusted network, or put a reverse proxy with
authentication in front. Reporting vulnerabilities: [SECURITY.md](SECURITY.md).

## Troubleshooting

```bash
docker logs -f panacontrol
```

The log starts with the TomJoad Images banner and the user IDs; the first
line of the app names the version (`PanaControl 2.0.1 starting`). The state
is at `http://<address>:8080/api/status`. More symptoms and causes are in
[`docs/unraid.md`](docs/unraid.md#when-something-is-stuck).

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

The smoke test needs no camera. It checks the settings, the switch to
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

## Layout

| File | Content |
|---|---|
| `lumixremote/camera.py` | DLNA probe, registration, commands, reconnect |
| `lumixremote/stream.py` | UDP receiver, JPEG extraction, statistics |
| `lumixremote/app.py` | Connection keeper, routes, MJPEG output |
| `lumixremote/static/` | Interface without a build step, stylesheets and fonts |
| `lumixremote/probe.py` | Measuring run and results |
| `docs/protocol.md` | What was verified on the device |
| `docs/unraid.md` | Running on an Unraid server |
| `unraid/PanaControl.xml` | Container template for Unraid |
| `root/etc/s6-overlay/` | s6 services: settings check, app as `abc`, start banner |
| `tests/smoke.sh` | Smoke test of the image |
| `.github/workflows/build-and-push.yml` | Checks; for tags build, publish, sign |

`config.toml` is excluded on purpose. IP, device name, UDN and serial number
don't belong in the repository; the client reads the UDN from the DLNA
descriptor at runtime.

Changes are listed in the [CHANGELOG](CHANGELOG.md).
