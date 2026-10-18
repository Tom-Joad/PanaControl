# The cam.cgi protocol

Verified on a DC-GH6 with firmware 3.01, over WLAN in the same subnet.
Everything documented here was observed on the device, not derived.

## Overview

The camera runs two services:

| Port | Service | Purpose |
|---|---|---|
| 60606 | UPnP/DLNA | Device description, media access |
| 80 | `cam.cgi` | Control and stream control |

Control commands are plain HTTP GETs with query parameters. The reply is
usually XML of the form `<camrply><result>ok</result></camrply>`; on errors it
says `err_busy`, `err_param` or similar.

## Prerequisite on the camera

Menu `[Wi-Fi] -> [Wi-Fi Function] -> [Control With Smartphone]`. On the
Bluetooth pairing screen, switch to the Wi-Fi connection with the **DISP.**
button, then choose `[Via Network]` and the WLAN. The camera then stays in a
waiting state, and exactly then it can be reached by HTTP.

`[Direct]` would be wrong: with it the camera becomes its own access point
instead of joining an existing WLAN.

## Device description

```
GET http://<cam>:60606/Lumix/Server0/ddd
```

Returns UPnP XML. Relevant fields in the `<device>` element:

| Field | Content |
|---|---|
| `modelNumber` | Model name |
| `UDN` | Identifier prefixed with `uuid:`, needed for the registration |
| `pana:X_FirmVersion` | Firmware version |
| `pana:X_AdditionalFunction` | contains `CPRemoteView` when remote control works |

The request has no side effects and needs no session. So it serves as a
reachability test.

## Registration

Two stages. First the sign-in:

```
GET /cam.cgi?mode=accctrl&type=req_acc_g
```

Then the actual registration, with the UDN (without `uuid:`) and a freely
chosen device name passed **hex-encoded**:

```
GET /cam.cgi?mode=accctrl&type=req_acc_e&value=<UDN hex>&value2=<name hex>
```

For once the reply is not XML but comma-separated plain text. Four forms
were observed, **all with HTTP 200**:

```
ok_under_research_no_msg,<model>,remote,open       still working
ok_under_research,<model>,remote,open              likewise, shorter form
err_user_refused,<model>,remote,open               refused
ok,<model>,remote,open,<session ID>                done
```

In addition, `req_acc_e` at times returns the ordinary
`<camrply><result>err_critical</result></camrply>` as XML, so not even in the
comma-separated format. Observed while an older session was still occupied.

So the status code is no help in telling them apart, only the body, and that
is not fully known: the interim reply alone came in two spellings, with and
without `_no_msg`. Whoever excludes a list of known error forms runs after
the firmware.

The reverse check is reliable: the first field must be **exactly** `ok`, and
the last one must look like a session ID. Only sixteen-digit hex strings have
been seen there; in all other forms the same position holds `open`, which
would otherwise pass as a session.

**The gap between attempts decides.** Measured: at half-second intervals the
camera stayed at `ok_under_research_no_msg` for twelve attempts and then
stopped answering at all. With three seconds between attempts, the second one
succeeded, reproduced several times. Asking quickly keeps the camera stuck in
the interim reply instead of speeding up the registration.

On first contact, a confirmation on the display is added; until it is
acknowledged, the reply also stays the interim one. Later connections skip
the prompt.

**A new registration tears down the existing session.** The camera then
briefly shows "not connected to the app", and **a running video recording is
aborted with it**.

The exact moment matters, as observed on the device: a network dropout alone
does no harm. The recording continues during the radio silence and **is only
aborted at the moment the connection is set up again**. So the session stays
valid on the camera as long as only the network is gone.

Hence the rule: a missing reply is **not** a reason to give up the session.
Only a reply in which the camera refuses the session is. A time limit for
sitting it out brings nothing anyway: registering again only works with a
reachable camera, and once it answers again, its reply says where things
stand.

Also to be avoided while `rec` is `on`: any mode change via `recmode`. Shortly
after a registration the camera reports `cammode=play`, and a reflexive
switch back at that moment ends the recording.

**`err_busy` on a command doesn't mean the session is lost.** The camera did
answer. Reproduced on the device with a `startstream` refused during a stream
gap: registering again after that loses the recording, although the session
was still valid and the `getstate` keepalive went through unchanged. Whether a
session still exists is sensibly decided by the keepalive alone.

From then on the session ID goes into every request as the header
`X-SESSION_ID`.

Sent afterwards optionally; fails on some firmware versions without
consequences:

```
GET /cam.cgi?mode=setsetting&type=device_name&value=<name in plain text>
```

## Commands

