r"""磁碟溫度與健康度 — 多路徑偵測（spec §2.9）。

**為什麼要多條路徑**

沒有任何一條路徑能涵蓋所有硬體。實測結果：

| 裝置 | StorageTemperature | NVMe Log 0x02 | ATA SMART |
|---|---|---|---|
| QEMU 虛擬磁碟 | 只回 28 bytes 標頭 | 不支援 | 不支援 |
| SAMSUNG PM871b（Intel RST RAID 模式） | 只回 28 bytes 標頭 | 不支援 | **可用，38°C** |
| 直連 NVMe（預期） | 通常可用 | **可用** | 不適用 |
| 直連 SATA（預期） | 視韌體 | 不適用 | 通常可用 |

因此依序嘗試四條路徑，取第一個成功者。全部失敗時回傳空 dict——
「此裝置無溫度感測器」是正常情況，不是錯誤（spec §6.9：絕不捏造數值，
該列直接不出現）。

**為什麼不用 LibreHardwareMonitor**

spec §2.9：LHM 依賴 WinRing0 驅動，CVE-2020-14979 使其可提權，
已列入 Microsoft vulnerable driver blocklist。在啟用 HVCI 的政府／醫院端點上
不但無法運作，還會觸發 Defender 告警，使 agent 被當成事件來源。
本模組全部走 Windows 原生 IOCTL，**不需要任何核心驅動**。

**權限**

SMART 與 ATA passthrough 需要 `GENERIC_READ | GENERIC_WRITE` 開啟
`\\.\PhysicalDriveN`，一般使用者權限會失敗。agent 以 LocalSystem 執行，
因此可用；開發時以一般帳號測試會誤判為「不支援」——實測踩過。
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
# ATA 規範規定的魔術值：發命令前填入，回來時由這兩個暫存器帶回結果。
SMART_CYL_LOW = 0x4F
SMART_CYL_HIGH = 0xC2
# 回傳值：4F/C2 = 門檻未超過（健康）；F4/2C = 門檻已超過（預測即將故障）
SMART_THRESHOLD_EXCEEDED_LOW = 0xF4
SMART_THRESHOLD_EXCEEDED_HIGH = 0x2C

# SMART 屬性 ID → 意義。溫度可能出現在 0xC2 或 0xBE，視廠商而定。
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
    """開啟實體磁碟。

    SMART 需要 READ|WRITE；先試最高權限，失敗逐級降級，
    讓「只想讀型號容量」的情境在低權限下仍可運作。
    """
    path = f"\\\\.\\PhysicalDrive{index}"
    attempts = ([GENERIC_READ | GENERIC_WRITE, GENERIC_READ, 0] if want_write
                else [GENERIC_READ, 0])
    for access in attempts:
        h = _k32.CreateFileW(path, access, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
        if h and h != INVALID_HANDLE:
            return h, access
    return None, 0


# --- 路徑 1：StorageDeviceTemperatureProperty --------------------------------
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
    # DeviceIoControl 成功不代表回傳完整描述子：不支援的裝置只回標頭
    # （實測 QEMU 與 Intel RST 都只回 28 bytes），強制轉型會拋 ValueError。
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


# --- 路徑 2：NVMe SMART / Health Log (0x02) ----------------------------------
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
    """NVMe SMART / Health Information Log（NVM Express 1.4 §5.14.1.2）。"""
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


# --- 路徑 3：ATA SMART（SMART_RCV_DRIVE_DATA）--------------------------------
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
                ("bBuffer", ctypes.c_ubyte * 8)]      # 這裡放回傳的 IDEREGS


def smart_overall_health(h, drive_index: int) -> dict:
    """ATA SMART RETURN STATUS（0xDA）—— 磁碟自己的整體健康自我評估。

    這是 `smartctl -H` 顯示的那一行，也是 LibreNMS 的 smart 應用程式用
    `health_pass` 決定要顯示 `(OK)` 還是 `(FAIL)` 的依據。

    **為什麼不從屬性推導**：重新配置磁區為 0 不代表健康——韌體可能因為
    其他屬性跌破門檻而已經在預測故障。反過來說，少量重新配置磁區在某些
    型號上完全正常。真正的判斷是韌體自己做的，我們只該去問它，不該猜。

    回傳值由 ATA 規範定義，藉 CylLow/CylHigh 兩個暫存器帶回：

        4F / C2  門檻未超過 → 健康
        F4 / 2C  門檻已超過 → 韌體預測即將故障

    兩者皆非時代表這顆碟沒有回答（例如 USB 橋接器不轉送 SMART 命令），
    此時**不輸出** health_pass —— LibreNMS 的 `?? null` 分支會什麼都不顯示，
    那比顯示一個猜出來的 (OK) 誠實得多。
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
    return {}        # 沒有給出可辨識的答案 → 不猜


