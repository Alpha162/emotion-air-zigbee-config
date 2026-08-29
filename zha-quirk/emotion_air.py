"""ZHA quirk for the LinknLink eMotion Air running the custom config-write firmware.

Stock firmware exposes no way to change the radar/sensor settings over Zigbee — they
are BLE-only. The patched firmware adds a shim to the global ZCL callback that turns
writes to the Occupancy cluster (0x0406) into the device's own native config commands,
which apply the setting to the 24GHz radar over its internal UART *and* persist it to
flash. Requires firmware reporting version 0x101A3001 or higher.

WHAT EACH CONTROL DOES
----------------------
"Verified" below means a write was driven end-to-end and observed arriving at the
radar; the rest are correct by inspection of the stock firmware's own handlers but
have not been exercised.

  "Detection threshold" (1-10)      -> AT+TRITH=n              [verified]
      ** LOWER = MORE SENSITIVE. ** This is a trigger threshold, so it runs the
      opposite way to the vendor app's "Sensitivity" label. Confirmed against the
      app on 2026-08-29: selecting "Low" sets 7, and the firmware default of 3
      shows as "High".   3 = High, ~5 = Medium, 7 = Low.

  "Presence timeout" (seconds)      -> AT+HOLD=<seconds/2>     [verified]
      How long presence stays "detected" after motion stops. The firmware halves
      it for the radar, so odd values truncate; the step is 2.

  "Light interval"   (seconds)      -> lux sampling period
  "Climate interval" (seconds)      -> temperature/humidity sampling period
      Lower = fresher readings, more battery. The firmware clamps these below
      2 s and 10 s respectively back to defaults, hence the entity minimums.

  "Normal threshold" / "Bright threshold" -> the lux boundaries it reports
      against ("Dim" below normal, "Normal" between, "Bright" above). Both are
      set by ONE device command, so the quirk composes the pair on write.

  "Presence monitoring" (switch)    -> ON: AT+RESET / OFF: AT+SLEEP
      Powers the radar down entirely. There is no single on/off command, so this
      maps to two different opcodes. Off = the device stops detecting presence.

  "Radar frequency"                 -> AT+FREQ=<0..4>   [disabled by default]
      Meaning UNKNOWN. Most plausibly an RF frequency point to stop neighbouring
      24GHz radars interfering — but that is inference. Left disabled.

  "Learn room (room empty)"         -> AT+CALI    [disabled by default]
      The real environment-calibration scan (the app's "Start detection"): it
      rewrites the radar's per-range-gate thresholds based on what it can see, so
      run it with the room EMPTY and still. Measured: 19 thresholds changed.
  "Reinitialise radar"              -> AT+INITTH  [disabled by default]
      Acknowledged and restarts the radar, but measured to leave the per-gate
      thresholds untouched — it neither learns nor restores. Effect unidentified;
      the app's "Restore" is evidently something else.
  "Restart radar"                   -> AT+RESET            [disabled by default]
      One-shot actions. None is destructive, but the first two characterise
      whatever the radar can see AT THAT MOMENT, so run them with the room empty
      and still. Enable in HA when you want them.

Genuinely destructive commands (factory reset, key regeneration, encryption) are
refused by the firmware itself and cannot be reached through this quirk at all.

READING CURRENT VALUES: firmware v1.9+ publishes read-only mirrors of the live
config block (0xF1nn), so the entities show the device's REAL settings rather than
the last value written. Reads use those; writes go via the 0xF0nn command path so
the radar is actually updated and the change persisted to flash.

Install: put this file in the directory referenced by `custom_quirks_path` in your
configuration.yaml, e.g.

    zha:
      custom_quirks_path: /config/zha_quirks/

then restart Home Assistant. It attaches only to devices running the patched
firmware — a stock unit is left alone, since the controls would do nothing there.

UPGRADING FROM AN EARLIER VERSION OF THIS QUIRK: Home Assistant stores each entity's
enabled state in its registry keyed by unique_id, and deleting the ZHA device does NOT
purge those rows — so entities you already had come back with their old enabled state.
The "disabled by default" behaviour therefore only applies to a genuinely new install.
On an existing one, disable the action buttons by hand in the HA UI once.

Entity names are kept SHORT on purpose: Home Assistant truncates them in the device
panel (number entities lose width to their value box, so ~18 characters is the
practical limit). Rename any of them in the HA UI if you prefer something else.
"""