| Call | Effect |
|---|---|
| `mode=camcmd&value=recmode` | Force record mode, see below |
| `mode=camcmd&value=oneshot_af` | Trigger autofocus once |
| `mode=camcmd&value=video_recstart` | Start video recording |
| `mode=camcmd&value=video_recstop` | Stop video recording |
| `mode=camcmd&value=capture` | Take a photo |
| `mode=getstate` | State query, doubles as the keepalive |

Among other things, `getstate` returns the battery level (`batt`), the
recording state (`rec`), the remaining capacity (`remaincapacity`), the
remaining video time in seconds (`video_remaincapacity`) and the operating
mode (`cammode`).

## The mode change after the registration

Verified on the DC-GH6 with firmware 3.01, reproduced several times:

**After the registration the camera switches into playback by itself.**
Measured course from the moment the session is up:

| Time | `cammode` |
|---|---|
| 0.7 s | `rec` |
| from about 2 s | `play` |
| after `recmode` | `rec` |

That gives the order that works: register, **wait for the state**, read
`cammode`, and only when it says `play`, send `recmode`. The call is then
acknowledged with `ok` and takes about 0.5 s.

How long the settling takes varies a lot. In one series of measurements the
camera was ready throughout 3.3 s after the registration, in another
`recmode` ran into a 5 s timeout. A fixed wait never fits; the right way is to
read `cammode` every half second and only continue once `rec` has held for
a few seconds without a break.

Sending `recmode` right after the registration gets `err_critical`: at that
moment the camera is still in `rec`, so the command is moot. Afterwards it
drops into playback, and without a second attempt it stays there.

**In playback mode `startstream` is acknowledged with `ok`, but not a single
datagram arrives.** So a silent stream is not a network problem first, but a
hint at `cammode`. Cross-checked: 25 s of listening on the UDP port after a
clean `startstream` in playback gave zero packets.

Not accepted: `camcmd&value=rec_mode` and `setsetting&type=cammode` (both
`err_param`), and `camcmd&value=playmode`.

Further options from others' reverse engineering, not used so far:
`setsetting&type=focusmode` with `afs`/`aff`/`afc`, and
`camctrl&type=touch_trace` for focus by coordinate.

## Settings

```
GET /cam.cgi?mode=getsetting&type=<name>
GET /cam.cgi?mode=setsetting&type=<name>&value=<value>
```

The reply to `getsetting` carries the value as an attribute:
`<settingvalue iso="auto"/>`. An unknown name gives `err_critical`, an unusable
value `err_param`.

Read on the device were, among others, `iso`, `shtrspeed`, `exposure`,
`focusmode`, `photostyle`, `drivemode`, `whitebalance`, `quality`, `afmode`,
`ex_tele_conv` and `liveviewsize`.

**An acknowledged `ok` doesn't mean the value applies.** Measured: `iso=400`
was answered with `ok`, reading back gave `800`. The camera snaps to the
value allowed in the current mode. Whoever sets a value reads it back.

Shutter speed and aperture are APEX values in steps of 1/256:
`shtrspeed="1110/256"` is 4.336, so 1/20 s. For the aperture, N = 2^(Av/2);
Av 1280/256 = 5.0 gives 5.66 and so the marked aperture f/5.6. Cross-checked
on the device.

### List of values

```
GET /cam.cgi?mode=getinfo&type=allmenu
```

Returns the complete menu definition, about 380 KB on the DC-GH6. The allowed
values of a setting are there as `<item>` with `cmd_mode="setsetting"`,
`cmd_type` and `cmd_value`; for ISO these are 32 entries, some of them twice,
because the same sensitivity appears in several menu contexts. So the list
comes from the camera and doesn't have to be maintained in the program.

`getinfo&type=curmenu` returns the same structure in short, with `enable`
flags. What they mean is unclear: for ISO it said `enable="no"`, although ISO
could be set without problems.

### Shutter speed

`setsetting&type=shtrspeed&value=<n>/256` is verified on the device: `1024/256`
was set, and the same value came back.

The **camera publishes no list of values for it**: in the menu tree the
shutter speed, like the aperture, appears as `sp_embeded_shutter_speed`
without a command. Whoever wants to build a select field for it must build
the series themselves; whether the camera then accepts every value, is
limited by the frame rate in movie mode, or snaps like with ISO is unchecked.

The interface currently doesn't offer the shutter speed.

### The aperture can't be set

On the DC-GH6 with firmware 3.01 there is **no** way to set the aperture
remotely. Evidence:

* The `camcmdlist` in `getinfo&type=capability` lists exactly six supported
  commands: four times zoom, `poweroff`, `4kphoto_marking`.
