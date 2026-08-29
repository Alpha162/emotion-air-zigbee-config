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
| Sensitivity (High/Medium/Low) | ✅ verified on hardware |
| Presence timeout (seconds) | ✅ verified on hardware |
| Reading real current values | ✅ verified on hardware |
| Light / temp-humidity sample intervals | ✅ verified against the vendor app |
| Lux thresholds (bright / normal) | ✅ verified against the vendor app |
| Radar frequency | ⚠️ implemented, untested |
| Presence monitoring switch | ⚠️ implemented, untested |
| Radar action buttons | ✅ verified on hardware |

The ⚠️ items are correct by inspection of the stock firmware's own command handlers,
but no write has been driven through them end-to-end. Reports welcome.

## What the settings actually do

Home Assistant has no tooltips for these entities, and it truncates long entity names in
the device panel — so the names are deliberately short and the explanations live here and
in the quirk's docstring. Rename anything you like in the HA UI.

| Control | Device command | Notes |
|---|---|---|
| Sensitivity | `AT+TRITH=n` | **High / Medium / Low** select. The raw value runs backwards (see below), which the select hides |
| Presence timeout | `AT+HOLD=<secs/2>` | How long presence stays "detected" after motion stops. The firmware halves it, so the step is 2 |
| Light interval | — | Lower = fresher readings, more battery. Firmware clamps below 2 s |
| Climate interval | — | Same trade-off; clamped below 10 s |
| Light normal / Light bright | one `AT` command | The lux boundaries the device reports against. **Both are set by a single command**, so the quirk composes the pair. Bright 11–60000 lx, normal below it. (The vendor app's bright slider runs to 60000 lx with no text entry, which makes precise values nearly impossible — the ranges here are the same but typeable.) |
| Presence monitoring (switch) | on `AT+RESET` / off `AT+SLEEP` | Powers the radar down entirely. No single toggle exists, hence two opcodes |
| Radar frequency | `AT+FREQ=<0..4>` | **Meaning unknown** — disabled by default. Probably an RF channel; see below |
| Restore calibration | `AT+INITTH` | The app's "Restore". **Measured to change nothing** — see below. Do not rely on it to undo a scan |
| Learn room (room empty) | `AT+CALI` | **The environment-calibration scan.** Rewrites the per-gate thresholds from what it can see — **run with the room empty and still** |
| Restart radar | `AT+RESET` | Harmless; also re-enables presence monitoring |

> ### ⚠️ Sensitivity runs backwards — so this is called a *threshold*
> The device command is `AT+TRITH`, a trigger **threshold**, while the vendor app labels
> it "Sensitivity". They run in opposite directions.
>
> **Confirmed against the app (2026-08-29):** selecting **"Low"** writes **7**, and the
> firmware default of **3** displays as **"High"**. So:
>
> | App | Value |
> |---|---|
> | High | **3** |
> | Medium | ~5 *(interpolated)* |
> | Low | **7** |
>
> **Lower = more sensitive.** Rather than expose that trap, the quirk offers a
> **Sensitivity** select with High / Medium / Low, matching the vendor app. The raw 1–10
> value is still available as **Detection threshold (raw)** under Diagnostic, disabled by
> default, for fine control.
>
> The raw scale accepts 1–10, so a device can hold a value the app cannot express (ours
> sat at 6). The select snaps to the nearest band for display; the raw entity shows the
> exact number.

> ### ℹ️ Radar frequency, and phantom presence with multiple sensors
> `AT+FREQ` takes 0–4 and its meaning is not documented anywhere we could find. In the
> radar's command table it sits with `GAIN` and `HW` — the RF/hardware group — rather than
> with the detection thresholds, so the likeliest reading is a **frequency point within the
> 24.00–24.25 GHz ISM band**, i.e. a channel so that nearby radars do not interfere.
>
> If you run several of these within range of each other and see **phantom presence** — one
> tripping while its room is empty, or presence that will not clear — that is the classic
> symptom, and putting them on different values is worth trying. Note the entity shows the
> value the sensor's MCU stored, not one read back from the radar, so judge it by behaviour.
>
> Full reasoning, and the radar's complete AT command set, in
> [docs/reverse-engineering.md](docs/reverse-engineering.md).

The three one-shot actions live in their own **Diagnostic** section, separate from the
settings, and along with the frequency control they are **disabled by default**.

> ### What the calibration buttons actually do (measured 2026-08-29)
> The radar reports its per-range-gate thresholds in the status block it emits on reset,
> which makes these testable. Pressing each and comparing dumps:
>
> - **`AT+CALI`** rewrote **19 thresholds** — the factory ramp (`R1TH=8,7,6,4,4…`) became an
>   environment-shaped profile (`10,10,6,6…`). This is the real calibration scan, and the
>   vendor app's "Start detection".
> - **`AT+INITTH`** is acknowledged (`AT+OK`) and restarts the radar, but left every
>   threshold **unchanged** across two restarts.
>
> Pressing **Restore** in the vendor app produced an identical UART signature and identical
> (non-)results, which identifies `AT+INITTH` as that button — **and shows the vendor's own
> restore does not work.** It is described as clearing the per-area calibration and
> restoring defaults; measurably, it does neither.
>
> **Consequence: there is no working undo for a calibration scan** short of a factory
> reset. If you run one, run it with the sensor mounted where it will live and the room
> empty — you cannot simply put it back.
>
> Because `AT+CALI` characterises whatever the radar can see at that instant, run it with
> the room empty and still, or you will teach it that you are background.

> **Upgrading?** HA keys each entity's enabled state to its `unique_id` and does not purge
> those registry rows when you delete a ZHA device — so entities you already had return
> with their previous state, and "disabled by default" only takes effect on a genuinely
> fresh install. On an existing one, just disable the buttons by hand once.

None of them is destructive and none can brick anything, but the two calibration actions
characterise whatever the radar can see at the moment you press them, so they should not
be one accidental click away. Enable the one you want in HA, use it, disable it again.

Genuinely destructive commands — factory reset, key regeneration, encryption — are
refused by the patched firmware itself and cannot be reached through the quirk at all.

## ⚠️ Known issue: writes work, but Home Assistant reports them as failed

**Symptom.** You change a setting, HA shows an error, and the slider snaps back to its old
value. It looks like the write was rejected.

**What actually happens.** The write succeeds. The shim fires, the command reaches the
radar, and the value is written and persisted to flash — verifiable on the radar's UART
and by reading the value back. What fails is only the *reply*: the device's ZCL layer
answers `UNSUPPORTED_ATTRIBUTE`, zigpy raises, and HA reverts the displayed value.

**Handled by the quirk.** The quirk recognises this specific reply on the specific
attributes the firmware handles out-of-band, treats it as success, and updates the entity
to the written value. Only `UNSUPPORTED_ATTRIBUTE` on those attributes is forgiven — any
other failure is passed through as a real error.

If you are running an older quirk, the workaround is to refresh the entity (Developer
Tools → `homeassistant.update_entity`) to see the real value.

**Cause.** The read path (firmware v1.9+) registers an attribute table on the occupancy
cluster, which makes the device's ZCL layer validate *writes* against that table too. The
attributes we write to are not in it, so it refuses them — after our shim has already
acted on them. Firmware v1.8, which had no such table, returned success.

**A firmware-side fix is possible but not implemented.** Registering the written
attributes as writable would make the device answer SUCCESS properly. For most of them the data pointer can point straight at the
matching config offset and the direct write is harmless — `0x0010`→`+0x16`,
`0xF013`→`+0x18`, `0xF014`→`+0x1A`, `0xF015`→`+0x1C` (one u32 covering both lux
thresholds, which are contiguous). **Sensitivity is the exception:** it occupies bits 3-6
of the packed byte at `+0x15`, so a raw ZCL write there would clobber the frequency and
awake bits — it needs a scratch RAM address instead, and a safe one has not been
identified yet (`+0x00..0x0F` appears to hold key material; the OTA header buffer at
`0x844548` is live during transfers; and the SRAM map shows static data running to roughly
`0x848000` with what look like heap/stack bounds above it, leaving no region that can be
claimed with confidence). Since the quirk-side fix is complete and carries no reflashing
risk, this is filed as "possible" rather than "pending".

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
