/* ============================================================================
 * eMotion Air — Zigbee/ZHA configuration shim   (Telink TLSR8258, TC32 core)
 * SPDX-License-Identifier: MIT
 * ----------------------------------------------------------------------------
 * Stock firmware exposes the radar/sensor settings over BLE only. This shim wraps
 * the firmware's GLOBAL ZCL message callback (pointer in flash @0x942C). On a ZCL
 * Write-Attributes to the Occupancy cluster (0x0406) it translates the attribute
 * into the device's OWN native command and runs it through the stock dispatcher —
 * which writes the config block, pushes the setting to the 24GHz radar over its
 * internal UART (an AT command), AND persists it to flash. It then chains to the
 * original callback, so stock behaviour is untouched.
 *
 * Verified on hardware: a ZHA write of 0x0012=9 produced AT+TRITH=9 on the radar
 * UART, and 0x0010=300 produced AT+HOLD=150 (the firmware halves it). Both read
 * back correctly over BLE after a reboot, i.e. they reached flash.
 *
 * TWO RULES THAT WILL BITE YOU (both cost hours — see docs/reverse-engineering.md):
 *  1. NEVER jump straight to a per-opcode handler. They are *continuations* of the
 *     dispatcher: they use its 56-byte stack frame (sp+0x24) and return via ITS
 *     epilogue. Jumping to one unwinds your stack and reboots the device. Always
 *     go through the dispatcher at 0x4B7D.
 *  2. Write records are { u16 attrID; u8 dataType; u8 *attrData; } (stride 7) —
 *     the last 4 bytes are a POINTER to the value, not the value. They also sit at
 *     odd addresses, so read every field BYTE-WISE; unaligned 16/32-bit loads
 *     silently return rotated garbage on this core.
 *
 * Addresses are for stock firmware v1.1.9 (215,860 bytes). The image loads at 0x0,
 * so every file offset equals its runtime address.
 *
 * Build: firmware/build.py does this for you.
 * ==========================================================================*/

typedef unsigned char  u8;
typedef unsigned short u16;
typedef unsigned int   u32;

#define ADDR_DISPATCH 0x00004b7du        /* FUN_00004b7c  (thumb bit set)     */
#define ADDR_ORIG_CB  0x0000a311u        /* FUN_0000a310  (thumb bit set)     */
#define OCC_CLUSTER   0x0406u            /* Occupancy sensing                 */
#define ADDR_AT_SEND  0x000088b9u        /* FUN_000088b8 (thumb): send an AT command
                                          * (cmd, expected_reply, retries, timeout_ms).
                                          * A NORMAL function — safe to call, unlike the
                                          * opcode handlers. */
#define PTR_AT_OK     0x00004F4Cu        /* flash word holding the "AT+OK" string ptr */
#define ATTR_RAW_AT   0xF0FFu            /* write a raw AT command to the radar       */

#define SRAM_LO       0x00840000u
#define SRAM_HI       0x00850000u

/* ============================================================================
 * v1.9 READ PATH — register extra occupancy attributes that point straight at the
 * live config block, so ZHA can read the device's REAL current settings.
 *
 * The stock occupancy attribute table (0x334fc) has 2 entries. We publish a larger
 * one from flash and repoint the cluster registration at it (attrTbl @0x3356c,
 * attrNum @0x3356a — patched by build_shim_image.py).
 *
 * All added entries are READ-ONLY (access 0x01) on purpose: writes must keep going
 * through the 0xF0nn shim path so the radar AT command and the flash persist happen.
 * A directly-writable pointer here would silently change RAM and nothing else.
 * ==========================================================================*/
typedef struct {
    u16 id;
    u8  type;      /* 0x20 = uint8, 0x21 = uint16, 0x18 = map8 */
    u8  access;    /* bit0 read, bit1 write, bit2 report       */
    u32 data;      /* pointer to the value                     */
} zclAttrInfo_t;   /* naturally 8 bytes, matching the SDK       */

#define CFG 0x00844210u    /* config block base (FUN_00005bb4)  */