* The whole menu tree holds 884 `setsetting` entries, among them ISO, shutter
  speed and white balance, but none for the aperture. It appears there as
  `func_type="sp_embeded_aperture"` without any command.
* Tried without success: `setsetting` and `camctrl` with the types
  `aperture`, `fnumber`, `f`, `av`, `iris` and a dozen more spellings, plus
  `camcmd&value=aperture_up`, all `err_param`. The attempts were repeated in
  the settled state with the same result.

### The aperture can be read

It is in the header of the live view datagram. The anchor is the lens limits:

```
GET /cam.cgi?mode=getinfo&type=lens
-> ok,2304/256,925/256,3755/256,768/256,...,<lens name>,...
```

The first two numbers are the smallest and largest aperture in APEX
encoding. Exactly these two stand one after the other in the header, each
followed by `0x0100`, and right after them comes the current value in the
same format:

```
09 00 01 00   03 9d 01 00   05 00 01 00
 f/22          f/3.5         current f/5.6
```

Because the header has no fixed length, this pattern is searched, not
computed, as with the JPEG start.

## Live stream

```
GET /cam.cgi?mode=startstream&value=<local UDP port>
GET /cam.cgi?mode=stopstream
```

The camera sends the frames to **the IP the call came from**. So the
receiver must listen on the same machine and should already be bound before
`startstream` is sent.

After `startstream` the stream needs a moment to start. A second
`startstream` three seconds after the first is answered with `err_busy` and
takes the session with it, measured. Whoever kicks the stream when it stalls
keeps at least eight seconds between attempts.

Every datagram holds a complete JPEG with a proprietary header in front. Its
length varies, see the measurements. The JPEG start is found by the SOI
marker `FF D8 FF`.

## Measurements

Sixty seconds of continuous operation, WLAN in the 5 GHz band:

| Quantity | Value |
|---|---|
| Resolution | 640x360 |
| Frame rate | 29.9 fps |
| Average frame | 23.5 KB |
| Data rate | about 700 KB/s, so about 5.6 Mbit/s |
| Datagrams per frame | exactly one |
| Dropped packets | none |
| Truncated frames | none |
| Header offsets | 340, 324 and 276 bytes in the same run |
| Stream interruptions | none |
| Session over 60 s | stable |

So a frame fits comfortably into one datagram; reassembly across packet
boundaries is not needed on the application level.

**On the IP level it very much is.** 23.5 KB clearly exceed any usual MTU: a
frame is split into about 17 IP fragments, at 30 frames per second more than
500 packets. If a single fragment is lost, the receiver drops the whole
frame.

That sets a requirement for the network path: **no NAT between camera and
receiver.** Address translation has to reassemble fragmented datagrams first,
and that is exactly where they get lost under load. In a container this means
an own address on the LAN (macvlan/ipvlan) or host networking instead of
bridge with port mapping.

## Known failure patterns

**Every command answers `err_busy`.** The camera holds exactly one remote
session, and a crashed client leaves it occupied.

Measured by killing the app with `SIGKILL` in the middle of operation:

| Observation | Result |
|---|---|
| `startstream` right after | `err_busy` |
| Registration afterwards | `err_critical` already on `req_acc_e` |
| Sign-in step `req_acc_g` | still `ok,<token>` |
| Descriptor on port 60606 | still `HTTP 200` |
| Recovery after 3 min of complete radio silence | none |
| Recovery after a good 5 min in total | none |

So the camera stays reachable and even accepts the first sign-in step; only
the actual registration is refused for good.

**There is no known call that releases the session.** Tested without
success: `accctrl` with the types `req_acc_dis`, `req_acc_d`, `req_acc_del`,
`req_acc_end`, `req_acc_stop` and `req_acc_r` (all `err_param`, so unknown to
the camera), and `stopstream` and `camcmd&value=stopstream` without a session
(both `err_critical`).

The remedy remains restarting the camera. Hence, unchanged: every path that
ends the app must send `stopstream`. That is not optional, but the only
safeguard there is.

**Commands go through, but no picture arrives.** Two causes; check them in
this order. First `cammode`: if it says `play`, the camera sends nothing,
although it acknowledges `startstream`. Second the local firewall, blocking
incoming UDP for the Python process; this affects macOS and Windows alike.

**`recmode` runs into the timeout.** The mode change takes longer than a
tightly set HTTP timeout allows. Measured: with a five-second timeout it ran
into it, with fifteen it didn't. A call that timed out still takes effect:
the camera finishes the change, only the reply comes too late.

**`err_critical` on `recmode`.** Not a defect, but the hint that the camera is
already in record mode. See the section on the mode change.