def _smart_supported(h) -> bool:
    buf = ctypes.create_string_buffer(64)
    ret = wintypes.DWORD(0)
    return bool(_k32.DeviceIoControl(h, SMART_GET_VERSION, None, 0,
                                     buf, 64, ctypes.byref(ret), None))


def health_via_ata_smart(h, drive_index: int) -> dict:
    """讀取 ATA SMART 屬性表。

    輸出 buffer 前 16 bytes 是 SENDCMDOUTPARAMS 標頭，之後 512 bytes 是
    SMART 屬性資料：offset 0-1 為版本，之後每 12 bytes 一筆屬性
    （id, flags(2), value, worst, raw(6), reserved）。
    """
    if not _smart_supported(h):
        return {}
    inp = _SENDCMDINPARAMS()
    inp.cBufferSize = 512
    inp.bFeaturesReg = SMART_READ_ATTRIBUTES
    inp.bSectorCountReg = 1
    inp.bSectorNumberReg = 1
    inp.bCylLowReg = 0x4F          # SMART 的魔術值
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

    # smart_by_id 保留**所有**屬性的原始 ID → raw 值。
    # SMART_ATTR_NAMES 只涵蓋我們取了名字的那些，但 LibreNMS 的 smart
    # 應用程式要的是 ID（5/9/10/183/184/187/188/196/199...），
    # 其中好幾個沒有名稱對照。只留名字等於把它們丟掉。
    res: dict = {"health_source": "ata-smart", "smart": {}, "smart_by_id": {}}
    for i in range(30):
        off = 2 + i * 12
        aid = data[off]
        if aid == 0:
            continue
        value = data[off + 3]
        worst = data[off + 4]
        raw = int.from_bytes(data[off + 5:off + 11], "little")
        # raw 是 48 位元；部分屬性把多個欄位塞在高位（例如溫度的最高/最低值）。
        # 對計數型屬性取低 32 位即為計數本身，也避免送出荒謬的大數。
        res["smart_by_id"][aid] = raw & 0xFFFFFFFF
        name = SMART_ATTR_NAMES.get(aid)
        if name:
            res["smart"][name] = {"value": value, "worst": worst, "raw": raw}
        # 溫度：raw 的低位元組通常就是攝氏溫度。部分韌體把最高/最低溫
        # 塞在 raw 的高位元組，故只取低 8 位。
        if aid in SMART_TEMP_IDS and "temp_c" not in res:
            t = raw & 0xFF
            if 0 < t < 150:
                res["temp_c"] = t
                res["temp_source"] = f"ata-smart-0x{aid:02X}"
    if not res["smart"]:
        return {}
    return res


# --- 對外介面 ---------------------------------------------------------------
def probe(drive_index: int) -> dict:
    """對單一實體磁碟嘗試所有路徑，回傳合併結果。

    任何一條路徑的例外都不得中止其餘路徑——各家韌體對這些 IOCTL 的
    支援差異極大（spec §6.7：啟動絕不硬失敗）。
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
            except Exception:  # noqa: BLE001 - 韌體行為不可預期，逐條隔離
                continue
            if not got:
                continue
            for k, v in got.items():
                # 先成功的路徑優先，不覆寫已取得的溫度
                if k not in result:
                    result[k] = v
    finally:
        _k32.CloseHandle(h)
    return result
