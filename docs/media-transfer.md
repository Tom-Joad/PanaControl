# Listing and downloading the card's contents

**Status: read from the phone app, not yet verified on a camera.** Unlike
[`protocol.md`](protocol.md), nothing here was observed on the device. Once a
step has been measured on the DC-GH6, move it to `protocol.md` and remove it
here.

Source: the Android app **LUMIX Sync** (`com.panasonic.jp.lumixsync`, APK
sha256 `6986e22f…ab10e377`), decompiled with jadx 1.5.3 on 2026-11-22, for
interoperability. Class names are the ones jadx gives.

## Sequence the app uses

1. Register as usual (`req_acc_g`, `req_acc_e`); the session id travels in the
   `X-SESSION_ID` header on every later request, downloads included.
2. `GET /cam.cgi?mode=camcmd&value=playmode` with a 3 s timeout; on
   `err_busy` the app retries. If this fails, the app gives up browsing.
   (`A5.a`, `B5.a.n`)
3. `GET /cam.cgi?mode=getinfo&type=capability` names the folders that can be
   listed: `<media id type detail>` elements (`detail="sd"` for a card) with
   `<dir id type detail title_id object_id>` children. The `object_id` is the
   root for the listing. (`x5.o`, `x5.u`)
4. `GET /cam.cgi?mode=get_content_info` (for a folder:
   `&value=<object_id>`) returns `total_content_number` and
   `current_position`. While the camera still builds its index the count is
   `-1`; the app asks again every second. `err_db` means the camera's media
   database is broken. (`B5.a.z`, `B5.o`)
5. List with UPnP `Browse`, 15 entries per call in the browser screen, 50 in
   the automatic transfer; up to 3 attempts per call.
6. Download each file with a plain HTTP GET of the address from the listing.

## The Browse call

```
POST http://<cam>:60606/Server0/CDS_control
Content-Type: text/xml; charset="utf-8"
SOAPAction: urn:schemas-upnp-org:service:ContentDirectory:1#Browse
User-Agent: Panasonic MIL DLNA CP UPnP/1.0
```

```xml
<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1"
              xmlns:pana="urn:schemas-panasonic-com:pana">
      <ObjectID>0</ObjectID>
      <BrowseFlag>BrowseDirectChildren</BrowseFlag>
      <Filter>*</Filter>
      <StartingIndex>0</StartingIndex>
      <RequestedCount>50</RequestedCount>
      <SortCriteria></SortCriteria>
      <pana:X_FromCP>LumixLink2.0</pana:X_FromCP>
    </u:Browse>
  </s:Body>
</s:Envelope>
```

`ObjectID` is `0` for everything or an `object_id` from the capability reply.
Note the path: `/Server0/CDS_control`, without the `/Lumix` prefix that the
device description has. (`z5.C3142a`, `B5.m.a`)

The reply carries an escaped DIDL-Lite document in `<Result>`. Per `<item>`
the app reads `id`, `parentID`, `dc:title`, `dc:date`
(`yyyy-MM-dd'T'HH:mm:ss`), `upnp:class`, `X_Type`, `X_Rating`,
`X_RecGroupType` and every `<res>`. Folders come as `<container>`.
(`q5.C2649b`)

## The `<res>` entries

The text of a `<res>` is the download address. Its `protocolInfo` attribute
tells what it is: the MIME type, `PANASONIC.COM_PN=<kind>` and
`OriginalFileName='<name>'`. (`q5.e`)

| MIME type | `PANASONIC.COM_PN` | Meaning |
|---|---|---|
| `image/jpeg` | `CAM_TN` | thumbnail |
| `image/jpeg` | `CAM_LRGTN` | large preview |
| `image/jpeg` | `CAM_ORG` | original JPEG |
| `application/octet-stream` | `CAM_RAW` | RAW |
| `application/octet-stream` | `CAM_RAW_JPG` | JPEG of a RAW+JPEG pair |
| `application/octet-stream` | `CAM_HSP_JPG`, `CAM_HSP_RAW`, `CAM_HSP_RAW_JPG` | high-resolution shots |
| `application/octet-stream` | `CAM_AVC_MP4_…` (e.g. `CAM_AVC_MP4_ORG`, `CAM_AVC_MP4_4K_ORG`) | MP4 video |
| `application/octet-stream` | `CAM_AVC_MOV_…` | MOV video |
| `application/octet-stream` | `CAM_AVC_TS_HP_…` | AVCHD stream |
| `video/mp4` | any | MP4 video |

The app's automatic transfer takes exactly these kinds as "the original":
`CAM_ORG`, `CAM_RAW_JPG`, `CAM_RAW`, `CAM_AVC_MP4_ORG`, `CAM_HSP_JPG`,
`CAM_HSP_RAW`, `CAM_HSP_RAW_JPG`, `CAM_AVC_MP4_4K_ORG`. MOV is known to the
parser but not on that list.

RAW files are only offered after
`GET /cam.cgi?mode=setsetting&type=raw_img_send&value=enable` (`disable` to
switch it off again).

## The download

A GET of the `<res>` address with `User-Agent: LUMIX Sync` and the
`X-SESSION_ID` header. The camera answers with the size in the `X-FILE_SIZE`
header. The optional request header `X-CONVERT: MediumSize` or
`X-CONVERT: SmallSize` asks for a reduced picture. The app does not send
`Range`; whether the camera can resume a broken download is unknown.
(`r5.d`, `r5.AbstractC2682b`)

## Measured since

On 2026-11-22 the listing, the download of a MOV clip, `Range` and
`playmode` were measured on the DC-GH6; see "Listing and downloading the
card's contents" in `protocol.md`. The sequence above held, with two
differences: the DC-GH6 names no folders in the capability reply, everything
is listed below `ObjectID` `0`, and MOV clips can be fetched although the
app's own table forbids the app to copy them. The listing shows one card
slot at a time; `setsetting&type=current_sd` changes it.

Still to be measured:

* Photos: which `<res>` kinds JPEG and RAW carry, and whether RAW needs
  `raw_img_send`.
* A complete download of a large clip: does the session survive it without
  `getstate`, and what does the camera do when recording is asked for
  meanwhile?
