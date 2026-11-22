# The cam.cgi protocol

Verified on a DC-GH6 with firmware 3.01, over WLAN in the same subnet.
Everything documented here was observed on the device, not derived.

## Overview

The camera runs two services:

| Port | Service | Purpose |
|---|---|---|
| 60606 | UPnP/DLNA | Device description, media access |
| 80 | `cam.cgi` | Control and stream control |

Files are served from a third port, 50001; see "Listing and downloading the
card's contents". What the phone app does beyond that and is not yet measured
is in [`media-transfer.md`](media-transfer.md) and
[`phone-app-notes.md`](phone-app-notes.md).

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

Seen in continuous operation (logs of recordings running for hours): the
camera stopped answering for up to two minutes, every two to four hours,
with `Host is unreachable` on the app's side. The access point's log showed
no disconnect at those times, so the camera stays in the WLAN and only stops
answering. When it answered again, it refused the old session
with `err_critical`. Right after the new registration `rec` was still `on`,
five seconds later `off` with `cammode` still `rec`, and only a few seconds
after that the camera reported `play`. So a recording does not survive such
an outage; the app starts it again instead (`RESUME_WINDOW` in
`app.py`).

When it does, the order matters. In four logged cases the camera dropped into
`play` after the recording had ended, 6 to 11 s after the registration.
A `video_recstart` sent before that drop, with `cammode` still `rec`, was
refused with `err_critical`, and the `recmode` right after it with
`err_busy`. One sent after the drop and a `recmode` was accepted, and the
recording ran on.

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
| `mode=camcmd&value=playmode` | Switch to playback |
| `mode=camctrl&type=touch&value=<x>/<y>&value2=on` | Touch focus at a point, see below |
| `mode=camctrl&type=af_ae_lock&value=on` (or `off`) | AF/AE lock |
| `mode=camctrl&type=focus&value=tele-normal` (or `wide-normal`) | Move the focus a step, see below |
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
`err_param`).

`camcmd&value=playmode` is answered with `ok` while the camera is in playback
(measured 2026-11-22, ten seconds after the registration). An earlier attempt
was noted as not accepted; under which conditions is no longer known.

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

Set and read back on the device (2026-11-22), each restored afterwards:

| Setting | Asked | Camera adopted |
|---|---|---|
| `whitebalance` | `auto_cool` | `auto_cool` |
| `shtrspeed` | `1536/256` | `1536/256` |
| `exposure` | `-5` | `-3` |

So exposure compensation snaps like ISO does. Its values are EV in thirds,
written as `-5`, `-14/3` ... `1/3` ... `5`; the white balance has 16 values
(`auto`, `auto_cool`, `auto_warm`, `daylight`, `cloudy`, `shade`, `halogen`,
`flash`, `white_set1`-`4`, `color_temp1`-`4`). Both lists are in the menu
definition.

Of the 49 settings the menu definition names, 46 could be read with
`getsetting`, among them `shtrspeed_angle` (`357`), `photostyle`,
`videoformat` (`mp4`), `peaking`, `lightmetering`, `afmode` and
`sensitivity_db`. `focusmode` is readable (`mf`) and follows the lever on the
camera. Not readable: `videoquality`, `videoquality_filter` and `recmode`.

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

## Focus by command

Measured on 2026-11-22 with the focus lever on `mf` and the live view
running.

| Call | Reply |
|---|---|
| `camcmd&value=oneshot_af` | `ok` |
| `camctrl&type=touch&value=500/500&value2=on` | `ok` |
| the same with `250/250`, 1.7 s later | `err_reject` |
| `camctrl&type=touch&value=0/0&value2=off` | `ok` |
| `camcmd&value=touch_af_cancel`, `camcmd&value=af_area_reset` | `ok` |
| `camctrl&type=af_ae_lock&value=on`, then `off` | `ok` |
| `camctrl&type=focus&value=tele-normal` | `ok,346,1024,0,0,1024,32000/214,...` |
| `camctrl&type=focus&value=wide-normal` | `ok,349,1024,0,0,1024,32000/214,...` |
| `camctrl&type=focus&value=focus-stop` | `err_param` |

The touch coordinates run from 0 to 1000 in both directions, taken from the
phone app; `500/500` is the centre. What the touch did to the focus frame
and why the second one was rejected was not looked at.

