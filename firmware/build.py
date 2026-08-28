#!/usr/bin/env python3
"""Build patched eMotion Air firmware from YOUR OWN stock image.

This repository deliberately ships no firmware binaries — the stock image is
LinknLink's copyrighted work. You supply it (see FIRMWARE.md); this script
compiles the shim, applies the patches, and produces:

  * <out>.bin  — raw image, flashable over BLE
  * <out>.ota  — the same image wrapped as a Zigbee OTA container (optional)

What it patches into the stock image:

  1. the global ZCL message-callback pointer  @0x942C  ->  our shim
  2. the occupancy cluster's attribute table  @0x3356A/@0x3356C  ->  a larger
     table (read-only mirrors of the live config block, so ZHA can read real values)
  3. the OTA file version @0x34788 and the DIS version string @0x2DC80, so the
     flashed firmware reports the new version
  4. the trailing whole-image CRC, recomputed

Requires the TC32 toolchain on PATH (tc32-elf-gcc / ld / objcopy). See README.

Usage:
    python3 build.py --base stock_1.1.9.bin --out emotion_air_patched
    python3 build.py --base stock_1.1.9.bin --out fw --no-ota --version 0x101A3001
"""
import argparse
import os
import struct
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- offsets in the stock image (load base is 0x0, so offset == address) ----
APPEND_AT   = 0x34B30      # where the stock 4-byte CRC sits; shim goes here
HOOK_PTR    = 0x0942C      # flash word holding &FUN_0000a310 (global ZCL msg cb)
ORIG_CB     = 0x0000A311   # its expected current value (thumb bit set)
SHIM_ENTRY  = 0x00034B31   # shim_zcl_cb | thumb bit
ATTRNUM_OFF = 0x3356A      # occupancy cluster: attribute count (u16)
ATTRTBL_OFF = 0x3356C      # occupancy cluster: attribute table pointer (u32)
STOCK_TBL   = 0x000334FC   # its expected current value
STOCK_COUNT = 2
NEW_TABLE   = 0x00034E00   # our table's address (fixed by link.ld)
NEW_COUNT   = 8
VER_OFF     = 0x2DC80      # DIS "Firmware Revision" string (5 chars)
OTAVER_OFF  = 0x34788      # OTA file version (u32)
LEN_FIELD   = 0x18
STOCK_LEN   = 0x34B34      # length of the known-good stock image
MAX_IMAGE   = 0x40000

DEFAULT_OTA_VERSION = 0x101A3001   # stock reports 0x10193001


def jamcrc(data: bytes) -> int:
    """CRC-32/JAMCRC — the trailing whole-image checksum the bootloader checks."""
    return (~zlib.crc32(data)) & 0xFFFFFFFF


def compile_shim(verbose=False):
    """Compile + link shim.c and return the raw blob."""
    src = os.path.join(HERE, "shim.c")
    ld = os.path.join(HERE, "link.ld")
    obj = os.path.join(HERE, "shim.o")
    elf = os.path.join(HERE, "shim.elf")
    binf = os.path.join(HERE, "shim.bin")

    steps = [
        ["tc32-elf-gcc", "-Os", "-std=gnu99", "-ffunction-sections",
         "-fno-jump-tables", "-c", src, "-o", obj],
        ["tc32-elf-ld", "-T", ld, "-o", elf, obj],
        ["tc32-elf-objcopy", "-O", "binary", elf, binf],
    ]
    for cmd in steps:
        if verbose:
            print("  $", " ".join(cmd))
        try:
            subprocess.run(cmd, check=True, capture_output=not verbose)
        except FileNotFoundError:
            sys.exit(f"error: {cmd[0]} not found on PATH.\n"
                     "The TC32 toolchain is required — see README.md "
                     "(on Windows, run this inside WSL).")
        except subprocess.CalledProcessError as e:
            out = (e.stderr or b"").decode(errors="replace")
            sys.exit(f"error: {cmd[0]} failed:\n{out}")

    blob = open(binf, "rb").read()
    # sanity: our attribute table must have landed at NEW_TABLE
    tbl_at = NEW_TABLE - APPEND_AT
    if len(blob) < tbl_at + 8 or struct.unpack_from("<H", blob, tbl_at)[0] != 0x0000:
        sys.exit("error: attribute table not at the expected address — check link.ld")
    return blob