__attribute__((section(".attrtbl"), used))
const zclAttrInfo_t emotion_attrs[] = {
    /* --- the two the stock firmware already published --- */
    {0x0000, 0x18, 0x05, 0x008442DCu},   /* occupancy bitmap (read|report) */
    {0xFFFD, 0x21, 0x01, 0x000341D0u},   /* cluster revision (in flash)    */
    /* --- our read-only views onto the live config block --- */
    {0xF115, 0x20, 0x01, CFG + 0x15},    /* packed: freq b0-2, sens b3-6, awake b7 */
    {0xF116, 0x21, 0x01, CFG + 0x16},    /* presence timeout, seconds      */
    {0xF118, 0x21, 0x01, CFG + 0x18},    /* light sample interval          */
    {0xF11A, 0x21, 0x01, CFG + 0x1A},    /* temp/humidity sample interval  */
    {0xF11C, 0x21, 0x01, CFG + 0x1C},    /* lux threshold low  (normal)    */
    {0xF11E, 0x21, 0x01, CFG + 0x1E},    /* lux threshold high (bright)    */
};

/* ---- run a native BLE command by feeding the real dispatcher -------------- */
static u8 ble_cmd(u8 opcode, u8 arg_len, const u8 *args)
{
    u8 buf[24];
    u8 i;

    for (i = 0; i < sizeof(buf); i++)
        buf[i] = 0;
    if (arg_len > 8u)
        arg_len = 8u;

    /* synthetic ATT packet: payload_len = l2cap-3 must be >= 2 and > arg_len */
    buf[6]    = (u8)(arg_len + 5u);
    buf[0x0d] = opcode;
    buf[0x0e] = arg_len;
    for (i = 0; i < arg_len; i++)
        buf[0x0f + i] = args[i];

    return ((u8 (*)(void *))ADDR_DISPATCH)(buf);
}

/* ---- how many argument bytes does a given BLE opcode take? ---------------- */
static u8 arglen_for(u8 op)
{
    switch (op) {
        case 0x04:      /* presence timeout    u16 (seconds)  */
        case 0x13:      /* lux sample interval u16            */
        case 0x14:      /* sht sample interval u16            */
            return 2;
        case 0x15:      /* lux thresholds      2 x u16        */
            return 4;
        case 0x01:      /* radar soft reset    (no args)      */
        case 0x07:      /* calibration reset   (no args)      */
            return 0;
        case 0x05:      /* calibrate: ONE byte = number of passes, 1..20.
                         * Sending zero args makes cmd_05's `arg_len == 1` guard
                         * fail and the handler returns having done nothing —
                         * which is exactly the bug in builds before v1.10. */
            return 1;
        default:        /* frequency, sensitivity, ...  u8    */
            return 1;
    }
}

/* ---- send an arbitrary AT command to the radar -----------------------------
 * The radar understands 58 commands; the stock firmware issues about ten. This
 * opens the rest — per-range-gate thresholds (R1TH..R10TH / MR1TH..MR10TH, i.e.
 * per-distance sensitivity), GAIN, FTIME/STIME and so on — without needing another
 * firmware build for each experiment.
 *
 * Takes a ZCL character string: [u8 length][chars...]. CRLF is appended here.  */
static void raw_at(const u8 *zcl_string)
{
    char buf[40];
    u8 len = zcl_string[0];
    u8 i;

    if (len == 0u || len > 32u)
        return;

    /* Refuse anything containing "BAUD". The MCU always talks 115200 to the radar,
     * so a successful AT+BAUD would silently and permanently cut the link — and
     * there is no radar-side factory reset to recover it. Everything else in the
     * radar's vocabulary is recoverable with AT+RESET. */
    for (i = 0; (u16)i + 4u <= (u16)len; i++) {
        if (zcl_string[1 + i] == 'B' && zcl_string[2 + i] == 'A'
                && zcl_string[3 + i] == 'U' && zcl_string[4 + i] == 'D')
            return;
    }

    for (i = 0; i < len; i++)
        buf[i] = (char)zcl_string[1 + i];
    buf[len]     = 0x0D;      /* CR  */
    buf[len + 1] = 0x0A;      /* LF  */
    buf[len + 2] = 0x00;      /* NUL */

    ((void (*)(const char *, const char *, int, int))ADDR_AT_SEND)(
        buf, *(const char **)PTR_AT_OK, 5, 500);
}

