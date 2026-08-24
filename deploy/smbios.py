"""SMBIOS 解析（spec §2.10）— ENTITY-MIB 的資料來源。

spec §2.10 明確指出**不需要 WMI**：用
`GetSystemFirmwareTable(FIRMWARE_TABLE_PROVIDER 'RSMB', 0, buffer, size)`
取得 raw SMBIOS table 後自行解析。此路徑**不需要特殊權限**，
符合 spec §31 的資料來源優先序，也符合「不用 wmic、不用 PowerShell subprocess」
的鐵則（§10-32）。

對應關係（spec §2.10 表）：

    Type 0   BIOS          → entPhysicalFirmwareRev
    Type 1   System        → 製造商、型號、序號、UUID
    Type 2   Baseboard     → 主機板
    Type 4   Processor     → CPU package
    Type 17  Memory Device → DIMM（含序號、容量、速度）

SMBIOS 結構格式：每個 structure 由 4-byte header（type, length, handle）起頭，
接著是 formatted area，之後是以雙 NUL 結尾的字串區。結構中的字串欄位存的是
**索引**（1-based），0 代表無字串。
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

# 'RSMB' 以 little-endian DWORD 表示
RSMB = 0x52534D42


def get_raw_smbios() -> bytes:
    """取得 raw SMBIOS table。失敗回傳空 bytes（呼叫端視為該表不存在）。"""
    k32 = ctypes.windll.kernel32
    k32.GetSystemFirmwareTable.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.DWORD]
    k32.GetSystemFirmwareTable.restype = wintypes.UINT

    size = k32.GetSystemFirmwareTable(RSMB, 0, None, 0)
    if not size:
        return b""
    buf = ctypes.create_string_buffer(size)
    got = k32.GetSystemFirmwareTable(RSMB, 0, buf, size)
    if not got or got > size:
        return b""
    return buf.raw[:got]


def _strings(blob: bytes, pos: int) -> tuple[list[str], int]:
    """讀取結構尾端的字串區，回傳 (字串清單, 下一個結構的起始位置)。

    字串區以雙 NUL 結尾。無字串時仍有一個 NUL（即 0x00 0x00）。
    """
    out: list[str] = []
    start = pos
    while pos < len(blob):
        if blob[pos] == 0:
            if pos == start:                      # 空字串區
                return out, pos + 2
            out.append(blob[start:pos].decode("utf-8", "replace"))
            start = pos + 1
            if start < len(blob) and blob[start] == 0:
                return out, start + 1
        pos += 1
    return out, len(blob)


# SMBIOS 佔位字串。OEM 沒填時韌體會塞這些值，它們不是真實資料。
# 原樣輸出會讓 LibreNMS 的 Inventory 頁顯示 "Serial No. To Be Filled By O.E.M."
# ——比空白更糟，因為看起來像真的。實測回報。
_PLACEHOLDERS = {
    "to be filled by o.e.m.", "to be filled by o.e.m", "system serial number",
    "default string", "not specified", "not available", "none", "n/a",
    "unknown", "chassis serial number", "base board serial number",
    "system product name", "system manufacturer", "system version",
    "asset-1234567890", "0123456789", "00000000", "填寫者 o.e.m.",
    "fill by oem", "oem", "no dimm", "unknown manufacturer",
}


def _s(strings: list[str], idx: int) -> str:
    """SMBIOS 字串索引為 1-based，0 代表無字串。

    一併濾掉 OEM 佔位字串——把 "To Be Filled By O.E.M." 當成序號輸出，
    比留空更糟，因為它看起來像真實資料。
    """
    if idx <= 0 or idx > len(strings):
        return ""
    val = strings[idx - 1].strip()
    if val.lower() in _PLACEHOLDERS:
        return ""
    # 全是重複字元的字串（"0000000"、"........"）也是佔位
    if len(val) > 2 and len(set(val)) == 1:
        return ""
    return val


def parse_smbios(blob: bytes) -> list[dict]:
    """把 raw SMBIOS 解析成結構清單。

    Windows 的 RSMB 回傳前有 8 bytes header：
        BYTE Used20CallingMethod; BYTE SMBIOSMajorVersion;
        BYTE SMBIOSMinorVersion;  BYTE DmiRevision; DWORD Length;
    """
    if len(blob) < 8:
        return []
    (_used, _major, _minor, _rev, length) = struct.unpack_from("<BBBBI", blob, 0)
    data = blob[8:8 + length] if length and 8 + length <= len(blob) else blob[8:]

    out: list[dict] = []
    pos = 0
    while pos + 4 <= len(data):
        stype, slen, handle = struct.unpack_from("<BBH", data, pos)
        if slen < 4:
            break
        formatted = data[pos:pos + slen]
        strings, nxt = _strings(data, pos + slen)
        out.append({"type": stype, "handle": handle, "data": formatted, "strings": strings})
        if stype == 127:                          # End-of-Table
            break
        if nxt <= pos:                            # 防呆：避免畸形資料造成無限迴圈
            break
        pos = nxt
    return out


def _u8(d: bytes, off: int) -> int:
    return d[off] if off < len(d) else 0


def _u16(d: bytes, off: int) -> int:
    return struct.unpack_from("<H", d, off)[0] if off + 2 <= len(d) else 0


def _u32(d: bytes, off: int) -> int:
    return struct.unpack_from("<I", d, off)[0] if off + 4 <= len(d) else 0


# --- 各 type 的萃取 ---------------------------------------------------------

def bios_info(structs) -> dict:
    """Type 0 — BIOS。"""
    for s in structs:
        if s["type"] == 0:
            d, st = s["data"], s["strings"]
            return {"vendor": _s(st, _u8(d, 4)),
                    "version": _s(st, _u8(d, 5)),
                    "release_date": _s(st, _u8(d, 8))}
    return {}


def system_info(structs) -> dict:
    """Type 1 — System。"""
    for s in structs:
        if s["type"] == 1:
            d, st = s["data"], s["strings"]
            return {"manufacturer": _s(st, _u8(d, 4)),
                    "product": _s(st, _u8(d, 5)),
                    "version": _s(st, _u8(d, 6)),
                    "serial": _s(st, _u8(d, 7))}
    return {}


def baseboard_info(structs) -> dict:
    """Type 2 — Baseboard。"""
    for s in structs:
        if s["type"] == 2:
            d, st = s["data"], s["strings"]
            return {"manufacturer": _s(st, _u8(d, 4)),
                    "product": _s(st, _u8(d, 5)),
                    "version": _s(st, _u8(d, 6)),
                    "serial": _s(st, _u8(d, 7))}
    return {}


def processors(structs) -> list[dict]:
    """Type 4 — Processor。只取已安裝（socket populated）的。"""
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
    """Type 17 — Memory Device。跳過未安裝的插槽（size == 0）。"""
    out = []
    for s in structs:
        if s["type"] != 17:
            continue
        d, st = s["data"], s["strings"]
        size = _u16(d, 0x0C)
        if size == 0:
            continue                              # 空插槽
        if size == 0x7FFF:                        # 需用 extended size（32-bit，MB）
            size_mb = _u32(d, 0x1C) & 0x7FFFFFFF
        elif size & 0x8000:                       # bit15 set = 單位是 KB
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
    """一次取得全部 inventory 資料。

    spec §2.7：硬體 inventory 啟動時取一次即可，**永久快取**——
    SMBIOS 在開機後不會變。
    """
    structs = parse_smbios(get_raw_smbios())
    if not structs:
        return {}
    return {"bios": bios_info(structs),
            "system": system_info(structs),
            "baseboard": baseboard_info(structs),
            "processors": processors(structs),
            "memory": memory_devices(structs)}
