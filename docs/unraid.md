# Running on Unraid

How to run the container on an Unraid server. The image is private, so the
server needs a login to `ghcr.io` first.

## Before you start

**The camera must be in the same subnet as the container.** The protocol has
no way around a router: the camera sends the picture data by UDP to exactly
the address the request came from. With NAT or a separate VLAN in between,
the control gets through, but no picture.

**The interface has no login.** Whoever reaches its port controls the
camera. For access from outside, put a reverse proxy with authentication in
front.

## Logging in to the registry

Once, in the Unraid console:

```bash
docker login ghcr.io
```

User: the GitHub name; password: a personal access token with the
`read:packages` scope.

## Creating the container

The easy way is the template in this repository. Copy it under its own name
to `/boot/config/plugins/dockerMan/templates-user/`:

```bash
cp unraid/PanaControl.xml /boot/config/plugins/dockerMan/templates-user/
```

`PanaControl` then shows up under `Docker` -> `Add Container` in the
`Template` list. Set:

- **Network Type** `br0` (preset) and a **Fixed IP address** in the camera's
  subnet. The interface is then at `http://<that address>:8080/`.
- **LUMIX_CAMERA_IP**, the camera's address.
- **TZ**, your time zone. `PUID=99` and `PGID=100` are preset.

The template has no `TemplateURL`: the repository is private and Unraid
couldn't fetch it. Copy a new template by hand as above.

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

## Icon

The template names the icon `unraid/icon.png` of this repository. The
repository is private, so Unraid can't download it. Copy it once into
Unraid's icon folder instead; Unraid uses a file there before it tries the
URL (`getIcon` in `DockerClient.php`, Unraid 7):

1. Download [`unraid/icon.png`](../unraid/icon.png) from the repository on GitHub
   (logged in) and put it on any share, for example as
   `\\<SERVER>\appdata\PanaControl-icon.png`.
2. In the Unraid terminal, copy it into the icon folder of the Docker data:

   ```sh
   cp /mnt/user/appdata/PanaControl-icon.png /var/lib/docker/unraid/images/PanaControl-icon.png
   ```

   The part before `-icon.png` is the container's name in Unraid, with the
   same capitals; use that name if you named the container differently.
3. Unraid keeps the placeholder it has already picked for the container
   until the next reboot. To show the icon at once, drop that entry from its
   cache, then reload the Docker tab:

   ```sh
   php -r '$f="/usr/local/emhttp/state/plugins/dynamix.docker.manager/docker.json"; $j=json_decode(file_get_contents($f),true); unset($j["PanaControl"]["icon"]); file_put_contents($f,json_encode($j,JSON_PRETTY_PRINT|JSON_UNESCAPED_SLASHES));'
   ```

The copy survives reboots and container updates. It lives in the Docker
data, not on the flash drive, so it is gone when the Docker image or
directory is recreated; copy it again then. Unraid does not read icons from
`/boot/config/plugins/dockerMan/images/`.

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
this is measured, see `protocol.md`.

That is why the template sets `--stop-timeout 30`. s6-overlay passes the
signal straight to the app and gives it 20 seconds
(`S6_SERVICES_GRACETIME`) before it would kill it. Unraid's `Stop` and
`Restart` take the ordered way. A `docker kill` or a power cut locks the
camera out.

## Checking that it runs

```bash
docker logs -f PanaControl
```

The log starts with the TomJoad Images banner and the user IDs. A good start
continues like this (every line starts with date, time, level and module,
shortened here):

```
PanaControl 2.0.1 starting
web interface on http://0.0.0.0:8080
connected to DC-GH6 (..., firmware 3.01) after 1 attempt(s)
registered, session up; waiting until the camera is ready to record
recmode sent, camera was in 'play'
camera ready to record after 3.7s
UDP receiver listening on port 49199
stream requested on UDP 49199
```

The state is also at `http://<address>:8080/api/status`.

## When something is stuck

| Symptom | Cause |
|---|---|
| The container stops right after the start, log says `ERROR: LUMIX_CAMERA_IP is required` | The camera's address is not set |
| The container doesn't start after the update to 2.0.0 | `--read-only` or `--cap-drop ALL` still in Extra Parameters, see below |
| The interface says "Connecting" and stays there | Camera not in its waiting state, see `protocol.md` |
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

The image is private, so `docker login ghcr.io` must have run first.

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
