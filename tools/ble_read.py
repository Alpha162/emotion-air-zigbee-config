"""Read-only ground truth over BLE: opcode 0x81 -> radar/config params, decoded.

Reply layout (confirmed live 2026-08-28):
  02 06 <sw0> <sw1> <sw2> <cfg15> <timeout_lo> <timeout_hi>
    sw      = radar firmware, e.g. 22 31 18 -> "22.31.18"
    cfg15   = config byte +0x15: bits0-2 freq, bits3-6 sensitivity, bit7 awake
    timeout = u16 presence timeout (seconds), config +0x16   e.g. b4 00 -> 180

Windows only, using native WinRT, because bleak loses the race with this device.
Put the sensor in BLE pairing mode first: hold the button ~10 seconds.

Usage: python ble_read.py <BLE_MAC>      e.g. python ble_read.py A1:B2:C3:D4:E5:F6
"""
import asyncio, sys, time, uuid
from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothCacheMode
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattSession, GattCommunicationStatus, GattWriteOption,
    GattClientCharacteristicConfigurationDescriptorValue as CCCD,
)
from winrt.windows.storage.streams import DataWriter, DataReader

if len(sys.argv) < 2:
    sys.exit(__doc__)
ADDR = int(sys.argv[1].replace(":", "").replace("-", ""), 16)
DEADLINE = time.time() + 240
CMD_UUID = uuid.UUID("00010203-0405-0607-0809-0a0b0c0d2b11")
RSP_UUID = uuid.UUID("00010203-0405-0607-0809-0a0b0c0d2b10")
seen = []


def decode_81(p):
    if len(p) < 8 or p[0] != 0x02:
        return None
    sw = f"{p[2]:02x}.{p[3]:02x}.{p[4]:02x}"   # BCD-ish: 0x22 0x31 0x18 -> "22.31.18"
    c = p[5]
    return {
        "radar_sw": sw,
        "raw_cfg15": f"0x{c:02x}",
        "frequency": c & 0x07,
        "sensitivity": (c >> 3) & 0x0F,
        "awake": (c >> 7) & 1,
        "timeout_s": p[6] | (p[7] << 8),
    }


def ib(b):
    if b is None or b.length == 0:
        return b""
    r = DataReader.from_buffer(b)
    o = bytearray(b.length)
    r.read_bytes(o)
    return bytes(o)


def wb(d):
    w = DataWriter()
    w.write_bytes(bytes(d))
    return w.detach_buffer()


def on_notify(s, a):
    d = ib(a.characteristic_value)
    seen.append(d)
    print(f"  <NOTIFY> {d.hex(' ')}", flush=True)
    dec = decode_81(d)
    if dec:
        print("  *** DECODED ***")
        for k, v in dec.items():
            print(f"      {k:12}= {v}")


async def _call(o, names, *args):
    last = None
    for n in names:
        f = getattr(o, n, None)
        if f is None:
            continue
        for a in (args, ()):
            try:
                return await f(*a)
            except TypeError:
                continue
            except Exception as e:
                if "Invalid parameter count" in str(e):
                    continue
                last = e
    if last:
        raise last
    raise RuntimeError("no method")


async def find(dev):
    sr = await _call(dev, ["get_gatt_services_with_cache_mode_async",
                           "get_gatt_services_async"], BluetoothCacheMode.UNCACHED)
    if sr.status != GattCommunicationStatus.SUCCESS:
        return None, None
    c = r = None
    for s in sr.services:
        cr = await _call(s, ["get_characteristics_with_cache_mode_async",
                             "get_characteristics_async"], BluetoothCacheMode.UNCACHED)
        if cr.status != GattCommunicationStatus.SUCCESS:
            continue
        for ch in cr.characteristics:
            if ch.uuid == CMD_UUID:
                c = ch
            elif ch.uuid == RSP_UUID:
                r = ch
    return c, r


async def main():
    print(f"reading config from {ADDR:012X} (10s hold -> BLE pair if it won't attach)")
    dev = await BluetoothLEDevice.from_bluetooth_address_async(ADDR)
    while dev is None and time.time() < DEADLINE:
        await asyncio.sleep(2)
        dev = await BluetoothLEDevice.from_bluetooth_address_async(ADDR)
    if dev is None:
        print("could not resolve"); return 2
    sess = await GattSession.from_device_id_async(dev.bluetooth_device_id)
    sess.maintain_connection = True
    cmd = rsp = None
    n = 0
    while time.time() < DEADLINE:
        n += 1
        try:
            c, r = await find(dev)
            if c and r:
                cmd, rsp = c, r
                break
        except Exception:
            pass
        if n % 5 == 0:
            print(f"  connecting... {int(DEADLINE-time.time())}s left", flush=True)
        await asyncio.sleep(2)
    if not cmd:
        print("no GATT before deadline"); return 2
    print("connected.", flush=True)
    await _call(rsp, ["write_client_characteristic_configuration_descriptor_async"], CCCD.NOTIFY)
    rsp.add_value_changed(on_notify)
    for attempt in range(3):          # first query after connect often drops its notify
        print(f"\n--> 0x81 read radar/config params (attempt {attempt+1})", flush=True)
        await _call(cmd, ["write_value_with_result_async", "write_value_with_option_async",
                          "write_value_async"],
                    wb(bytes([0x81, 0x00])), GattWriteOption.WRITE_WITHOUT_RESPONSE)
        await asyncio.sleep(3)
        if any(d and d[0] == 0x02 for d in seen):
            break
    sess.maintain_connection = False
    return 0


sys.exit(asyncio.run(main()))