import zigpy.types as t
from zigpy.quirks import CustomCluster
from zigpy.quirks.v2.homeassistant import UnitOfTime
from zigpy.zcl.clusters.measurement import OccupancySensing
from zigpy.zcl.foundation import Status, ZCLAttributeDef

from zhaquirks.builder import QuirkBuilder

try:                                    # running inside Home Assistant
    from zha.application import EntityType
except ImportError:                     # standalone (linting, CI)
    from zigpy.quirks.v2 import EntityType

MANUFACTURER = "LinknLink"
MODEL = "eMotion Air"

# Firmware defaults (FUN_00005c74), matching the native app's Configuration screen:
#   no-motion 3 min, light interval 30 s, temp/humidity 60 s, lux 10 / 100
LUX_LOW_DEFAULT = 10
LUX_HIGH_DEFAULT = 100

# Display attribute -> (readable source attribute, decoder). Firmware v1.9+ publishes
# read-only mirrors of the config block; 0xF115 packs three settings into one byte.
READ_MAP = {
    0x0012: (0xF115, lambda v: (v >> 3) & 0x0F),   # radar sensitivity
    0xF002: (0xF115, lambda v: v & 0x07),          # radar frequency
    0xFF03: (0xF115, lambda v: bool(v >> 7)),      # presence monitoring (awake bit)
    0xFF04: (0xF115, lambda v: SensitivityLevel(_snap_level((v >> 3) & 0x0F))),
    0x0010: (0xF116, lambda v: v),                 # presence timeout, seconds
    0xF013: (0xF118, lambda v: v),                 # light sample interval
    0xF014: (0xF11A, lambda v: v),                 # temp/humidity sample interval
    0xFF01: (0xF11C, lambda v: v),                 # lux threshold low  (normal)
    0xFF02: (0xF11E, lambda v: v),                 # lux threshold high (bright)
}


class SensitivityLevel(t.enum8):
    """The three levels the vendor app offers, with their measured raw values.

    AT+TRITH is a trigger THRESHOLD, so the raw scale runs the opposite way to
    "sensitivity": High is the LOWEST number. Confirmed against the app 2026-08-29
    (app "Low" writes 7; the firmware default of 3 displays as "High").
    """

    High = 3
    Medium = 5
    Low = 7


def _snap_level(raw: int) -> int:
    """Map any raw 1-10 threshold onto the nearest of the three app levels.

    The raw attribute accepts 1-10, so a device may legitimately hold a value the
    app cannot express (ours sat at 6). The select shows the closest band; the
    "Detection threshold" number entity exposes the exact value.
    """
    return min((3, 5, 7), key=lambda level: (abs(level - raw), level))


# Attributes the patched firmware acts on via its shim rather than through the normal
# ZCL attribute machinery. The device performs the write (radar command + flash persist)
# and THEN answers UNSUPPORTED_ATTRIBUTE, because they are not in its registered
# attribute table. See "Known issue" in the README.
SHIM_HANDLED = {0x0010, 0x0012, 0xF001, 0xF002, 0xF005,
                0xF006, 0xF007, 0xF013, 0xF014, 0xF015}


