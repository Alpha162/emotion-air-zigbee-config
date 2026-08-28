# Obtaining the stock firmware image

**This repository deliberately contains no firmware binaries.** The eMotion Air's stock
firmware is LinknLink's copyrighted work; a patched image is ~99.8% their code with a few
hundred bytes of ours appended, so distributing one would be redistributing their product.

The tooling here is a *patch generator*: you supply your own copy of the stock image, and
it produces the patched build locally. Nothing about the result differs from ours — the
patches are deterministic.

## What you need

The raw Telink firmware image for **eMotion Air v1.1.9**:

| Property | Value |
|---|---|
| Size | 215,860 bytes (`0x34B34`) |
| Magic | `KNLT` at offset `0x08` |
| Format marker | `5D 02` at offsets `0x06`–`0x07` |
| Length field | u32 at `0x18` == the file size |
| Trailing 4 bytes | CRC-32/JAMCRC over everything before them |

`firmware/build.py` checks all of these and refuses to proceed if they do not match, so a
wrong or corrupted file will be rejected rather than silently mangled.

## Where it comes from

The image is distributed by the vendor as part of their normal firmware-update mechanism.
Anyone with one of these sensors already has a lawful copy of the firmware running on the
device they own; obtaining the image for interoperability work is the same act.

We are not going to publish a download link or a scraping script, because that would
amount to redistributing it by proxy. Getting hold of it is the one step you do yourself.

## A note on the legal footing

In the UK, **CDPA 1988 s50B** permits decompiling a program to obtain the information
needed to make an independently created program interoperate with it, and **s296A** voids
contract terms purporting to forbid that. The EU Software Directive (2009/24/EC, Arts. 5–6)
is equivalent. Making a Home Assistant integration work with hardware you own is close to
the textbook example.

That protects the *analysis* and the interoperable software — this repository. It does not
extend to redistributing the vendor's binary, which is exactly why you have to bring your
own. This is a description of why the repository is shaped this way, not legal advice.

## Verifying what you have

```bash
python3 - <<'EOF'
import struct, zlib, sys
d = open(sys.argv[1] if len(sys.argv) > 1 else "stock.bin", "rb").read()
print("size        :", len(d), "(expect 215860)")
print("magic       :", d[8:12], "(expect b'KNLT')")
print("marker      :", d[6:8].hex(" "), "(expect 5d 02)")
print("length field:", struct.unpack_from("<I", d, 0x18)[0], "(should equal size)")
crc = struct.unpack_from("<I", d, len(d) - 4)[0]
calc = (~zlib.crc32(d[:-4])) & 0xFFFFFFFF
print("crc         :", f"{crc:08x}", "vs computed", f"{calc:08x}",
      "-> OK" if crc == calc else "-> MISMATCH")
print("OTA version :", f"0x{struct.unpack_from('<I', d, 0x34788)[0]:08X}", "(stock = 0x10193001)")
EOF
```

If the CRC matches and the OTA version reads `0x10193001`, you have a good stock v1.1.9
image and `build.py` will accept it.