The reply to a focus step carries the lens position: the second field is the
current value, the third the maximum (1024). The pairs after that look like a
distance scale (`<distance>/<position>`). **There is no call that only reads
the position**: `focus-stop`, which the phone app sends, is refused. A step
is small: two steps moved the value by 3.

The zoom commands (`camcmd` with `tele-normal`, `wide-normal`, `zoomstop`)
and `lcd_on` were all acknowledged with `ok`, with a lens that has no power
zoom; `ok` there says nothing about an effect.

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

The header starts with 32 fixed bytes, all numbers big-endian: the length of
the datagram at offset 0, a running number at offset 4, the camera's MAC
address at offset 9, and at offset 30 the length of the information block
that follows. The JPEG then starts at `32 + (u16 at offset 30)`. Measured on
samples with the JPEG at 324 and at 292: the computed start matched the SOI
marker in every one, and the information block began with `00 0c`. The third
header length, 276, occurred in the same runs but is in none of the kept
samples, so the receiver keeps searching for the marker.

Which length occurs follows the camera's display state, not the picture:
right after `startstream` and during a recording it was 324, after a touch
focus and the settings changes that followed it was 292.

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

## The state query

`getstate` on the DC-GH6 returns these fields (2026-11-22, record mode, no
recording running):

`batt`, `batt_per`, `rec`, `cammode`, `video_remaincapacity`,
`remaincapacity`, `sdcardstatus`, `sd_memory`, `version`, `temperature`,
`burst_interval_status`, `sd_access`, `rem_disp_typ`, `progress_time`,
`operate`, `stop_motion_num`, `stop_motion`, `lens`, `interval_status`,
`sdi_state`, `sd2_cardstatus`, `sd2_memory`, `sd2_access`, `current_sd`,
`backupmode`, `batt_grip`, `batt_per_grip`, `warn_disp`, `cinelike`,
`slotfunc`, `sd_usage_rate`, `sd_full`, `sd2_full`, `mf_guide`,
`interval_remain_num`, `interval_end_time`, `video_priority_disp`,
`lc_state`, `lc_expo_time`, `lc_shoot_num`, `lc_elapsed_sec`, `hrs_num`,
`hrs_state`, `bt_auto_trans`, `usb_ssd`.

Values seen: `temperature` `low`, `warn_disp` `no_disp`, `current_sd` `sd1`,
`slotfunc` `relay2_1`, `sdcardstatus` and `sd2_cardstatus` `write_enable`,
`sd_full` and `sd2_full` `false`, `progress_time` `0`, `batt` `5/5` with
`batt_per` `-1`. Which other values `temperature` and `warn_disp` take is
unmeasured.

During a recording (measured over three of them):

* `rec` is `on` from the first poll after `video_recstart`.
* `progress_time` counts the seconds of the running recording and is `0`
  again afterwards. It starts about a second late: `0` in the first poll.
* `video_remaincapacity` counts down by about one per second.
* `sd2_access` was `on` for the card being written, and `off` again after.
* After the recording `current_sd` had changed from `sd1` to `sd2`, the slot
  that was written (`slotfunc` was `relay2_1`).

In playback the reply has two more fields, `play` (`stop`) and
`add_location_data` (`off`).

## Events

The camera accepts a UPnP event subscription and then reports changes by
itself:

```
SUBSCRIBE http://<cam>:60606/Server0/CMS_event
CALLBACK: <http://<own address>:<port>/Camera/event>
NT: upnp:event
TIMEOUT: Second-300
```

The address is the `eventSubURL` of the `ConnectionManager` service in the
device description. The reply is `HTTP 200` with a `SID` and
`TIMEOUT: Second-300`. No session is needed: the subscription was made before
the registration. `UNSUBSCRIBE` with the `SID` ends it.

The camera then sends `NOTIFY` requests to the callback address. The first
one comes at once and carries the standard `SourceProtocolInfo`. Later ones
look like this:

```xml
<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">
  <e:property><X_Panasonic_Cam_Sync>lens_Atta</X_Panasonic_Cam_Sync></e:property>
</e:propertyset>
```

Measured events:

