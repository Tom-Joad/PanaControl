# Running on Unraid

How to run the container on an Unraid server.

## Before you start

**The camera must be in the same subnet as the container.** The protocol has
no way around a router: the camera sends the picture data by UDP to exactly
the address the request came from. With NAT or a separate VLAN in between,
the control gets through, but no picture.

**The interface has no login.** Whoever reaches its port controls the
camera. For access from outside, put a reverse proxy with authentication in
front.

## Creating the container

The easy way is Community Applications: search for **PanaControl** under
`Apps` and install it. Without Community Applications, add
`https://github.com/Tom-Joad/unraid-templates` under `Docker` ->
`Add Container` -> `Template repositories`, then pick `PanaControl` from
the `Template` list. Set:

- **Network Type** `br0` (preset) and a **Fixed IP address** in the camera's
  subnet. The interface is then at `http://<that address>:8080/`.
- **LUMIX_CAMERA_IP**, the camera's address.
- **TZ**, your time zone. `PUID=99` and `PGID=100` are preset.

The template carries its `TemplateURL`, so Unraid picks up changes to it
by itself.

When editing the template, keep two quirks of the format, or Unraid shows
empty fields: **no comment before the root element**, and the `Config`
entries stay self-closing, with the value in `Default` rather than as text
content.

The same from the console (`br0` is Unraid's macvlan/ipvlan network on the
main interface):

```bash
docker run -d \
  --name PanaControl \
  --restart unless-stopped \
  --stop-timeout 30 \
  --network br0 --ip 192.0.2.20 \
  --security-opt no-new-privileges \
  -e PUID=99 -e PGID=100 -e TZ=Europe/Berlin \
  -e LUMIX_CAMERA_IP=192.0.2.10 \
  ghcr.io/tom-joad/panacontrol:latest
```

The image is built on linuxserver.io's Alpine base: it starts as root,
prepares the `abc` user with `PUID:PGID` and runs the app as that user. The
app writes no files and needs no path.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `PUID` | `99` | User the app runs as (`nobody`), not `0` |
| `PGID` | `100` | Group the app runs as (`users`), not `0` |
| `TZ` | `Etc/UTC` | Time zone of the log time stamps, e.g. `Europe/Berlin` |
| `UMASK` | `002` | File mode mask; the app writes no files |
| `LUMIX_CAMERA_IP` | -- | The camera's address, required |
| `LUMIX_DEVICE_NAME` | `lumix-remote` | Name shown on the camera's display during registration |
| `LUMIX_STREAM_PORT` | `49199` | UDP port for the live view |
| `LUMIX_WEB_HOST` | `0.0.0.0` | Where the app listens in the container; leave it |
| `LUMIX_WEB_PORT` | `8080` | Port of the interface |
| `LUMIX_LOG_LEVEL` | `INFO` | `DEBUG` for troubleshooting |

## Network: an own address, not bridge with port mapping

A live view frame is 23.5 KB on average and goes out as **one** UDP datagram.
That is many times any usual MTU: on the IP level it is split into about 17
fragments, at 30 frames per second more than 500 packets. If a single
fragment is lost, the receiver drops the whole frame.

In bridge mode with port mapping, this stream runs through Docker's address
translation, which has to reassemble every fragmented datagram first. That is
exactly where it gets lost under constant load. The symptom is stream gaps
of ten to forty seconds while the control over HTTP carries on as normal.
Without NAT, the same stream ran for seven minutes without a single gap.

With `br0` and a fixed IP, the container has its own address on the LAN and
there is no translation at all. No port mappings are needed: the interface
is at `LUMIX_WEB_PORT` of that address, the live view at `LUMIX_STREAM_PORT`.

`host` works as well, with the interface on the server's own address. If you
must use bridge with port mapping, the UDP port needs the same number on both
sides (`49199:49199/udp`): the app tells the camera its own port, and a
different outside number sends the picture data to the wrong place. Expect
the stream gaps described above.

## Stopping

**The container must be stopped in order.** On shutdown the app sends
`stopstream` and releases the remote session. Without that, the camera
refuses every further registration until it is switched off and on again;
this is measured, see `docs/protocol.md`.

That is why the template sets `--stop-timeout 30`. s6-overlay passes the
signal straight to the app and gives it 20 seconds
(`S6_SERVICES_GRACETIME`) before it would kill it. Unraid's `Stop` and
`Restart` take the ordered way. A `docker kill` or a power cut locks the
camera out.

## Checking that it runs

```bash
docker logs -f PanaControl
```

The log starts with the TomJoad Images banner and the user IDs. After that
the app writes one JSON object per line: time stamp, level, a short event
name and its details. A good start continues like this (time stamps
shortened):

```
{"ts":"…","level":"INFO","event":"starting","version":"2.1.0","camera":"192.0.2.10","device_name":"lumix-remote","stream_port":49199,"web":"http://0.0.0.0:8080","log_level":"INFO"}
{"ts":"…","level":"INFO","event":"connected","model":"DC-GH6","firmware":"3.01","attempts":1}
{"ts":"…","level":"INFO","event":"registered"}
{"ts":"…","level":"INFO","event":"recmode_sent","cammode":"play"}
{"ts":"…","level":"INFO","event":"camera_ready","after_s":3.7}
{"ts":"…","level":"INFO","event":"udp_listening","port":49199}
{"ts":"…","level":"INFO","event":"stream_requested","port":49199}
```

Before 2.1.0 the same lines were plain text.

The state is also at `http://<address>:8080/api/status`.

## When something is stuck

| Symptom | Cause |
|---|---|
| The container stops right after the start, log says `ERROR: LUMIX_CAMERA_IP is required` | The camera's address is not set |
| The container doesn't start after the update to 2.0.0 | `--read-only` or `--cap-drop ALL` still in Extra Parameters, see below |
| The interface says "Connecting" and stays there | Camera not in its waiting state, see `docs/protocol.md` |
| "Camera not in record mode" | It is in playback; the app sends `recmode`, which takes a moment |
| "No picture" although connected | Camera in another subnet, or bridge with port mapping |
| Stream gaps of 10 to 40 s | Bridge with port mapping: the fragmented datagrams don't survive the NAT |
| Every registration is refused | Orphaned session after a hard kill. Switch the camera off and on. |

## Verifying the signature

The image is signed keylessly. To check it:

```bash
cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/Tom-Joad/PanaControl/\.github/workflows/build-and-push\.yml@refs/tags/v' \
  ghcr.io/tom-joad/panacontrol:latest
```

## Upgrading from 1.x to 2.0.0

2.0.0 moves to linuxserver.io's base image. Its init needs a writable root
file system and the usual capabilities, so the hardening options of 1.x stop
the container from starting. In the container, under `Edit`:

1. In **Extra Parameters**, remove `--read-only`, `--cap-drop ALL`,
   `--cap-add SETUID` and `--cap-add SETGID`. What stays is
   `--stop-timeout 30 --security-opt no-new-privileges`.
2. Optionally add the variable `UMASK` (`002`).
3. `Apply`. The log now starts with the TomJoad Images banner, and the first
   line of the app names the version (`PanaControl 2.0.0 starting`).

Settings, ports and the network stay as they are. Copying the new template
and recreating the container from it does the same.