def build(base_path, out_base, ota_version, make_ota=True, ver_str=b"1.4.0",
          verbose=False):
    base = open(base_path, "rb").read()
    print(f"base image : {base_path}  ({len(base)} bytes)")

    if len(base) != STOCK_LEN:
        print(f"  ! warning: expected {STOCK_LEN} bytes for stock v1.1.9; "
              f"offsets may not apply to this build")
    if base[8:12] != b"KNLT":
        sys.exit("error: no KNLT magic at 0x08 — is this an eMotion Air image?")
    if base[APPEND_AT:APPEND_AT + 4] != base[-4:]:
        sys.exit("error: image does not end where expected; wrong variant?")

    print("compiling shim ...")
    blob = compile_shim(verbose)
    print(f"  shim blob : {len(blob)} bytes -> 0x{APPEND_AT:05X}..0x{APPEND_AT + len(blob):05X}")

    body = bytearray(base[:APPEND_AT]) + blob

    # payload (everything but the 4-byte CRC) must be a multiple of 16
    pad = (-len(body)) % 16
    body += b"\x00" * pad
    if pad:
        print(f"  padded    : +{pad} bytes (payload 16-aligned)")

    # 1) hook the global ZCL message callback
    cur = struct.unpack_from("<I", body, HOOK_PTR)[0]
    if cur != ORIG_CB:
        sys.exit(f"error: hook pointer @0x{HOOK_PTR:05X} is 0x{cur:08X}, "
                 f"expected 0x{ORIG_CB:08X} — already patched, or unknown build")
    struct.pack_into("<I", body, HOOK_PTR, SHIM_ENTRY)
    print(f"  hook      : 0x{HOOK_PTR:05X}  0x{cur:08X} -> 0x{SHIM_ENTRY:08X}")

    # 2) repoint the occupancy attribute table at ours
    n = struct.unpack_from("<H", body, ATTRNUM_OFF)[0]
    t = struct.unpack_from("<I", body, ATTRTBL_OFF)[0]
    if (n, t) != (STOCK_COUNT, STOCK_TBL):
        sys.exit(f"error: occupancy attr table is num={n} tbl=0x{t:08X}, "
                 f"expected {STOCK_COUNT}/0x{STOCK_TBL:08X}")
    struct.pack_into("<H", body, ATTRNUM_OFF, NEW_COUNT)
    struct.pack_into("<I", body, ATTRTBL_OFF, NEW_TABLE)
    print(f"  attr table: 0x{t:08X} -> 0x{NEW_TABLE:08X}   count {n} -> {NEW_COUNT}")

    # 3) version stamps
    old_ver = bytes(body[VER_OFF:VER_OFF + len(ver_str)])
    body[VER_OFF:VER_OFF + len(ver_str)] = ver_str
    old_ota = struct.unpack_from("<I", body, OTAVER_OFF)[0]
    struct.pack_into("<I", body, OTAVER_OFF, ota_version)
    print(f"  DIS string: {old_ver.decode(errors='replace')} -> {ver_str.decode()}")
    print(f"  OTA version: 0x{old_ota:08X} -> 0x{ota_version:08X}")

    # 4) length + CRC
    total = len(body) + 4
    struct.pack_into("<I", body, LEN_FIELD, total)
    crc = jamcrc(bytes(body))
    out = bytes(body) + struct.pack("<I", crc)
    print(f"  length    : {total} bytes;  CRC 0x{crc:08X}")

    # invariants the device itself checks
    assert out[8:12] == b"KNLT"
    assert out[6] == 0x5D and out[7] == 0x02, "format marker damaged"
    assert out[8] == 0x4B, "boot flag byte@8 != 0x4B"
    assert struct.unpack_from("<I", out, LEN_FIELD)[0] == len(out)
    assert struct.unpack_from("<I", out, len(out) - 4)[0] == jamcrc(out[:-4])
    assert total & 0xF == 4, "payload not 16-aligned"
    assert total <= MAX_IMAGE, "image exceeds bank size"

    bin_path = out_base + ".bin"
    open(bin_path, "wb").write(out)
    print(f"\nwrote {bin_path}  ({len(out)} bytes) — self-check OK")

    if make_ota:
        sys.path.insert(0, os.path.join(HERE, os.pardir, "tools"))
        from make_ota import wrap  # noqa: E402
        ota_path = out_base + ".ota"
        wrap(out, ota_path, ota_version)
        print(f"wrote {ota_path} — serve this from ZHA")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="your stock firmware image (.bin)")
    ap.add_argument("--out", default="emotion_air_patched",
                    help="output basename (default: emotion_air_patched)")
    ap.add_argument("--version", default=hex(DEFAULT_OTA_VERSION),
                    help="OTA file version to stamp; must differ from the running one "
                         f"(stock = 0x10193001, default {DEFAULT_OTA_VERSION:#x})")
    ap.add_argument("--dis", default="1.4.0",
                    help="DIS firmware-revision string, exactly 5 chars (default 1.4.0)")
    ap.add_argument("--no-ota", action="store_true", help="skip the .ota wrapper")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    dis = a.dis.encode()
    if len(dis) != 5:
        sys.exit("error: --dis must be exactly 5 characters (it overwrites in place)")

    build(a.base, a.out, int(a.version, 0), make_ota=not a.no_ota,
          ver_str=dis, verbose=a.verbose)


if __name__ == "__main__":
    main()
