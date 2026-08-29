# eMotion Air — Zigbee configuration firmware

Patched firmware and a ZHA quirk that let you configure a **LinknLink eMotion Air**
presence sensor from Home Assistant over Zigbee. Stock firmware exposes those settings
over **BLE only** — via the vendor app — so they are unreachable from ZHA.

With this you get proper entities for radar sensitivity, presence timeout, sample
intervals, lux thresholds and more, reading the device's **real current values** and
writing changes that are applied to the radar and persisted to flash.

> Not affiliated with or endorsed by LinknLink. "LinknLink" and "eMotion Air" are their
> trademarks, used here only to say what this is compatible with.

---

## Status

Developed and verified against **stock firmware v1.1.9** on the eMotion Air, with a
ConBee II coordinator and Home Assistant 2026.8.

| Control | State |
|---|---|
| Radar sensitivity | ✅ verified on hardware |
| Presence timeout (seconds) | ✅ verified on hardware |
| Reading real current values | ✅ verified on hardware |
| Light / temp-humidity sample intervals | ⚠️ implemented, lightly tested |
| Lux thresholds (bright / normal) | ⚠️ implemented, untested |
| Radar frequency | ⚠️ implemented, untested |
| Presence monitoring switch | ⚠️ implemented, untested |
| Restart / calibrate / relearn buttons | ⚠️ implemented, untested |

The ⚠️ items are correct by inspection of the stock firmware's own command handlers,
but no write has been driven through them end-to-end. Reports welcome.

## What the settings actually do

Home Assistant has no tooltips for these entities, and it truncates long entity names in
the device panel — so the names are deliberately short and the explanations live here and
in the quirk's docstring. Rename anything you like in the HA UI.

| Control | Device command | Notes |
|---|---|---|
| Radar sensitivity | `AT+TRITH=n` | **Direction unconfirmed — see below** |
| Presence timeout | `AT+HOLD=<secs/2>` | How long presence stays "detected" after motion stops. The firmware halves it, so the step is 2 |
| Light interval | — | Lower = fresher readings, more battery. Firmware clamps below 2 s |
| Climate interval | — | Same trade-off; clamped below 10 s |
| Normal threshold / Bright threshold | `AT` + one command | The lux boundaries the device reports against. **Both are set by a single command**, so the quirk composes the pair |
| Presence monitoring (switch) | on `AT+RESET` / off `AT+SLEEP` | Powers the radar down entirely. No single toggle exists, hence two opcodes |
| Radar frequency | `AT+FREQ=<0..4>` | **Meaning unknown** — disabled by default |
| Relearn empty room | `AT+INITTH` | Re-learns detection thresholds — **run with the room empty** |
| Calibrate radar (room empty) | `AT+CALI` | Vendor calibration — same caveat |
| Restart radar | `AT+RESET` | Harmless; also re-enables presence monitoring |

> ### ⚠️ "Sensitivity" probably runs backwards
> The underlying command is `AT+TRITH` — a trigger **threshold** — while the vendor app
> labels it "Sensitivity". Those run in opposite directions. A unit at the firmware
> default of `3` is shown by the app as sensitivity **"High"**, which implies
> **lower value = more sensitive**.
>
> That is one observation, not a measurement. Try `1` versus `10` in your own room before
> trusting it, and please open an issue with what you find.

The three one-shot action buttons and the frequency control are **disabled by default**.
None of them is destructive and none can brick anything, but the two calibration actions
characterise whatever the radar can see at the moment you press them, so they should not
be one accidental click away. Enable the one you want in HA, use it, disable it again.

Genuinely destructive commands — factory reset, key regeneration, encryption — are
refused by the patched firmware itself and cannot be reached through the quirk at all.

## Is this risky?

Less than you would expect, for two reasons:

1. **The device has dual-bank flash.** A new image is written to the *inactive* bank and
   the boot flag is only flipped after the device verifies a CRC over the whole image.
   A corrupt or rejected image simply never runs — the old one stays live.
2. **The button is an unconditional escape hatch.** Holding it ~10 s enters BLE pairing
   regardless of firmware or configuration, and the BLE flash path has **no version
   check**, so you can always write a stock image back.

It will certainly void your warranty, and nobody but you is responsible for what you
flash. But "bricked beyond recovery" is genuinely hard to achieve here.

---

## What you need

- An eMotion Air on **stock v1.1.9** (other versions: the offsets will differ — see below)
- **Your own copy of the stock firmware image** — this repo ships none, see [FIRMWARE.md](FIRMWARE.md)
- The **TC32 toolchain** to compile the shim:
  ```
  git clone --depth 1 -b linux https://github.com/flyskywhy/tc32.git /opt/tc32
  export PATH=/opt/tc32/bin:$PATH
  ```
  On Windows, run the build inside WSL — the Windows build of that toolchain is broken
  (it hard-links against an ancient `libgcj-13.dll` that is not shipped).
