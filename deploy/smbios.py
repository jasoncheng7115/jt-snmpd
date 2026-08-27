"""SMBIOS parsing  — where the ENTITY-MIB data comes from.

**WMI is not needed** here: call
`GetSystemFirmwareTable(FIRMWARE_TABLE_PROVIDER 'RSMB', 0, buffer, size)`
to fetch the raw SMBIOS table and parse it here. That path **needs no special
privilege**, follows the documented data-source precedence, and honours the rule
against wmic and PowerShell subprocesses (§10-32).

The mapping :

    Type 0   BIOS          → entPhysicalFirmwareRev
    Type 1   System        → manufacturer, model, serial, UUID
    Type 2   Baseboard     → mainboard
    Type 4   Processor     → CPU package
    Type 17  Memory Device → DIMMs, with serial, capacity and speed

SMBIOS structure format: each structure starts with a 4-byte header (type,
length, handle), followed by the formatted area, followed by a string area
terminated by two NULs. String fields inside a structure hold a **1-based
index**, where 0 means "no string".
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

# 'RSMB' as a little-endian DWORD
RSMB = 0x52534D42

# Ceilings on firmware-supplied sizes. Every length in this file comes from the
# BIOS, so each one is treated as hostile input: a real SMBIOS table is a few
# kilobytes with a few dozen structures, and nothing here should scale with what
# the firmware claims. Python will not read out of bounds, but a nonsense length
# would still have the agent allocate or loop on the strength of it -- which on
# something whose first requirement is not to slow the host is the same problem
# by a different name.
MAX_TABLE_BYTES = 1 << 20        # 1 MB
MAX_STRUCTURES = 4096            # a large server reports a few hundred


def get_raw_smbios() -> bytes:
    """Fetch the raw SMBIOS table. Returns empty bytes on failure, which the
    caller treats as "the table is not there"."""
    k32 = ctypes.windll.kernel32
    k32.GetSystemFirmwareTable.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.DWORD]
    k32.GetSystemFirmwareTable.restype = wintypes.UINT

    size = k32.GetSystemFirmwareTable(RSMB, 0, None, 0)
    if not size or size > MAX_TABLE_BYTES:
        # The size comes from firmware. Asking the API for it is right; trusting
        # the answer without a ceiling is not. A buggy BIOS or a hypervisor
        # reporting nonsense would otherwise have this allocate whatever it said.
        return b""
    buf = ctypes.create_string_buffer(size)
    got = k32.GetSystemFirmwareTable(RSMB, 0, buf, size)
    if not got or got > size:
        return b""
    return buf.raw[:got]


def _strings(blob: bytes, pos: int) -> tuple[list[str], int]:
    """Read the string area at the end of a structure and return
    (list of strings, offset of the next structure).

    The area is terminated by two NULs. A structure with no strings still has
    one NUL, so the terminator is 0x00 0x00.
    """
    out: list[str] = []
    start = pos
    while pos < len(blob):
        if blob[pos] == 0:
            if pos == start:                      # empty string area
                return out, pos + 2
            out.append(blob[start:pos].decode("utf-8", "replace"))
            start = pos + 1
            if start < len(blob) and blob[start] == 0:
                return out, start + 1
        pos += 1
    return out, len(blob)


# SMBIOS placeholder strings. Firmware fills these in when the OEM did not, and
# they are not real data. Passing them through makes the LibreNMS Inventory page
# read "Serial No. To Be Filled By O.E.M." — worse than blank, because it looks
# genuine. Reported from the field.
_PLACEHOLDERS = {
    "to be filled by o.e.m.", "to be filled by o.e.m", "system serial number",
    "default string", "not specified", "not available", "none", "n/a",
    "unknown", "chassis serial number", "base board serial number",
    "system product name", "system manufacturer", "system version",
    "asset-1234567890", "0123456789", "00000000",
    "fill by oem", "oem", "no dimm", "unknown manufacturer",
}


def _s(strings: list[str], idx: int) -> str:
    """SMBIOS string indices are 1-based; 0 means there is no string.

    OEM placeholders are filtered out here too: reporting
    "To Be Filled By O.E.M." as a serial number is worse than reporting nothing,
    because it looks like real data.
    """
    if idx <= 0 or idx > len(strings):
        return ""
    val = strings[idx - 1].strip()
    if val.lower() in _PLACEHOLDERS:
        return ""
    # A string of one repeated character ("0000000", "........") is also a
    # placeholder
    if len(val) > 2 and len(set(val)) == 1:
        return ""
    return val


def parse_smbios(blob: bytes) -> list[dict]:
    """Parse raw SMBIOS into a list of structures.

    Windows' RSMB response starts with an 8-byte header:
        BYTE Used20CallingMethod; BYTE SMBIOSMajorVersion;
        BYTE SMBIOSMinorVersion;  BYTE DmiRevision; DWORD Length;
    """
    if len(blob) < 8:
        return []
    (_used, _major, _minor, _rev, length) = struct.unpack_from("<BBBBI", blob, 0)
    data = blob[8:8 + length] if length and 8 + length <= len(blob) else blob[8:]

    out: list[dict] = []
    pos = 0
    while pos + 4 <= len(data) and len(out) < MAX_STRUCTURES:
        stype, slen, handle = struct.unpack_from("<BBH", data, pos)
        if slen < 4:
            break
        formatted = data[pos:pos + slen]
        strings, nxt = _strings(data, pos + slen)
        out.append({"type": stype, "handle": handle, "data": formatted, "strings": strings})
        if stype == 127:                          # End-of-Table
            break
        if nxt <= pos:                            # guard: malformed data must
                                                  # not spin forever
            break
        pos = nxt
    return out


def _u8(d: bytes, off: int) -> int:
    return d[off] if off < len(d) else 0


def _u16(d: bytes, off: int) -> int:
    return struct.unpack_from("<H", d, off)[0] if off + 2 <= len(d) else 0


def _u32(d: bytes, off: int) -> int:
    return struct.unpack_from("<I", d, off)[0] if off + 4 <= len(d) else 0


# --- Extraction per structure type ------------------------------------------

def bios_info(structs) -> dict:
    """Type 0 — BIOS."""
    for s in structs:
        if s["type"] == 0:
            d, st = s["data"], s["strings"]
            return {"vendor": _s(st, _u8(d, 4)),
                    "version": _s(st, _u8(d, 5)),
                    "release_date": _s(st, _u8(d, 8))}
    return {}


def system_info(structs) -> dict:
    """Type 1 — System."""
    for s in structs:
        if s["type"] == 1:
            d, st = s["data"], s["strings"]
            return {"manufacturer": _s(st, _u8(d, 4)),
                    "product": _s(st, _u8(d, 5)),
                    "version": _s(st, _u8(d, 6)),
                    "serial": _s(st, _u8(d, 7))}
    return {}


def baseboard_info(structs) -> dict:
    """Type 2 — Baseboard."""
    for s in structs:
        if s["type"] == 2:
            d, st = s["data"], s["strings"]
            return {"manufacturer": _s(st, _u8(d, 4)),
                    "product": _s(st, _u8(d, 5)),
                    "version": _s(st, _u8(d, 6)),
                    "serial": _s(st, _u8(d, 7))}
    return {}


def processors(structs) -> list[dict]:
    """Type 4 — Processor. Only sockets that are actually populated."""
    out = []
    for s in structs:
        if s["type"] != 4:
            continue
        d, st = s["data"], s["strings"]
        status = _u8(d, 0x18)
        if not (status & 0x40):                   # bit6 = CPU Socket Populated
            continue
        out.append({"socket": _s(st, _u8(d, 4)),
                    "manufacturer": _s(st, _u8(d, 7)),
                    "version": _s(st, _u8(d, 0x10)),
                    "serial": _s(st, _u8(d, 0x20)),
                    "max_speed_mhz": _u16(d, 0x14),
                    "core_count": _u8(d, 0x23),
                    "thread_count": _u8(d, 0x25)})
    return out


def memory_devices(structs) -> list[dict]:
    """Type 17 — Memory Device. Empty slots (size == 0) are skipped."""
    out = []
    for s in structs:
        if s["type"] != 17:
            continue
        d, st = s["data"], s["strings"]
        size = _u16(d, 0x0C)
        if size == 0:
            continue                              # empty slot
        if size == 0x7FFF:                        # use extended size (32-bit, MB)
            size_mb = _u32(d, 0x1C) & 0x7FFFFFFF
        elif size & 0x8000:                       # bit 15 set: the unit is KB
            size_mb = (size & 0x7FFF) // 1024
        else:
            size_mb = size
        out.append({"locator": _s(st, _u8(d, 0x10)),
                    "bank": _s(st, _u8(d, 0x11)),
                    "manufacturer": _s(st, _u8(d, 0x17)),
                    "serial": _s(st, _u8(d, 0x18)),
                    "part_number": _s(st, _u8(d, 0x1A)),
                    "size_mb": size_mb,
                    "speed_mts": _u16(d, 0x15)})
    return out


def collect() -> dict:
    """Fetch the whole inventory in one pass.

    hardware inventory is read once at startup and **cached for the
    lifetime of the process** — SMBIOS does not change after boot.
    """
    structs = parse_smbios(get_raw_smbios())
    if not structs:
        return {}
    return {"bios": bios_info(structs),
            "system": system_info(structs),
            "baseboard": baseboard_info(structs),
            "processors": processors(structs),
            "memory": memory_devices(structs)}
