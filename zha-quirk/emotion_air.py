"""ZHA quirk for the LinknLink eMotion Air running the custom config-write firmware.

Stock firmware exposes no way to change the radar/sensor settings over Zigbee — they
are BLE-only. Our patched firmware (v1.8+, reported as 0x101B3001 / "1.3.0") adds a
shim to the global ZCL callback that turns writes to the Occupancy cluster (0x0406)
into the device's own native config commands, which apply the setting to the 24GHz
radar over its internal UART *and* persist it to flash.

Attribute -> device command mapping implemented by the firmware shim:
    0x0010  (uint16)  -> presence timeout, seconds        [PROVEN]
    0x0012  (uint8)   -> radar sensitivity, 1..10         [PROVEN]
    0xF0nn  (varies)  -> native BLE opcode 0xnn, generic  [see below]
       0xF002 uint8   frequency (0..4)
       0xF013 uint16  lux sample interval (>= 2)
       0xF014 uint16  temp/humidity sample interval (>= 10)
       0xF001 any     restart radar   (opcode takes no args)
       0xF005 any     calibrate radar (opcode takes no args)
    Destructive opcodes (0x10 encryption, 0x11 key regen, 0x12 factory reset) are
    refused by the firmware itself, so they cannot be reached from here.

NOTE ON READ-BACK: these attributes are not (yet) in the device's readable attribute
table, so ZHA cannot read current values from the device. The entities are therefore
cache-backed: they show "unknown" until you set them once, then track the last value
written. Writes work regardless — that is the part we verified on hardware. A future
firmware revision can register the attributes properly so real values are readable.

Install: put this file in the directory referenced by `custom_quirks_path` in your
configuration.yaml, e.g.

    zha:
      custom_quirks_path: /config/zha_quirks/

then restart Home Assistant.
"""

import zigpy.types as t
from zigpy.quirks import CustomCluster
from zigpy.quirks.v2.homeassistant import UnitOfTime
from zigpy.zcl.clusters.measurement import OccupancySensing
from zigpy.zcl.foundation import ZCLAttributeDef

from zhaquirks.builder import QuirkBuilder

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
    0x0010: (0xF116, lambda v: v),                 # presence timeout, seconds
    0xF013: (0xF118, lambda v: v),                 # light sample interval
    0xF014: (0xF11A, lambda v: v),                 # temp/humidity sample interval
    0xFF01: (0xF11C, lambda v: v),                 # lux threshold low  (normal)
    0xFF02: (0xF11E, lambda v: v),                 # lux threshold high (bright)
}


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

        result = None

        if presence is not None:
            # ON -> radar restart (sets awake bit); OFF -> radar sleep (clears it)
            opcode_attr = "radar_restart" if presence else "radar_sleep"
            result = await super().write_attributes(
                {opcode_attr: 1}, manufacturer=manufacturer, **kwargs
            )
            self._update_attribute(0xFF03, bool(presence))
        if low is not None or high is not None:
            if low is None:
                low = self._attr_cache.get(0xFF01, LUX_LOW_DEFAULT)
            if high is None:
                high = self._attr_cache.get(0xFF02, LUX_HIGH_DEFAULT)
            combined = (int(low) & 0xFFFF) | ((int(high) & 0xFFFF) << 16)
            result = await super().write_attributes(
                {"lux_thresholds": combined}, manufacturer=manufacturer, **kwargs
            )
            # keep the halves in cache so both entities show what was set
            self._update_attribute(0xFF01, int(low))
            self._update_attribute(0xFF02, int(high))

        if attrs:
            result = await super().write_attributes(
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
    # ---- verified on hardware -------------------------------------------------
    .number(
        OccupancySensing.AttributeDefs.pir_u_to_o_threshold.name,   # 0x0012
        EmotionAirOccupancyCluster.cluster_id,
        min_value=1,
        max_value=10,
        step=1,
        translation_key="radar_sensitivity",
        fallback_name="Radar sensitivity",
    )
    .number(
        OccupancySensing.AttributeDefs.pir_o_to_u_delay.name,       # 0x0010
        EmotionAirOccupancyCluster.cluster_id,
        min_value=2,
        max_value=3600,
        step=2,          # firmware halves this for the radar, so odd values truncate
        unit=UnitOfTime.SECONDS,
        translation_key="presence_timeout",
        fallback_name="Presence timeout",
    )
    # ---- implemented in firmware, not yet exercised ---------------------------
    .number(
        EmotionAirOccupancyCluster.AttributeDefs.radar_frequency.name,
        EmotionAirOccupancyCluster.cluster_id,
        min_value=0,
        max_value=4,
        step=1,
        translation_key="radar_frequency",
        fallback_name="Radar frequency",
    )
    .number(
        EmotionAirOccupancyCluster.AttributeDefs.lux_sample_interval.name,
        EmotionAirOccupancyCluster.cluster_id,
        min_value=2,             # firmware clamps below 2
        max_value=3600,
        step=1,
        unit=UnitOfTime.SECONDS,
        translation_key="lux_sample_interval",
        fallback_name="Light sample interval",
    )
    .number(
        EmotionAirOccupancyCluster.AttributeDefs.climate_sample_interval.name,
        EmotionAirOccupancyCluster.cluster_id,
        min_value=10,            # firmware clamps below 10
        max_value=3600,
        step=1,
        unit=UnitOfTime.SECONDS,
        translation_key="climate_sample_interval",
        fallback_name="Temperature sample interval",
    )
    .number(
        EmotionAirOccupancyCluster.AttributeDefs.lux_threshold_low.name,
        EmotionAirOccupancyCluster.cluster_id,
        min_value=1,
        max_value=10000,
        step=1,
        unit="lx",
        translation_key="lux_threshold_low",
        fallback_name="Light threshold (normal)",
    )
    .number(
        EmotionAirOccupancyCluster.AttributeDefs.lux_threshold_high.name,
        EmotionAirOccupancyCluster.cluster_id,
        min_value=1,
        max_value=10000,
        step=1,
        unit="lx",
        translation_key="lux_threshold_high",
        fallback_name="Light threshold (bright)",
    )
    .switch(
        EmotionAirOccupancyCluster.AttributeDefs.presence_monitoring.name,
        EmotionAirOccupancyCluster.cluster_id,
        translation_key="presence_monitoring",
        fallback_name="Presence monitoring",
    )
    .write_attr_button(
        EmotionAirOccupancyCluster.AttributeDefs.relearn_environment.name,
        1,
        EmotionAirOccupancyCluster.cluster_id,
        translation_key="relearn_environment",
        fallback_name="Relearn environment",
    )
    .write_attr_button(
        EmotionAirOccupancyCluster.AttributeDefs.radar_restart.name,
        1,
        EmotionAirOccupancyCluster.cluster_id,
        translation_key="restart_radar",
        fallback_name="Restart radar",
    )
    .write_attr_button(
        EmotionAirOccupancyCluster.AttributeDefs.radar_calibrate.name,
        1,
        EmotionAirOccupancyCluster.cluster_id,
        translation_key="calibrate_radar",
        fallback_name="Calibrate radar",
    )
    .add_to_registry()
)
