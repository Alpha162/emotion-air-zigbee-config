# OTA formats: BLE and Zigbee

Two independent ways to get an image onto the device. Both end in the same place: the
inactive flash bank, verified, then a boot-flag flip.

## What the device accepts (both paths)

After a transfer completes, the firmware checks:

- `img[6] == 0x5D` and `img[7] == 0x02`, the format marker
- `KNLT` magic at `0x08`
- length field at `0x18` no greater than `0x40000`
- CRC-32/JAMCRC over `image[:-4]` equals the trailing 4 bytes

Only then does it write `0x4B` to the new bank's boot flag, invalidate the old bank and
reboot. There is no signature and no encryption on either path.

## BLE OTA

Characteristic `00010203-0405-0607-0809-0a0b0c0d2b12`.

| Message | Bytes |
|---|---|
| START | `01 FF` (`0xFF01`) |
| data | `[u16 index][16 bytes payload][u16 crc16]`, 20 bytes |
| END | `02 FF [u16 count][u16 ~count]` |
| ABORT | `00 FF` (`0xFF00`) |

`crc16` covers the first 18 bytes of the packet: poly `0xA001`, init `0xFFFF`, no final
xor. `count` is the last index used. A short final chunk is padded with `0xFF`.

There is no version check at all on this path. It will write an older image over a newer
one without complaint, which makes it the reliable recovery route.

Roughly 13,500 writes, about 4 minutes, for a 216 KB image.

There is also a third way in, worth knowing during development: the vendor app's own manual
firmware update accepts a raw patched `.bin` with no signature or provenance check, and
takes about a minute. Use the `.bin` rather than the `.ota`, since the BLE path does not
expect the Zigbee wrapper.

## Zigbee OTA (ZCL cluster 0x0019)

The device is a stock ZCL OTA client and polls for images on its own: 12 seconds after
joining, then every 300 seconds.

### What it validates

| Field | Required value |
|---|---|
| OTA file identifier | `0x0BEEF11E` |
| Zigbee stack version | `0x0002`, a hard gate; anything else is rejected |
| manufacturer code | `0x4231` (16945) |
| image type | `0x0301` (769) |
| total image size | no greater than `0x40000` |
| file version | must differ from the running one |

Note that last row. It rejects only an exactly equal version, so there is no anti-downgrade
check and an older image can be delivered by stamping it with a higher version number. The
header string and hardware version are not checked at all.

### Container layout

A 56-byte header, then a 6-byte sub-element header, then the raw image verbatim.

| Offset | Size | Field |
|---|---|---|
| `0x00` | u32 | file identifier `0x0BEEF11E` |
| `0x04` | u16 | header version `0x0100` |
| `0x06` | u16 | header length `56` |
| `0x08` | u16 | field control `0` |
| `0x0A` | u16 | manufacturer `0x4231` |
| `0x0C` | u16 | image type `0x0301` |
| `0x0E` | u32 | file version |
| `0x12` | u16 | zigbee stack version `0x0002` |
| `0x14` | 32 B | header string (ignored) |
| `0x34` | u32 | total size, `62 + len(image)` |
| `0x38` | u16 | sub-element tag `0x0000` |
| `0x3A` | u32 | sub-element length, `len(image)` |
| `0x3E` | ... | the image |

> Do not use sub-element tag `0xF000`. It shifts the payload and breaks the image's own CRC
> offset.

Also stamp the in-image OTA version at `0x34788` and repair the trailing CRC, or the device
will report its old version after flashing and ZHA will keep offering the update forever.
`make_ota.py` warns if the two disagree.

### Serving it from ZHA

See the README for the `configuration.yaml` block. The one thing worth repeating:

> The `warning:` string must match zigpy's expected text exactly. A truncated one makes
> zigpy silently drop the provider: ZHA loads fine, config validation passes, and there is
> no log line anywhere. If images are never offered, check this first.

The provider type is `advanced` (`AdvancedFileProvider`). It recursively scans the directory
and parses OTA headers, so no index file is needed, and you can just drop the `.ota` in.

### Driving an update on a sleepy device

Modern ZHA answers a device's own image queries with "no image available" unless an install
is actively running, so waiting for the device to notice on its own will not work.
`update.install` has to succeed.

Expect it to need retries:

| Error | Meaning |
|---|---|
| `NWK_ROUTE_DISCOVERY_FAILED` | device asleep, no route, retry |
| `TimeoutError()` | notify delivered, but it did not start pulling blocks in time |

Retry in a loop while generating motion in front of the sensor, which keeps it in fast-poll
long enough to get started. Once blocks are flowing it sustains itself.

A full transfer takes 30 to 40 minutes. Do not pair other Zigbee devices meanwhile: a join
killed one of our transfers at 99%, and ZHA does not resume, it restarts from zero.
