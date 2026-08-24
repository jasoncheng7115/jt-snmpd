# -*- coding: utf-8 -*-
"""免核心驅動的硬體感測器：ACPI 熱區、電池、CPU 頻率。

**為什麼不讀 CPU 核心溫度**

CPU 封裝溫度要存取 MSR（Intel `IA32_THERM_STATUS`、AMD SMN），那必須有核心
驅動。LibreHardwareMonitor / OpenHardwareMonitor 用的 WinRing0 已列入 Microsoft
的易受攻擊驅動封鎖清單，在客戶端普遍啟用的 HVCI / WDAC 環境下根本載不進去；
就算載得進去，為了一個溫度值在數百台政府與醫院主機上安裝一個能任意讀寫 MSR
與實體記憶體的驅動，是把監控工具變成提權管道。CLAUDE.md 鐵則 8 因此禁止。

本模組提供的是韌體本來就願意公開的資料：

- **ACPI 熱區**（`MSAcpi_ThermalZoneTemperature`）—— 主機板/CPU 附近的熱區溫度，
  含 ACPI 自己定義的 passive / critical 跳脫點，可直接當門檻值。
- **電池** —— `GetSystemPowerStatus`（充電百分比、市電狀態）。
- **CPU 頻率** —— `CallNtPowerInformation(ProcessorInformation)`。

三者都是文件化的公開 API，不需要驅動、不需要提權、不開 subprocess。

**這個模組的解析為什麼寫得這麼防禦**

WMI 資料區塊的每一個偏移量與長度都**取自緩衝區自身**，而緩衝區來自韌體與
驅動。Python 是記憶體安全的，所以不會有典型的溢位；真正的風險是：

1. 一個亂寫的 `InstanceCount` 讓迴圈跑上百萬次 —— 在「絕不能拖慢 host」的
   硬性要求下，這就是一次 DoS。
2. 一個亂寫的 `BufferSize` 讓我們配置巨大的緩衝區。
3. 韌體提供的執行個體名稱直接進 SNMP OCTET STRING —— 控制字元與超長字串會
   讓回應變形或撐破 1400 位元組上限。
4. `0` 或 `0xFFFFFFFF` 這種「未知」值換算成攝氏是 -273°C 或 4 億度，
   進了 LibreNMS 就是一串假告警。

因此**解析與採集完全分離**：`parse_wnode_all_data()` 是純函式，吃 bytes 吐
結構，可以在 Linux 上用惡意緩衝區做 property test（見 tests/test_sensors.py）。
"""

from __future__ import annotations

import struct
import sys
from typing import NamedTuple

# --- 防禦性上限 -------------------------------------------------------------
# 這些數字不是「應該夠用」，而是「超過就代表資料有問題，寧可不要」。
MAX_WMI_BUFFER = 1 << 20        # 1 MB：熱區資料實際只有數百位元組
MAX_INSTANCES = 64              # 熱區數量；真實機器是 1~8
MAX_NAME_CHARS = 128            # 執行個體名稱字元數上限
MAX_PROCESSORS = 512            # CallNtPowerInformation 緩衝區上限

# ACPI 溫度以十分之一克耳文表示。合理範圍取 -40°C ~ 200°C：
# 低於此多半是「未知」（ACPI 規範以 0 或 0xFFFFFFFF 表示），
# 高於此則是解析錯位。兩者都必須丟棄而不是輸出。
TENTHS_K_MIN = 2332             # -40.0 °C
TENTHS_K_MAX = 4732             # 200.0 °C


def tenths_kelvin_to_celsius(v: int) -> float | None:
    """十分之一克耳文 → 攝氏；不合理值回 None（絕不捏造，spec §6.9）。"""
    if not isinstance(v, int) or not (TENTHS_K_MIN <= v <= TENTHS_K_MAX):
        return None
    return round(v / 10.0 - 273.15, 1)


def sanitise_name(raw: str, *, limit: int = MAX_NAME_CHARS) -> str:
    """韌體提供的字串在進入 SNMP 之前必須先清理。

    控制字元會讓記錄檔與 LibreNMS 的顯示變形；超長字串會擠壓回應的
    1400 位元組上限。兩者都在來源端處理，不留給下游。
    """
    cleaned = "".join(c for c in raw if c.isprintable() and c not in "\r\n\t")
    return cleaned[:limit]


