"""Flash a firmware image to the eMotion Air over BLE (Telink OTA).

This is the recovery path: it has NO version check, so it will happily write a
stock image back over a patched one. Put the sensor in BLE pairing mode first
(hold the button ~10 seconds).

Transport:
  - write to the OTA characteristic 00010203-...-2b12
  - START = 0xFF01  (note: 0xFF00 is ABORT)
  - data packet = [u16 index][16 bytes data][u16 crc16]  (20 bytes), index 0,1,2,...
                  crc16 over the first 18 bytes (poly 0xA001, init 0xFFFF)
  - END   = [0xFF02][u16 last_index][u16 ~last_index]
  - status arrives as a notify on 2b10, or the device simply reboots.

~13,500 writes ≈ 4 minutes for a 216 KB image.

Dual-bank: a bad image lands in the INACTIVE bank and is never booted, so this is
safe to experiment with. This script also REFUSES to write without an explicit
--go flag, printing a dry run (packet count, first/last packets) otherwise.

Windows only (native WinRT — bleak loses the race with this sleepy device).

Usage:
    python ble_flash.py <image.bin> --addr <BLE_MAC> [--go] [--delay MS]
"""
import asyncio
import struct
import sys
import time
import uuid

from winrt.windows.devices.bluetooth import (
    BluetoothLEDevice, BluetoothConnectionStatus, BluetoothCacheMode,
)
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattSession, GattCommunicationStatus, GattWriteOption,
    GattClientCharacteristicConfigurationDescriptorValue as CCCD,
)
from winrt.windows.storage.streams import DataWriter, DataReader

ADDR = None      # set from --addr in main()
OTA_UUID = uuid.UUID("00010203-0405-0607-0809-0a0b0c0d2b12")
RSP_UUID = uuid.UUID("00010203-0405-0607-0809-0a0b0c0d2b10")

CMD_OTA_START = 0xFF01   # 0xFF00 is ABORT; START is 0xFF01 (per OTA RE)
CMD_OTA_END = 0xFF02


