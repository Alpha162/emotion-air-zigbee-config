"""Independently verify a .ota file against EVERY gate the eMotion Air's stock OTA
client enforces (documented in docs/ota-format.md). Deliberately does NOT reuse
make_ota.py's logic, so it catches mistakes in the builder rather than echoing them.

Run this before serving any image you have built.

Usage: python verify_ota.py <file.ota> [device_current_version_hex]
  e.g. python verify_ota.py patched.ota 0x10193001
"""
import struct
import sys
import zlib

OK, BAD = "PASS", "FAIL"
results = []


def check(label, cond, detail=""):
    results.append((OK if cond else BAD, label, detail))
    return cond


def main():
    path = sys.argv[1]
    cur = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x10193001
    d = open(path, "rb").read()
    print(f"verifying {path}  ({len(d)} bytes)")
    print(f"device current fileVersion assumed: 0x{cur:08x}\n")

    # ---- OTA header gates (device: FUN_000115ac stream parser) ----
    fid  = struct.unpack_from("<I", d, 0x00)[0]
    hlen = struct.unpack_from("<H", d, 0x06)[0]
    manu = struct.unpack_from("<H", d, 0x0A)[0]
    ityp = struct.unpack_from("<H", d, 0x0C)[0]
    fver = struct.unpack_from("<I", d, 0x0E)[0]
    stkv = struct.unpack_from("<H", d, 0x12)[0]
    tot  = struct.unpack_from("<I", d, 0x34)[0]

    check("file identifier == 0x0BEEF11E", fid == 0x0BEEF11E, f"0x{fid:08x}")
    check("header length == 56",           hlen == 56,        f"{hlen}")
    check("manufacturer == 0x4231",        manu == 0x4231,    f"0x{manu:04x}")
    check("image type == 0x0301",          ityp == 0x0301,    f"0x{ityp:04x}")
    check("stack version == 0x0002 (hard gate)", stkv == 2,   f"0x{stkv:04x}")
    check("total size field == file size", tot == len(d),     f"{tot} vs {len(d)}")
    check("total <= 0x40000",              len(d) <= 0x40000, f"{len(d)}")
    check("fileVersion != device current", fver != cur,       f"0x{fver:08x}")
    check("fileVersion > device current (so ZHA offers it)", fver > cur,
          f"0x{fver:08x} > 0x{cur:08x}")

    # ---- sub-element framing ----
    tag, slen = struct.unpack_from("<HI", d, hlen)
    img = d[hlen + 6:]
    check("sub-element tag == 0x0000", tag == 0x0000, f"0x{tag:04x}")
    check("sub-element length matches payload", slen == len(img), f"{slen} vs {len(img)}")

    # ---- the inner KNLT image: same acceptance test as the BLE path ----
    check("KNLT magic @0x08", img[8:12] == b"KNLT", img[8:12].hex(" "))
    check("format marker img[6:8] == 5D 02", img[6] == 0x5D and img[7] == 0x02,
          img[6:8].hex(" "))
    check("boot flag byte@8 == 0x4B", img[8] == 0x4B, f"0x{img[8]:02x}")
    ilen = struct.unpack_from("<I", img, 0x18)[0]
    check("length field @0x18 == image length", ilen == len(img), f"{ilen} vs {len(img)}")
    check("length & 0xF == 4 (payload 16-aligned)", (ilen & 0xF) == 4, f"0x{ilen:x}")
    check("image <= 0x40000 (bank size)", len(img) <= 0x40000, f"{len(img)}")
    trailer = struct.unpack_from("<I", img, len(img) - 4)[0]
    calc = (~zlib.crc32(img[:-4])) & 0xFFFFFFFF
    check("trailing JAMCRC valid", trailer == calc, f"0x{trailer:08x} vs 0x{calc:08x}")

    # ---- in-image OTA identity must agree with the container ----
    in_ver  = struct.unpack_from("<I", img, 0x34788)[0]
    in_type = struct.unpack_from("<H", img, 0x34790)[0]
    in_manu = struct.unpack_from("<H", img, 0x34792)[0]
    check("in-image fileVersion == header fileVersion", in_ver == fver,
          f"0x{in_ver:08x} vs 0x{fver:08x}")
    check("in-image imageType == header imageType", in_type == ityp, f"0x{in_type:04x}")
    check("in-image manufacturer == header manufacturer", in_manu == manu, f"0x{in_manu:04x}")

    # ---- report ----
    print(f"{'RESULT':6}  {'CHECK':46}  DETAIL")
    for r, label, detail in results:
        print(f"{r:6}  {label:46}  {detail}")
    failed = [r for r in results if r[0] == BAD]
    print()
    if failed:
        print(f"*** {len(failed)} CHECK(S) FAILED — do not serve this image ***")
        return 1
    print(f"*** ALL {len(results)} CHECKS PASSED — image satisfies every device gate ***")
    print("NOTE: static verification only. A live transfer over Zigbee is still untested.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
