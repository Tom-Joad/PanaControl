# What the phone app does beyond our protocol notes

**Status: read from the phone app, not yet verified on a camera.** Same source
and same rule as [`media-transfer.md`](media-transfer.md): LUMIX Sync,
decompiled on 2026-11-22; whatever gets measured on the DC-GH6 moves to
[`protocol.md`](protocol.md).

These are the points where the app works differently from PanaControl and
that could improve the live view, the readings in the top bar or the
recording.

## The camera reports events by itself

The app does not learn about the end of a recording by polling. It subscribes
to UPnP events (GENA `SUBSCRIBE`) at the `eventSubURL` of the
`ConnectionManager` service from the device description, with a callback
address on the phone (`…/Camera/event`). The camera then sends `NOTIFY`
requests whose body holds lines of the form `@X_Panasonic_Cam_<group>=<value>`.
(`com.panasonic.jp.service.c`, `q5.h`; the request itself is built in the
native library `libdlnaCore.so`.)

| Group | Values |
|---|---|
| `VRec` (video recording) | `start`, `done`, and as reasons for an end: `err_SDfull`, `err_UsrStp`, `err_UpLimit`, `err_wr`, `err_SpdInsuf`, `err_AVCHDTr`, `err_Tempa`, `err_MemFull`, `err_CrFold` |
| `PRec` (photo) | `err_rec`, `err_CrFold`, `err_wr`, `err_DisableShoot`, `err_Wifi_Connecting`, `err_Tempa_Multi` |
| `Sync` | `mod_Play`, `mod_Rec`, `lens_Atta`, `lens_Deta`, `lens_Update`, `busy`, `busy_no_msg`, `update`, `fan_alert_on`, `fan_alert_off` |
| `Req` | `mod_Rec`, `mod_LoopRecStop` |
| `Transfer` | `transfer_Start`, `transfer_Stop` |
| `PictBrst` | `SD_wr_fin`, `PictBrst_End` and several `err_…` |

What this could give PanaControl:

* **Why a recording ended**: stopped on the camera (`err_UsrStp`), card full,
  card too slow (`err_SpdInsuf`), temperature (`err_Tempa`), write error.
  Today only "`rec` is no longer `on`" is known, up to a poll interval late.
* **The mode change without polling**: `mod_Play` and `mod_Rec` could replace
  the half-second `cammode` polls after the registration.
* **Lens changes** (`lens_Atta`, `lens_Deta`), so the aperture limits are
  re-read at the right moment, and the **fan alert**.

It needs a TCP port on the container that the camera can reach.

Measured on 2026-11-22: the DC-GH6 accepts the subscription and sent
`Sync` = `lens_Atta`; see "Events" in `protocol.md`. Still open: the `VRec`
values during a real recording, why no `mod_Play` came for the change to
playback, and whether the subscription survives the dropouts described in
`protocol.md`.

## More fields in `getstate`

Besides the fields PanaControl shows, the app reads these from the `getstate`
reply (`x5.C3065i`):

| Field | Content |
|---|---|
| `rectime` | elapsed recording time, with `hour`, `min`, `sec` |
| `recremaincapacity` | remaining capacity while recording |
| `rec_target_slot`, `multi_sd`, `slotfunc_icon` | which card slot records |
| `sd_memory`, `sdcardstatus` | card present and its state, e.g. `write_protected`, `card_not_supported`, `media_over2tb` |
| `state_icon`, `processingstate` | warnings, among them `heat_warn_cannot_use`, `heat_warn_card_temperature`, `memory_card_heat_detect`, `memory_card_heat_cannot_rec`, `battery_degradation` |

The DC-GH6 names its fields differently; the measured list is under "The
state query" in `protocol.md`. Of the app's names only `sdcardstatus` and
`sd_memory` occur there. The counterparts are probably `progress_time` for
the elapsed recording time, `current_sd` and `slotfunc` for the recording
slot, and `temperature` and `warn_disp` for heat warnings; their values
during a recording and in a warning state are unmeasured.

## The live view datagram has a fixed layout

`protocol.md` says the header length varies and the JPEG start is searched.
The app computes it (`s5.C2818b`, all numbers big-endian):

| Offset | Size | Content |
|---|---|---|
| 0 | 2 | length of the datagram's payload |
| 2 | 2 | `0x0100` |
| 4 | 4 | running number |
| 8 | 1 | |
| 9 | 6 | |
| 15 | 1 | |
| 16 | 1 | kind of data |
| 17 | 1 | |
| 18 | 4 | |
| 22 | 4 | total size, used when a frame spans several datagrams |
| 26 | 4 | |
| 30 | 2 | length of the information block that follows |
| 32 | n | information block, starts with a 2-byte type (1, 2, 3, 5, 6, 8, 9, 10, 11 or 12) |
| 32 + n | | JPEG |

So the JPEG starts at `32 + (u16 at offset 30)`. Measured on the DC-GH6 for
the header length 324, see "Live stream" in `protocol.md`; bytes 9 to 14 are
the camera's MAC address, byte 16 was `0x21`, and the information block had
type 12. The typed information block carries the exposure readings the app
shows over the picture. Its fields are not named in the decompiled code;
mapping them (shutter speed, ISO, exposure compensation, focus frames with
coordinates and colour) needs captured datagrams next to known camera
settings.

Also seen: the app can merge frames that span several datagrams. On the
DC-GH6 at 640x360 one datagram per frame was measured; a larger
`liveviewsize` may change that.

## No difference found

* `startstream`, `stopstream`, `video_recstart` and `video_recstop` are sent
  exactly as PanaControl sends them, with no extra parameters. On `err_busy`
  the app waits and retries `startstream` up to five times.
* The app also binds the UDP port (from 49152 upwards) before `startstream`.