def crc16_telink(data: bytes) -> int:
    """Telink CRC16: reflected poly 0xA001 (0x8005), init 0xFFFF, no final xor."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF


def build_packets(image: bytes):
    """Return the ordered list of BLE writes for the whole OTA session."""
    pkts = [struct.pack("<H", CMD_OTA_START)]
    n = (len(image) + 15) // 16
    for i in range(n):
        chunk = image[i * 16:(i + 1) * 16]
        if len(chunk) < 16:
            chunk = chunk + b"\xff" * (16 - len(chunk))
        body = struct.pack("<H", i) + chunk           # 18 bytes
        pkts.append(body + struct.pack("<H", crc16_telink(body)))  # 20
    last = n - 1
    pkts.append(struct.pack("<H", CMD_OTA_END)
                + struct.pack("<H", last)
                + struct.pack("<H", (~last) & 0xFFFF))
    return pkts, n


# ---- WinRT helpers (same proven approach as winrt_talk.py) ----
def to_ibuf(data: bytes):
    w = DataWriter()
    w.write_bytes(bytes(data))
    return w.detach_buffer()


def from_ibuf(ibuf):
    if ibuf is None or ibuf.length == 0:
        return b""
    r = DataReader.from_buffer(ibuf)
    out = bytearray(ibuf.length)
    r.read_bytes(out)
    return bytes(out)


async def _call(obj, names, *args):
    last = None
    for name in names:
        fn = getattr(obj, name, None)
        if fn is None:
            continue
        for a in (args, ()):
            try:
                return await fn(*a)
            except TypeError:
                continue
            except Exception as e:
                if "Invalid parameter count" in str(e):
                    continue
                last = e
    if last:
        raise last
    raise RuntimeError(f"no working method among {names}")


async def find_chars(dev):
    svc_res = await _call(dev, ["get_gatt_services_with_cache_mode_async",
                                "get_gatt_services_async"], BluetoothCacheMode.UNCACHED)
    ota = rsp = None
    for svc in svc_res.services:
        ch_res = await _call(svc, ["get_characteristics_with_cache_mode_async",
                                   "get_characteristics_async"], BluetoothCacheMode.UNCACHED)
        if ch_res.status != GattCommunicationStatus.SUCCESS:
            continue
        for ch in ch_res.characteristics:
            if ch.uuid == OTA_UUID:
                ota = ch
            elif ch.uuid == RSP_UUID:
                rsp = ch
    return ota, rsp


async def write_pkt(ch, data, response=False):
    opt = GattWriteOption.WRITE_WITH_RESPONSE if response else GattWriteOption.WRITE_WITHOUT_RESPONSE
    await _call(ch, ["write_value_with_result_async", "write_value_with_option_async",
                     "write_value_async"], to_ibuf(data), opt)


async def main():
    global ADDR
    if len(sys.argv) < 2 or "--addr" not in sys.argv:
        print(__doc__)
        return 2
    path = sys.argv[1]
    ADDR = int(sys.argv[sys.argv.index("--addr") + 1]
               .replace(":", "").replace("-", ""), 16)
    go = "--go" in sys.argv
    response = "--response" in sys.argv
    delay = 0.0
    if "--delay" in sys.argv:
        delay = int(sys.argv[sys.argv.index("--delay") + 1]) / 1000.0

    image = open(path, "rb").read()
    pkts, n = build_packets(image)
    print(f"image   : {path}  {len(image)} bytes")
    print(f"packets : {n} data packets + START + END = {len(pkts)} writes")
    print(f"START   : {pkts[0].hex(' ')}")
    print(f"pkt[0]  : {pkts[1].hex(' ')}")
    print(f"pkt[1]  : {pkts[2].hex(' ')}")
    print(f"END     : {pkts[-1].hex(' ')}")
    if not go:
        print("\nDRY RUN (no BLE writes). Re-run with --go to actually push.")
        print("NOTE: confirm packet format against the OTA RE findings before --go.")
        return 0

    print(f"\nconnecting to {ADDR:012X} ...")
    dev = await BluetoothLEDevice.from_bluetooth_address_async(ADDR)
    t0 = time.time()
    while dev is None and time.time() - t0 < 60:
        await asyncio.sleep(2)
        dev = await BluetoothLEDevice.from_bluetooth_address_async(ADDR)
    if dev is None:
        print("could not resolve device")
        return 2
    session = await GattSession.from_device_id_async(dev.bluetooth_device_id)
    session.maintain_connection = True

    ota = rsp = None
    t0 = time.time()
    while time.time() - t0 < 60:
        try:
            ota, rsp = await find_chars(dev)
            if ota:
                break
        except Exception:
            pass
        await asyncio.sleep(2)
    if not ota:
        print("OTA characteristic not found (device asleep? power-cycle it)")
        return 2
    print("connected; OTA characteristic found.")

    if rsp:
        def on_notify(_c, data):
            print(f"  <NOTIFY> {from_ibuf(data.characteristic_value if hasattr(data,'characteristic_value') else data).hex(' ')}")
        try:
            await _call(rsp, ["write_client_characteristic_configuration_descriptor_async"], CCCD.NOTIFY)
            rsp.add_value_changed(lambda s, a: print(f"  <NOTIFY> {from_ibuf(a.characteristic_value).hex(' ')}"))
        except Exception as e:
            print(f"  (notify subscribe failed: {e})")

    print(f"pushing {len(pkts)} writes (response={response}, delay={delay*1000:.0f}ms)...")
    for i, p in enumerate(pkts):
        await write_pkt(ota, p, response=response)
        if delay:
            await asyncio.sleep(delay)
        if i % 512 == 0:
            print(f"  {i}/{len(pkts)}")
    print("all packets sent; waiting 8s for result / reboot...")
    await asyncio.sleep(8)
    print(f"connection_status: {dev.connection_status}")
    session.maintain_connection = False
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