class EmotionAirOccupancyCluster(CustomCluster, OccupancySensing):
    """Occupancy cluster carrying the eMotion Air's extra configuration attributes.

    The ids live in the 0xF0nn manufacturer range but are declared as ordinary
    attributes on purpose: the firmware shim matches on the attribute id alone and
    does not expect a manufacturer code in the frame.
    """

    class AttributeDefs(OccupancySensing.AttributeDefs):
        """Standard occupancy attributes plus our configuration ones."""

        radar_frequency = ZCLAttributeDef(
            id=0xF002, type=t.uint8_t, access="rw", mandatory=False
        )
        radar_restart = ZCLAttributeDef(
            id=0xF001, type=t.uint8_t, access="w", mandatory=False
        )
        radar_calibrate = ZCLAttributeDef(
            id=0xF005, type=t.uint8_t, access="w", mandatory=False
        )
        radar_sleep = ZCLAttributeDef(
            id=0xF006, type=t.uint8_t, access="w", mandatory=False
        )
        lux_sample_interval = ZCLAttributeDef(
            id=0xF013, type=t.uint16_t, access="rw", mandatory=False
        )
        climate_sample_interval = ZCLAttributeDef(
            id=0xF014, type=t.uint16_t, access="rw", mandatory=False
        )
        # The device sets BOTH lux thresholds in a single 4-byte command
        # (cmd_15: [low uint16][high uint16] -> config +0x1c / +0x1e).
        lux_thresholds = ZCLAttributeDef(
            id=0xF015, type=t.uint32_t, access="rw", mandatory=False
        )
        # Virtual halves so the GUI can offer them separately. These ids are outside
        # the 0xF0nn range the firmware decodes, so they are never acted on even if
        # one were to reach the device; write_attributes() below intercepts them.
        lux_threshold_low = ZCLAttributeDef(
            id=0xFF01, type=t.uint16_t, access="rw", mandatory=False
        )
        lux_threshold_high = ZCLAttributeDef(
            id=0xFF02, type=t.uint16_t, access="rw", mandatory=False
        )
        # "Environment self-learning" in the native app: cmd_07 -> AT+INITTH, a one-shot
        # re-learn of the radar's baseline thresholds.
        relearn_environment = ZCLAttributeDef(
            id=0xF007, type=t.uint8_t, access="w", mandatory=False
        )
        # "Presence monitoring" in the native app. There is no single on/off command:
        # ON  = cmd_01 (sets awake bit 7 of config +0x15, AT+RESET)
        # OFF = cmd_06 (clears awake bit 7, AT+SLEEP)
        # so this virtual attribute is translated to the right opcode on write.
        presence_monitoring = ZCLAttributeDef(
            id=0xFF03, type=t.Bool, access="rw", mandatory=False
        )
        # Friendly three-level view of the raw threshold. Virtual: writes are
        # translated to pir_u_to_o_threshold below, reads snap to the nearest band.
        sensitivity_level = ZCLAttributeDef(
            id=0xFF04, type=SensitivityLevel, access="rw", mandatory=False
        )

        # --- read-only views onto the live config block (firmware v1.9+) --------
        # These are registered in the device's attribute table and point straight at
        # SRAM, so they return the REAL current settings. Reads use these; writes
        # still go via the 0xF0nn shim path so the radar is updated and flashed.
        cfg_packed = ZCLAttributeDef(          # freq b0-2, sensitivity b3-6, awake b7
            id=0xF115, type=t.uint8_t, access="r", mandatory=False
        )
        cfg_presence_timeout = ZCLAttributeDef(
            id=0xF116, type=t.uint16_t, access="r", mandatory=False
        )
        cfg_lux_interval = ZCLAttributeDef(
            id=0xF118, type=t.uint16_t, access="r", mandatory=False
        )
        cfg_climate_interval = ZCLAttributeDef(
            id=0xF11A, type=t.uint16_t, access="r", mandatory=False
        )
        cfg_lux_low = ZCLAttributeDef(
            id=0xF11C, type=t.uint16_t, access="r", mandatory=False
        )
        cfg_lux_high = ZCLAttributeDef(
            id=0xF11E, type=t.uint16_t, access="r", mandatory=False
        )

    async def _write(self, attributes, manufacturer=None, **kwargs):
        """Write, then forgive the device's spurious UNSUPPORTED_ATTRIBUTE replies.

        The patched firmware genuinely performs these writes — the value reaches the
        radar and is persisted to flash — but answers UNSUPPORTED_ATTRIBUTE because the
        attribute is not in its registered table. Left alone, zigpy treats that as a
        failure and Home Assistant reverts the entity to its old value, so the setting
        appears not to stick even though it did.

        We only rewrite the status for attributes we know the shim handles, and only
        for that one specific status — any other failure is passed through untouched.
        The cache is updated to the written value so the entity reflects reality
        immediately rather than after the next poll.
        """
        result = await super().write_attributes(
            attributes, manufacturer=manufacturer, **kwargs)
        wanted = {self._aid(a): v for a, v in attributes.items()}
        for group in result or []:
            for record in group:
                if (record.status == Status.UNSUPPORTED_ATTRIBUTE
                        and record.attrid in SHIM_HANDLED):
                    record.status = Status.SUCCESS
                    if record.attrid in wanted:
                        self._update_attribute(record.attrid, wanted[record.attrid])
        return result

    def _aid(self, attr):
        """Resolve an attribute name or id to a numeric id."""
        if isinstance(attr, str):
            return self.attributes_by_name[attr].id
        return int(attr)

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None, **kwargs
    ):
        """Serve the display attributes from the device's read-only config views.

        The attributes the entities are bound to (0x0010, 0x0012, 0xF0nn and the
        virtual 0xFFnn) are not readable on the device. The firmware instead publishes
        read-only mirrors of the config block, so translate, read once, and decode.
        A single read of 0xF115 yields sensitivity, frequency and presence monitoring.
        """
        wanted, passthrough = [], []
        for a in attributes:
            (wanted if self._aid(a) in READ_MAP else passthrough).append(a)

        success, failure = {}, {}

        if wanted:
            sources = sorted({READ_MAP[self._aid(a)][0] for a in wanted})
            try:
                s, f = await super().read_attributes(
                    sources, allow_cache=allow_cache, only_cache=only_cache,
                    manufacturer=manufacturer, **kwargs
                )
            except Exception:            # pre-v1.9 firmware has no such attributes
                s, f = {}, {src: 0x86 for src in sources}
            for a in wanted:
                aid = self._aid(a)
                src, decode = READ_MAP[aid]
                if src in s:
                    value = decode(s[src])
                    success[aid] = value
                    self._update_attribute(aid, value)
                else:
                    failure[aid] = f.get(src, 0x86)

        if passthrough:
            s, f = await super().read_attributes(
                passthrough, allow_cache=allow_cache, only_cache=only_cache,
                manufacturer=manufacturer, **kwargs
            )
            success.update(s)
            failure.update(f)

        return success, failure

    async def write_attributes(self, attributes, manufacturer=None, **kwargs):
        """Compose the two virtual lux thresholds into the single device command."""
        attrs = dict(attributes)

        def take(name, attr_id):
            v = attrs.pop(name, None)
            v2 = attrs.pop(attr_id, None)
            return v if v is not None else v2

        low = take("lux_threshold_low", 0xFF01)
        high = take("lux_threshold_high", 0xFF02)
        presence = take("presence_monitoring", 0xFF03)
        level = take("sensitivity_level", 0xFF04)

        result = None

        if level is not None:
            # the select is just a friendly face on the raw threshold
            raw = int(level)
            result = await self._write(
                {OccupancySensing.AttributeDefs.pir_u_to_o_threshold.name: raw},
                manufacturer=manufacturer, **kwargs)
            self._update_attribute(0xFF04, SensitivityLevel(_snap_level(raw)))

        if presence is not None:
            # ON -> radar restart (sets awake bit); OFF -> radar sleep (clears it)
            opcode_attr = "radar_restart" if presence else "radar_sleep"
            result = await self._write(
                {opcode_attr: 1}, manufacturer=manufacturer, **kwargs
            )
            self._update_attribute(0xFF03, bool(presence))
        if low is not None or high is not None:
            if low is None:
                low = self._attr_cache.get(0xFF01, LUX_LOW_DEFAULT)
            if high is None:
                high = self._attr_cache.get(0xFF02, LUX_HIGH_DEFAULT)
            combined = (int(low) & 0xFFFF) | ((int(high) & 0xFFFF) << 16)
            result = await self._write(
                {"lux_thresholds": combined}, manufacturer=manufacturer, **kwargs
            )
            # keep the halves in cache so both entities show what was set
            self._update_attribute(0xFF01, int(low))
            self._update_attribute(0xFF02, int(high))

        if attrs:
            result = await self._write(
                attrs, manufacturer=manufacturer, **kwargs
            )
        return result


