# Reverse-engineering notes — eMotion Air (Telink TLSR8258)

Everything here is for **stock firmware v1.1.9** (215,860 bytes). The image loads at
`0x0`, so **every file offset equals its runtime address** — which makes patching easy.

## The device

- **SoC:** Telink **TLSR8258** (module marked LL8253-P). Custom **TC32** core — thumb-like,
  little-endian, its own instruction encoding. Telink Zigbee SDK v2.4.1.0.
- **Radar:** an **RC2410 24 GHz** presence module on an *internal* UART at 115200 8N1,
  driven with `AT+…` commands. Its own firmware reports `SW=22.31.18`.
- **Dual-mode:** the device runs **Zigbee or BLE, never both**. Selected by config byte
  `+0x14` bit 0 (`0` = Zigbee, `1` = BLE).
- **Button:** hold ~10 s → BLE pairing; ~15 s → Zigbee pairing. These are independent of
  stored configuration, which is what makes every experiment reversible.

## Toolchain

**Compiler.** Native Linux `tc32-elf-gcc 4.5.1` from
[`flyskywhy/tc32`](https://github.com/flyskywhy/tc32), branch `linux`.

- flags: `-Os -std=gnu99` (gcc 4.5 defaults to C89), and **no `-mthumb`** — not a valid
  option for this target.
- disassemble a raw image: `tc32-elf-objdump -D -b binary -m tc32 --start-address=… --stop-address=…`
  (**`-m tc32` is required**, or it errors "architecture UNKNOWN").
- The **Windows** build of that toolchain does not work: `gcc`/`cc1`/binutils hard-import
  `libgcj-13.dll` from an ancient MinGW GCJ runtime that is not bundled. The repo's
  `posix/` tree is only wine wrappers around those same `.exe`s. Use WSL.

**Disassembler.** Ghidra with the community **Telink_TC32** processor module (2019 vintage;
still builds against current Ghidra).

> **Ghidra gotcha:** auto-analysis gap-fill misclassifies the AES S-box / Rcon tables as
> code and crashes the decompiler. Force a code/data boundary at `0x2C400`.

## Image format

| Field | Where | Meaning |
|---|---|---|
| format marker | `img[6:8]` | `5D 02` |
| boot flag | byte `@0x08` | `0x4B` (`'K'`) — held back until the OTA completes |
| magic | `@0x08` | `KNLT` |
| total length | u32 `@0x18` | must equal the file length |
| trailing CRC | last 4 bytes | **CRC-32/JAMCRC** = `(~zlib.crc32(image[:-4])) & 0xFFFFFFFF` |

The payload (everything but the CRC) must be a multiple of 16, i.e. `length & 0xF == 4`.

**Dual-bank ping-pong** at `0x0` / `0x40000`: a new image goes to the inactive bank, the
boot flag is written only after a CRC pass, then the old bank is invalidated. A bad image
never boots. Max image = `0x40000`.

## Config block

**Base `0x844210`** in SRAM, 32 bytes. Returned by the getter at `0x5BB4`.

| Offset | Type | Meaning | Stock default |
|---|---|---|---|
| `+0x14` | bits | b0 mode (0=Zigbee, 1=BLE), b1 encryption, b2 Zigbee→BLE flag | — |
| `+0x15` | packed u8 | **b0-2 frequency, b3-6 sensitivity, b7 awake** | — |
| `+0x16` | u16 | presence timeout, seconds | 180 |
| `+0x18` | u16 | light (lux) sample interval | 30 |
| `+0x1A` | u16 | temp/humidity sample interval | 60 |
| `+0x1C` | u16 | lux threshold low ("Normal") | 10 |
| `+0x1E` | u16 | lux threshold high ("Bright") | 100 |

Defaults are written by the init routine at `0x5C74`, which also **clamps** a lux interval
below 2 and a temp/humidity interval below 10 back to defaults — those are the real
minimums. The persist helper is at `0x5BD4`; NV lives in flash around `0x80000`/`0x81000`.

## Addresses of interest

| Address | What |
|---|---|
| `0x4B7C` | command dispatcher — **the only safe entry point** (see landmine 1) |
| `0x2CC88` | opcode jump table, `0x87` entries × 4 bytes |
| `0x88B8` | radar AT-command UART sender |
| `0xA310` | global ZCL message callback |
| **`0x942C`** | flash word holding `&globalZclCallback` — **the hook point** |
| `0x33566` | occupancy cluster registration (18-byte `zcl_specClusterInfo_t`) |
| `0x3356A` / `0x3356C` | its attribute count (u16) / table pointer (u32); stock `2` / `0x334FC` |
| `0x34788` / `0x34790` / `0x34792` | OTA file version u32 / image type u16 / manufacturer u16 |
| `0x2DC80` | DIS "Firmware Revision" string (5 chars) |
| `0x9F5C` / `0xA018` | OTA query delays: 12,000 ms after join, then 300,000 ms |

`zclAttrInfo_t` = `{u16 id; u8 type; u8 access; u32 data}` (8 bytes). Types: `0x18` map8,
`0x20` u8, `0x21` u16, `0x29` i16, `0x23` u32. Access bits: 1 read, 2 write, 4 report.

## Command protocol

GATT: `…2b11` command write · `…2b10` notify · `…2b12` OTA
(base UUID `00010203-0405-0607-0809-0a0b0c0d….`).

Frame is `[opcode][arg_len][args…]`.

| Opcode | Action | Args |
|---|---|---|
| `0x01` | radar restart — **sets** awake bit (`+0x15 |= 0x80`), `AT+RESET` | — |
| `0x02` | frequency (0–4) → `AT+FREQ=n` | u8 |
| `0x03` | sensitivity (1–10) → `AT+TRITH=n` | u8 |
| `0x04` | presence timeout → `AT+HOLD=<secs/2>` | u16 |
| `0x05` | calibrate → `AT+CALI` | — |
| `0x06` | radar sleep — **clears** awake bit (`&= 0x7F`), `AT+SLEEP` | — |
| `0x07` | calibration reset → `AT+INITTH` | — |
| `0x10` / `0x11` / `0x12` | encryption / key regen / **factory reset** — destructive | |
| `0x13` / `0x14` | lux / temp-humidity sample interval | u16 |
| `0x15` | lux thresholds → `+0x1C` and `+0x1E` | **`[low u16][high u16]`** |
| `0x80`/`0x81`/`0x84`/`0x85`/`0x86` | read sensor / radar params / key / intervals / thresholds | — |

**Opcode `0x81` reply** — the most useful read, since it exposes the packed config byte:

```
02 06 <sw0> <sw1> <sw2> <cfg15> <timeout_lo> <timeout_hi>
```

`sw` is the radar firmware as hex digits (`22 31 18` → "22.31.18"). Live example
`02 06 22 31 18 c8 2c 01` decodes to sensitivity 9, frequency 0, awake 1, timeout 300.

Each handler writes the config byte **and** sends the radar command **and** persists to
flash — which is precisely why routing a ZCL write into one produces a real, durable change
rather than a cosmetic one.

## Four landmines

Each of these produced a real failure that took hours to find. They are the most valuable
thing in this document.

### 1. The opcode handlers are not callable functions

They are **continuations of the dispatcher**. The dispatcher allocates a 56-byte frame and
*jumps* to the handler (`tmov pc, r3`); the handler builds its AT string inside that frame
at `sp+0x24`, then returns by executing the **dispatcher's** epilogue
(`add sp,#56; pop {r2,r3}; …; pop {r4,r5,r6,r7,pc}`).

Proof: the sensitivity handler at `0x4C60` contains `tjne 0x4BD8` — a branch into the
middle of the dispatcher's exit sequence.

Call one directly from your own code and it unwinds **your** stack by 56 bytes plus seven
registers and branches to whatever it pops as `pc` ⇒ crash ⇒ watchdog reboot. The symptom
is a device that reboots every 30–60 s and never answers, which looks nothing like "my
function is wrong".

**Always call the dispatcher** and let it set up the frame and its informal register
convention: `r6 = &[opcode, arg_len, args…]`, `r7 = arg_len`, `r8 = config base`.

### 2. Write records hold a pointer, not a value

```c
typedef struct { u16 attrID; u8 dataType; u8 *attrData; } zclWriteRec_t;  /* stride 7 */
```

Reached via `*(u8**)(pInMsg + 0x0C)` — a `u8` count, then records at `+1`. The last four
bytes are a **pointer to** the value. The tell: a fixed 7-byte record physically cannot
hold an inline string or array, so it must be indirect.

Comparing those pointer bytes against an expected value silently matches nothing.

### 3. Read the record fields byte-wise

Records sit at `list + 1 + i*7`, so `attrID` lands on an **odd address**. Unaligned 16/32-bit
loads on this core return **rotated garbage rather than faulting**, so the bug is silent.
Assemble every field from bytes.

### 4. Ghidra misrenders a negate trick

The radar-restart handler really does:

```
mov r3, #128      ; 0x80
neg r3, r3        ; 0xFFFFFF80
or  r3, cfg15
strb r3, [r1, #21]    ; stores the low byte -> cfg15 | 0x80
```

Ghidra decompiles this as `| 0x7f`. Trusting it would have inverted the meaning of the
awake bit. **Disassemble when the semantics matter.**

## The radar module (RC2410)

The Telink drives the radar over an internal UART at **115200 8N1** using `AT+…` commands.
No public datasheet or AT reference for this module could be found, so the following is
reconstructed from the firmware's string table — it may be useful to anyone working with
the same radar in another product.

### Full AT vocabulary

58 distinct `AT+` strings appear in the image, in **two separate clusters**:

- **`0x2CFC4`–`0x2E214`** — the subset the Telink actually *sends*, sitting near the
  command handlers that use them.
- **`0x30E68`–`0x3125C`** — the complete vocabulary, apparently a reference/parser table.
  The firmware knows far more commands than it ever issues.

Grouped by apparent function:

| Group | Commands |
|---|---|
| Link / IO | `AT+BAUD` `AT+GPIO` `AT+TAGOUT` `AT+OUTTRI` `AT+LED` |
| Session / status | `AT+START` `AT+RESET` `AT+SLEEP` `AT+OK` `AT+ERR` `AT+ATERR` |
| Detection timing | `AT+HOLD` `AT+FTIME` `AT+STIME` |
| Detection thresholds | `AT+TRITH` `AT+ONTH` `AT+R1`…`AT+R10` `AT+R1TH`…`AT+R10TH` `AT+MR1TH`…`AT+MR10TH` |
| Signal processing | `AT+CFAR` `AT+NMF` `AT+SIGN` |
| RF / hardware | `AT+GAIN` **`AT+FREQ`** `AT+HW` |
| Calibration | `AT+CALI` `AT+INIT` `AT+INITTH` |
| Debug / factory | `AT+LAB` `AT+DEBUG` `AT+OLDIDX` |

Only a handful are ever issued by the stock firmware: `RESET`, `TRITH`, `HOLD`, `CALI`,
`SLEEP`, `INITTH`, `FREQ`, `INIT`, and the `TAGOUT`/`FTIME`/`STIME` trio at bring-up.
Everything else is available to the radar but unused — a large unexplored surface if you
wanted to expose more settings than the vendor does.

### What the radar reports back

On reset it emits a status block, which the Telink parses (the field-name strings are in
the image too):

```
ROM12_OK / SW=22.31.18 / BaudRate=115200 / GPIO=0 / TAGOUT=0 / HoldFrame=90
FastTime=2000 / SlowTime=500 / TRITH=3 / HOLDONTH=1
Range1..Range10 = 0.7m .. 7.0m   (0.7 m per range gate)
MR1TH..MR10TH, R1TH..R10TH, CFAR=15, NMF=3
```

Ten range gates at 0.7 m each, with separate per-gate thresholds (`R*TH`) and what look
like moving-target thresholds (`MR*TH`). The stock firmware's single "sensitivity" control
only drives `AT+TRITH`; the per-gate thresholds are never touched, so **per-distance
sensitivity is theoretically reachable but not implemented** by either the vendor or this
project.

Note what is **absent** from that dump: `FREQ`, `GAIN` and `HW` are settable but never
reported.

### What `AT+FREQ` probably does

Honestly: **unknown.** The evidence:

- The firmware accepts `0..4`, stores it in config `+0x15` bits 0-2, sends `AT+FREQ=<digit>`,
  and defaults to `0`.
- It sits with `GAIN` and `HW` in the command table — the RF/hardware group — rather than
  with the detection thresholds.
- The radar never reports it back in its status dump.
- The vendor app does not expose it at all.

The most plausible reading is a **frequency point within the 24.00–24.25 GHz ISM band**,
five selectable channels, so multiple radars in proximity do not interfere with each other
— a common feature on 24 GHz modules. That is inference from naming and grouping, not
something confirmed against documentation or measurement.

**Where it might matter:** if you run several of these sensors within range of one another
and see phantom presence — one tripping while its room is empty, or presence that will not
clear — mutual interference is a classic cause and separating them across frequency values
is exactly the remedy such a setting exists for.

**Caveat if you experiment:** the read path reports the *Telink's* stored copy of the
value, not the radar's, because the radar never reports it. The entity will show whatever
you set even if the radar ignored it. Judge it by behaviour, not by the number.

## Working out the app's settings

Mapping the vendor app's Configuration screen onto the firmware:

| App control | Mechanism |
|---|---|
| Sensitivity | opcode `0x03` (the app presents Low/Med/High over the 1–10 range) |
| Default no-motion duration | opcode `0x04` |
| Light collection interval | opcode `0x13` |
| Temperature and humidity collection interval | opcode `0x14` |
| Bright / Normal Lux | opcode `0x15` (both in one command) |
| **Presence monitoring** | no single toggle: **on** = opcode `0x01`, **off** = opcode `0x06`, i.e. the awake bit |
| **Environment self-learning** | opcode `0x07` → `AT+INITTH`, a one-shot threshold relearn |

One unexplained case remains: sensitivity value `10` branches away at `0x5046` instead of
sending `AT+TRITH`, possibly an auto/self-learning mode.

## Dead ends

- **Hardware (SWS) flashing is not viable on this module.** No reset pad is exposed on the
  LL8253-P, back-powering does not help, and Telink's `floader` expects UART on PA0/PB1
  rather than the accessible test point. OTA is strictly better and needs no soldering.
- **`bleak` on Windows** cannot reliably reach this sleepy device; use native WinRT with
  `GattSession.maintain_connection` and drive an UNCACHED service discovery in a loop.
- **Never trust a silent instrument.** A serial monitor died for 15 minutes and a quiet
  line is indistinguishable from a dead one, so a genuine success was recorded as a
  failure and sent the investigation down a blind alley. Emit a liveness heartbeat.
- **A client-side timeout is not a failed write.** Test tooling that gives up after a few
  seconds reports "never delivered" while ZHA still has the write queued; it lands minutes
  later. Confirm from device state, not from your own return value.
