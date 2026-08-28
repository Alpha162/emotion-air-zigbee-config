/* Place the shim at the fixed append base (== file offset, image load base 0x0).
 * 0x34B30 is where the genuine image's 4-byte CRC trailer currently sits; the
 * builder strips that trailer, drops the shim blob here, and re-appends the CRC.
 * shim_zcl_cb is first, so its address == the base (used for the 0x942c hook). */
ENTRY(shim_zcl_cb)
SECTIONS
{
  . = 0x00034B30;
  .shim : {
    KEEP(*(.text.shim_zcl_cb))
    *(.text .text.*)
    *(.rodata .rodata.* .constdata .constdata.*)
    . = ALIGN(4);
  }

  /* The ZCL attribute table lives at a FIXED address so the build script can
   * repoint the occupancy cluster registration at it without parsing symbols. */
  . = 0x00034E00;
  .attrtbl : {
    KEEP(*(.attrtbl))
  }
  /DISCARD/ : { *(.comment) *(.ARM.*) *(.note.*) }
}