# Only apply to units running the patched firmware. A stock device would show all of
# these controls and silently ignore every write, which is worse than not offering them.
# Stock 1.1.9 reports 0x10193001; our builds report 0x101A3001 and up.
MIN_PATCHED_FW = 0x101A3001

(
    QuirkBuilder(MANUFACTURER, MODEL)
    .firmware_version_filter(min_version=MIN_PATCHED_FW, allow_missing=False)
    .replaces(EmotionAirOccupancyCluster)
    # ZHA auto-creates its own number for pir_o_to_u_delay ("Occupied to unoccupied
    # delay"), duplicating our "Presence timeout". Suppress just that one. This is
    # scoped by unique_id suffix, which is why our own entity above is given an
    # explicit unique_id_suffix: without it both would end in "pir_o_to_u_delay" and
    # this filter would remove ours too. The occupancy binary_sensor on the same
    # cluster has a different suffix and is unaffected.
    .prevent_default_entity_creation(
        cluster_id=EmotionAirOccupancyCluster.cluster_id,
        unique_id_suffix="pir_o_to_u_delay",
    )
    # ---- verified on hardware -------------------------------------------------
    # AT+TRITH is a trigger THRESHOLD, so it runs OPPOSITE to the vendor app's
    # "Sensitivity" label. CONFIRMED against the app 2026-08-29: setting "Low" in the
    # app yields 7, and a unit at the firmware default of 3 shows as "High".
    #   3 = High, ~5 = Medium, 7 = Low   (3 and 7 measured; 5 interpolated)
    # LOWER value = MORE sensitive. Named as a threshold so the direction is not a
    # surprise; calling it "sensitivity" would invert the user's expectation.
    .enum(
        EmotionAirOccupancyCluster.AttributeDefs.sensitivity_level.name,
        SensitivityLevel,
        EmotionAirOccupancyCluster.cluster_id,
        translation_key="emotion_air_sensitivity_level",
        fallback_name="Sensitivity",
    )
    # The raw 1-10 threshold behind the select. Disabled by default: the three levels
    # cover what the vendor app offers, and this one runs "backwards" (lower = more
    # sensitive), which is a good way to confuse yourself. Enable it for fine control.
    .number(
        OccupancySensing.AttributeDefs.pir_u_to_o_threshold.name,   # 0x0012
        EmotionAirOccupancyCluster.cluster_id,
        min_value=1,
        max_value=10,
        step=1,
        initially_disabled=True,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="emotion_air_radar_sensitivity",
        fallback_name="Detection threshold (raw)",
    )
    .number(
        OccupancySensing.AttributeDefs.pir_o_to_u_delay.name,       # 0x0010
        EmotionAirOccupancyCluster.cluster_id,
        min_value=2,
        max_value=3600,
        step=2,          # the firmware halves this for the radar, so odd values truncate
        unit=UnitOfTime.SECONDS,
        # MUST differ from the attribute name. ZHA's own auto-created entity for this
        # attribute is <ieee>-1-1030-pir_o_to_u_delay and ours would default to
        # <ieee>-1-pir_o_to_u_delay — the suppression filter below matches on the
        # suffix, so identical suffixes mean it removes BOTH and the control vanishes.
        unique_id_suffix="presence_timeout",
        translation_key="emotion_air_presence_timeout",
        fallback_name="Presence timeout",
    )
    # ---- implemented in firmware, not yet exercised ---------------------------
    # Disabled by default: we know this sends AT+FREQ=<0..4> to the radar, but not
    # what the values mean. Most likely an RF frequency point, used to stop nearby
    # 24GHz radars interfering with each other — but that is INFERENCE, not
    # confirmed, so it is not something to nudge by accident. Enable it in HA if
    # you want to experiment.
    .number(
        EmotionAirOccupancyCluster.AttributeDefs.radar_frequency.name,
        EmotionAirOccupancyCluster.cluster_id,
        min_value=0,
        max_value=4,
        step=1,
        initially_disabled=True,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="emotion_air_radar_frequency",
        fallback_name="Radar frequency",
    )
    .number(
        EmotionAirOccupancyCluster.AttributeDefs.lux_sample_interval.name,
        EmotionAirOccupancyCluster.cluster_id,
        min_value=2,             # firmware clamps below 2
        max_value=3600,
        step=1,
        unit=UnitOfTime.SECONDS,
        translation_key="emotion_air_lux_sample_interval",
        fallback_name="Light interval",
    )
    .number(
        EmotionAirOccupancyCluster.AttributeDefs.climate_sample_interval.name,
        EmotionAirOccupancyCluster.cluster_id,
        min_value=10,            # firmware clamps below 10
        max_value=3600,
        step=1,
        unit=UnitOfTime.SECONDS,
        translation_key="emotion_air_climate_sample_interval",
        fallback_name="Climate interval",
    )
    .number(
        EmotionAirOccupancyCluster.AttributeDefs.lux_threshold_low.name,
        EmotionAirOccupancyCluster.cluster_id,
        min_value=0,
        max_value=60000,   # vendor app allows 0..(bright-1); we cannot cap dynamically
        step=1,
        unit="lx",
        translation_key="emotion_air_lux_threshold_low",
        fallback_name="Light normal",
    )
    .number(
        EmotionAirOccupancyCluster.AttributeDefs.lux_threshold_high.name,
        EmotionAirOccupancyCluster.cluster_id,
        min_value=11,
        max_value=60000,   # matches the vendor app's range
        step=1,
        unit="lx",
        translation_key="emotion_air_lux_threshold_high",
        fallback_name="Light bright",
    )
    .switch(
        EmotionAirOccupancyCluster.AttributeDefs.presence_monitoring.name,
        EmotionAirOccupancyCluster.cluster_id,
        translation_key="emotion_air_presence_monitoring",
        fallback_name="Presence monitoring",
    )
    # ---- one-shot actions -----------------------------------------------------
    # All three are DISABLED BY DEFAULT. None is destructive and none can brick
    # anything, but they change how the radar sees the room and a stray click is
    # annoying to undo — the results depend on what the room looked like at the
    # moment you pressed. Enable the one you want in HA, use it, and disable it
    # again if you like. (Genuinely destructive commands — factory reset, key
    # regeneration, encryption — are refused by the firmware and are unreachable
    # from here at all.)
    .write_attr_button(
        EmotionAirOccupancyCluster.AttributeDefs.relearn_environment.name,
        1,
        EmotionAirOccupancyCluster.cluster_id,
        initially_disabled=True,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="emotion_air_relearn_environment",
        # AT+INITTH. MEASURED 2026-08-29: the radar acknowledges it (AT+OK) and
        # restarts, but the per-gate thresholds are UNCHANGED across two restarts —
        # so it neither learns nor restores. Its actual effect is unidentified.
        fallback_name="Reinitialise radar",
    )
    .write_attr_button(
        EmotionAirOccupancyCluster.AttributeDefs.radar_calibrate.name,
        1,
        EmotionAirOccupancyCluster.cluster_id,
        initially_disabled=True,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="emotion_air_calibrate_radar",
        # AT+CALI — CONFIRMED 2026-08-29 to be the vendor app's "Start detection":
        # it rewrote 19 per-gate thresholds from the factory ramp to an
        # environment-shaped profile. Characterises whatever it can see AT THAT
        # MOMENT, so the room must be empty and still.
        fallback_name="Learn room (room empty)",
    )
    .write_attr_button(
        EmotionAirOccupancyCluster.AttributeDefs.radar_restart.name,
        1,
        EmotionAirOccupancyCluster.cluster_id,
        initially_disabled=True,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="emotion_air_restart_radar",
        # AT+RESET — harmless; the radar re-initialises in a second or so. Also
        # sets the awake bit, so it doubles as "turn presence monitoring back on".
        fallback_name="Restart radar",
    )
    .add_to_registry()
)
