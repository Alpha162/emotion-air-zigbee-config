#!/usr/bin/env python3
"""Wrap a raw eMotion Air firmware image in a Zigbee OTA (.ota) container.

Normally invoked by firmware/build.py, but usable standalone, e.g. to wrap a
STOCK image with a high file version so ZHA will offer it as an "upgrade", which
is how you roll back over the air (the device accepts any version that differs
from the one it is running; it only rejects an exactly equal one).

Container layout (all little-endian):

  56-byte OTA header
    0x00 u32  file identifier   0x0BEEF11E   <- device VALIDATES
    0x04 u16  header version    0x0100
    0x06 u16  header length     56
    0x08 u16  field control     0
    0x0A u16  manufacturer      0x4231       <- device VALIDATES
    0x0C u16  image type        0x0301       <- device VALIDATES
    0x0E u32  file version
    0x12 u16  zigbee stack ver  0x0002       <- device VALIDATES (hard gate)
    0x14 32B  header string                  (not checked)
    0x34 u32  total image size  = 62 + len(image)
  6-byte sub-element
    0x00 u16  tag id            0x0000       (do NOT use 0xF000, it shifts the payload)
    0x02 u32  length            = len(image)
  then the raw image, verbatim.

Usage:
    python3 make_ota.py <image.bin> <output.ota> [file_version_hex]
"""
import struct
import sys
import zlib

OTA_MAGIC = 0x0BEEF11E
HDR_LEN = 56
MANUF = 0x4231           # 16945
IMAGE_TYPE = 0x0301      # 769
STACK_VER = 0x0002       # device hard-checks == 2
MAX_TOTAL = 0x40000

OTAVER_OFF = 0x34788     # in-image OTA file version
ITYPE_OFF = 0x34790      # in-image image type


def wrap(image: bytes, out_path: str, file_version: int, header_string=b"eMotion Air"):
    """Wrap `image` and write it to `out_path`. Returns the container bytes."""
    itype = struct.unpack_from("<H", image, ITYPE_OFF)[0]
    if itype != IMAGE_TYPE:
        raise SystemExit(f"unexpected in-image type 0x{itype:04X}; not an eMotion Air image?")

    in_ver = struct.unpack_from("<I", image, OTAVER_OFF)[0]
    if in_ver != file_version:
        print(f"  ! note: in-image version is 0x{in_ver:08X} but the container says "
              f"0x{file_version:08X}. The device will report the in-image value after "
              f"flashing, so ZHA may keep offering the update. Stamp them the same.")

    total = HDR_LEN + 6 + len(image)
    if total > MAX_TOTAL:
        raise SystemExit(f"total {total} exceeds the device ceiling 0x{MAX_TOTAL:X}")

    h = bytearray(HDR_LEN)
    struct.pack_into("<I", h, 0x00, OTA_MAGIC)
    struct.pack_into("<H", h, 0x04, 0x0100)
    struct.pack_into("<H", h, 0x06, HDR_LEN)
    struct.pack_into("<H", h, 0x08, 0x0000)
    struct.pack_into("<H", h, 0x0A, MANUF)
    struct.pack_into("<H", h, 0x0C, IMAGE_TYPE)
    struct.pack_into("<I", h, 0x0E, file_version)
    struct.pack_into("<H", h, 0x12, STACK_VER)
    h[0x14:0x34] = header_string.ljust(32, b"\x00")[:32]
    struct.pack_into("<I", h, 0x34, total)

    ota = bytes(h) + struct.pack("<HI", 0x0000, len(image)) + image
    open(out_path, "wb").write(ota)
    return ota


def stamp_version(image: bytes, file_version: int) -> bytes:
    """Set the in-image OTA file version and repair the trailing whole-image CRC."""
    d = bytearray(image)
    struct.pack_into("<I", d, OTAVER_OFF, file_version)
    struct.pack_into("<I", d, len(d) - 4, (~zlib.crc32(bytes(d[:-4]))) & 0xFFFFFFFF)
    return bytes(d)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    version = int(sys.argv[3], 0) if len(sys.argv) > 3 else None

    image = open(src, "rb").read()
    if version is None:
        version = struct.unpack_from("<I", image, OTAVER_OFF)[0]
        print(f"using the image's own OTA version 0x{version:08X}")
    else:
        # keep the in-image version consistent with the container
        image = stamp_version(image, version)
        print(f"stamped in-image version 0x{version:08X} and repaired the CRC")

    ota = wrap(image, dst, version)
    print(f"wrote {dst}  ({len(ota)} bytes)")
    print(f"  manufacturer 0x{MANUF:04X} | image type 0x{IMAGE_TYPE:04X} | "
          f"version 0x{version:08X} | stack {STACK_VER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