class WnodeInstance(NamedTuple):
    index: int
    data: bytes
    name: str


def parse_wnode_all_data(raw: bytes, *,
                         max_instances: int = MAX_INSTANCES) -> list[WnodeInstance]:
    """解析 WNODE_ALL_DATA 緩衝區。

    純函式，不呼叫任何 Win32 API —— 這是為了能用惡意輸入測試它。
    任何不合理的內容都以「回傳目前為止解析成功的部分」收場，
    絕不拋例外、絕不信任緩衝區自稱的數字。

    版面（wmistr.h）::

        WNODE_HEADER          48 bytes
          +0  BufferSize      ULONG
          +4  ProviderId      ULONG
          +8  Version/HistoricalContext
          +16 TimeStamp       LARGE_INTEGER
          +24 Guid            GUID (16)
          +40 KernelHandle/ProviderPtr
          +44 Flags           ULONG        (WNODE_HEADER 實際為 48)
        WNODE_ALL_DATA
          +48 DataBlockOffset            ULONG
          +52 InstanceCount              ULONG
          +56 OffsetInstanceNameOffsets  ULONG
          +60 FixedInstanceSize 或 OffsetInstanceDataAndLength[]
    """
    out: list[WnodeInstance] = []
    if len(raw) < 64:
        return out

    buffer_size = struct.unpack_from("<I", raw, 0)[0]
    # 緩衝區自稱的大小不可超過我們實際持有的位元組數。
    limit = min(buffer_size, len(raw)) if buffer_size >= 64 else len(raw)

    flags = struct.unpack_from("<I", raw, 44)[0]
    data_off, inst_count, name_off = struct.unpack_from("<III", raw, 48)

    if inst_count == 0:
        return out
    # 截斷而不是相信：真實機器熱區個位數，百萬筆代表資料壞了。
    count = min(inst_count, max_instances)

    fixed = bool(flags & 0x0200)        # WNODE_FLAG_FIXED_INSTANCE_SIZE
    spans: list[tuple[int, int]] = []
    if fixed:
        inst_size = struct.unpack_from("<I", raw, 60)[0]
        if not (0 < inst_size <= limit):
            return out
        for i in range(count):
            off = data_off + i * inst_size
            if off < 0 or off + inst_size > limit:
                break                   # 越界即停止，已解析的仍然有效
            spans.append((off, inst_size))
    else:
        # OffsetInstanceDataAndLength[] 每項 8 bytes（Offset + Length）
        table_end = 60 + count * 8
        if table_end > limit:
            count = max(0, (limit - 60) // 8)
        for i in range(count):
            off, length = struct.unpack_from("<II", raw, 60 + i * 8)
            if length == 0 or off < 0 or length > limit or off + length > limit:
                continue                # 跳過壞的那筆，不放棄整批
            spans.append((off, length))

    names = _parse_instance_names(raw, name_off, len(spans), limit)
    for i, (off, length) in enumerate(spans):
        out.append(WnodeInstance(index=i, data=raw[off:off + length],
                                 name=names[i] if i < len(names) else ""))
    return out


def _parse_instance_names(raw: bytes, name_off: int, count: int,
                          limit: int) -> list[str]:
    """執行個體名稱是一組偏移量，各自指向一個計數式 UNICODE 字串。

    每一層偏移都要重新檢查——名稱表的偏移、字串本身的偏移、字串長度，
    任何一個都可能是垃圾。
    """
    names: list[str] = []
    if not (0 < name_off < limit) or count <= 0:
        return names
    if name_off + count * 4 > limit:
        return names
    for i in range(count):
        try:
            off = struct.unpack_from("<I", raw, name_off + i * 4)[0]
            if not (0 < off < limit) or off + 2 > limit:
                names.append("")
                continue
            nbytes = struct.unpack_from("<H", raw, off)[0]
            # 字元數上限同時擋住「超長」與「長度欄位是垃圾」兩種情況
            if nbytes == 0 or nbytes > MAX_NAME_CHARS * 2 or off + 2 + nbytes > limit:
                names.append("")
                continue
            s = raw[off + 2:off + 2 + nbytes].decode("utf-16-le", errors="replace")
            names.append(sanitise_name(s.rstrip("\x00")))
        except struct.error:
            names.append("")
    return names


class ThermalZone(NamedTuple):
    name: str
    celsius: float
    critical_c: float | None
    passive_c: float | None


def parse_thermal_zone(inst: WnodeInstance) -> ThermalZone | None:
    """把一筆熱區資料轉成溫度。

    MSAcpi_ThermalZoneTemperature 的欄位序（ACPI 驅動的 MOF）::

        ThermalStamp, ThermalConstant1, ThermalConstant2, Reserved,
        SamplingPeriod, CurrentTemperature, PassiveTripPoint,
        CriticalTripPoint, ActiveTripPointCount, ActiveTripPoint[10]

    共 9 個 ULONG 加 10 個 ULONG。只需要前 9 個，但長度不足就不硬解。
    """
    if len(inst.data) < 36:             # 9 * 4
        return None
    try:
        f = struct.unpack_from("<9I", inst.data, 0)
    except struct.error:
        return None
    celsius = tenths_kelvin_to_celsius(f[5])
    if celsius is None:
        return None                     # 未知或不合理 → 這一列從快照消失
    return ThermalZone(
        name=inst.name or "ThermalZone",
        celsius=celsius,
        critical_c=tenths_kelvin_to_celsius(f[7]),
        passive_c=tenths_kelvin_to_celsius(f[6]),
    )


# --- 以下需要 Windows；在 Linux 上 import 本模組仍然安全 --------------------

if sys.platform == "win32":  # pragma: no cover - 只在 Windows 上執行
    import ctypes
    from ctypes import wintypes

    _adv = ctypes.windll.advapi32
    _k32 = ctypes.windll.kernel32
    _pwr = ctypes.windll.powrprof

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    # 鐵則 11：所有 Win32 呼叫必須宣告 argtypes/restype。
    _adv.WmiOpenBlock.argtypes = [ctypes.POINTER(_GUID), ctypes.c_ulong,
                                  ctypes.POINTER(wintypes.HANDLE)]
    _adv.WmiOpenBlock.restype = ctypes.c_ulong
    _adv.WmiQueryAllDataW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulong),
                                      ctypes.c_void_p]
    _adv.WmiQueryAllDataW.restype = ctypes.c_ulong
    _adv.WmiCloseBlock.argtypes = [wintypes.HANDLE]
    _adv.WmiCloseBlock.restype = ctypes.c_ulong

    _k32.GetActiveProcessorCount.argtypes = [ctypes.c_ushort]
    _k32.GetActiveProcessorCount.restype = ctypes.c_ulong

    class _SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                    ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte),
                    ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong)]

    _k32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SYSTEM_POWER_STATUS)]
    _k32.GetSystemPowerStatus.restype = wintypes.BOOL

    class _PROCESSOR_POWER_INFORMATION(ctypes.Structure):
        _fields_ = [("Number", ctypes.c_ulong), ("MaxMhz", ctypes.c_ulong),
                    ("CurrentMhz", ctypes.c_ulong), ("MhzLimit", ctypes.c_ulong),
                    ("MaxIdleState", ctypes.c_ulong), ("CurrentIdleState", ctypes.c_ulong)]

    _pwr.CallNtPowerInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.c_void_p, ctypes.c_ulong]
    _pwr.CallNtPowerInformation.restype = ctypes.c_long

    _WMIGUID_QUERY = 0x0001
    _ERROR_INSUFFICIENT_BUFFER = 122
    _ALL_PROCESSOR_GROUPS = 0xFFFF
    _PROCESSOR_INFORMATION = 11
    # MSAcpi_ThermalZoneTemperature（ACPI 驅動的 WMI 資料區塊）
    _TZ_GUID = _GUID(0xA1BC18C0, 0xA7C8, 0x11D1,
                     (ctypes.c_ubyte * 8)(0xBF, 0x3C, 0x00, 0xA0, 0xC9, 0x06, 0x29, 0x10))

    def read_thermal_zones() -> list[ThermalZone]:
        """讀取 ACPI 熱區。無熱區的機器（虛擬機、多數桌機）回空清單。

        `WmiOpenBlock` 在沒有熱區時回 4200（ERROR_WMI_GUID_NOT_FOUND）——
        這是正常情況，不是錯誤，不必記錄。
        """
        h = wintypes.HANDLE()
        if _adv.WmiOpenBlock(ctypes.byref(_TZ_GUID), _WMIGUID_QUERY,
                             ctypes.byref(h)) != 0:
            return []
        try:
            size = ctypes.c_ulong(0)
            rc = _adv.WmiQueryAllDataW(h, ctypes.byref(size), None)
            if rc not in (0, _ERROR_INSUFFICIENT_BUFFER):
                return []
            # 驅動自稱需要的大小也要設上限，否則一個壞掉的驅動就能讓我們
            # 配置任意大的記憶體。
            if not (0 < size.value <= MAX_WMI_BUFFER):
                return []
            buf = (ctypes.c_ubyte * size.value)()
            if _adv.WmiQueryAllDataW(h, ctypes.byref(size), ctypes.byref(buf)) != 0:
                return []
            # 第二次呼叫可能回報比配置量更小的實際長度；取兩者較小者。
            used = min(size.value, ctypes.sizeof(buf))
            raw = bytes(buf)[:used]
        finally:
            _adv.WmiCloseBlock(h)

        zones: list[ThermalZone] = []
        for inst in parse_wnode_all_data(raw):
            z = parse_thermal_zone(inst)
            if z is not None:
                zones.append(z)
        return zones

    class Battery(NamedTuple):
        percent: int
        on_ac: bool
        seconds_left: int | None

    def read_battery() -> Battery | None:
        """電池狀態。桌機與虛擬機沒有電池，回 None。"""
        st = _SYSTEM_POWER_STATUS()
        if not _k32.GetSystemPowerStatus(ctypes.byref(st)):
            return None
        # BATTERY_FLAG_NO_SYSTEM_BATTERY = 0x80；百分比 255 = 未知。
        if st.BatteryFlag & 0x80 or st.BatteryLifePercent > 100:
            return None
        secs = st.BatteryLifeTime
        return Battery(
            percent=int(st.BatteryLifePercent),
            on_ac=(st.ACLineStatus == 1),
            # 0xFFFFFFFF = 未知；接市電時本來就沒有意義
            seconds_left=None if secs == 0xFFFFFFFF else int(secs),
        )

    class CpuFreq(NamedTuple):
        number: int
        current_mhz: int
        max_mhz: int

    def read_cpu_frequencies() -> list[CpuFreq]:
        """每個邏輯處理器的目前/最高頻率。

        **緩衝區大小必須用 GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)**，
        不能用 os.cpu_count()：後者只反映呼叫端所屬的處理器群組，在超過 64 核
        的機器上會少報，而核心是照**實際處理器數**寫回來的——緩衝區配小了
        就是一次真正的堆積毀損。ctypes 正是 Python 記憶體安全性失效之處。
        """
        n = _k32.GetActiveProcessorCount(_ALL_PROCESSOR_GROUPS)
        if n <= 0:
            return []
        # 上限同時擋住 API 回傳異常值，以及避免配置過大的緩衝區。
        n = min(int(n), MAX_PROCESSORS)
        arr = (_PROCESSOR_POWER_INFORMATION * n)()
        if _pwr.CallNtPowerInformation(_PROCESSOR_INFORMATION, None, 0,
                                       ctypes.byref(arr), ctypes.sizeof(arr)) != 0:
            return []
        out: list[CpuFreq] = []
        for p in arr:
            # 0 MHz 代表核心沒填；不合理的高值代表解析錯位。兩者都丟棄。
            if 0 < p.CurrentMhz <= 100_000 and 0 < p.MaxMhz <= 100_000:
                out.append(CpuFreq(number=int(p.Number),
                                   current_mhz=int(p.CurrentMhz),
                                   max_mhz=int(p.MaxMhz)))
        return out

else:   # 非 Windows：提供同名函式，讓 agent 的匯入與測試不必分歧
    def read_thermal_zones() -> list[ThermalZone]:
        return []

    def read_battery():
        return None

    def read_cpu_frequencies() -> list:
        return []
