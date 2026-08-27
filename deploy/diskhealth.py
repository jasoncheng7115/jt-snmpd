r"""Disk temperature and health — multiple detection paths.

**Why more than one path**

No single path covers all hardware. Measured:

| Device | StorageTemperature | NVMe Log 0x02 | ATA SMART |
|---|---|---|---|
| QEMU virtual disk | header only, 28 bytes | unsupported | unsupported |
| SAMSUNG PM871b (Intel RST RAID mode) | header only, 28 bytes | unsupported | **works, 38 °C** |
| Direct NVMe (expected) | usually works | **works** | not applicable |
| Direct SATA (expected) | firmware-dependent | not applicable | usually works |

So the four paths are tried in order and the first success wins. When all fail
the result is an empty dict — "this device has no temperature sensor" is normal,
not an error.

**Why not LibreHardwareMonitor**

LHM depends on the WinRing0 driver, which CVE-2020-14979 makes a
privilege-escalation path, and which is on Microsoft's vulnerable-driver
blocklist. On the HVCI-enabled government and hospital endpoints this targets it
would not merely fail to work — it would raise Defender alerts and make the agent
itself the incident.

Everything here goes through native Windows IOCTLs and **needs no kernel driver**.

**Privileges**

SMART and ATA passthrough require opening `\\.\PhysicalDriveN` with
`GENERIC_READ | GENERIC_WRITE`, which fails for an ordinary user. The agent runs
as LocalSystem so it succeeds there; testing from a normal account during
development reports "unsupported" instead, which has caused confusion before.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
_k32.CreateFileW.restype = ctypes.c_void_p
_k32.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.restype = wintypes.BOOL

INVALID_HANDLE = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 3
OPEN_EXISTING = 3

IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
SMART_GET_VERSION = 0x00074080
SMART_RCV_DRIVE_DATA = 0x0007C088

STORAGE_PROPERTY_TEMPERATURE = 4
STORAGE_PROPERTY_PROTOCOL_SPECIFIC = 50
PROTOCOL_TYPE_NVME = 3
NVME_DATA_TYPE_LOG_PAGE = 2
NVME_LOG_HEALTH_INFO = 0x02

SMART_SEND_DRIVE_COMMAND = 0x0007C084
SMART_READ_ATTRIBUTES = 0xD0
SMART_READ_THRESHOLDS = 0xD1
SMART_RETURN_STATUS = 0xDA
SMART_CMD = 0xB0
# Magic values from the ATA specification: written before the command, and the
# same two registers carry the answer back.
SMART_CYL_LOW = 0x4F
SMART_CYL_HIGH = 0xC2
# Return values: 4F/C2 means no threshold exceeded (healthy); F4/2C means a
# threshold has been exceeded (failure predicted)
SMART_THRESHOLD_EXCEEDED_LOW = 0xF4
SMART_THRESHOLD_EXCEEDED_HIGH = 0x2C

# SMART attribute ID to meaning. Temperature appears at 0xC2 or 0xBE depending
# on the vendor.
SMART_TEMP_IDS = (0xC2, 0xBE, 0xE7)
SMART_ATTR_NAMES = {
    0x05: "reallocated_sectors",
    0x09: "power_on_hours",
    0x0C: "power_cycle_count",
    0xAA: "available_reserved_space_pct",
    0xB1: "wear_leveling_count",
    0xB3: "used_reserved_block_count",
    0xBE: "airflow_temperature_c",
    0xC2: "temperature_c",
    0xC5: "pending_sectors",
    0xC6: "uncorrectable_sectors",
    0xE7: "ssd_life_left_pct",
    0xE9: "media_wearout_indicator",
    0xF1: "total_lbas_written",
}


def open_disk(index: int, want_write: bool = True):
    """Open a physical disk.

    SMART needs READ|WRITE, so the most privileged open is tried first and then
    downgraded step by step — reading just the model and capacity still works at
    lower privilege.
    """
    path = f"\\\\.\\PhysicalDrive{index}"
    attempts = ([GENERIC_READ | GENERIC_WRITE, GENERIC_READ, 0] if want_write
                else [GENERIC_READ, 0])
    for access in attempts:
        h = _k32.CreateFileW(path, access, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
        if h and h != INVALID_HANDLE:
            return h, access
    return None, 0


# --- Path 1: StorageDeviceTemperatureProperty --------------------------------
class _STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [("PropertyId", ctypes.c_int), ("QueryType", ctypes.c_int),
                ("AdditionalParameters", ctypes.c_ubyte * 1)]


class _STORAGE_TEMPERATURE_INFO(ctypes.Structure):
    _fields_ = [("Index", ctypes.c_ushort), ("Temperature", ctypes.c_short),
                ("OverThreshold", ctypes.c_short), ("UnderThreshold", ctypes.c_short),
                ("OverThresholdChangable", ctypes.c_ubyte),
                ("UnderThresholdChangable", ctypes.c_ubyte),
                ("EventGenerated", ctypes.c_ubyte), ("Reserved0", ctypes.c_ubyte),
                ("Reserved1", wintypes.DWORD)]


class _STORAGE_TEMPERATURE_DATA_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                ("CriticalTemperature", ctypes.c_short),
                ("WarningTemperature", ctypes.c_short),
                ("InfoCount", ctypes.c_ushort), ("Reserved0", ctypes.c_ubyte * 6),
                ("Reserved1", ctypes.c_ulonglong * 2),
                ("TemperatureInfo", _STORAGE_TEMPERATURE_INFO * 1)]


def temp_via_storage_property(h) -> dict:
    q = _STORAGE_PROPERTY_QUERY()
    q.PropertyId = STORAGE_PROPERTY_TEMPERATURE
    q.QueryType = 0
    buf = ctypes.create_string_buffer(512)
    ret = wintypes.DWORD(0)
    if not _k32.DeviceIoControl(h, IOCTL_STORAGE_QUERY_PROPERTY,
                                ctypes.byref(q), ctypes.sizeof(q),
                                buf, ctypes.sizeof(buf), ctypes.byref(ret), None):
        return {}
    # A successful DeviceIoControl does not mean a complete descriptor came
    # back: unsupported devices return only the header (28 bytes, measured on
    # both QEMU and Intel RST), and casting that would raise ValueError.
    need = ctypes.sizeof(_STORAGE_TEMPERATURE_DATA_DESCRIPTOR)
    if ret.value < need:
        return {}
    d = _STORAGE_TEMPERATURE_DATA_DESCRIPTOR.from_buffer_copy(buf.raw[:need])
    if not d.InfoCount:
        return {}
    info = d.TemperatureInfo[0]
    if info.Temperature <= -273 or info.Temperature == 0:
        return {}
    out = {"temp_c": int(info.Temperature), "temp_source": "storage-property"}
    if d.WarningTemperature > 0:
        out["temp_warn_c"] = int(d.WarningTemperature)
    if d.CriticalTemperature > 0:
        out["temp_crit_c"] = int(d.CriticalTemperature)
    return out


# --- Path 2: NVMe SMART / Health Log (0x02) ----------------------------------
class _STORAGE_PROTOCOL_SPECIFIC_DATA(ctypes.Structure):
    _fields_ = [("ProtocolType", ctypes.c_int), ("DataType", wintypes.DWORD),
                ("ProtocolDataRequestValue", wintypes.DWORD),
                ("ProtocolDataRequestSubValue", wintypes.DWORD),
                ("ProtocolDataOffset", wintypes.DWORD),
                ("ProtocolDataLength", wintypes.DWORD),
                ("FixedProtocolReturnData", wintypes.DWORD),
                ("ProtocolDataRequestSubValue2", wintypes.DWORD),
                ("ProtocolDataRequestSubValue3", wintypes.DWORD),
                ("ProtocolDataRequestSubValue4", wintypes.DWORD)]


class _STORAGE_PROTOCOL_DATA_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                ("ProtocolSpecificData", _STORAGE_PROTOCOL_SPECIFIC_DATA)]


def health_via_nvme(h) -> dict:
    """NVMe SMART / Health Information Log(NVM Express 1.4 §5.14.1.2)."""
    hdr_size = ctypes.sizeof(_STORAGE_PROTOCOL_DATA_DESCRIPTOR)
    total = hdr_size + 512
    outbuf = ctypes.create_string_buffer(total)

    psd = _STORAGE_PROTOCOL_SPECIFIC_DATA()
    psd.ProtocolType = PROTOCOL_TYPE_NVME
    psd.DataType = NVME_DATA_TYPE_LOG_PAGE
    psd.ProtocolDataRequestValue = NVME_LOG_HEALTH_INFO
    psd.ProtocolDataOffset = ctypes.sizeof(_STORAGE_PROTOCOL_SPECIFIC_DATA)
    psd.ProtocolDataLength = 512

    payload = struct.pack("<ii", STORAGE_PROPERTY_PROTOCOL_SPECIFIC, 0) + bytes(psd)
    inbuf = ctypes.create_string_buffer(payload, len(payload))
    ret = wintypes.DWORD(0)
    if not _k32.DeviceIoControl(h, IOCTL_STORAGE_QUERY_PROPERTY,
                                inbuf, len(payload), outbuf, total,
                                ctypes.byref(ret), None):
        return {}
    if ret.value < hdr_size:
        return {}
    desc = _STORAGE_PROTOCOL_DATA_DESCRIPTOR.from_buffer_copy(outbuf.raw[:hdr_size])
    off = desc.ProtocolSpecificData.ProtocolDataOffset
    ln = desc.ProtocolSpecificData.ProtocolDataLength
    if ln < 512 or off + ln > len(outbuf.raw):
        return {}
    log = outbuf.raw[off:off + 512]

    out = {"health_source": "nvme"}
    crit = log[0]
    if crit:
        out["critical_warning"] = crit
    comp_k = int.from_bytes(log[1:3], "little")
    if comp_k > 273:
        out["temp_c"] = comp_k - 273
        out["temp_source"] = "nvme"
    out["available_spare_pct"] = log[3]
    out["available_spare_threshold_pct"] = log[4]
    out["percentage_used"] = log[5]
    out["power_on_hours"] = int.from_bytes(log[128:144], "little")
    out["unsafe_shutdowns"] = int.from_bytes(log[160:176], "little")
    out["media_errors"] = int.from_bytes(log[176:192], "little")
    return out


# --- Path 3: ATA SMART (SMART_RCV_DRIVE_DATA) --------------------------------
class _SENDCMDINPARAMS(ctypes.Structure):
    _fields_ = [("cBufferSize", wintypes.DWORD),
                ("bFeaturesReg", ctypes.c_ubyte), ("bSectorCountReg", ctypes.c_ubyte),
                ("bSectorNumberReg", ctypes.c_ubyte), ("bCylLowReg", ctypes.c_ubyte),
                ("bCylHighReg", ctypes.c_ubyte), ("bDriveHeadReg", ctypes.c_ubyte),
                ("bCommandReg", ctypes.c_ubyte), ("bReserved", ctypes.c_ubyte),
                ("dwReserved", wintypes.DWORD * 4),
                ("bDriveNumber", ctypes.c_ubyte), ("bReserved2", ctypes.c_ubyte * 3),
                ("dwReserved2", wintypes.DWORD * 4),
                ("bBuffer", ctypes.c_ubyte * 1)]


class _IDEREGS(ctypes.Structure):
    _fields_ = [("bFeaturesReg", ctypes.c_ubyte), ("bSectorCountReg", ctypes.c_ubyte),
                ("bSectorNumberReg", ctypes.c_ubyte), ("bCylLowReg", ctypes.c_ubyte),
                ("bCylHighReg", ctypes.c_ubyte), ("bDriveHeadReg", ctypes.c_ubyte),
                ("bCommandReg", ctypes.c_ubyte), ("bReserved", ctypes.c_ubyte)]


class _DRIVERSTATUS(ctypes.Structure):
    _fields_ = [("bDriverError", ctypes.c_ubyte), ("bIDEError", ctypes.c_ubyte),
                ("bReserved", ctypes.c_ubyte * 2), ("dwReserved", wintypes.DWORD * 2)]


class _SENDCMDOUTPARAMS(ctypes.Structure):
    _fields_ = [("cBufferSize", wintypes.DWORD), ("DriverStatus", _DRIVERSTATUS),
                ("bBuffer", ctypes.c_ubyte * 8)]      # the returned IDEREGS


def smart_overall_health(h, drive_index: int) -> dict:
    """ATA SMART RETURN STATUS (0xDA) — the disk's own overall health
    self-assessment.

    This is the line `smartctl -H` prints, and it is what LibreNMS's smart
    application uses via `health_pass` to decide between `(OK)` and `(FAIL)`.

    **Why not derive it from attributes**: zero reallocated sectors does not mean
    healthy — the firmware may already be predicting failure because some other
    attribute has crossed its threshold. And a handful of reallocated sectors is
    entirely normal on some models. The firmware makes this judgement, so the
    firmware is what gets asked; guessing is not an option.

    The answer is defined by the ATA specification and comes back in the CylLow
    and CylHigh registers:

        4F / C2  no threshold exceeded → healthy
        F4 / 2C  threshold exceeded    → the firmware predicts failure

    Anything else means the drive did not answer (a USB bridge that does not pass
    SMART commands through, for instance). In that case `health_pass` is **not
    emitted** — LibreNMS's `?? null` branch then shows nothing, which is far more
    honest than a guessed (OK).
    """
    inp = _SENDCMDINPARAMS()
    inp.cBufferSize = 0
    inp.bFeaturesReg = SMART_RETURN_STATUS
    inp.bSectorCountReg = 1
    inp.bSectorNumberReg = 1
    inp.bCylLowReg = SMART_CYL_LOW
    inp.bCylHighReg = SMART_CYL_HIGH
    inp.bDriveHeadReg = 0xA0
    inp.bCommandReg = SMART_CMD
    inp.bDriveNumber = drive_index & 0xFF

    out = _SENDCMDOUTPARAMS()
    ret = wintypes.DWORD(0)
    if not _k32.DeviceIoControl(h, SMART_SEND_DRIVE_COMMAND,
                                ctypes.byref(inp), ctypes.sizeof(inp),
                                ctypes.byref(out), ctypes.sizeof(out),
                                ctypes.byref(ret), None):
        return {}
    if out.DriverStatus.bDriverError:
        return {}
    regs = _IDEREGS.from_buffer_copy(bytes(out.bBuffer))
    if (regs.bCylLowReg, regs.bCylHighReg) == (SMART_CYL_LOW, SMART_CYL_HIGH):
        return {"health_pass": True, "health_source_overall": "ata-return-status"}
    if (regs.bCylLowReg, regs.bCylHighReg) == (SMART_THRESHOLD_EXCEEDED_LOW,
                                               SMART_THRESHOLD_EXCEEDED_HIGH):
        return {"health_pass": False, "health_source_overall": "ata-return-status"}
    return {}        # no recognisable answer: do not guess


def _smart_supported(h) -> bool:
    buf = ctypes.create_string_buffer(64)
    ret = wintypes.DWORD(0)
    return bool(_k32.DeviceIoControl(h, SMART_GET_VERSION, None, 0,
                                     buf, 64, ctypes.byref(ret), None))


def health_via_ata_smart(h, drive_index: int) -> dict:
    """Read the ATA SMART attribute table.

    The first 16 bytes of the output buffer are the SENDCMDOUTPARAMS header,
    followed by 512 bytes of attribute data: offsets 0-1 are the version, then
    one attribute every 12 bytes.
    (id, flags(2), value, worst, raw(6), reserved).
    """
    if not _smart_supported(h):
        return {}
    inp = _SENDCMDINPARAMS()
    inp.cBufferSize = 512
    inp.bFeaturesReg = SMART_READ_ATTRIBUTES
    inp.bSectorCountReg = 1
    inp.bSectorNumberReg = 1
    inp.bCylLowReg = 0x4F          # SMART magic value
    inp.bCylHighReg = 0xC2
    inp.bDriveHeadReg = 0xA0
    inp.bCommandReg = SMART_CMD
    inp.bDriveNumber = drive_index

    outsize = 16 + 512
    out = ctypes.create_string_buffer(outsize)
    ret = wintypes.DWORD(0)
    if not _k32.DeviceIoControl(h, SMART_RCV_DRIVE_DATA,
                                ctypes.byref(inp), ctypes.sizeof(inp),
                                out, outsize, ctypes.byref(ret), None):
        return {}
    if ret.value < 16 + 512:
        return {}
    data = out.raw[16:16 + 512]

    # smart_by_id keeps the raw value of **every** attribute against its ID.
    # SMART_ATTR_NAMES only covers the ones that were given names, but LibreNMS's
    # smart application wants IDs (5, 9, 10, 183, 184, 187, 188, 196, 199 and so
    # on), several of which have no name here. Keeping only the named ones would
    # be the same as discarding them.
    res: dict = {"health_source": "ata-smart", "smart": {}, "smart_by_id": {}}
    for i in range(30):
        off = 2 + i * 12
        aid = data[off]
        if aid == 0:
            continue
        value = data[off + 3]
        worst = data[off + 4]
        raw = int.from_bytes(data[off + 5:off + 11], "little")
        # raw is 48 bits, and some attributes pack extra fields into the high
        # bits (minimum and maximum temperature, for instance). For counters the
        # low 32 bits are the count, and taking them also avoids emitting an
        # absurd number.
        res["smart_by_id"][aid] = raw & 0xFFFFFFFF
        name = SMART_ATTR_NAMES.get(aid)
        if name:
            res["smart"][name] = {"value": value, "worst": worst, "raw": raw}
        # Temperature: the low byte of raw is usually degrees Celsius. Some
        # firmware packs the maximum and minimum into the higher bytes, so only
        # the low 8 bits are taken.
        if aid in SMART_TEMP_IDS and "temp_c" not in res:
            t = raw & 0xFF
            if 0 < t < 150:
                res["temp_c"] = t
                res["temp_source"] = f"ata-smart-0x{aid:02X}"
    if not res["smart"]:
        return {}
    return res


# --- Public interface --------------------------------------------------------
def probe(drive_index: int) -> dict:
    """Try every path against one physical disk and merge the results.

    An exception from any one path must not stop the others: firmware support for
    these IOCTLs varies enormously between vendors.
    """
    h, _access = open_disk(drive_index, want_write=True)
    if h is None:
        return {}
    result: dict = {}
    try:
        for fn in (lambda: temp_via_storage_property(h),
                   lambda: health_via_nvme(h),
                   lambda: health_via_ata_smart(h, drive_index),
                   lambda: smart_overall_health(h, drive_index)):
            try:
                got = fn()
            except Exception:  # noqa: BLE001 - firmware is unpredictable;
                               # isolate each path
                continue
            if not got:
                continue
            for k, v in got.items():
                # First path to succeed wins; do not overwrite a temperature
                # that has already been found
                if k not in result:
                    result[k] = v
    finally:
        _k32.CloseHandle(h)
    return result