- Python 3.9+ (`pip install -r tools/requirements.txt`; the BLE tools are Windows-only)

## Build

```bash
python3 firmware/build.py --base /path/to/your_stock_1.1.9.bin --out emotion_air_patched
python3 tools/verify_ota.py emotion_air_patched.ota 0x10193001
```

That produces `emotion_air_patched.bin` (flash over BLE) and `emotion_air_patched.ota`
(serve over Zigbee). `verify_ota.py` re-checks the result against every gate the device
enforces, using independent code from the builder.

## Install — over Zigbee (recommended, no physical access)

1. Drop the `.ota` into a directory on your HA host, e.g. `/config/zigpy_ota/`.
2. Add the local OTA provider to `configuration.yaml`:

   ```yaml
   zha:
     custom_quirks_path: /config/zha_quirks/
     zigpy_config:
       ota:
         extra_providers:
           - type: advanced
             warning: "I understand I can *destroy* my devices by enabling OTA updates from files. Some OTA updates can be mistakenly applied to the wrong device, breaking it. I am consciously using this at my own risk."
             path: /config/zigpy_ota/
   ```

   > ⚠️ **That `warning` string must be exact.** If it is wrong by a single character,
   > zigpy **silently discards the provider** — ZHA still loads, config validation
   > passes, and nothing appears in any log. This is the single most likely reason
   > "nothing happens". Validate it first:
   > ```bash
   > python3 -c "from zigpy.config import SCHEMA_OTA; SCHEMA_OTA({'extra_providers':[{'type':'advanced','warning':open('warning.txt').read().strip(),'path':'/config/zigpy_ota/'}]}); print('ok')"
   > ```

3. Copy `zha-quirk/emotion_air.py` to `/config/zha_quirks/`, then restart Home Assistant.
4. Trigger the update from the device's firmware entity.

**Expect it to be slow and to need nudging.** This is a battery-powered sleepy device:

- `update.install` often fails first with `NWK_ROUTE_DISCOVERY_FAILED` (asleep) or
  `TimeoutError` (woke, but did not start pulling blocks). **Retry it while walking in
  front of the sensor** — motion keeps it in fast-poll. Once blocks flow it self-sustains.
- A full transfer takes roughly **30–40 minutes**.
- Don't pair other Zigbee devices during the transfer; a join killed one of ours at 99%.

## Install — over BLE (fallback / recovery)

```bash
python3 tools/ble_flash.py emotion_air_patched.bin --addr AA:BB:CC:DD:EE:FF        # dry run
python3 tools/ble_flash.py emotion_air_patched.bin --addr AA:BB:CC:DD:EE:FF --go
```

Hold the button ~10 s first to enter BLE pairing. Windows only (native WinRT — `bleak`
loses the connection race with this device). ~4 minutes.

## Rolling back

Either path works:

```bash
# over BLE — unconditional, no version check
python3 tools/ble_flash.py your_stock_1.1.9.bin --addr AA:BB:CC:DD:EE:FF --go

# over Zigbee — stamp the stock image with a HIGHER version so ZHA offers it
python3 tools/make_ota.py your_stock_1.1.9.bin stock_restore.ota 0x101D3001
```

The firmware accepts any version that *differs* from the running one, so a downgrade can
be delivered as an "upgrade" without touching the hardware.

---

## How it works

Two independent mechanisms, both patched into the stock image:

**Writes** — a small shim (≈340 bytes of C) hooks the firmware's global ZCL message
callback. When a Write-Attributes lands on the Occupancy cluster it maps the attribute to
the device's *own* native command and runs it through the stock dispatcher, which writes
the config block, sends the radar an `AT+…` command over the internal UART, and persists
to flash. It then chains to the original callback, so stock behaviour is unchanged.

**Reads** — the occupancy cluster's attribute table is replaced with a larger one whose
extra entries are **read-only pointers into the live config block**, so ZHA reads genuine
current values. They are deliberately read-only: a writable pointer would change RAM
without sending the radar command or persisting, producing settings that look applied and
silently are not.

Full detail, including every offset and the traps that cost us hours:
[docs/reverse-engineering.md](docs/reverse-engineering.md) ·
[docs/ota-format.md](docs/ota-format.md)

## Other firmware versions

Everything is pinned to stock **v1.1.9** (215,860 bytes). `build.py` refuses to patch
anything whose hook pointer and attribute table are not where it expects, rather than
producing a broken image. To port to another build you need to relocate the handful of
addresses in `docs/reverse-engineering.md` — the technique is unchanged.

## Licence

MIT for everything here — see [LICENSE](LICENSE). This covers **our** code only. The
firmware image you supply remains LinknLink's, is not included in this repository, and
must not be redistributed.