/* ---- attribute id -> BLE opcode (0xFF = ignore) ---------------------------
 * Standard occupancy attributes that stock zigpy can already write (no quirk):
 *   0x0010 PIROccupiedToUnoccupiedDelay (u16) -> presence timeout
 *   0x0012 PIRUnoccupiedToOccupiedThreshold (u8) -> radar sensitivity
 * Manufacturer range 0xF0nn -> BLE opcode nn (needs a ZHA quirk to be sent).
 * Destructive opcodes are refused outright so a stray write can't wipe the
 * device: 0x10 enable-encryption, 0x11 regenerate-key, 0x12 factory-reset.  */
static u8 attr_to_opcode(u16 a)
{
    if ((a & 0xFF00u) == 0xF000u) {
        u8 op = (u8)(a & 0x00FFu);
        if (op == 0x10 || op == 0x11 || op == 0x12)
            return 0xFF;                 /* never expose destructive commands */
        return op;
    }
    switch (a) {
        case 0x0010: return 0x04;        /* occupied->unoccupied delay -> timeout   */
        case 0x0012: return 0x03;        /* unoccupied->occupied thresh -> sensitivity */
        default:     return 0xFF;
    }
}

/* ---- replacement for the global ZCL message callback ----------------------
 * zclIncoming_t offsets (from the original FUN_0000a310):
 *   *(u32*)(p+0x04) -> apsInd;  clusterId = *(u16*)(apsInd+8)
 *   *(u8 *)(p+0x20) -> ZCL command id  (0x02 Write, 0x05 Write-Undivided)
 *   *(u8**)(p+0x0c) -> [u8 count] then zclWriteRec_t[] at +1, stride 7        */
void shim_zcl_cb(void *pInMsg)
{
    u8 *p       = (u8 *)pInMsg;
    u32 apsInd  = *(u32 *)(p + 0x04);
    u16 cluster = *(u16 *)(apsInd + 8);
    u8  cmd     = *(u8 *)(p + 0x20);

    if ((cmd == 0x02 || cmd == 0x05) && cluster == OCC_CLUSTER) {
        u8 *list = *(u8 **)(p + 0x0c);
        if (list != (u8 *)0 && list[0] != 0) {
            u8 count = list[0];
            u8 i;
            for (i = 0; i < count && i < 8u; i++) {
                u8 *rec = list + 1 + (u32)i * 7u;
                u16 attrId = (u16)rec[0] | ((u16)rec[1] << 8);
                u8  opcode;

                /* raw AT passthrough is handled before the opcode mapping, because
                 * 0xF0FF would otherwise decode to opcode 0xFF, our "ignore" value */
                if (attrId == ATTR_RAW_AT) {
                    u32 pv = (u32)rec[3] | ((u32)rec[4] << 8)
                           | ((u32)rec[5] << 16) | ((u32)rec[6] << 24);
                    if (pv >= SRAM_LO && pv < SRAM_HI)
                        raw_at((const u8 *)pv);
                    continue;
                }

                opcode = attr_to_opcode(attrId);
                if (opcode != 0xFF) {
                    u32 pv = (u32)rec[3] | ((u32)rec[4] << 8)
                           | ((u32)rec[5] << 16) | ((u32)rec[6] << 24);
                    if (pv >= SRAM_LO && pv < SRAM_HI) {
                        u8 *data = (u8 *)pv;       /* attrData points at the value */
                        u8  val[4];
                        val[0] = data[0]; val[1] = data[1];
                        val[2] = data[2]; val[3] = data[3];
                        ble_cmd(opcode, arglen_for(opcode), val);
                    }
                }
            }
        }
    }

    ((void (*)(void *))ADDR_ORIG_CB)(pInMsg);   /* preserve stock behaviour */
}