| Event | When |
|---|---|
| `X_Panasonic_Cam_VRec` = `start` | a recording starts, by command or on the camera; 0.1 s after `video_recstart` |
| `X_Panasonic_Cam_VRec` = `done` | a recording was stopped by `video_recstop`; about 1.5 s after the command |
| `X_Panasonic_Cam_VRec` = `err_UsrStp` | a recording was stopped on the camera |
| `X_Panasonic_Cam_Sync` = `busy` | a button on the camera was pressed: seen about a second before `err_UsrStp` and before the `start` of a recording started on the camera |
| `X_Panasonic_Cam_Sync` = `update`, then `lens_Atta` | after a recording ended or started on the camera |
| `X_Panasonic_Cam_Sync` = `lens_Atta` | right after `recmode` was acknowledged |
| `X_Panasonic_Cam_Sync` = `lens_Update` | once, a few seconds after a registration |
| `X_Panasonic_Cam_Sync` = `mod_Play` | the camera's own change to playback after the registration; seen in one of three runs |

So the event tells why a recording ended, which the state query cannot: both
show `rec` = `off`, but only a stop on the camera sends `err_UsrStp`. The
event arrived before the next state poll showed the change. The other
reasons the phone app knows (card full, card too slow, temperature) are
unmeasured.

No event came for `playmode`, and none for `mod_Play` in two of three
registrations, so the mode still has to be read from the state query.

## Listing and downloading the card's contents

Measured on 2026-11-22 with `python3 -m lumixremote.mediaprobe`; the cards
held three MOV clips.

The camera must be in playback (`cammode` = `play`), which it is by itself
shortly after the registration.

```
GET /cam.cgi?mode=get_content_info
-> <current_position>0</current_position>
   <total_content_number>3</total_content_number>
   <content_number>3</content_number>
```

The listing is a UPnP `Browse` on `ObjectID` `0`:

```
POST http://<cam>:60606/Server0/CDS_control
Content-Type: text/xml; charset="utf-8"
SOAPAction: urn:schemas-upnp-org:service:ContentDirectory:1#Browse
```

with the request body shown in `media-transfer.md`. It needs no session
header. The reply names `NumberReturned` and `TotalMatches` and holds an
escaped DIDL-Lite document. All three clips came as `<item>` directly below
`0`, without folders.

**The listing covers one card slot, the one in `current_sd`.** The other one
is reached with

```
GET /cam.cgi?mode=setsetting&type=current_sd&value=sd1    (or sd2)
```

in playback; `get_content_info` and `Browse` then show that card. Measured:
`sd1` held three MOV clips (`CAM_AVC_MOV_ORG`), `sd2` three MP4 clips
(`CAM_AVC_MP4_4K_ORG`). The listing was read two seconds after the change;
whether it needs that long is unmeasured. A recording sets `current_sd` to
the slot it wrote.

Per item: `id`, `dc:title` (folder and file number, like `123-0001`),
`upnp:class` and three `<res>` entries, each with an address on port 50001
and a `size` attribute:

| `PANASONIC.COM_PN` | Address | Content |
|---|---|---|
| `CAM_AVC_MOV_ORG` | `/DO<id>.MOV` | the clip itself, with `duration`, `size` and `OriginalFileName` |
| `CAM_TN` | `/DT<id>.JPG` | thumbnail, 160x120 |
| `CAM_LRGTN` | `/DL<id>.JPG` | large preview |

The `size` of the two pictures is a placeholder (5000 and 100000); the
thumbnail really had 9021 bytes.

`getinfo&type=capability` names no folders with an `object_id` on this
model, apart from one for the automatic upload. It does carry a table
(`contents_action_info`) of what the phone app may do per kind of file; for
`CAM_AVC_MOV_ORG` it says `copy_to_sp enable="no"`. That only binds the app:

**MOV clips can be downloaded.** A GET of the `<res>` address with the
`X-SESSION_ID` header returned `HTTP 200` and the header `X-FILE_SIZE` with
the full size (about 824 MB). The first 32 MB arrived at 45.5 Mbit/s over
WLAN; the transfer was then cut off by closing the connection, which the
camera took without trouble: `recmode`, the state query and the live view
worked right afterwards.

**`Range` works.** `Range: bytes=0-1023` returned `HTTP 206` with
`Content-Range: bytes 0-1023/<size>`, for the thumbnail and for the clip. So
a broken download can be resumed.

At that rate a large clip takes long: one of the three had 178 GB, which is
almost nine hours.

Unmeasured: photos (JPEG and RAW), MP4 clips, a complete download of a large
file, whether the session survives it, and what happens when a second file
is fetched at the same time.

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
