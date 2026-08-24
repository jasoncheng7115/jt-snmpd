"""jt-snmpd — JT SNMP Agent for Windows (Phase 0.5 端到端可部署版)

單檔自足。可前景執行（除錯）或由 pywin32 註冊為 Windows 服務（開機自啟、LocalSystem）。

架構依 spec.md §4.3：snapshot + bisect。整份 MIB 是一個依 OID 字典序排好的陣列，
GET 用 bisect_left、GETNEXT 用 bisect_right，故 §36 的 ordering / 無重複 OID /
無 GETNEXT loop / 正確 endOfMibView 成為結構保證。

用法：
    python jt_agent.py --foreground [--port 161] [--community <community>]
    python jt_agent.py install|start|stop|remove       (pywin32 服務)
"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import winreg
from bisect import bisect_left, bisect_right
from ctypes import wintypes

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.proto import rfc1902, rfc1905
from pysnmp.proto.api import v2c
from pysnmp.smi.instrum import AbstractMibInstrumController

from preauth import PreAuthGate

# ---------------------------------------------------------------- 設定 / 記錄
STATE_DIR = r"C:\ProgramData\JT-SNMP"
LOG_DIR = os.path.join(STATE_DIR, "logs")
STATE_FILE = os.path.join(STATE_DIR, "state", "index-map.json")

# Defaults only. The real values come from config.json, written by the
# installer and editable afterwards (edit, then restart the service).
#
# `community` and `allowed_networks` are deliberately empty rather than carrying
# sensible-looking values. An earlier version shipped "mon2" and
# "192.168.1.0/24" as defaults *and never read the config file at all*: the
# installer wrote the operator's answers to config.json, the agent ignored them,
# and every install that did not happen to use those exact two values failed its
# loopback health check with MSI error 1603. The defaults were what made the bug
# survive testing — our own lab used precisely those values.
CFG = {"port": 161, "community": "", "contact": "", "location": "",
       # spec §3.3: deny by default, never Any/Any. Empty means "not configured"
       # and is treated as deny-all (loopback excepted); to serve every source
       # deliberately, set 0.0.0.0/0 and ::/0 explicitly.
       "allowed_networks": (), "rate_pps": 50, "rate_burst": 100,
       # spec §3.5: ipNetToPhysicalTable is the local ARP table, which is a
       # ready-made target list for lateral movement. Off unless asked for.
       "enable_arp_table": False}

_gate: "PreAuthGate | None" = None
CFG_PATH = os.path.join(STATE_DIR, "config.json")
CFG_SOURCE = "defaults"


def load_config() -> None:
    """Merge config.json into CFG.

    Called once at service start, so the documented workflow is: edit the file,
    restart the service. Reloading on every snapshot would mean a half-written
    file could be picked up mid-edit.

    A malformed or missing file is not fatal here — the startup checks in
    `run_agent()` decide whether the resulting configuration is usable. That
    separation keeps "the file is broken" and "the settings are unusable" as two
    distinct, separately reported problems.
    """
    global CFG_SOURCE
    if CFG_SOURCE != "defaults":
        return          # 已載入。兩個進入點都會呼叫，重複讀檔只會重複記錄
    try:
        # utf-8-sig, not utf-8: Windows PowerShell 5.1's `Set-Content -Encoding
        # UTF8` writes a BOM, and so does Notepad when an operator edits the file
        # by hand. Plain utf-8 raises "Unexpected UTF-8 BOM" and the agent then
        # refuses to serve — which is exactly what happened the first time the
        # installer's config was actually read. utf-8-sig accepts both forms.
        with open(CFG_PATH, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        log(f"no config file at {CFG_PATH}; using built-in defaults")
        return
    except (OSError, ValueError, UnicodeError) as exc:
        log(f"config file at {CFG_PATH} could not be read: {exc!r}", error=True)
        return
    if not isinstance(data, dict):
        log(f"config file at {CFG_PATH} is not an object", error=True)
        return

    applied = []
    if isinstance(data.get("community"), str) and data["community"].strip():
        CFG["community"] = data["community"].strip()
        applied.append("community")
    nets = data.get("allowed_networks")
    if isinstance(nets, (list, tuple)):
        clean = tuple(n.strip() for n in nets if isinstance(n, str) and n.strip())
        CFG["allowed_networks"] = clean
        applied.append(f"allowed_networks({len(clean)})")
    port = data.get("port")
    if isinstance(port, int) and 1 <= port <= 65535:
        CFG["port"] = port
        applied.append("port")
    if isinstance(data.get("enable_arp_table"), bool):
        CFG["enable_arp_table"] = data["enable_arp_table"]
        applied.append("enable_arp_table")
    for key in ("rate_pps", "rate_burst"):
        v = data.get(key)
        if isinstance(v, int) and 0 < v <= 100000:
            CFG[key] = v
            applied.append(key)

    CFG_SOURCE = CFG_PATH
    log(f"config loaded from {CFG_PATH}: {', '.join(applied) or 'nothing usable'}")

# 版本來自 deploy/version.py（單一來源）。硬編碼在此會與 MSI 版本脫節——
# 實測發生過：MSI 已是 0.1.6，jtAgentVersion 仍回報 0.1.0-dev。
try:
    from version import VERSION as AGENT_VERSION, BUILD_DATE as AGENT_BUILD_DATE
except ImportError:                     # 打包後 version.py 已併入，理論上不會發生
    AGENT_VERSION, AGENT_BUILD_DATE = "unknown", "unknown"

try:
    import sensors as _sensors          # ACPI 熱區 / 電池 / CPU 頻率
except ImportError:
    _sensors = None
try:
    import smartjson as _smartjson      # LibreNMS smart 應用程式的 JSON
except ImportError:
    _smartjson = None


LOG_MAX_BYTES = 5 * 1024 * 1024     # 單檔上限
LOG_KEEP = 3                        # 保留 .1 ~ .3


def _rotate_log(path: str) -> None:
    """記錄檔輪替。

    無上限成長在數百台、跑數年的部署下會把系統碟寫滿——**監控代理程式把被監控
    的主機弄掛**是最不可接受的失敗模式。快照重建失敗時每 5 秒一行，一天就是
    一萬七千行，這不是假設性情境。
    """
    try:
        oldest = f"{path}.{LOG_KEEP}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for n in range(LOG_KEEP - 1, 0, -1):
            src = f"{path}.{n}"
            if os.path.exists(src):
                os.replace(src, f"{path}.{n + 1}")
        os.replace(path, f"{path}.1")
    except OSError:
        # 輪替失敗只會讓記錄檔繼續長，不該影響 agent 本身。
        pass


def _event_log_error(msg: str) -> None:
    """錯誤同時寫進 Windows 事件檢視器（來源 jt-snmpd）。

    現場人員與稽核工具第一個看的是事件檢視器，不是 %ProgramData% 下的文字檔；
    遠端診斷數百台時 `Get-WinEvent` 可以集中撈，散落各機的記錄檔不行。
    servicemanager 在模組後段才 import，此處以 globals() 延遲取得。
    """
    sm = globals().get("servicemanager")
    if sm is None:
        return
    try:
        sm.LogErrorMsg(f"jt-snmpd: {msg}")
    except Exception:   # noqa: BLE001, S110
        # 寫事件記錄失敗（權限、事件來源未註冊）不得讓 agent 跟著倒。
        pass


def log(msg: str, *, error: bool = False) -> None:
    """所有檔案 I/O 一律明確 encoding="utf-8"。

    安裝路徑可能含中文（例如 C:\\程式集\\JT SNMP Agent）。Python 的 open() 在
    Windows 上預設使用系統 ANSI 代碼頁（正體中文為 cp950），寫入含非 cp950 字元
    的內容會丟 UnicodeEncodeError；而路徑本身 Python 3 以 str 處理（內部 UTF-16），
    只要不自行編碼就安全。真正會出事的是「內容編碼」與「子行程參數傳遞」。

    `error=True` 另外寫入事件檢視器——保留給「使用者需要知道」的事件，
    不是每個 collector 的小失敗，否則事件記錄會被洗掉而失去價值。
    """
    size = 0
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "jt-snmpd.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} "
                     f"{'ERROR ' if error else ''}{msg}\n")
            size = fh.tell()        # tell() 免費，不必額外 stat
    except (OSError, UnicodeError):
        pass
    if size > LOG_MAX_BYTES:
        _rotate_log(os.path.join(LOG_DIR, "jt-snmpd.log"))
    if error:
        _event_log_error(msg)


# ------------------------------------------------------- Win32 函式簽名宣告
# 鐵則：所有 Win32 呼叫必須宣告 argtypes/restype。否則 64 位回傳值會被 ctypes
# 預設的 c_int 截斷——實測過 C: 磁碟顯示 0 GB、GetTickCount64 超過 24.8 天溢位。
_k32 = ctypes.windll.kernel32
_k32.GetTickCount64.restype = ctypes.c_ulonglong
_k32.GetTickCount64.argtypes = []
_k32.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
_k32.GlobalMemoryStatusEx.restype = wintypes.BOOL
_k32.GetLogicalDriveStringsW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
_k32.GetLogicalDriveStringsW.restype = wintypes.DWORD
_k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_k32.GetDriveTypeW.restype = wintypes.UINT
_k32.GetDiskFreeSpaceExW.argtypes = [
    wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong)]
_k32.GetDiskFreeSpaceExW.restype = wintypes.BOOL
_k32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
_k32.GetVolumeInformationW.restype = wintypes.BOOL
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
_k32.CreateFileW.restype = ctypes.c_void_p
_k32.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.SetPriorityClass.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_k32.SetPriorityClass.restype = wintypes.BOOL
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
_k32.SetThreadPriority.restype = wintypes.BOOL
_k32.GetCurrentThread.restype = ctypes.c_void_p

# 讓路用的優先權常數
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
THREAD_MODE_BACKGROUND_END = 0x00020000


def lower_process_priority() -> bool:
    """把整個 agent 程序降為 BELOW_NORMAL。

    使用者的硬性要求：poll 時不得讓 Windows 變慢。實測在 7,000 倍真實負載下，
    未降優先權時 host 上的固定工作負載退化 4.2%（目標 < 3%）。
    SNMP agent 對延遲不敏感（LibreNMS 每 5 分鐘才 poll 一次，逾時以秒計），
    把 CPU 讓給前景工作是正確取捨。
    """
    return bool(_k32.SetPriorityClass(_k32.GetCurrentProcess(),
                                      BELOW_NORMAL_PRIORITY_CLASS))


def begin_background_mode() -> bool:
    """讓當前執行緒進入背景模式：同時降低 CPU **與磁碟 I/O** 優先權。

    只用於 collector 執行緒。SNMP 回應路徑不套用，否則會拖慢回應。
    注意：THREAD_MODE_BACKGROUND_BEGIN 只能對自己的執行緒呼叫。
    """
    return bool(_k32.SetThreadPriority(_k32.GetCurrentThread(),
                                       THREAD_MODE_BACKGROUND_BEGIN))

INVALID_HANDLE = ctypes.c_void_p(-1).value
INT32_MAX = 2147483647
U32 = 0xFFFFFFFF


def octet(s) -> rfc1902.OctetString:
    """SNMP OCTET STRING 是位元組串，不是文字。pyasn1 預設以 latin-1 編碼 str，
    遇到非 ASCII 會丟 PyAsn1UnicodeEncodeError——台灣環境的網路卡別名就是中文
    （「乙太網路」），這在正體中文 Windows 上是必踩的。一律明確編成 UTF-8。"""
    if isinstance(s, bytes):
        return rfc1902.OctetString(s)
    return rfc1902.OctetString(str(s).encode("utf-8"))


def _reg(path: str, name: str, root=winreg.HKEY_LOCAL_MACHINE):
    with winreg.OpenKey(root, path) as key:
        return winreg.QueryValueEx(key, name)[0]


# --- 自身程序資源（spec §7.1 / §6.4 自我重新啟動門檻）---------------------------
class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def _proc_rss_bytes() -> int | None:
    """自身 RSS。取不到時回 None，**不回 0**。

    spec §6.9：絕不捏造數值。回 0 會讓 LibreNMS 圖表顯示「RSS = 0」，
    看起來像正常讀數，而實際上是量測失敗——比該 OID 不存在更糟。
    §6.4 的自我重新啟動門檻（RSS > 250 MB）也會因此永遠不觸發。
    Bandit S110 指出這個 try/except/pass，查證後確認是真正的規格違反。
    """
    try:
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        c = _PROCESS_MEMORY_COUNTERS()
        c.cb = ctypes.sizeof(c)
        if psapi.GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return min(int(c.WorkingSetSize), U32)
        log("GetProcessMemoryInfo 回傳失敗，jtAgentRssBytes 將不輸出")
    except Exception as exc:  # noqa: BLE001
        log(f"_proc_rss_bytes 失敗: {exc!r}")
    return None


def _proc_handle_count() -> int | None:
    """自身 handle 數。取不到時回 None，理由同 _proc_rss_bytes。"""
    try:
        _k32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        _k32.GetProcessHandleCount.restype = wintypes.BOOL
        n = wintypes.DWORD(0)
        if _k32.GetProcessHandleCount(_k32.GetCurrentProcess(), ctypes.byref(n)):
            return int(n.value)
        log("GetProcessHandleCount 回傳失敗，jtAgentHandleCount 將不輸出")
    except Exception as exc:  # noqa: BLE001
        log(f"_proc_handle_count 失敗: {exc!r}")
    return None


def _proc_thread_count() -> int:
    return threading.active_count()


# --------------------------------------------- 設定來源（ADMX 原則 / MS SNMP 移轉）
POLICY_KEY = r"SOFTWARE\Policies\JasonTools\JTSNMPD"
MSSNMP_KEY = r"SYSTEM\CurrentControlSet\Services\SNMP\Parameters"


def _reg_opt(path: str, name: str, default=None):
    """讀取單一登錄檔值，不存在回傳 default（不拋出）。"""
    try:
        return _reg(path, name)
    except OSError:
        return default


def load_system_identity() -> dict:
    """決定 sysContact / sysLocation 的生效值。

    優先序（spec §5.5：原則值**覆寫**本機設定，與 Windows 其他元件行為一致）：

      1. ADMX 原則  HKLM\\SOFTWARE\\Policies\\JasonTools\\JTSNMPD
      2. Windows 內建 SNMP 的既有設定（spec §5.9.3 移轉來源）
      3. 空字串

    第 2 項是刻意的：客戶原本就在用內建 SNMP，換過來時不該要求他們
    重新填一次 sysContact / sysLocation（spec §5.9 的核心使用者體驗）。
    即使內建 SNMP 已被停用，登錄檔仍在，設定仍值得沿用（§5.9.1）。
    """
    out = {"contact": "", "location": "", "contact_source": "none",
           "location_source": "none"}

    # 2) 先讀 MS SNMP 的既有值當底
    ms = MSSNMP_KEY + r"\RFC1156Agent"
    v = _reg_opt(ms, "sysContact")
    if v:
        out["contact"], out["contact_source"] = str(v), "ms-snmp"
    v = _reg_opt(ms, "sysLocation")
    if v:
        out["location"], out["location_source"] = str(v), "ms-snmp"

    # 1) ADMX 原則覆寫
    v = _reg_opt(POLICY_KEY, "SysContact")
    if v:
        out["contact"], out["contact_source"] = str(v), "policy"
    v = _reg_opt(POLICY_KEY, "SysLocation")
    if v:
        out["location"], out["location_source"] = str(v), "policy"

    return out


def _install_dir() -> str:
    """spec §5.10：安裝目錄必須能從 SNMP 查到，不必登入該機。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _config_warnings() -> str:
    """目前生效設定的安全性警告摘要（spec §7.1 jtAgentConfigWarnings）。

    §5.9.4：從 Windows SNMP 移轉時匯入 community 會使 v2c 從預設的停用變成啟用，
    這是一次明確的安全性降級，必須在此反映。
    """
    warns = []
    if CFG["community"]:
        warns.append("v2c enabled")
        if CFG["community"] in ("public", "private"):
            warns.append(f"default community '{CFG['community']}'")
    if not warns:
        return "none"
    return "; ".join(warns)


# --------------------------------------------------------------- 記憶體
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class PERFORMANCE_INFORMATION(ctypes.Structure):
    """psapi GetPerformanceInfo —— 一次取得 Windows 全部記憶體面向。

    比 GlobalMemoryStatusEx 多出 SystemCache、KernelPaged、KernelNonpaged，
    以及 ProcessCount / ThreadCount / HandleCount。
    後者讓 hrSystemProcesses 不必再跑 Toolhelp32 快照（那要列舉全部程序，
    300 個程序約 50–300 ms，而這裡是單次呼叫、數十 µs）。
    """
    _fields_ = [("cb", wintypes.DWORD),
                ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t),
                ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
                ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t),
                ("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD),
                ("ThreadCount", wintypes.DWORD)]


_psapi = ctypes.windll.psapi
_psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(PERFORMANCE_INFORMATION),
                                      wintypes.DWORD]
_psapi.GetPerformanceInfo.restype = wintypes.BOOL


def get_perf_info():
    pi = PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(pi)
    if not _psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        return None
    return pi


def get_memory() -> MEMORYSTATUSEX:
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    _k32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m


# --------------------------------------------------------------- 磁碟區
DRIVE_FIXED = 3


def _volume_info(root: str) -> tuple[str, str, str]:
    """回傳 (磁碟區標籤, 序號十六進位, 檔案系統)。

    標籤可能是中文（例如「資料」）。GetVolumeInformationW 是寬字元 API，
    回傳 UTF-16，Python 直接得到 str；真正的風險在後續編成 OCTET STRING 時，
    必須經 octet() 明確轉 UTF-8，否則 pyasn1 會以 latin-1 編碼而拋
    PyAsn1UnicodeEncodeError。
    """
    name = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD(0)
    maxlen = wintypes.DWORD(0)
    flags = wintypes.DWORD(0)
    ok = _k32.GetVolumeInformationW(root, name, 261, ctypes.byref(serial),
                                    ctypes.byref(maxlen), ctypes.byref(flags), fs, 261)
    if not ok:
        return "", "0", ""
    return name.value, f"{serial.value:X}", fs.value


def get_fixed_volumes() -> list[dict]:
    """只列 fixed 磁碟。spec §4.5：絕不列舉網路磁碟與光碟（斷線網芳會阻塞 30 秒以上）。"""
    buf = ctypes.create_unicode_buffer(1024)
    _k32.GetLogicalDriveStringsW(1024, buf)
    drives, cur = [], ""
    for ch in buf[:]:
        if ch == "\x00":
            if cur:
                drives.append(cur)
                cur = ""
        else:
            cur += ch
    out = []
    for d in drives:
        if _k32.GetDriveTypeW(d) != DRIVE_FIXED:
            continue
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        totfree = ctypes.c_ulonglong(0)
        if not _k32.GetDiskFreeSpaceExW(d, ctypes.byref(free), ctypes.byref(total),
                                        ctypes.byref(totfree)):
            continue
        label, serial, fstype = _volume_info(d)
        out.append({"root": d, "total": total.value,
                    "used": total.value - totfree.value,
                    "label": label, "serial": serial, "fs": fstype})
    return out


def storage_units(total_bytes: int) -> int:
    """spec §2.1：hrStorageSize/Used 是 Integer32，必須動態放大 allocation unit。"""
    unit = 4096
    while total_bytes // unit > INT32_MAX:
        unit *= 2
    return unit


# --------------------------------------------------------------- CPU
class _LI(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class _SPPI(ctypes.Structure):
    """SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION"""
    _fields_ = [("IdleTime", _LI), ("KernelTime", _LI), ("UserTime", _LI),
                ("DpcTime", _LI), ("InterruptTime", _LI), ("InterruptCount", wintypes.ULONG)]


_ntdll = ctypes.windll.ntdll
_ntdll.NtQuerySystemInformation.argtypes = [wintypes.ULONG, ctypes.c_void_p,
                                            wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
_ntdll.NtQuerySystemInformation.restype = ctypes.c_long
_prev_cpu: dict[int, tuple[int, int]] = {}

# spec §2.7：硬體 inventory 啟動時取一次，**永久快取**。SMBIOS 開機後不會變，
# 實體磁碟型號/容量亦然。每次快照重建都重取是純粹浪費。
_inventory_cache: dict | None = None


def get_inventory() -> dict:
    global _inventory_cache
    if _inventory_cache is None:
        try:
            import smbios
            info = smbios.collect()
        except Exception as exc:  # noqa: BLE001
            log(f"SMBIOS 讀取失敗: {exc!r}")
            info = {}
        info["disks"] = get_physical_disks()
        _inventory_cache = info
    return _inventory_cache

# --- 自我健康狀態（spec §7）------------------------------------------------
# 本 agent 的失效是無聲的：服務顯示 Running、LibreNMS 圖表卻是斷的。
# 這組狀態讓 LibreNMS 能監控 agent 本身，是判斷「活著但壞了」與「死了」的唯一依據。
_health = {
    "start_monotonic": time.monotonic(),
    "snapshot_generation": 0,
    "snapshot_built_monotonic": 0.0,
    "snapshot_build_ms": 0,
    "snapshot_failures": 0,
    "collectors": {},        # name -> dict(status, last_ok_monotonic, duration_ms, errors, last_error)
}


def _collector(name: str, fn, default):
    """執行一個 collector 並記錄其健康狀態（spec §7.1 jtAgentCollectorTable）。

    §10-25：每一個新的 collector 都必須同時實作其在 jtAgentCollectorTable 中的健康狀態。
    失敗時回傳 default 而非拋出——spec §6.7「啟動絕不硬失敗」、
    §6.9「collector 失敗時該列從 snapshot 消失，不得捏造數值」。
    """
    st = _health["collectors"].setdefault(
        name, {"status": 1, "last_ok": 0.0, "duration_ms": 0, "errors": 0, "last_error": ""})
    t0 = time.monotonic()
    try:
        result = fn()
        st["status"] = 1                      # ok
        st["last_ok"] = time.monotonic()
        st["last_error"] = ""
    except Exception as exc:                  # noqa: BLE001
        st["status"] = 3                      # failed
        st["errors"] += 1
        st["last_error"] = repr(exc)[:200]
        log(f"collector {name} 失敗: {exc!r}")
        result = default
    st["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return result


def get_cpu_loads() -> list[int]:
    """每核使用率 %。spec §4.5：用 NtQuerySystemInformation 一次取得全部 CPU，
    比 PDH 在多核上做 wildcard 展開便宜得多。"""
    ncpu = os.cpu_count() or 1
    buf = (_SPPI * ncpu)()
    ret = wintypes.ULONG(0)
    if _ntdll.NtQuerySystemInformation(8, ctypes.byref(buf), ctypes.sizeof(buf),
                                       ctypes.byref(ret)) != 0:
        return [0] * ncpu
    loads = []
    for i in range(ncpu):
        idle = buf[i].IdleTime.QuadPart
        total = buf[i].KernelTime.QuadPart + buf[i].UserTime.QuadPart  # KernelTime 已含 idle
        pi, pt = _prev_cpu.get(i, (0, 0))
        di, dt = idle - pi, total - pt
        loads.append(0 if dt <= 0 else max(0, min(100, int(round((1 - di / dt) * 100)))))
        _prev_cpu[i] = (idle, total)
    return loads


def get_cpu_name() -> str:
    try:
        return str(_reg(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString"))
    except OSError:
        return "CPU"


# --------------------------------------------------------------- Disk I/O
class _DISK_PERFORMANCE(ctypes.Structure):
    _fields_ = [("BytesRead", ctypes.c_longlong), ("BytesWritten", ctypes.c_longlong),
                ("ReadTime", ctypes.c_longlong), ("WriteTime", ctypes.c_longlong),
                ("IdleTime", ctypes.c_longlong), ("ReadCount", wintypes.DWORD),
                ("WriteCount", wintypes.DWORD), ("QueueDepth", wintypes.DWORD),
                ("SplitCount", wintypes.DWORD), ("QueryTime", ctypes.c_longlong),
                ("StorageDeviceNumber", wintypes.DWORD), ("StorageManagerName", wintypes.WCHAR * 8)]


IOCTL_DISK_PERFORMANCE = 0x00070020
OPEN_EXISTING = 3
FILE_SHARE_RW = 3


def get_disk_io() -> list[tuple[int, int, int, int, int]]:
    """spec §4.5：IOCTL_DISK_PERFORMANCE。需系統管理權限，服務以 LocalSystem 執行。
    回傳 (drive_no, BytesRead, BytesWritten, ReadCount, WriteCount) 皆為累積值。"""
    out = []
    for n in range(16):
        h = _k32.CreateFileW(f"\\\\.\\PhysicalDrive{n}", 0, FILE_SHARE_RW,
                             None, OPEN_EXISTING, 0, None)
        if not h or h == INVALID_HANDLE:
            continue
        try:
            dp = _DISK_PERFORMANCE()
            ret = wintypes.DWORD(0)
            if _k32.DeviceIoControl(h, IOCTL_DISK_PERFORMANCE, None, 0, ctypes.byref(dp),
                                    ctypes.sizeof(dp), ctypes.byref(ret), None):
                out.append((n, int(dp.BytesRead), int(dp.BytesWritten),
                            int(dp.ReadCount), int(dp.WriteCount)))
        finally:
            _k32.CloseHandle(h)
    return out


# ------------------------------------- 網路協定統計（IP / TCP / UDP / ICMP）
# 這些是 LibreNMS「Netstats」整組圖表的來源。全部走 iphlpapi，
# 一次呼叫取得整組計數器，成本極低（§10-32：不用 wmic、不用 PowerShell）。
AF_INET = 2
_iph = ctypes.windll.iphlpapi


class MIB_IPSTATS(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in (
        "Forwarding", "DefaultTTL", "InReceives", "InHdrErrors", "InAddrErrors",
        "ForwDatagrams", "InUnknownProtos", "InDiscards", "InDelivers",
        "OutRequests", "RoutingDiscards", "OutDiscards", "OutNoRoutes",
        "ReasmTimeout", "ReasmReqds", "ReasmOks", "ReasmFails",
        "FragOks", "FragFails", "FragCreates", "NumIf", "NumAddr", "NumRoutes")]


class MIB_TCPSTATS(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in (
        "RtoAlgorithm", "RtoMin", "RtoMax", "MaxConn", "ActiveOpens",
        "PassiveOpens", "AttemptFails", "EstabResets", "CurrEstab",
        "InSegs", "OutSegs", "RetransSegs", "InErrs", "OutRsts", "NumConns")]


class MIB_UDPSTATS(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in (
        "InDatagrams", "NoPorts", "InErrors", "OutDatagrams", "NumAddrs")]


class MIBICMPSTATS(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in (
        "Msgs", "Errors", "DestUnreachs", "TimeExcds", "ParmProbs", "SrcQuenchs",
        "Redirects", "Echos", "EchoReps", "Timestamps", "TimestampReps",
        "AddrMasks", "AddrMaskReps")]


class MIB_ICMP(ctypes.Structure):
    _fields_ = [("InStats", MIBICMPSTATS), ("OutStats", MIBICMPSTATS)]


_iph.GetIpStatisticsEx.argtypes = [ctypes.POINTER(MIB_IPSTATS), wintypes.ULONG]
_iph.GetIpStatisticsEx.restype = wintypes.DWORD
_iph.GetTcpStatisticsEx.argtypes = [ctypes.POINTER(MIB_TCPSTATS), wintypes.ULONG]
_iph.GetTcpStatisticsEx.restype = wintypes.DWORD
_iph.GetUdpStatisticsEx.argtypes = [ctypes.POINTER(MIB_UDPSTATS), wintypes.ULONG]
_iph.GetUdpStatisticsEx.restype = wintypes.DWORD
_iph.GetIcmpStatistics.argtypes = [ctypes.POINTER(MIB_ICMP)]
_iph.GetIcmpStatistics.restype = wintypes.DWORD


def get_ip_stats():
    st = MIB_IPSTATS()
    if _iph.GetIpStatisticsEx(ctypes.byref(st), AF_INET) != 0:
        return None
    return st


def get_tcp_stats():
    st = MIB_TCPSTATS()
    if _iph.GetTcpStatisticsEx(ctypes.byref(st), AF_INET) != 0:
        return None
    return st


def get_udp_stats():
    st = MIB_UDPSTATS()
    if _iph.GetUdpStatisticsEx(ctypes.byref(st), AF_INET) != 0:
        return None
    return st


def get_icmp_stats():
    st = MIB_ICMP()
    if _iph.GetIcmpStatistics(ctypes.byref(st)) != 0:
        return None
    return st


# ----------------------------------------- IP 位址表 / 鄰居表（ARP / ND）
class SOCKADDR_IN(ctypes.Structure):
    _fields_ = [("sin_family", ctypes.c_ushort), ("sin_port", ctypes.c_ushort),
                ("sin_addr", ctypes.c_ubyte * 4), ("sin_zero", ctypes.c_ubyte * 8)]


class SOCKADDR_IN6(ctypes.Structure):
    _fields_ = [("sin6_family", ctypes.c_ushort), ("sin6_port", ctypes.c_ushort),
                ("sin6_flowinfo", wintypes.ULONG), ("sin6_addr", ctypes.c_ubyte * 16),
                ("sin6_scope_id", wintypes.ULONG)]


class SOCKADDR_INET(ctypes.Union):
    _fields_ = [("Ipv4", SOCKADDR_IN), ("Ipv6", SOCKADDR_IN6),
                ("si_family", ctypes.c_ushort)]


AF_INET6 = 23


def _inet_str(sa: SOCKADDR_INET):
    """把 SOCKADDR_INET 轉成 (family, 位址字串, 位址位元組)。"""
    import socket as _sock
    fam = sa.si_family
    if fam == AF_INET:
        raw = bytes(sa.Ipv4.sin_addr)
        return 4, _sock.inet_ntop(_sock.AF_INET, raw), raw
    if fam == AF_INET6:
        raw = bytes(sa.Ipv6.sin6_addr)
        return 6, _sock.inet_ntop(_sock.AF_INET6, raw), raw
    return 0, "", b""


class MIB_UNICASTIPADDRESS_ROW(ctypes.Structure):
    _fields_ = [("Address", SOCKADDR_INET), ("InterfaceLuid", ctypes.c_ulonglong),
                ("InterfaceIndex", wintypes.ULONG), ("PrefixOrigin", ctypes.c_int),
                ("SuffixOrigin", ctypes.c_int), ("ValidLifetime", wintypes.ULONG),
                ("PreferredLifetime", wintypes.ULONG), ("OnLinkPrefixLength", ctypes.c_ubyte),
                ("SkipAsSource", ctypes.c_ubyte), ("DadState", ctypes.c_int),
                ("ScopeId", wintypes.ULONG), ("CreationTimeStamp", ctypes.c_longlong)]


class MIB_UNICASTIPADDRESS_TABLE(ctypes.Structure):
    _fields_ = [("NumEntries", wintypes.ULONG),
                ("Table", MIB_UNICASTIPADDRESS_ROW * 1)]


class MIB_IPNET_ROW2(ctypes.Structure):
    _fields_ = [("Address", SOCKADDR_INET), ("InterfaceIndex", wintypes.ULONG),
                ("InterfaceLuid", ctypes.c_ulonglong),
                ("PhysicalAddress", ctypes.c_ubyte * 32),
                ("PhysicalAddressLength", wintypes.ULONG), ("State", ctypes.c_int),
                ("Flags", ctypes.c_ubyte), ("ReachabilityTime", wintypes.ULONG)]


class MIB_IPNET_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", wintypes.ULONG), ("Table", MIB_IPNET_ROW2 * 1)]


_iph.GetUnicastIpAddressTable.argtypes = [
    wintypes.USHORT, ctypes.POINTER(ctypes.POINTER(MIB_UNICASTIPADDRESS_TABLE))]
_iph.GetUnicastIpAddressTable.restype = wintypes.DWORD
_iph.GetIpNetTable2.argtypes = [
    wintypes.USHORT, ctypes.POINTER(ctypes.POINTER(MIB_IPNET_TABLE2))]
_iph.GetIpNetTable2.restype = wintypes.DWORD

AF_UNSPEC = 0


def get_ip_addresses() -> list[dict]:
    """本機單播 IP 位址（IPv4 + IPv6）。

    對應 LibreNMS 的 ipv4-addresses / ipv6-addresses 探索模組。
    這是**本機自己的位址**，揭露風險低；ARP 表才是內網拓撲（見 get_ip_neighbors）。
    """
    ptr = ctypes.POINTER(MIB_UNICASTIPADDRESS_TABLE)()
    if _iph.GetUnicastIpAddressTable(AF_UNSPEC, ctypes.byref(ptr)) != 0:
        return []
    try:
        n = ptr.contents.NumEntries
        rows = (MIB_UNICASTIPADDRESS_ROW * n).from_address(
            ctypes.addressof(ptr.contents.Table))
        out = []
        for r in rows:
            ver, addr, raw = _inet_str(r.Address)
            if not ver:
                continue
            out.append({"version": ver, "addr": addr, "raw": raw,
                        "prefix_len": int(r.OnLinkPrefixLength),
                        "if_index": int(r.InterfaceIndex),
                        "luid": f"{r.InterfaceLuid:016x}"})
        return out
    finally:
        _iph.FreeMibTable(ptr)


# ipNetToPhysicalState: 依 RFC 4293 —— reachable(1) stale(2) delay(3)
# probe(4) invalid(5) unknown(6) incomplete(7)
_NDSTATE_TO_MIB = {0: 7, 1: 7, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 1}


def get_ip_neighbors() -> list[dict]:
    """鄰居快取（ARP / IPv6 ND）。

    spec §3.5 明確警告：ipNetToPhysicalTable 是**內網 ARP 表**，
    等同橫向移動的目標清單，攻擊價值高。因此預設**停用**，
    需在設定中明確開啟（對應 VACM 的 standard preset 而非 librenms-minimal）。
    """
    ptr = ctypes.POINTER(MIB_IPNET_TABLE2)()
    if _iph.GetIpNetTable2(AF_UNSPEC, ctypes.byref(ptr)) != 0:
        return []
    try:
        n = ptr.contents.NumEntries
        rows = (MIB_IPNET_ROW2 * n).from_address(ctypes.addressof(ptr.contents.Table))
        out = []
        for r in rows:
            ver, addr, raw = _inet_str(r.Address)
            if not ver or not r.PhysicalAddressLength:
                continue
            out.append({"version": ver, "addr": addr, "raw": raw,
                        "if_index": int(r.InterfaceIndex),
                        "mac": bytes(r.PhysicalAddress[:r.PhysicalAddressLength]),
                        "state": _NDSTATE_TO_MIB.get(int(r.State), 6)})
        return out
    finally:
        _iph.FreeMibTable(ptr)


# --------------------------------------------------------- 路由表
class MIB_IPFORWARD_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_ulonglong), ("InterfaceIndex", wintypes.ULONG),
        ("DestinationPrefix_Prefix", SOCKADDR_INET),
        ("DestinationPrefix_PrefixLength", ctypes.c_ubyte),
        ("_pad0", ctypes.c_ubyte * 3),
        ("NextHop", SOCKADDR_INET),
        ("SitePrefixLength", ctypes.c_ubyte), ("_pad1", ctypes.c_ubyte * 3),
        ("ValidLifetime", wintypes.ULONG), ("PreferredLifetime", wintypes.ULONG),
        ("Metric", wintypes.ULONG), ("Protocol", ctypes.c_int),
        ("Loopback", ctypes.c_ubyte), ("AutoconfigureAddress", ctypes.c_ubyte),
        ("Publish", ctypes.c_ubyte), ("Immortal", ctypes.c_ubyte),
        ("Age", wintypes.ULONG), ("Origin", ctypes.c_int)]


class MIB_IPFORWARD_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", wintypes.ULONG), ("Table", MIB_IPFORWARD_ROW2 * 1)]


_iph.GetIpForwardTable2.argtypes = [
    wintypes.USHORT, ctypes.POINTER(ctypes.POINTER(MIB_IPFORWARD_TABLE2))]
_iph.GetIpForwardTable2.restype = wintypes.DWORD


def get_routes() -> list[dict]:
    """IPv4 路由表。

    spec §3.5 把 ipForwardTable 列為「完整內部路由拓撲」，攻擊價值高，
    因此歸在 VACM 的 standard preset。但它不像 ARP 表那樣直接是
    橫向移動的目標清單（路由是網段層級，ARP 是主機層級），
    且 LibreNMS 的部分功能會用到，故預設輸出、可由設定關閉。
    """
    ptr = ctypes.POINTER(MIB_IPFORWARD_TABLE2)()
    if _iph.GetIpForwardTable2(AF_INET, ctypes.byref(ptr)) != 0:
        return []
    try:
        n = ptr.contents.NumEntries
        rows = (MIB_IPFORWARD_ROW2 * n).from_address(
            ctypes.addressof(ptr.contents.Table))
        out = []
        for r in rows:
            dv, dest, draw = _inet_str(r.DestinationPrefix_Prefix)
            nv, nexthop, nraw = _inet_str(r.NextHop)
            if dv != 4:
                continue
            plen = int(r.DestinationPrefix_PrefixLength)
            mask = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
            out.append({
                "dest": dest, "dest_raw": draw, "prefix_len": plen,
                "mask": ".".join(str((mask >> sh) & 0xFF) for sh in (24, 16, 8, 0)),
                # 直連路由沒有下一跳，RFC1213 的 ipRouteNextHop 以 0.0.0.0 表示。
                # 這不是綁定位址，Bandit 的 B104 在此為誤報。
                "next_hop": nexthop if nv == 4 else "0.0.0.0",  # nosec B104
                "if_index": int(r.InterfaceIndex), "metric": int(r.Metric),
                # RFC1213 ipRouteProto: other(1) local(2) netmgmt(3) ... 
                # Windows 的 NL_ROUTE_PROTOCOL: 1=Other 2=Local 3=NetMgmt
                "proto": 2 if r.Protocol == 2 else (3 if r.Protocol == 3 else 1),
                # ipRouteType: direct(3) 表示目的地在本地網段，indirect(4) 需經閘道
                "type": 3 if nv != 4 or nexthop == "0.0.0.0" else 4,  # nosec B104
            })
        return out
    finally:
        _iph.FreeMibTable(ptr)


# ------------------------------------ UCD-SNMP systemStats（LibreNMS System 圖表）
class _SYSTEM_PERFORMANCE_INFORMATION(ctypes.Structure):
    """NtQuerySystemInformation(SystemPerformanceInformation)。

    Windows 未公開文件化這個結構，但它自 NT 以來版面穩定，
    工作管理員與 perfmon 都靠它。實測 Win11 26200 回傳 312 bytes，
    與此定義相符——若未來版面改變，回傳長度會不符，我們據此拒用而非誤讀。
    """
    _fields_ = [
        ("IdleProcessTime", ctypes.c_longlong),
        ("IoReadTransferCount", ctypes.c_longlong),
        ("IoWriteTransferCount", ctypes.c_longlong),
        ("IoOtherTransferCount", ctypes.c_longlong),
        ("IoReadOperationCount", wintypes.ULONG),
        ("IoWriteOperationCount", wintypes.ULONG),
        ("IoOtherOperationCount", wintypes.ULONG),
        ("AvailablePages", wintypes.ULONG), ("CommittedPages", wintypes.ULONG),
        ("CommitLimit", wintypes.ULONG), ("PeakCommitment", wintypes.ULONG),
        ("PageFaultCount", wintypes.ULONG), ("CopyOnWriteCount", wintypes.ULONG),
        ("TransitionCount", wintypes.ULONG), ("CacheTransitionCount", wintypes.ULONG),
        ("DemandZeroCount", wintypes.ULONG), ("PageReadCount", wintypes.ULONG),
        ("PageReadIoCount", wintypes.ULONG), ("CacheReadCount", wintypes.ULONG),
        ("CacheIoCount", wintypes.ULONG), ("DirtyPagesWriteCount", wintypes.ULONG),
        ("DirtyWriteIoCount", wintypes.ULONG), ("MappedPagesWriteCount", wintypes.ULONG),
        ("MappedWriteIoCount", wintypes.ULONG), ("PagedPoolPages", wintypes.ULONG),
        ("NonPagedPoolPages", wintypes.ULONG), ("PagedPoolAllocs", wintypes.ULONG),
        ("PagedPoolFrees", wintypes.ULONG), ("NonPagedPoolAllocs", wintypes.ULONG),
        ("NonPagedPoolFrees", wintypes.ULONG), ("FreeSystemPtes", wintypes.ULONG),
        ("ResidentSystemCodePage", wintypes.ULONG),
        ("TotalSystemDriverPages", wintypes.ULONG),
        ("TotalSystemCodePages", wintypes.ULONG),
        ("NonPagedPoolLookasideHits", wintypes.ULONG),
        ("PagedPoolLookasideHits", wintypes.ULONG),
        ("AvailablePagedPoolPages", wintypes.ULONG),
        ("ResidentSystemCachePage", wintypes.ULONG),
        ("ResidentPagedPoolPage", wintypes.ULONG),
        ("ResidentSystemDriverPage", wintypes.ULONG),
        ("CcFastReadNoWait", wintypes.ULONG), ("CcFastReadWait", wintypes.ULONG),
        ("CcFastReadResourceMiss", wintypes.ULONG),
        ("CcFastReadNotPossible", wintypes.ULONG),
        ("CcFastMdlReadNoWait", wintypes.ULONG), ("CcFastMdlReadWait", wintypes.ULONG),
        ("CcFastMdlReadResourceMiss", wintypes.ULONG),
        ("CcFastMdlReadNotPossible", wintypes.ULONG),
        ("CcMapDataNoWait", wintypes.ULONG), ("CcMapDataWait", wintypes.ULONG),
        ("CcMapDataNoWaitMiss", wintypes.ULONG), ("CcMapDataWaitMiss", wintypes.ULONG),
        ("CcPinMappedDataCount", wintypes.ULONG), ("CcPinReadNoWait", wintypes.ULONG),
        ("CcPinReadWait", wintypes.ULONG), ("CcPinReadNoWaitMiss", wintypes.ULONG),
        ("CcPinReadWaitMiss", wintypes.ULONG), ("CcCopyReadNoWait", wintypes.ULONG),
        ("CcCopyReadWait", wintypes.ULONG), ("CcCopyReadNoWaitMiss", wintypes.ULONG),
        ("CcCopyReadWaitMiss", wintypes.ULONG), ("CcMdlReadNoWait", wintypes.ULONG),
        ("CcMdlReadWait", wintypes.ULONG), ("CcMdlReadNoWaitMiss", wintypes.ULONG),
        ("CcMdlReadWaitMiss", wintypes.ULONG), ("CcReadAheadIos", wintypes.ULONG),
        ("CcLazyWriteIos", wintypes.ULONG), ("CcLazyWritePages", wintypes.ULONG),
        ("CcDataFlushes", wintypes.ULONG), ("CcDataPages", wintypes.ULONG),
        ("ContextSwitches", wintypes.ULONG), ("FirstLevelTbFills", wintypes.ULONG),
        ("SecondLevelTbFills", wintypes.ULONG), ("SystemCalls", wintypes.ULONG),
    ]


SYSTEM_PERFORMANCE_INFORMATION_CLASS = 2


def get_system_perf():
    """整機層級的效能計數器。回傳 None 表示無法取得（不捏造）。"""
    buf = _SYSTEM_PERFORMANCE_INFORMATION()
    ret = wintypes.ULONG(0)
    st = _ntdll.NtQuerySystemInformation(
        SYSTEM_PERFORMANCE_INFORMATION_CLASS, ctypes.byref(buf),
        ctypes.sizeof(buf), ctypes.byref(ret))
    if st != 0:
        return None
    # 結構版面若在未來 Windows 改變，回傳長度會不符——此時拒用而非誤讀。
    if ret.value != ctypes.sizeof(buf):
        log(f"SystemPerformanceInformation 長度不符（{ret.value} != "
            f"{ctypes.sizeof(buf)}），不輸出 UCD systemStats")
        return None
    return buf


def get_cpu_times_total() -> dict | None:
    """全機累計的 CPU 時間與中斷數（100ns 單位）。

    UCD 的 ssCpuRaw* 單位是 **USER_HZ（1/100 秒）**，與 Windows 的
    100ns 單位差 10^5 倍，換算時不可搞錯——否則 LibreNMS 的
    Detailed Processor Usage 會顯示荒謬的百分比。
    """
    ncpu = os.cpu_count() or 1
    arr = (_SPPI * ncpu)()
    ret = wintypes.ULONG(0)
    if _ntdll.NtQuerySystemInformation(8, ctypes.byref(arr),
                                       ctypes.sizeof(arr), ctypes.byref(ret)) != 0:
        return None
    idle = sum(arr[i].IdleTime.QuadPart for i in range(ncpu))
    kernel = sum(arr[i].KernelTime.QuadPart for i in range(ncpu))
    user = sum(arr[i].UserTime.QuadPart for i in range(ncpu))
    intr = sum(arr[i].InterruptTime.QuadPart for i in range(ncpu))
    icount = sum(arr[i].InterruptCount for i in range(ncpu))
    # KernelTime 已含 idle（Windows 的慣例），system 時間需扣掉
    return {"idle": idle, "system": max(kernel - idle, 0), "user": user,
            "interrupt": intr, "interrupt_count": icount}


# ------------------------------------------------- 程序數（hrSystemProcesses）
TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_k32.Process32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32)]
_k32.Process32First.restype = wintypes.BOOL
_k32.Process32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32)]
_k32.Process32Next.restype = wintypes.BOOL


def get_process_count() -> int:
    """hrSystemProcesses。只數數量，不列舉細節。

    spec §3.5：完整的 hrSWRunTable 是資訊揭露來源（哪套 EDR 在跑、裝在哪），
    預設停用。但**單純的程序數量**沒有揭露價值，而 LibreNMS 的
    System → Processes 圖需要它。
    """
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return 0
    try:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(pe)
        if not _k32.Process32First(snap, ctypes.byref(pe)):
            return 0
        n = 1
        while _k32.Process32Next(snap, ctypes.byref(pe)):
            n += 1
        return n
    finally:
        _k32.CloseHandle(snap)


# --------------------------------------------------- 實體磁碟（hrDiskStorage）
class _STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [("PropertyId", ctypes.c_int), ("QueryType", ctypes.c_int),
                ("AdditionalParameters", ctypes.c_ubyte * 1)]


class _STORAGE_DEVICE_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                ("DeviceType", ctypes.c_ubyte), ("DeviceTypeModifier", ctypes.c_ubyte),
                ("RemovableMedia", ctypes.c_ubyte), ("CommandQueueing", ctypes.c_ubyte),
                ("VendorIdOffset", wintypes.DWORD), ("ProductIdOffset", wintypes.DWORD),
                ("ProductRevisionOffset", wintypes.DWORD),
                ("SerialNumberOffset", wintypes.DWORD),
                ("BusType", ctypes.c_int), ("RawPropertiesLength", wintypes.DWORD)]


IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
_BUS_TYPE = {0: "Unknown", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA",
             6: "Fibre", 7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA",
             12: "SD", 13: "MMC", 17: "NVMe"}


def _asciiz(buf: bytes, off: int) -> str:
    """從 descriptor buffer 的位移取出 NUL 結尾字串。offset 為 0 代表無此欄位。"""
    if not off or off >= len(buf):
        return ""
    end = buf.find(b"\x00", off)
    return buf[off:end if end >= 0 else len(buf)].decode("ascii", "replace").strip()


def get_physical_disks() -> list[dict]:
    """列舉實體磁碟並取得型號、序號、容量、匯流排類型。

    spec §4.5：handle 需快取，反覆開關實體磁碟裝置是浪費。
    此處為 inventory 用途，取一次即可（§2.7：硬體 inventory 永久快取）。
    """
    out = []
    for n in range(16):
        h = _k32.CreateFileW(f"\\\\.\\PhysicalDrive{n}", 0, FILE_SHARE_RW,
                             None, OPEN_EXISTING, 0, None)
        if not h or h == INVALID_HANDLE:
            continue
        try:
            info = {"index": n, "model": "", "serial": "", "bus": "",
                    "removable": False, "size_bytes": 0}
            # 裝置描述子：型號 / 序號 / 匯流排
            q = _STORAGE_PROPERTY_QUERY()
            q.PropertyId = 0          # StorageDeviceProperty
            q.QueryType = 0           # PropertyStandardQuery
            buf = ctypes.create_string_buffer(1024)
            ret = wintypes.DWORD(0)
            if _k32.DeviceIoControl(h, IOCTL_STORAGE_QUERY_PROPERTY,
                                    ctypes.byref(q), ctypes.sizeof(q),
                                    buf, ctypes.sizeof(buf), ctypes.byref(ret), None):
                raw = buf.raw[:ret.value]
                d = _STORAGE_DEVICE_DESCRIPTOR.from_buffer_copy(raw)
                vendor = _asciiz(raw, d.VendorIdOffset)
                product = _asciiz(raw, d.ProductIdOffset)
                info["model"] = (vendor + " " + product).strip() or product or vendor
                info["serial"] = _asciiz(raw, d.SerialNumberOffset)
                info["bus"] = _BUS_TYPE.get(d.BusType, str(d.BusType))
                info["removable"] = bool(d.RemovableMedia)
            # 容量：IOCTL_DISK_GET_LENGTH_INFO 需要 FILE_READ_ACCESS，
            # 但我們刻意以 dwDesiredAccess=0 開檔（最小權限，spec §3.6）。
            # GET_DRIVE_GEOMETRY_EX 在零存取權下即可取得，故列為主要來源。
            geo = ctypes.create_string_buffer(64)
            if _k32.DeviceIoControl(h, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, None, 0,
                                    geo, ctypes.sizeof(geo), ctypes.byref(ret), None):
                # DISK_GEOMETRY_EX: DISK_GEOMETRY Geometry(24 bytes); LARGE_INTEGER DiskSize
                info["size_bytes"] = int(struct.unpack_from("<q", geo.raw, 24)[0])
            if not info["size_bytes"]:
                length = ctypes.c_ulonglong(0)
                if _k32.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO, None, 0,
                                        ctypes.byref(length), ctypes.sizeof(length),
                                        ctypes.byref(ret), None):
                    info["size_bytes"] = int(length.value)
            # 溫度與健康度（spec §2.9：原生路徑，不需核心驅動）。
            # 各家韌體對這兩個 IOCTL 的支援程度差異極大，單一裝置的異常
            # 不得讓整個磁碟列舉失敗（spec §6.7 啟動絕不硬失敗）。
            # 溫度與健康度交給 diskhealth 模組（多路徑，見該模組說明）。
            # 這裡不共用 handle：diskhealth 需要 READ|WRITE 才能下 SMART，
            # 而本函式刻意以最小權限開檔（spec §3.6）。
            try:
                import diskhealth
                hl = diskhealth.probe(n)
                if hl:
                    if hl.get("temp_c"):
                        info["temp_c"] = hl["temp_c"]
                    info["health"] = hl
            except Exception as exc:  # noqa: BLE001
                log(f"PhysicalDrive{n} 溫度/健康度查詢失敗: {exc!r}")
            if not info["model"]:
                info["model"] = f"PhysicalDrive{n}"
            out.append(info)
        finally:
            _k32.CloseHandle(h)
    return out


# --------------------------------------------------------------- IF-MIB
IF_MAX_STRING_SIZE = 256
IF_MAX_PHYS_ADDRESS_LENGTH = 32


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_ulonglong),
        ("InterfaceIndex", wintypes.ULONG),
        ("InterfaceGuid", _GUID),
        ("Alias", wintypes.WCHAR * (IF_MAX_STRING_SIZE + 1)),
        ("Description", wintypes.WCHAR * (IF_MAX_STRING_SIZE + 1)),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("PhysicalAddress", ctypes.c_ubyte * IF_MAX_PHYS_ADDRESS_LENGTH),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * IF_MAX_PHYS_ADDRESS_LENGTH),
        ("Mtu", wintypes.ULONG),
        ("Type", wintypes.ULONG),
        ("TunnelType", ctypes.c_int),
        ("MediaType", ctypes.c_int),
        ("PhysicalMediumType", ctypes.c_int),
        ("AccessType", ctypes.c_int),
        ("DirectionType", ctypes.c_int),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_int),
        ("AdminStatus", ctypes.c_int),
        ("MediaConnectState", ctypes.c_int),
        ("NetworkGuid", _GUID),
        ("ConnectionType", ctypes.c_int),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
        ("InOctets", ctypes.c_ulonglong),
        ("InUcastPkts", ctypes.c_ulonglong),
        ("InNUcastPkts", ctypes.c_ulonglong),
        ("InDiscards", ctypes.c_ulonglong),
        ("InErrors", ctypes.c_ulonglong),
        ("InUnknownProtos", ctypes.c_ulonglong),
        ("InUcastOctets", ctypes.c_ulonglong),
        ("InMulticastOctets", ctypes.c_ulonglong),
        ("InBroadcastOctets", ctypes.c_ulonglong),
        ("OutOctets", ctypes.c_ulonglong),
        ("OutUcastPkts", ctypes.c_ulonglong),
        ("OutNUcastPkts", ctypes.c_ulonglong),
        ("OutDiscards", ctypes.c_ulonglong),
        ("OutErrors", ctypes.c_ulonglong),
        ("OutUcastOctets", ctypes.c_ulonglong),
        ("OutMulticastOctets", ctypes.c_ulonglong),
        ("OutBroadcastOctets", ctypes.c_ulonglong),
        ("OutQLen", ctypes.c_ulonglong),
    ]


class MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", wintypes.ULONG), ("Table", MIB_IF_ROW2 * 1)]


_iph.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.POINTER(MIB_IF_TABLE2))]
_iph.GetIfTable2.restype = wintypes.DWORD
_iph.FreeMibTable.argtypes = [ctypes.c_void_p]
_iph.FreeMibTable.restype = None

IF_TYPE_SOFTWARE_LOOPBACK = 24


# --------------------------------------------------- SNMP engine 身分與時間
ENGINE_FILE = os.path.join(STATE_DIR, "state", "engine.json")


def _extend_index(token: str) -> tuple[int, ...]:
    """NET-SNMP-EXTEND-MIB 各表以 nsExtendToken（OCTET STRING）為索引。

    SMI 的字串索引編碼是「長度 + 每個位元組一個子識別碼」，
    所以 "smart" → (5, 115, 109, 97, 114, 116)。
    LibreNMS 端對應 `Oid::encodeString('smart')`。
    """
    raw = token.encode("ascii", errors="ignore")
    return (len(raw),) + tuple(raw)


def _machine_guid() -> str:
    """取 Windows 的 MachineGuid，作為 engineID 的穩定來源。

    engineID 必須跨重開機、跨服務重新啟動保持不變（RFC 3411），
    SNMPv3 的使用者金鑰是以它做 localization 的——變了就全部失效。
    """
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return str(winreg.QueryValueEx(k, "MachineGuid")[0])
    except Exception:  # noqa: BLE001 - 註冊表不可用時退回主機名稱
        return socket.gethostname()


def _engine_id() -> bytes:
    """RFC 3411 §5 的 SnmpEngineID。

    格式：最高位元為 1 表示採用 RFC 3411 的新格式，其後 31 位是企業編號，
    第 5 個位元組是格式碼（4 = 管理者自訂文字），之後最多 27 位元組。

    這裡用 MachineGuid 的雜湊而非 GUID 原文：GUID 是 36 個字元，超過長度上限，
    而雜湊同樣穩定且長度固定。PEN 目前是暫用值，取得正式 PEN 後會變動——
    屆時 v3 使用者需重新設定，這點要寫進升級說明。
    """
    pen = 99999
    head = bytes([(pen >> 24) & 0xFF | 0x80, (pen >> 16) & 0xFF,
                  (pen >> 8) & 0xFF, pen & 0xFF, 4])
    digest = hashlib.sha256(_machine_guid().encode("utf-8")).digest()[:16]
    return head + digest


def _engine_boots() -> int:
    """snmpEngineBoots：本機每次**開機**加一。

    RFC 3414 要求 (snmpEngineBoots, snmpEngineTime) 這組值永不重複。
    我們把 snmpEngineTime 定義為「系統開機至今的秒數」而非「服務啟動至今」，
    理由見 snmpEngineTime 的輸出處——服務重新啟動時時間不歸零，因此開機次數
    不必跟著加，兩者合起來仍嚴格遞增。

    以「開機時刻」判定是否換了一次開機：開機時刻改變 → 計數加一。
    讀寫失敗一律回退為 1，絕不讓它中斷快照建置。
    """
    try:
        boot_ms = int(time.time() * 1000) - int(_k32.GetTickCount64())
        # 容忍百毫秒級誤差，否則每次取樣都會判定成新的一次開機
        boot_key = boot_ms // 10000
        data = {}
        try:
            with open(ENGINE_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeError):
            pass
        if data.get("boot_key") != boot_key:
            data = {"schema_version": 1, "boot_key": boot_key,
                    "boots": int(data.get("boots", 0)) + 1}
            os.makedirs(os.path.dirname(ENGINE_FILE), exist_ok=True)
            tmp = ENGINE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, ENGINE_FILE)
        return max(1, min(int(data.get("boots", 1)), 2147483647))
    except Exception as exc:  # noqa: BLE001
        log(f"snmpEngineBoots 讀寫失敗，回退為 1: {exc!r}")
        return 1


MAXTEMP_FILE = os.path.join(STATE_DIR, "state", "disk-maxtemp.json")
_maxtemp_cache: dict[str, int] | None = None


def observed_max_temp(name: str, current: int | None) -> int | None:
    """記錄並回傳某顆磁碟**觀測到的**最高溫。

    LibreNMS 的 smart 應用程式有一張 Max Temp 圖，來源是 JSON 裡的 `max_temp`。
    Windows 的儲存 API 只給門檻值（warning / critical），沒有「這輩子最高溫」，
    拿門檻值去填是標錯標籤。改記錄我們自己量到的最高值——語意是
    「jt-snmpd 安裝以來觀測到的最高溫」，是真的量到的數字。

    只有在最高溫真的上升時才寫檔：快照每 5 秒重建一次，每次都寫會是
    一天一萬七千次不必要的磁碟寫入，違反「不得拖慢 host」。
    """
    global _maxtemp_cache
    if _maxtemp_cache is None:
        try:
            with open(MAXTEMP_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            _maxtemp_cache = {str(k): int(v) for k, v in data.get("disks", {}).items()
                              if isinstance(v, int) and 0 < v < 150}
        except (OSError, ValueError, UnicodeError, TypeError):
            _maxtemp_cache = {}

    prev = _maxtemp_cache.get(name)
    if current is None or not (0 < current < 150):
        return prev
    if prev is not None and current <= prev:
        return prev

    _maxtemp_cache[name] = current
    try:
        os.makedirs(os.path.dirname(MAXTEMP_FILE), exist_ok=True)
        tmp = MAXTEMP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 1, "disks": _maxtemp_cache}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, MAXTEMP_FILE)
    except OSError as exc:
        log(f"disk-maxtemp 寫入失敗（不影響其餘功能）: {exc!r}")
    return current


def _load_index_map() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, UnicodeError):
        return {"schema_version": 1, "interfaces": {}, "next_if_index": 1}


def _save_index_map(m: dict) -> None:
    """spec §6.6：temp → flush → 原子取代，保留 .bak。index-map 損毀是最貴的失效模式。"""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=1, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(STATE_FILE):
            try:
                os.replace(STATE_FILE, STATE_FILE + ".bak")
            except OSError:
                pass
        os.replace(tmp, STATE_FILE)
    except (OSError, UnicodeError) as exc:
        log(f"index-map 寫入失敗: {exc!r}")


def get_interfaces() -> list[dict]:
    """spec §2.4：預設只輸出硬體介面（Hyper-V host 會回 40~80 個介面，
    含 WFP LightWeight Filter / Teredo / isatap，全輸出會產生大量無用 port 與孤兒 RRD）。
    spec §2.5：ifIndex 以 NET_LUID 為主鍵持久化，Windows InterfaceIndex 不保證跨重開機穩定。"""
    ptr = ctypes.POINTER(MIB_IF_TABLE2)()
    if _iph.GetIfTable2(ctypes.byref(ptr)) != 0:
        return []
    try:
        n = ptr.contents.NumEntries
        base = ctypes.addressof(ptr.contents.Table)
        rows = (MIB_IF_ROW2 * n).from_address(base)
        imap = _load_index_map()
        changed = False
        out = []
        for r in rows:
            flags = r.InterfaceAndOperStatusFlags
            hardware = bool(flags & 0x01)          # bit0 HardwareInterface
            filt = bool(flags & 0x02)              # bit1 FilterInterface
            if not hardware or filt:
                continue
            if r.Type == IF_TYPE_SOFTWARE_LOOPBACK:
                continue
            # NIC teaming / SET：team 介面與其成員都會回報 HardwareInterface=TRUE，
            # 兩者都輸出會讓 LibreNMS 對同一份流量計算兩次。
            # NDIS 對 team 成員的 ConnectionType 標為 Passive(2)，
            # 對正常介面與 team 本體標為 Dedicated(1)。
            # 參考 NET_IF_CONNECTION_TYPE：Dedicated=1 Passive=2 Demand=3
            if r.ConnectionType == 2:
                log(f"介面 {r.Alias!r} 為 team 成員（ConnectionType=Passive），不輸出")
                continue
            luid = f"{r.InterfaceLuid:016x}"
            ent = imap["interfaces"].get(luid)
            if ent is None:
                ent = {"if_index": imap["next_if_index"], "if_name": r.Alias,
                       "first_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                imap["interfaces"][luid] = ent
                imap["next_if_index"] += 1
                changed = True
            mac = bytes(r.PhysicalAddress[:r.PhysicalAddressLength])
            out.append({
                "idx": ent["if_index"], "alias": r.Alias or f"if{ent['if_index']}",
                # Windows 原生 InterfaceIndex。IP / ARP / 鄰居表都以它為索引，
                # 但我們的 ifTable 用 LUID 持久化索引（spec §2.5），兩者必須對映，
                # 否則 LibreNMS 會把 IP 綁到錯誤的 port 或完全找不到——實測踩過：
                # 127.0.0.1 的 Windows 索引剛好是 1，被誤綁到「乙太網路」。
                "win_idx": int(r.InterfaceIndex),
                "descr": r.Description or r.Alias, "type": int(r.Type), "mtu": int(r.Mtu),
                "speed": int(r.TransmitLinkSpeed), "mac": mac,
                "oper": int(r.OperStatus), "admin": int(r.AdminStatus),
                "in_octets": int(r.InOctets), "in_ucast": int(r.InUcastPkts),
                "in_nucast": int(r.InNUcastPkts), "in_disc": int(r.InDiscards),
                "in_err": int(r.InErrors), "in_unk": int(r.InUnknownProtos),
                "in_mcast": int(r.InMulticastOctets), "in_bcast": int(r.InBroadcastOctets),
                "out_octets": int(r.OutOctets), "out_ucast": int(r.OutUcastPkts),
                "out_nucast": int(r.OutNUcastPkts), "out_disc": int(r.OutDiscards),
                "out_err": int(r.OutErrors), "out_qlen": int(r.OutQLen),
                "out_mcast": int(r.OutMulticastOctets), "out_bcast": int(r.OutBroadcastOctets),
            })
        if changed:
            _save_index_map(imap)
        out.sort(key=lambda x: x["idx"])
        return out
    finally:
        _iph.FreeMibTable(ptr)


# --------------------------------------------------------------- 系統識別
# DsRoleGetPrimaryDomainInformation 的角色代碼
DSROLE_STANDALONE_WORKSTATION = 0
DSROLE_MEMBER_WORKSTATION = 1
DSROLE_STANDALONE_SERVER = 2
DSROLE_MEMBER_SERVER = 3
DSROLE_BACKUP_DC = 4
DSROLE_PRIMARY_DC = 5


class _DSROLE_PRIMARY_DOMAIN_INFO_BASIC(ctypes.Structure):
    _fields_ = [("MachineRole", ctypes.c_int), ("Flags", wintypes.ULONG),
                ("DomainNameFlat", wintypes.LPWSTR), ("DomainNameDns", wintypes.LPWSTR),
                ("DomainForestName", wintypes.LPWSTR),
                ("DomainGuid", ctypes.c_ubyte * 16)]


def _is_domain_controller() -> bool:
    """以 DsRoleGetPrimaryDomainInformation 判定是否為網域控制站。

    spec §1.2 要求三個 sysObjectID 分支（工作站 / 伺服器 / 網域控制站），
    而 LibreNMS 的 Windows.php 正是靠第三個分支呼叫 getDatacenterVersion()。
    只分 client/server 會讓 DC 落到 server 分支，版本字串因此不同。

    此 API 在 netapi32.dll，非網域環境亦可安全呼叫（回傳 standalone 角色）。
    """
    try:
        netapi = ctypes.windll.netapi32
        netapi.DsRoleGetPrimaryDomainInformation.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        netapi.DsRoleGetPrimaryDomainInformation.restype = wintypes.DWORD
        netapi.DsRoleFreeMemory.argtypes = [ctypes.c_void_p]
        buf = ctypes.c_void_p()
        # DsRolePrimaryDomainInfoBasic = 1
        if netapi.DsRoleGetPrimaryDomainInformation(None, 1, ctypes.byref(buf)) != 0:
            return False
        try:
            info = ctypes.cast(
                buf, ctypes.POINTER(_DSROLE_PRIMARY_DOMAIN_INFO_BASIC)).contents
            return info.MachineRole in (DSROLE_BACKUP_DC, DSROLE_PRIMARY_DC)
        finally:
            netapi.DsRoleFreeMemory(buf)
    except Exception as exc:  # noqa: BLE001
        log(f"DC 判定失敗，視為非 DC: {exc!r}")
        return False


def get_product_type() -> str:
    """回傳 'client' / 'server' / 'domain_controller'。

    InstallationType 的可能值：Client、Server、Server Core、
    Windows Server Core（不同版本用詞不同），因此用 startswith("server")
    而非等值比較——Server Core 必須被認成 server（spec §9.3 的平台 DoD）。
    """
    is_server = False
    try:
        it = str(_reg(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
                      "InstallationType"))
        is_server = it.lower().startswith("server")
    except OSError:
        # 舊版或精簡安裝可能沒有此值，退回 ProductType 判斷：
        # ProductType 1=WinNT(工作站) 2=LanmanNT(DC) 3=ServerNT(伺服器)
        try:
            pt = str(_reg(r"SYSTEM\CurrentControlSet\Control\ProductOptions",
                          "ProductType"))
            if pt == "LanmanNT":
                return "domain_controller"
            is_server = pt == "ServerNT"
        except OSError:
            return "client"

    if is_server and _is_domain_controller():
        return "domain_controller"
    return "server" if is_server else "client"


def build_sysdescr() -> str:
    """spec §1.2：完全模仿 Microsoft SNMP Service 的格式，否則 LibreNMS 的
    Windows.php regex 不會 match，Hardware / Version / Features 三欄位會空白。
    實測：真實 MS SNMP 在 Win11 build 26200 回報 NT 版本 6.3（非 10.0）。"""
    try:
        cpu = str(_reg(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "Identifier"))
    except OSError:
        cpu = "x86 Family 0 Model 0 Stepping 0"
    try:
        build = str(_reg(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "CurrentBuildNumber"))
    except OSError:
        build = "0"
    smp = "Multiprocessor Free" if (os.cpu_count() or 1) > 1 else "Uniprocessor Free"
    return (f"Hardware: {cpu} AT/AT COMPATIBLE - "
            f"Software: Windows Version 6.3 (Build {build} {smp})")


class _WTS_SESSION_INFOW(ctypes.Structure):
    _fields_ = [("SessionId", wintypes.DWORD), ("pWinStationName", wintypes.LPWSTR),
                ("State", ctypes.c_int)]


# WTS_CONNECTSTATE_CLASS：0=Active 1=Connected 2=ConnectQuery 3=Shadow
# 4=Disconnected 5=Idle 6=Listen 7=Reset 8=Down 9=Init
_WTS_ACTIVE, _WTS_CONNECTED, _WTS_DISCONNECTED = 0, 1, 4
WTS_CURRENT_SERVER_HANDLE = 0


def get_session_count() -> int:
    """hrSystemNumUsers —— 實際的互動工作階段數。

    先前固定回 1，在遠端桌面工作階段主機（RDS）上直接就是錯的：
    一台可能有數十個使用者。整理 Windows Server 情境時發現，
    這不是「待驗證」而是「現在就錯」。

    只計算 Active 與 Disconnected 的工作階段——Disconnected 代表使用者
    仍登入但斷開連線，資源仍被佔用，RFC 2790 的 hrSystemNumUsers
    語意（登入的使用者數）應該包含它。Listen / Idle 等系統工作階段不計。
    """
    try:
        wts = ctypes.windll.wtsapi32
        wts.WTSEnumerateSessionsW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_WTS_SESSION_INFOW)),
            ctypes.POINTER(wintypes.DWORD)]
        wts.WTSEnumerateSessionsW.restype = wintypes.BOOL
        wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]

        ptr = ctypes.POINTER(_WTS_SESSION_INFOW)()
        count = wintypes.DWORD(0)
        if not wts.WTSEnumerateSessionsW(WTS_CURRENT_SERVER_HANDLE, 0, 1,
                                         ctypes.byref(ptr), ctypes.byref(count)):
            return 0
        try:
            n = 0
            for i in range(count.value):
                if ptr[i].State in (_WTS_ACTIVE, _WTS_DISCONNECTED):
                    n += 1
            return n
        finally:
            wts.WTSFreeMemory(ptr)
    except Exception as exc:  # noqa: BLE001
        log(f"WTSEnumerateSessions 失敗: {exc!r}")
        return 0


def _hr_system_date() -> bytes:
    """hrSystemDate 是 DateAndTime（RFC 2579）8 或 11 bytes 的二進位格式，
    不是文字。格式：年(2) 月 日 時 分 秒 秒/10 [時區方向 時 分]。"""
    lt = time.localtime()
    off = -(time.altzone if lt.tm_isdst else time.timezone)   # 秒，東為正
    sign = b"+" if off >= 0 else b"-"
    off = abs(off)
    return (lt.tm_year.to_bytes(2, "big")
            + bytes([lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, min(lt.tm_sec, 59), 0])
            + sign + bytes([off // 3600, (off % 3600) // 60]))


def uptime_centis() -> int:
    return int(_k32.GetTickCount64() // 10)


# --------------------------------------------------------------- Snapshot
# JT 私有 subtree。IANA PEN 尚未取得，暫用 Microsoft 相容前置碼下的保留分支，
# 取得 PEN 後遷移並提供對照表（spec §7.1）。
JT = (1, 3, 6, 1, 4, 1, 99999, 1)
JTAGENT = JT + (1,)          # 純量
JTCOLL = JT + (2, 1)         # jtAgentCollectorTable
# jtDiskHealthTable —— 磁碟健康狀態，供 LibreNMS 產生 state 類別感測器
# （綠燈／紅燈）。LibreNMS 的裝置概觀頁有 sensors 區塊但**沒有** applications
# 區塊，所以 SMART 應用程式的 (OK)/(FAIL) 只出現在 Apps 分頁；要讓健康狀態
# 直接顯示在概觀頁，唯一的路徑是 state 感測器，而那需要一個可對映的 OID。
JTDISK = JT + (3, 1)         # jtDiskHealthEntry
DISK_STATE_OK, DISK_STATE_WARNING, DISK_STATE_CRITICAL, DISK_STATE_UNKNOWN = 1, 2, 3, 4

SYS = (1, 3, 6, 1, 2, 1, 1)
IFT = (1, 3, 6, 1, 2, 1, 2, 2, 1)          # ifTable
IFX = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1)      # ifXTable（ifMIBObjects.ifXTable.ifXEntry）
HR = (1, 3, 6, 1, 2, 1, 25)
HRSTOR = HR + (2, 3, 1)                    # hrStorageTable
HRDEV = HR + (3, 2, 1)                     # hrDeviceTable
HRPROC = HR + (3, 3, 1)                    # hrProcessorTable
HRNET = HR + (3, 4, 1)                     # hrNetworkTable
HRPART = HR + (3, 7, 1)                    # hrPartitionTable
HRFS = HR + (3, 8, 1)                      # hrFSTable
IPROUTE = (1, 3, 6, 1, 2, 1, 4, 21, 1)     # RFC1213 ipRouteTable
HRDISK = HR + (3, 6, 1)                    # hrDiskStorageTable
HRDEVTYPE = HR + (3, 1)                    # hrDeviceTypes 前置碼
DIO = (1, 3, 6, 1, 4, 1, 2021, 13, 15, 1, 1)  # UCD-DISKIO
UCDLA = (1, 3, 6, 1, 4, 1, 2021, 10, 1)    # UCD laTable（負載平均）
UCDSS = (1, 3, 6, 1, 4, 1, 2021, 11)       # UCD systemStats
ENTPHY = (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1)   # ENTITY-MIB entPhysicalEntry
IPG = (1, 3, 6, 1, 2, 1, 4)                # IP-MIB ip group
ICMPG = (1, 3, 6, 1, 2, 1, 5)              # IP-MIB icmp group
TCPG = (1, 3, 6, 1, 2, 1, 6)               # TCP-MIB tcp group
UDPG = (1, 3, 6, 1, 2, 1, 7)               # UDP-MIB udp group
SNMPG = (1, 3, 6, 1, 2, 1, 11)             # SNMPv2-MIB snmp group
IPADDR = (1, 3, 6, 1, 2, 1, 4, 20, 1)      # RFC1213 ipAddrTable（IPv4，LibreNMS 主要來源）
IPADDRESS = (1, 3, 6, 1, 2, 1, 4, 34, 1)   # IP-MIB ipAddressTable（IPv4 + IPv6）
IPNETPHYS = (1, 3, 6, 1, 2, 1, 4, 35, 1)   # IP-MIB ipNetToPhysicalTable（ARP / ND）
ENTSENS = (1, 3, 6, 1, 2, 1, 99, 1, 1, 1)  # ENTITY-SENSOR-MIB entPhySensorEntry
ENT_SENSOR_BASE = 5000                     # entPhysicalIndex 分段：感測器
ENT_THERMAL_BASE = 5500                    # entPhysicalIndex 分段：ACPI 熱區
ENT_CPUFREQ_BASE = 5900                    # entPhysicalIndex 分段：CPU 頻率

# SNMP-FRAMEWORK-MIB snmpEngine 群組。
# **這組不是可有可無的**：LibreNMS 的 Core.php 以三個來源取 max() 決定 uptime，
#
#     max(round(sysUpTime/100),
#         bad_snmpEngineTime ? 0 : snmpEngineTime,
#         bad_hrSystemUptime ? 0 : round(hrSystemUptime/100))
#
# 而 windows.yaml **只設了 bad_hrSystemUptime**，沒有 bad_snmpEngineTime。
# sysUpTime 與 hrSystemUptime 都是 TimeTicks（Unsigned32，百分之一秒），
# 在 2^32/100 秒 ≈ 497.1 天必然回捲——回捲後數值驟降，LibreNMS 會判定
# 「Device rebooted」並發出假告警。snmpEngineTime 的單位是**秒**且範圍到
# 2147483647（約 68 年），提供它之後 max() 就有一個不回捲的來源，
# 假重開機警報因此消失。
SNMPFW = (1, 3, 6, 1, 6, 3, 10, 2, 1)      # snmpEngine 群組

# NET-SNMP-EXTEND-MIB。LibreNMS 的應用程式（smart 等）一律走這裡：
#   探索：walk nsExtendStatus
#   輪詢：get nsExtendOutputFull."<token>"
# 全程走 SNMP，被監控端不需要安裝 LibreNMS agent 或 smartctl。
NSEXT = (1, 3, 6, 1, 4, 1, 8072, 1, 3, 2)
NSEXT_CFG = NSEXT + (2, 1)                 # nsExtendConfigTable
NSEXT_OUT1 = NSEXT + (3, 1)                # nsExtendOutput1Table
NSEXT_OUT2 = NSEXT + (4, 1)                # nsExtendOutput2Table

# 單一 varbind 的位元組上限。回應上限是 1400 且不分片，一個超大的
# OCTET STRING 會讓 GET 直接放不進回應。SMART JSON 壓縮後通常 400~600
# 位元組，但磁碟很多時會成長——超過就砍磁碟數，且**必須留下記錄**，
# 不可無聲截斷（無聲截斷會讓人以為全部磁碟都在監控中）。
MAX_EXTEND_BYTES = 1100

# hrDeviceIndex 分段配置（spec §34.5 的精神：分段且持久化）。
# 196608 起是 Microsoft SNMP Service 對 CPU 的慣例，沿用以維持相容。
DEV_BASE_CPU = 196608
DEV_BASE_NET = 262144
DEV_BASE_DISK = 327680

# entPhysicalIndex 分段配置（spec §34.5）
ENT_SYSTEM = 1000
ENT_MAINBOARD = 1100
ENT_CPU_BASE = 2000
ENT_DIMM_BASE = 3000
ENT_DISK_BASE = 4000

# ENTITY-MIB entPhysicalClass
ENT_CLASS_OTHER, ENT_CLASS_CHASSIS, ENT_CLASS_MODULE = 1, 3, 9
ENT_CLASS_CPU = 12


def build_snapshot() -> tuple[tuple, tuple]:
    pairs: list[tuple[tuple, object]] = []
    add = lambda oid, val: pairs.append((tuple(oid), val))  # noqa: E731

    ptype = get_product_type()
    # spec §1.2：三個分支對應 LibreNMS Windows.php 的三條版本查表路徑
    sysobjid = {
        "client": (1, 3, 6, 1, 4, 1, 311, 1, 1, 3, 1, 1),
        "server": (1, 3, 6, 1, 4, 1, 311, 1, 1, 3, 1, 2),
        "domain_controller": (1, 3, 6, 1, 4, 1, 311, 1, 1, 3, 1, 3),
    }.get(ptype, (1, 3, 6, 1, 4, 1, 311, 1, 1, 3, 1, 1))
    host = os.environ.get("COMPUTERNAME", "windows")
    up = uptime_centis()

    # --- system group ---
    add(SYS + (1, 0), octet(build_sysdescr()))
    add(SYS + (2, 0), rfc1902.ObjectIdentifier(sysobjid))
    add(SYS + (3, 0), rfc1902.TimeTicks(up & U32))     # spec §2.6：自然回捲
    # sysUpTime 是 TimeTicks，2^32 百分之一秒 ≈ 497.1 天必然回捲，這是 RFC 3418
    # 規定的型別，任何相容的 agent 都一樣（Windows 內建 SNMP 也回捲）。
    # 回捲本身無法避免，但**假重開機告警**可以：LibreNMS 取三個來源的 max()，
    # 而 snmpEngineTime 的單位是秒、上限 2147483647（約 68 年），不會回捲。
    # 提供它之後，sysUpTime 回捲時 max() 會改用 snmpEngineTime，數值持續遞增，
    # LibreNMS 的 `if ($uptime < $device->uptime)` 就不會成立。
    _engine_secs = min(int(_k32.GetTickCount64() // 1000), 2147483647)
    add(SNMPFW + (1, 0), rfc1902.OctetString(_engine_id()))   # snmpEngineID
    add(SNMPFW + (2, 0), rfc1902.Integer32(_engine_boots()))  # snmpEngineBoots
    add(SNMPFW + (3, 0), rfc1902.Integer32(_engine_secs))     # snmpEngineTime
    add(SNMPFW + (4, 0), rfc1902.Integer32(1400))             # snmpEngineMaxMessageSize
    add(SYS + (4, 0), octet(CFG["contact"]))
    add(SYS + (5, 0), octet(host))
    add(SYS + (6, 0), octet(CFG["location"]))
    add(SYS + (7, 0), rfc1902.Integer32(76))           # spec §1.2：固定 76
    for i, (orid, descr) in enumerate([
        ((1, 3, 6, 1, 2, 1, 1), "SNMPv2-MIB"),
        ((1, 3, 6, 1, 2, 1, 2), "IF-MIB"),
        ((1, 3, 6, 1, 2, 1, 25, 1), "HOST-RESOURCES-MIB"),
    ], start=1):
        add(SYS + (9, 1, 2, i), rfc1902.ObjectIdentifier(orid))
        add(SYS + (9, 1, 3, i), octet(descr))
        add(SYS + (9, 1, 4, i), rfc1902.TimeTicks(0))

    # --- IF-MIB ---
    ifaces = _collector("interfaces", get_interfaces, [])
    add((1, 3, 6, 1, 2, 1, 2, 1, 0), rfc1902.Integer32(len(ifaces)))   # ifNumber
    for nic in ifaces:
        i = nic["idx"]
        speed = nic["speed"]
        add(IFT + (1, i), rfc1902.Integer32(i))                         # ifIndex
        add(IFT + (2, i), octet(nic["descr"]))            # ifDescr
        add(IFT + (3, i), rfc1902.Integer32(nic["type"]))               # ifType
        add(IFT + (4, i), rfc1902.Integer32(min(nic["mtu"], INT32_MAX)))  # ifMtu
        add(IFT + (5, i), rfc1902.Gauge32(min(speed, U32)))             # ifSpeed
        add(IFT + (6, i), octet(nic["mac"]))              # ifPhysAddress
        add(IFT + (7, i), rfc1902.Integer32(nic["admin"]))              # ifAdminStatus
        add(IFT + (8, i), rfc1902.Integer32(nic["oper"]))               # ifOperStatus
        add(IFT + (9, i), rfc1902.TimeTicks(0))                         # ifLastChange
        add(IFT + (10, i), rfc1902.Counter32(nic["in_octets"] & U32))
        add(IFT + (11, i), rfc1902.Counter32(nic["in_ucast"] & U32))
        add(IFT + (12, i), rfc1902.Counter32(nic["in_nucast"] & U32))
        add(IFT + (13, i), rfc1902.Counter32(nic["in_disc"] & U32))
        add(IFT + (14, i), rfc1902.Counter32(nic["in_err"] & U32))
        add(IFT + (15, i), rfc1902.Counter32(nic["in_unk"] & U32))
        add(IFT + (16, i), rfc1902.Counter32(nic["out_octets"] & U32))
        add(IFT + (17, i), rfc1902.Counter32(nic["out_ucast"] & U32))
        add(IFT + (18, i), rfc1902.Counter32(nic["out_nucast"] & U32))
        add(IFT + (19, i), rfc1902.Counter32(nic["out_disc"] & U32))
        add(IFT + (20, i), rfc1902.Counter32(nic["out_err"] & U32))
        add(IFT + (21, i), rfc1902.Gauge32(min(nic["out_qlen"], U32)))
        add(IFT + (22, i), rfc1902.ObjectIdentifier((0, 0)))            # ifSpecific
        # ifXTable — LibreNMS 的 windows.yaml 未設 bad_ifXEntry，會使用 64-bit counters
        add(IFX + (1, i), octet(nic["alias"]))            # ifName
        add(IFX + (2, i), rfc1902.Counter32(nic["in_mcast"] & U32))
        add(IFX + (3, i), rfc1902.Counter32(nic["in_bcast"] & U32))
        add(IFX + (4, i), rfc1902.Counter32(nic["out_mcast"] & U32))
        add(IFX + (5, i), rfc1902.Counter32(nic["out_bcast"] & U32))
        add(IFX + (6, i), rfc1902.Counter64(nic["in_octets"]))          # ifHCInOctets
        add(IFX + (7, i), rfc1902.Counter64(nic["in_ucast"]))
        add(IFX + (10, i), rfc1902.Counter64(nic["out_octets"]))        # ifHCOutOctets
        add(IFX + (11, i), rfc1902.Counter64(nic["out_ucast"]))
        add(IFX + (15, i), rfc1902.Gauge32(speed // 1_000_000))         # ifHighSpeed (Mbps)
        add(IFX + (18, i), octet(""))                     # ifAlias
        add(IFX + (19, i), rfc1902.TimeTicks(0))                        # ifCounterDiscontinuityTime

    # --- HOST-RESOURCES: hrSystem（完整）---
    perf = _collector("perf_info", get_perf_info, None)
    add(HR + (1, 1, 0), rfc1902.TimeTicks(up & U32))                    # hrSystemUptime
    add(HR + (1, 2, 0), octet(_hr_system_date()))                       # hrSystemDate
    add(HR + (1, 3, 0), rfc1902.Integer32(0))                           # hrSystemInitialLoadDevice
    add(HR + (1, 4, 0), octet(""))                                      # hrSystemInitialLoadParameters
    add(HR + (1, 5, 0), rfc1902.Gauge32(
        _collector("sessions", get_session_count, 0)))                  # hrSystemNumUsers
    # hrSystemProcesses —— LibreNMS 的 System → Processes 圖靠這個。
    # 優先用 GetPerformanceInfo（單次呼叫、數十 µs）；不可用時才退回
    # Toolhelp32 快照（要列舉全部程序，300 個約 50–300 ms，spec §4.5 列為昂貴）。
    nproc = perf.ProcessCount if perf is not None else _collector(
        "processes", get_process_count, 0)
    add(HR + (1, 6, 0), rfc1902.Gauge32(nproc))
    add(HR + (1, 7, 0), rfc1902.Integer32(0))                           # hrSystemMaxProcesses (0=無限制)

    mem = _collector("memory", get_memory, None)
    if mem is None:
        mem = MEMORYSTATUSEX()
    add(HR + (2, 2, 0), rfc1902.Integer32(min(mem.ullTotalPhys // 1024, INT32_MAX)))

    # --- hrStorageTable ---
    # 記憶體池命名刻意對齊 net-snmp 的用語，LibreNMS 的 mempool 探索才會把它們
    # 歸到 Memory 頁的正確類別（system / virtual / cached / buffers / shared / swap）。
    rows = [("Physical Memory", HR + (2, 1, 2), mem.ullTotalPhys,
             mem.ullTotalPhys - mem.ullAvailPhys),
            ("Virtual Memory", HR + (2, 1, 3), mem.ullTotalPageFile,
             mem.ullTotalPageFile - mem.ullAvailPageFile)]

    if perf is not None:
        page = perf.PageSize or 4096
        commit_limit = perf.CommitLimit * page
        commit_total = perf.CommitTotal * page
        phys_total = perf.PhysicalTotal * page
        cache = perf.SystemCache * page
        kpaged = perf.KernelPaged * page
        knonpaged = perf.KernelNonpaged * page

        # Cached Memory —— Windows 的系統檔案快取，對應 net-snmp 的 "Cached memory"。
        # 快取本質上「已使用但可回收」，故 used == total（與 net-snmp 一致）。
        rows.append(("Cached Memory", HR + (2, 1, 1), cache, cache))

        # Swap space —— 分頁檔的部分。Windows 的 commit limit 是
        # 實體記憶體 + 分頁檔，故分頁檔大小 = commit limit - 實體記憶體。
        # 這與「Virtual Memory」（= commit charge）是不同概念，不可混用（spec §2.2）。
        swap_total = max(commit_limit - phys_total, 0)
        swap_used = max(commit_total - (phys_total - mem.ullAvailPhys), 0)
        if swap_total:
            rows.append(("Swap Space", HR + (2, 1, 3),
                         swap_total, min(swap_used, swap_total)))

        # 核心集區。Windows 特有，但 hrStorageOther 是 RFC 2790 給這類項目的位置。
        if kpaged:
            rows.append(("Kernel Paged Pool", HR + (2, 1, 1), kpaged, kpaged))
        if knonpaged:
            rows.append(("Kernel Nonpaged Pool", HR + (2, 1, 1), knonpaged, knonpaged))
    _vols = _collector("volumes", get_fixed_volumes, [])
    for vol in _vols:
        # 描述格式刻意**不**沿用 Microsoft SNMP Service 的
        #   "C: Label:xxx  Serial Number 1A2B3C4D"
        # 序號對監控沒有意義，"Label:" 也只是雜訊。改為：
        #   有標籤 → "C: 系統碟"   無標籤 → "C:"
        # 序號仍可從 ENTITY-MIB 的 entPhysicalSerialNum 取得，資訊沒有遺失。
        # 標籤可能是中文，一律經 octet() 編成 UTF-8。
        drive = vol["root"].rstrip("\\")           # "C:\\" -> "C:"
        descr = f"{drive} {vol['label']}" if vol["label"] else drive
        rows.append((descr, HR + (2, 1, 4), vol["total"], vol["used"]))
    for idx, (descr, stype, total, used) in enumerate(rows, start=1):
        unit = storage_units(total)
        add(HRSTOR + (1, idx), rfc1902.Integer32(idx))
        add(HRSTOR + (2, idx), rfc1902.ObjectIdentifier(stype))
        add(HRSTOR + (3, idx), octet(descr))
        add(HRSTOR + (4, idx), rfc1902.Integer32(unit))
        add(HRSTOR + (5, idx), rfc1902.Integer32(min(total // unit, INT32_MAX)))
        add(HRSTOR + (6, idx), rfc1902.Integer32(min(used // unit, INT32_MAX)))

    def _disk_label(disk) -> str:
        """磁碟的人類可辨識名稱。

        只給 "PhysicalDrive0" 在多顆磁碟的機器上完全看不出是哪一顆——
        使用者實測回報。型號是最有辨識度的資訊，序號次之（但序號太長，
        放在 ENTITY-MIB 的 entPhysicalSerialNum 即可，不塞進顯示名稱）。
        """
        n = disk["index"]
        model = (disk.get("model") or "").strip()
        if not model or model == f"PhysicalDrive{n}":
            return f"PhysicalDrive{n}"
        # 參考 Linux net-snmp 的風格（"/dev/sda: SATA CVB-CD256"）：
        # 「裝置: 型號」，不含容量。LibreNMS 的欄位寬度有限，
        # 過長會被截斷成 "...M.2 2280 256G" 這種看不出重點的字串——實測回報。
        # 去掉廠商重複字樣與容量尾綴，只留最有辨識度的型號。
        words = model.split()
        if len(words) > 1 and words[0].upper() == words[1].upper():
            words = words[1:]                       # "QEMU QEMU HARDDISK" → "QEMU HARDDISK"
        model = " ".join(words)
        for suffix in (" 2280", " M.2"):            # 尺寸規格對辨識沒幫助
            model = model.replace(suffix, "")
        model = model.strip()
        if len(model) > 28:
            model = model[:28].rstrip()
        return f"PhysicalDrive{n}: {model}"

    # --- hrDeviceTable 全家族 ---
    # spec §2.3：所有 hrDevice 衍生表（hrProcessor / hrNetwork / hrDiskStorage）
    # 一律共用同一組 hrDeviceIndex，不另建 index 體系。
    inv = _collector("inventory", get_inventory, {})

    # (a) 處理器 → hrProcessorTable
    cpu_name = _collector("cpu_name", get_cpu_name, "CPU")
    loads = _collector("cpu", get_cpu_loads, [])
    for i, load in enumerate(loads):
        di = DEV_BASE_CPU + i
        add(HRDEV + (1, di), rfc1902.Integer32(di))                      # hrDeviceIndex
        add(HRDEV + (2, di), rfc1902.ObjectIdentifier(HRDEVTYPE + (3,)))  # hrDeviceProcessor
        add(HRDEV + (3, di), octet(cpu_name))                            # hrDeviceDescr
        add(HRDEV + (4, di), rfc1902.ObjectIdentifier((0, 0)))           # hrDeviceID
        add(HRDEV + (5, di), rfc1902.Integer32(2))                       # running
        add(HRDEV + (6, di), rfc1902.Counter32(0))                       # hrDeviceErrors
        add(HRPROC + (1, di), rfc1902.ObjectIdentifier((0, 0)))          # hrProcessorFrwID
        add(HRPROC + (2, di), rfc1902.Integer32(load))                   # hrProcessorLoad

    # (b) 網路介面 → hrNetworkTable
    for nic in ifaces:
        di = DEV_BASE_NET + nic["idx"]
        add(HRDEV + (1, di), rfc1902.Integer32(di))
        add(HRDEV + (2, di), rfc1902.ObjectIdentifier(HRDEVTYPE + (4,)))  # hrDeviceNetwork
        add(HRDEV + (3, di), octet(nic["descr"]))
        add(HRDEV + (4, di), rfc1902.ObjectIdentifier((0, 0)))
        # hrDeviceStatus：介面 up 才算 running(2)，否則 down(5)
        add(HRDEV + (5, di), rfc1902.Integer32(2 if nic["oper"] == 1 else 5))
        add(HRDEV + (6, di), rfc1902.Counter32((nic["in_err"] + nic["out_err"]) & U32))
        add(HRNET + (1, di), rfc1902.Integer32(nic["idx"]))               # hrNetworkIfIndex

    # (c) 實體磁碟 → hrDiskStorageTable
    for disk in inv.get("disks", []):
        di = DEV_BASE_DISK + disk["index"]
        add(HRDEV + (1, di), rfc1902.Integer32(di))
        add(HRDEV + (2, di), rfc1902.ObjectIdentifier(HRDEVTYPE + (6,)))  # hrDeviceDiskStorage
        add(HRDEV + (3, di), octet(_disk_label(disk)))
        add(HRDEV + (4, di), rfc1902.ObjectIdentifier((0, 0)))
        add(HRDEV + (5, di), rfc1902.Integer32(2))
        add(HRDEV + (6, di), rfc1902.Counter32(0))
        add(HRDISK + (1, di), rfc1902.Integer32(1))                       # readWrite
        # hrDiskStorageMedia: 3=hardDisk。可移除裝置歸 other(1)，不猜測介質類型。
        add(HRDISK + (2, di), rfc1902.Integer32(1 if disk["removable"] else 3))
        add(HRDISK + (3, di), rfc1902.Integer32(1 if disk["removable"] else 2))  # TruthValue
        # hrDiskStorageCapacity 是 Integer32、單位 KB。> 2 TB 會溢位，
        # RFC 無 allocation unit 機制可用，故 clamp 並在文件說明（§2.1 同源問題）。
        add(HRDISK + (4, di), rfc1902.Integer32(min(disk["size_bytes"] // 1024, INT32_MAX)))

    # --- ENTITY-MIB entPhysicalTable（LibreNMS Inventory 頁）---
    # spec §2.10：資料來自 GetSystemFirmwareTable('RSMB')，不需 WMI、不需特權。
    def ent(idx, cls, descr, name, parent, relpos, *, serial="", mfg="", model="",
            hw="", fw="", sw="", fru=False):
        add(ENTPHY + (1, idx), rfc1902.Integer32(idx))                    # entPhysicalIndex
        add(ENTPHY + (2, idx), octet(descr))                              # entPhysicalDescr
        add(ENTPHY + (3, idx), rfc1902.ObjectIdentifier((0, 0)))          # entPhysicalVendorType
        add(ENTPHY + (4, idx), rfc1902.Integer32(parent))                 # entPhysicalContainedIn
        add(ENTPHY + (5, idx), rfc1902.Integer32(cls))                    # entPhysicalClass
        add(ENTPHY + (6, idx), rfc1902.Integer32(relpos))                 # entPhysicalParentRelPos
        add(ENTPHY + (7, idx), octet(name))                               # entPhysicalName
        add(ENTPHY + (8, idx), octet(hw))                                 # entPhysicalHardwareRev
        add(ENTPHY + (9, idx), octet(fw))                                 # entPhysicalFirmwareRev
        add(ENTPHY + (10, idx), octet(sw))                                # entPhysicalSoftwareRev
        add(ENTPHY + (11, idx), octet(serial))                            # entPhysicalSerialNum
        add(ENTPHY + (12, idx), octet(mfg))                               # entPhysicalMfgName
        add(ENTPHY + (13, idx), octet(model))                             # entPhysicalModelName
        add(ENTPHY + (14, idx), octet(""))                                # entPhysicalAlias
        add(ENTPHY + (15, idx), octet(""))                                # entPhysicalAssetID
        add(ENTPHY + (16, idx), rfc1902.Integer32(1 if fru else 2))       # entPhysicalIsFRU

    sysinfo = inv.get("system", {})
    bios = inv.get("bios", {})
    board = inv.get("baseboard", {})

    if sysinfo or bios:
        ent(ENT_SYSTEM, ENT_CLASS_CHASSIS,
            descr=f"{sysinfo.get('manufacturer','')} {sysinfo.get('product','')}".strip() or host,
            name=host, parent=0, relpos=-1,
            serial=sysinfo.get("serial", ""), mfg=sysinfo.get("manufacturer", ""),
            model=sysinfo.get("product", ""), hw=sysinfo.get("version", ""),
            fw=f"{bios.get('vendor','')} {bios.get('version','')}".strip(),
            sw=build_sysdescr().split(" - Software: ")[-1])

    if board.get("product") or board.get("manufacturer"):
        ent(ENT_MAINBOARD, ENT_CLASS_MODULE,
            descr=f"{board.get('manufacturer','')} {board.get('product','')}".strip(),
            name="Mainboard", parent=ENT_SYSTEM, relpos=1,
            serial=board.get("serial", ""), mfg=board.get("manufacturer", ""),
            model=board.get("product", ""), hw=board.get("version", ""), fru=True)

    for i, cpu in enumerate(inv.get("processors", [])):
        cores = cpu.get("core_count", 0)
        speed = cpu.get("max_speed_mhz", 0)
        detail = f" ({cores} cores, {speed} MHz)" if cores or speed else ""
        ent(ENT_CPU_BASE + i, ENT_CLASS_CPU,
            descr=(cpu.get("version") or cpu_name) + detail,
            name=cpu.get("socket") or f"CPU {i}",
            parent=ENT_MAINBOARD if board.get("product") else ENT_SYSTEM, relpos=i + 1,
            serial=cpu.get("serial", ""), mfg=cpu.get("manufacturer", ""),
            model=cpu.get("version", ""), fru=True)

    for i, dimm in enumerate(inv.get("memory", [])):
        size = dimm.get("size_mb", 0)
        speed = dimm.get("speed_mts", 0)
        detail = f"{size} MB" + (f" {speed} MT/s" if speed else "")
        ent(ENT_DIMM_BASE + i, ENT_CLASS_MODULE,
            descr=f"{dimm.get('part_number') or 'Memory'} {detail}".strip(),
            name=dimm.get("locator") or f"DIMM {i}",
            parent=ENT_MAINBOARD if board.get("product") else ENT_SYSTEM, relpos=i + 1,
            serial=dimm.get("serial", ""), mfg=dimm.get("manufacturer", ""),
            model=dimm.get("part_number", ""), fru=True)

    for disk in inv.get("disks", []):
        gb = disk["size_bytes"] // (1024 ** 3)
        ent(ENT_DISK_BASE + disk["index"], ENT_CLASS_MODULE,
            descr=f"{disk['model']} {gb} GB ({disk['bus']})".strip(),
            name=_disk_label(disk),
            parent=ENT_SYSTEM, relpos=disk["index"] + 1,
            serial=disk.get("serial", ""), model=disk.get("model", ""), fru=True)

    # --- UCD-DISKIO ---
    _dio_names = {d["index"]: _disk_label(d) for d in inv.get("disks", [])}
    for idx, (dn, rd, wr, rc, wc) in enumerate(_collector("diskio", get_disk_io, []), start=1):
        add(DIO + (1, idx), rfc1902.Integer32(idx))
        add(DIO + (2, idx), octet(f"PhysicalDrive{dn}"))
        add(DIO + (3, idx), rfc1902.Counter32(rd & U32))
        add(DIO + (4, idx), rfc1902.Counter32(wr & U32))
        add(DIO + (5, idx), rfc1902.Counter32(rc & U32))
        add(DIO + (6, idx), rfc1902.Counter32(wc & U32))
        add(DIO + (12, idx), rfc1902.Counter64(rd))
        add(DIO + (13, idx), rfc1902.Counter64(wr))

    # --- IP / ICMP / TCP / UDP 群組（LibreNMS Netstats 整組圖表）---
    ipst = _collector("ip_stats", get_ip_stats, None)
    if ipst is not None:
        for col, val, typ in [
            (1, ipst.Forwarding, "i"), (2, ipst.DefaultTTL, "i"),
            (3, ipst.InReceives, "c"), (4, ipst.InHdrErrors, "c"),
            (5, ipst.InAddrErrors, "c"), (6, ipst.ForwDatagrams, "c"),
            (7, ipst.InUnknownProtos, "c"), (8, ipst.InDiscards, "c"),
            (9, ipst.InDelivers, "c"), (10, ipst.OutRequests, "c"),
            (11, ipst.OutDiscards, "c"), (12, ipst.OutNoRoutes, "c"),
            (13, ipst.ReasmTimeout, "i"), (14, ipst.ReasmReqds, "c"),
            (15, ipst.ReasmOks, "c"), (16, ipst.ReasmFails, "c"),
            (17, ipst.FragOks, "c"), (18, ipst.FragFails, "c"),
            (19, ipst.FragCreates, "c"),
        ]:
            v = rfc1902.Counter32(val & U32) if typ == "c" else rfc1902.Integer32(min(val, INT32_MAX))
            add(IPG + (col, 0), v)

    icmp = _collector("icmp_stats", get_icmp_stats, None)
    if icmp is not None:
        i, o = icmp.InStats, icmp.OutStats
        # icmp group：1-13 為 In*，14-26 為 Out*，順序依 RFC 1213
        for col, val in enumerate([
            i.Msgs, i.Errors, i.DestUnreachs, i.TimeExcds, i.ParmProbs, i.SrcQuenchs,
            i.Redirects, i.Echos, i.EchoReps, i.Timestamps, i.TimestampReps,
            i.AddrMasks, i.AddrMaskReps,
            o.Msgs, o.Errors, o.DestUnreachs, o.TimeExcds, o.ParmProbs, o.SrcQuenchs,
            o.Redirects, o.Echos, o.EchoReps, o.Timestamps, o.TimestampReps,
            o.AddrMasks, o.AddrMaskReps], start=1):
            add(ICMPG + (col, 0), rfc1902.Counter32(val & U32))

    tcp = _collector("tcp_stats", get_tcp_stats, None)
    if tcp is not None:
        add(TCPG + (1, 0), rfc1902.Integer32(min(tcp.RtoAlgorithm, INT32_MAX)))
        add(TCPG + (2, 0), rfc1902.Integer32(min(tcp.RtoMin, INT32_MAX)))
        add(TCPG + (3, 0), rfc1902.Integer32(min(tcp.RtoMax, INT32_MAX)))
        # MaxConn 為 -1 代表動態配置；Windows 回傳 0xFFFFFFFF，需轉回 -1
        maxconn = -1 if tcp.MaxConn == 0xFFFFFFFF else min(tcp.MaxConn, INT32_MAX)
        add(TCPG + (4, 0), rfc1902.Integer32(maxconn))
        add(TCPG + (5, 0), rfc1902.Counter32(tcp.ActiveOpens & U32))
        add(TCPG + (6, 0), rfc1902.Counter32(tcp.PassiveOpens & U32))
        add(TCPG + (7, 0), rfc1902.Counter32(tcp.AttemptFails & U32))
        add(TCPG + (8, 0), rfc1902.Counter32(tcp.EstabResets & U32))
        add(TCPG + (9, 0), rfc1902.Gauge32(tcp.CurrEstab & U32))       # tcpCurrEstab
        add(TCPG + (10, 0), rfc1902.Counter32(tcp.InSegs & U32))
        add(TCPG + (11, 0), rfc1902.Counter32(tcp.OutSegs & U32))
        add(TCPG + (12, 0), rfc1902.Counter32(tcp.RetransSegs & U32))
        add(TCPG + (14, 0), rfc1902.Counter32(tcp.InErrs & U32))
        add(TCPG + (15, 0), rfc1902.Counter32(tcp.OutRsts & U32))

    udp = _collector("udp_stats", get_udp_stats, None)
    if udp is not None:
        add(UDPG + (1, 0), rfc1902.Counter32(udp.InDatagrams & U32))
        add(UDPG + (2, 0), rfc1902.Counter32(udp.NoPorts & U32))
        add(UDPG + (3, 0), rfc1902.Counter32(udp.InErrors & U32))
        add(UDPG + (4, 0), rfc1902.Counter32(udp.OutDatagrams & U32))

    # --- ipAddrTable / ipAddressTable（LibreNMS ipv4-addresses / ipv6-addresses）---
    def _oid_addr(raw: bytes) -> tuple:
        """把位址位元組展開成 OID 後綴（每個 byte 一個 sub-identifier）。"""
        return tuple(raw)

    def _prefix_mask(plen: int) -> str:
        """IPv4 前置碼長度 → 點分十進位遮罩（ipAdEntNetMask 需要）。"""
        m = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
        return ".".join(str((m >> sh) & 0xFF) for sh in (24, 16, 8, 0))

    # Windows 原生 InterfaceIndex → 我們的持久化 ifIndex。
    # 不在此對映中的位址（loopback、隧道、已過濾掉的虛擬介面）一律不輸出：
    # 指向不存在的 ifIndex 只會讓 LibreNMS 產生孤兒資料（spec §6.9 的精神）。
    _win2if = {n["win_idx"]: n["idx"] for n in ifaces if "win_idx" in n}

    addrs = _collector("ip_addresses", get_ip_addresses, [])
    for a in addrs:
        our_if = _win2if.get(a["if_index"])
        if our_if is None:
            continue
        idx = _oid_addr(a["raw"])
        if a["version"] == 4:
            # RFC1213 ipAddrTable —— LibreNMS 的 ipv4-addresses 主要讀這張
            add(IPADDR + (1,) + idx, rfc1902.IpAddress(a["addr"]))        # ipAdEntAddr
            add(IPADDR + (2,) + idx, rfc1902.Integer32(our_if))           # ipAdEntIfIndex
            add(IPADDR + (3,) + idx,
                rfc1902.IpAddress(_prefix_mask(a["prefix_len"])))         # ipAdEntNetMask
            add(IPADDR + (4,) + idx, rfc1902.Integer32(1))                # ipAdEntBcastAddr
            add(IPADDR + (5,) + idx, rfc1902.Integer32(65535))            # ipAdEntReasmMaxSize
        # IP-MIB ipAddressTable —— IPv4 與 IPv6 共用，index 為 (addrType, addr)
        atype = 1 if a["version"] == 4 else 2
        aidx = (atype, len(a["raw"])) + idx
        add(IPADDRESS + (3,) + aidx, rfc1902.Integer32(our_if))           # ipAddressIfIndex
        add(IPADDRESS + (4,) + aidx, rfc1902.Integer32(1))                # ipAddressType unicast
        add(IPADDRESS + (5,) + aidx, rfc1902.Integer32(a["prefix_len"]))  # 前置碼長度（簡化）
        add(IPADDRESS + (6,) + aidx, rfc1902.Integer32(1))                # ipAddressOrigin
        add(IPADDRESS + (7,) + aidx, rfc1902.Integer32(1))                # ipAddressStatus preferred
        add(IPADDRESS + (10,) + aidx, rfc1902.Integer32(1))               # ipAddressRowStatus

    # --- ipNetToPhysicalTable（ARP / IPv6 ND）---
    # spec §3.5：預設停用。內網 ARP 表 = 橫向移動的目標清單。
    if CFG.get("enable_arp_table"):
        for nb in _collector("ip_neighbors", get_ip_neighbors, []):
            nb_if = _win2if.get(nb["if_index"])
            if nb_if is None:
                continue
            atype = 1 if nb["version"] == 4 else 2
            nidx = (nb_if, atype, len(nb["raw"])) + _oid_addr(nb["raw"])
            add(IPNETPHYS + (2,) + nidx, octet(nb["mac"]))                # PhysAddress
            add(IPNETPHYS + (3,) + nidx, rfc1902.TimeTicks(0))            # LastUpdated
            add(IPNETPHYS + (4,) + nidx, rfc1902.Integer32(3))            # Type dynamic
            add(IPNETPHYS + (5,) + nidx, rfc1902.Integer32(nb["state"]))  # State
            add(IPNETPHYS + (6,) + nidx, rfc1902.Integer32(1))            # RowStatus active

    # --- hrPartitionTable + hrFSTable ---
    # 內建 SNMP 有這兩張表（實測 20 / 27 筆），我們原本完全沒有。
    # 它們沒有資訊揭露問題（都是本機自己的磁碟區），對照 spec §3.5 的
    # 揭露清單也不在其中，故預設輸出。
    HRFS_TYPE_NTFS = HR + (3, 9, 4)      # hrFSNTFS
    HRFS_TYPE_FAT32 = HR + (3, 9, 3)     # hrFSFat32（近似，RFC 未區分 FAT/FAT32）
    HRFS_TYPE_OTHER = HR + (3, 9, 1)     # hrFSOther
    _FS_TYPES = {"NTFS": HRFS_TYPE_NTFS, "FAT32": HRFS_TYPE_FAT32,
                 "FAT": HRFS_TYPE_FAT32, "REFS": HRFS_TYPE_OTHER}

    # hrPartition 的 index 是 (hrDeviceIndex, hrPartitionIndex)（spec §2.3 / RFC 2790）。
    # 磁碟區沒有可靠的「屬於哪顆實體磁碟」對映（Storage Spaces、動態磁碟、
    # 多重掛載都會打破一對一），因此全部掛在第一顆磁碟的 hrDeviceIndex 之下
    # 並在文件說明——回報錯誤的歸屬比回報「未知歸屬」更糟。
    _disks = inv.get("disks", [])
    _part_dev = DEV_BASE_DISK + (_disks[0]["index"] if _disks else 0)

    for pi, vol in enumerate(_vols, start=1):
        label = vol["label"] or vol["root"].rstrip("\\")
        add(HRPART + (1, _part_dev, pi), rfc1902.Integer32(pi))          # hrPartitionIndex
        add(HRPART + (2, _part_dev, pi), octet(label))                   # hrPartitionLabel
        add(HRPART + (3, _part_dev, pi), octet(vol["serial"]))           # hrPartitionID
        # hrPartitionSize 單位為 KB，Integer32 → 2 TB 上限，需 clamp
        add(HRPART + (4, _part_dev, pi),
            rfc1902.Integer32(min(vol["total"] // 1024, INT32_MAX)))     # hrPartitionSize
        add(HRPART + (5, _part_dev, pi), rfc1902.Integer32(pi))          # hrPartitionFSIndex

        fs = _FS_TYPES.get((vol["fs"] or "").upper(), HRFS_TYPE_OTHER)
        add(HRFS + (1, pi), rfc1902.Integer32(pi))                       # hrFSIndex
        add(HRFS + (2, pi), octet(vol["root"].rstrip("\\")))            # hrFSMountPoint
        add(HRFS + (3, pi), octet(""))                                   # hrFSRemoteMountPoint
        add(HRFS + (4, pi), rfc1902.ObjectIdentifier(fs))                # hrFSType
        add(HRFS + (5, pi), rfc1902.Integer32(1))                        # hrFSAccess readWrite(1)
        add(HRFS + (6, pi), rfc1902.Integer32(2))                        # hrFSBootable false(2)
        add(HRFS + (7, pi), rfc1902.Integer32(2))                        # hrFSStorageIndex 佔位
        add(HRFS + (8, pi), rfc1902.Integer32(0))                        # hrFSLastFullBackupDate
        add(HRFS + (9, pi), rfc1902.Integer32(0))                        # hrFSLastPartialBackupDate

    # --- ipRouteTable（RFC1213）---
    # RFC1213 的 ipRouteTable 以**目的位址單獨**當索引，因此同一個目的位址
    # 只能有一筆。但真實主機常有多張網路卡各自的多播路由（224.0.0.0）、
    # 廣播路由（255.255.255.255）、甚至等價多路徑 —— 實測在一台有 7 個 IP 的
    # 筆電上，224.0.0.0 出現了多次，觸發「重複 OID」護欄而讓 agent 無法啟動。
    #
    # 處理方式：同一目的位址只保留 metric 最小的那筆（即實際會被選用的路由）。
    # 這是 RFC1213 的固有限制，較新的 ipForwardTable / inetCidrRouteTable
    # 才把介面納入索引。多餘的路由不輸出，而不是輸出錯的索引。
    _seen_routes: dict[tuple, dict] = {}
    for rt in _collector("routes", get_routes, []):
        our_if = _win2if.get(rt["if_index"])
        if our_if is None:
            continue
        key = tuple(rt["dest_raw"])
        prev = _seen_routes.get(key)
        if prev is None or rt["metric"] < prev["metric"]:
            _seen_routes[key] = {**rt, "our_if": our_if}

    for ridx, rt in _seen_routes.items():
        our_if = rt["our_if"]
        add(IPROUTE + (1,) + ridx, rfc1902.IpAddress(rt["dest"]))        # ipRouteDest
        add(IPROUTE + (2,) + ridx, rfc1902.Integer32(our_if))            # ipRouteIfIndex
        add(IPROUTE + (3,) + ridx, rfc1902.Integer32(
            min(rt["metric"], INT32_MAX)))                               # ipRouteMetric1
        add(IPROUTE + (7,) + ridx, rfc1902.IpAddress(rt["next_hop"]))    # ipRouteNextHop
        add(IPROUTE + (8,) + ridx, rfc1902.Integer32(rt["type"]))        # ipRouteType
        add(IPROUTE + (9,) + ridx, rfc1902.Integer32(rt["proto"]))       # ipRouteProto
        add(IPROUTE + (11,) + ridx, rfc1902.IpAddress(rt["mask"]))       # ipRouteMask

    # --- ENTITY-SENSOR-MIB（LibreNMS sensors 模組）---
    # spec §2.9：感測器資料**不從 LibreHardwareMonitor 取**（依賴 WinRing0，
    # 已列入 Microsoft vulnerable driver blocklist，在 HVCI 端點會觸發 Defender）。
    # 改用原生的 IOCTL_STORAGE_QUERY_PROPERTY + StorageDeviceTemperatureProperty。
    # 虛擬磁碟通常沒有溫度感測器，此時該列不出現（§6.9：絕不捏造數值）。
    def ent_sensor(idx, sensor_type, scale, precision, value, status, unit_descr):
        add(ENTSENS + (1, idx), rfc1902.Integer32(sensor_type))   # entPhySensorType
        add(ENTSENS + (2, idx), rfc1902.Integer32(scale))         # entPhySensorScale
        add(ENTSENS + (3, idx), rfc1902.Integer32(precision))     # entPhySensorPrecision
        add(ENTSENS + (4, idx), rfc1902.Integer32(value))         # entPhySensorValue
        add(ENTSENS + (5, idx), rfc1902.Integer32(status))        # 1=ok 2=unavailable 3=nonoperational
        # entPhySensorValueTimeStamp 的語意是「取得此讀數時的 sysUpTime」，
        # 不是感測器年齡。因為快照重建時才取值，用當下 sysUpTime 即正確。
        add(ENTSENS + (6, idx), rfc1902.TimeTicks(up & U32))      # entPhySensorValueTimeStamp
        add(ENTSENS + (7, idx), rfc1902.Integer32(60))            # entPhySensorValueUpdateRate
        add(ENTSENS + (8, idx), octet(unit_descr))                # entPhySensorUnitsDisplay

    # entPhySensorType (RFC 3433): other(1), celsius(8), percentRH(9), rpm(10),
    # cmm(11), truthvalue(12), volts/amps/watts/hertz…
    #
    # **警告：LibreNMS 只收下列型別**（includes/discovery/sensors/entity-sensor.inc.php）：
    #   voltsDC voltsAC amperes watts hertz percentRH rpm celsius dBm
    # `other(1)` 不在對照表裡，整筆會被**無聲丟棄**。第一版把 NVMe 耐用度與
    # 可用備援空間送成 other，於是現場只看得到溫度、看不到任何 SMART 指標，
    # 而 agent 這端完全正常——查了很久才發現問題在對照表。
    # 計數型的 SMART 指標因此改走 NET-SNMP-EXTEND-MIB（見下方 smart 應用程式）。
    SENSOR_CELSIUS, SENSOR_OTHER, SENSOR_HERTZ = 8, 1, 7
    SCALE_UNITS, SCALE_MEGA, STATUS_OK = 9, 11, 1
    for disk in inv.get("disks", []):
        base = ENT_SENSOR_BASE + disk["index"] * 10
        name = _disk_label(disk)

        temp = disk.get("temp_c")
        if temp is not None:
            ent_sensor(base, SENSOR_CELSIUS, SCALE_UNITS, 0, int(temp),
                       STATUS_OK, "C")
            # 感測器名稱不重複磁碟全名——LibreNMS 會把 entPhysicalName 直接當
            # 感測器標籤，重複一次型號只會撐爆欄位寬度（實測回報）。
            # 父項目已經是那顆磁碟，階層本身就說明了歸屬。
            ent(base, ENT_CLASS_OTHER, descr=f"Temperature ({name})",
                name=f"PhysicalDrive{disk['index']} Temp",
                parent=ENT_DISK_BASE + disk["index"], relpos=1)

        health = disk.get("health") or {}
        # NVMe 耐用度：Percentage Used（0-255，超過 100 代表已超出預估壽命）
        if "percentage_used" in health:
            ent_sensor(base + 1, SENSOR_OTHER, SCALE_UNITS, 0,
                       int(health["percentage_used"]), STATUS_OK, "%")
            ent(base + 1, ENT_CLASS_OTHER, descr=f"Endurance Used ({name})",
                name=f"PhysicalDrive{disk['index']} Wear",
                parent=ENT_DISK_BASE + disk["index"], relpos=2)
        # 可用備援空間百分比
        if "avail_spare_pct" in health:
            ent_sensor(base + 2, SENSOR_OTHER, SCALE_UNITS, 0,
                       int(health["avail_spare_pct"]), STATUS_OK, "%")
            ent(base + 2, ENT_CLASS_OTHER, descr=f"Available Spare ({name})",
                name=f"PhysicalDrive{disk['index']} Spare",
                parent=ENT_DISK_BASE + disk["index"], relpos=3)

    # --- ACPI 熱區（系統/主機板溫度）---
    # CPU 封裝溫度需要讀 MSR，那必須有核心驅動（鐵則 8 禁止）。ACPI 熱區是
    # 韌體本來就公開的替代值，多數筆電與部分桌機有，虛擬機沒有——沒有時
    # 這幾列直接不出現（§6.9：絕不捏造）。
    for zi, tz in enumerate(_collector("thermal_zones",
                                       lambda: (_sensors.read_thermal_zones()
                                                if _sensors else []), [])):
        idx = ENT_THERMAL_BASE + zi
        ent_sensor(idx, SENSOR_CELSIUS, SCALE_UNITS, 0, int(round(tz.celsius)),
                   STATUS_OK, "C")
        ent(idx, ENT_CLASS_OTHER, descr=f"Thermal Zone ({tz.name})",
            name=f"ThermalZone{zi}", parent=ENT_MAINBOARD, relpos=10 + zi)

    # --- CPU 頻率 ---
    # 只輸出一筆而非每個邏輯處理器一筆：CallNtPowerInformation 回報的是封裝
    # 層級的 P-state，實測各核心數值相同（.154 六核全為 3600、.163 為 2501）。
    # 一台 64 核主機生出 64 張一模一樣的圖表沒有價值，只會拖慢 LibreNMS。
    # 用 mega 刻度是必要的：entPhySensorValue 是 Integer32，3600 MHz 換成
    # Hz 是 3.6e9，會直接溢位。
    _freqs = _collector("cpu_frequency",
                        lambda: (_sensors.read_cpu_frequencies() if _sensors else []), [])
    if _freqs:
        cur = max(f.current_mhz for f in _freqs)
        ent_sensor(ENT_CPUFREQ_BASE, SENSOR_HERTZ, SCALE_MEGA, 0, int(cur),
                   STATUS_OK, "MHz")
        ent(ENT_CPUFREQ_BASE, ENT_CLASS_OTHER, descr="CPU Frequency",
            name="CPU Frequency", parent=ENT_MAINBOARD, relpos=20)

    # --- 電池（僅私有 OID）---
    # LibreNMS 的 entity-sensor 對照表沒有 charge / percent，送過去也不會被收下。
    # 放在私有子樹供 walk 查詢與我方診斷用，不假裝它會長出圖表。
    _bat = _collector("battery",
                      lambda: (_sensors.read_battery() if _sensors else None), None)
    if _bat is not None:
        add(JTAGENT + (40, 0), rfc1902.Gauge32(_bat.percent))          # jtBatteryPercent
        add(JTAGENT + (41, 0), rfc1902.Integer32(1 if _bat.on_ac else 2))  # jtBatteryOnAC
        if _bat.seconds_left is not None:
            add(JTAGENT + (42, 0), rfc1902.Gauge32(_bat.seconds_left))  # jtBatterySecondsLeft

    # --- jtDiskHealthTable：磁碟健康狀態（LibreNMS state 感測器）---
    # 分級刻意保守：
    #   ok(1)       韌體自我評估通過，且沒有任何已知的劣化跡象
    #   warning(2)  已出現重新配置／待處理磁區，或溫度超過門檻——碟還能用，
    #               但該排入更換計畫
    #   critical(3) 韌體自己說它即將故障（SMART RETURN STATUS 門檻已超過）
    #   unknown(4)  問不到（USB 橋接器不轉送 SMART 命令等）——
    #               明確標示「不知道」，而不是預設為健康
    for d in inv.get("disks", []):
        hl = d.get("health") or {}
        if not hl:
            continue
        di = d["index"]
        hp = hl.get("health_pass")
        cw = hl.get("critical_warning")
        if hp is False or (isinstance(cw, int) and cw != 0):
            st = DISK_STATE_CRITICAL
        elif hp is True or (isinstance(cw, int) and cw == 0):
            st = DISK_STATE_OK
        else:
            st = DISK_STATE_UNKNOWN
        if st == DISK_STATE_OK:
            attrs = hl.get("smart_by_id") or {}
            degraded = any(isinstance(attrs.get(a), int) and attrs[a] > 0
                           for a in (5, 197, 198))     # 重新配置／待處理／無法修正
            t = d.get("temp_c")
            if degraded or (isinstance(t, int) and t >= 70):
                st = DISK_STATE_WARNING
        add(JTDISK + (1, di), rfc1902.Integer32(di))                    # jtDiskHealthIndex
        add(JTDISK + (2, di), octet(f"PhysicalDrive{di}"))              # jtDiskHealthName
        add(JTDISK + (3, di), rfc1902.Integer32(st))                    # jtDiskHealthState
        add(JTDISK + (4, di), octet(_disk_label(d)[:64]))               # jtDiskHealthDescr

    # --- NET-SNMP-EXTEND-MIB：LibreNMS 的 smart 應用程式 ---
    # LibreNMS 讀 SMART 的正規路徑（json_app_get）：
    #   探索  walk nsExtendStatus
    #   輪詢  get  nsExtendOutputFull."smart"
    # 值是 base64(gzip(json))——LibreNMS 明確支援，而且是必要的：
    # 回應上限 1400 位元組且不分片，未壓縮的 JSON 兩顆磁碟就會爆掉。
    _smart_disks = []
    for d in inv.get("disks", []):
        if not d.get("health"):
            continue
        nm = f"PhysicalDrive{d['index']}"
        _smart_disks.append({
            "name": nm, "health": d["health"],
            "max_temp": observed_max_temp(nm, d.get("temp_c")),
            # 現場要換哪一顆碟時，型號與序號才是找得到的依據
            "model": d.get("model"), "serial": d.get("serial"),
            "vendor": d.get("vendor"),
        })
    if _smart_disks and _smartjson is not None:
        payload = _smartjson.build_smart_json(_smart_disks)
        blob = _smartjson.encode_extend_output(payload)
        # 磁碟很多時可能超過單一 varbind 的上限。砍到放得下為止，
        # 但**必須記錄砍掉幾顆**——無聲截斷會讓人以為全部磁碟都在監控中。
        dropped = 0
        while len(blob) > MAX_EXTEND_BYTES and len(_smart_disks) > 1:
            _smart_disks.pop()
            dropped += 1
            payload = _smartjson.build_smart_json(_smart_disks)
            blob = _smartjson.encode_extend_output(payload)
        if dropped:
            log(f"smart 應用程式輸出超過 {MAX_EXTEND_BYTES} 位元組，"
                f"已省略最後 {dropped} 顆磁碟（共 {len(_smart_disks) + dropped} 顆）",
                error=True)
        if len(blob) <= MAX_EXTEND_BYTES:
            tok = _extend_index("smart")
            # nsExtendConfigTable：LibreNMS 的探索走 nsExtendStatus，
            # 其餘欄位提供完整列，讓一般 SNMP 工具看起來也正常。
            add(NSEXT_CFG + (2,) + tok, octet("jt-snmpd-internal"))    # nsExtendCommand
            add(NSEXT_CFG + (3,) + tok, octet(""))                     # nsExtendArgs
            add(NSEXT_CFG + (4,) + tok, octet(""))                     # nsExtendInput
            add(NSEXT_CFG + (5,) + tok, rfc1902.Integer32(0))          # nsExtendCacheTime
            add(NSEXT_CFG + (6,) + tok, rfc1902.Integer32(1))          # nsExtendExecType=exec
            add(NSEXT_CFG + (7,) + tok, rfc1902.Integer32(1))          # nsExtendRunType=run-on-read
            add(NSEXT_CFG + (20,) + tok, rfc1902.Integer32(4))         # nsExtendStorage=permanent
            add(NSEXT_CFG + (21,) + tok, rfc1902.Integer32(1))         # nsExtendStatus=active
            # nsExtendOutput1Table
            add(NSEXT_OUT1 + (1,) + tok, rfc1902.OctetString(blob))    # nsExtendOutput1Line
            add(NSEXT_OUT1 + (2,) + tok, rfc1902.OctetString(blob))    # nsExtendOutputFull
            add(NSEXT_OUT1 + (3,) + tok, rfc1902.Integer32(1))         # nsExtendOutNumLines
            add(NSEXT_OUT1 + (4,) + tok, rfc1902.Integer32(0))         # nsExtendResult=exit 0
            # nsExtendOutput2Table（逐行；我們只有一行）
            add(NSEXT_OUT2 + (2,) + tok + (1,), rfc1902.OctetString(blob))  # nsExtendOutLine
            add(NSEXT + (1, 0), rfc1902.Integer32(1))                  # nsExtendNumEntries

    # --- UCD-SNMP systemStats（LibreNMS 的 System 圖表群組）---
    # Linux 裝置在 LibreNMS 上的 Detailed Processor Usage、Context Switches、
    # Interrupts、I/O、Swap I/O 等圖表全部來自這裡。
    #
    # 欄位編號**必須以 UCD-SNMP-MIB 為準**，不可憑記憶。實測踩過：
    # 我把 57~63 依直覺排成 SwapIn/SwapOut/IOSent/IOReceived/Contexts/Interrupts，
    # 但正確順序是 IOSent(57)/IOReceived(58)/Interrupts(59)/Contexts(60)/
    # SwapIn(62)/SwapOut(63)。錯位的結果是 context switches 被當成 I/O 顯示，
    # 圖表照樣有線、數字照樣在動，完全看不出是錯的。
    #     snmptranslate -m UCD-SNMP-MIB -On UCD-SNMP-MIB::ssRawContexts
    sp = _collector("sys_perf", get_system_perf, None)
    ct = _collector("cpu_times", get_cpu_times_total, None)

    if ct is not None:
        # UCD 的 ssCpuRaw* 單位是 USER_HZ（1/100 秒）；Windows 是 100ns。
        # 換算除以 10^5。搞錯係數會讓百分比完全失真。
        def _hz(v100ns: int) -> int:
            return (v100ns // 100_000) & U32

        add(UCDSS + (50, 0), rfc1902.Counter32(_hz(ct["user"])))            # ssCpuRawUser
        # ssCpuRawNice：Windows 沒有 nice。但 LibreNMS 的 ucd-mib poller 要求
        # user/nice/system/idle **四個都存在**才建立 Detailed Processor Usage 圖表
        # （includes/polling/ucd-mib.inc.php 的 isset 條件）。
        # 這裡輸出 0 是「Windows 上永遠沒有 nice 時間」的正確陳述，
        # 不是捏造未量測的值——與 iowait/steal 的情況不同（那些是「無法量測」）。
        add(UCDSS + (51, 0), rfc1902.Counter32(0))                          # ssCpuRawNice
        add(UCDSS + (52, 0), rfc1902.Counter32(_hz(ct["system"])))          # ssCpuRawSystem
        add(UCDSS + (53, 0), rfc1902.Counter32(_hz(ct["idle"])))            # ssCpuRawIdle
        # ssCpuRawWait(54)：Windows 沒有 iowait —— I/O 等待計入執行緒的等待狀態，
        #   不是獨立的 CPU 時間類別。**無法量測，故不輸出**，
        #   LibreNMS 的 I/O Wait 圖表因此不會出現，這是誠實的結果。
        # ssCpuRawKernel(55)：UCD 定義與 ssCpuRawSystem 重疊，Linux 上通常為 0
        add(UCDSS + (56, 0), rfc1902.Counter32(_hz(ct["interrupt"])))       # ssCpuRawInterrupt
        # ssCpuRawSoftIRQ(61) / ssCpuRawSteal(64) / ssCpuRawGuest(65,66)：
        #   Windows 無對應概念，不輸出。

    if sp is not None:
        # I/O（單位為 block，net-snmp 在 Linux 上以 512-byte block 計）
        add(UCDSS + (57, 0), rfc1902.Counter32(
            (sp.IoWriteTransferCount // 512) & U32))                        # ssIORawSent
        add(UCDSS + (58, 0), rfc1902.Counter32(
            (sp.IoReadTransferCount // 512) & U32))                         # ssIORawReceived
        if ct is not None:
            add(UCDSS + (59, 0), rfc1902.Counter32(
                ct["interrupt_count"] & U32))                               # ssRawInterrupts
        add(UCDSS + (60, 0), rfc1902.Counter32(sp.ContextSwitches & U32))   # ssRawContexts
        # 分頁活動 → Swap I/O Activity。Windows 的分頁檔讀寫即等同 Linux 的 swap。
        add(UCDSS + (62, 0), rfc1902.Counter32(sp.PageReadCount & U32))     # ssRawSwapIn
        add(UCDSS + (63, 0), rfc1902.Counter32(
            sp.DirtyPagesWriteCount & U32))                                 # ssRawSwapOut

        # 舊式的每秒瞬時值（ssSwapIn/ssSwapOut/ssIOSent/ssIOReceive/
        # ssSysInterrupts/ssSysContext，欄位 3~9）。LibreNMS 不使用它們
        # （只讀 Raw 版本），且它們需要維護前次取樣狀態，故不輸出。

        # ssIndex / ssErrorName：識別用，Linux 的 net-snmp 會提供，
        # 有些工具靠它判斷 UCD 支援度，成本極低故提供。
        add(UCDSS + (1, 0), rfc1902.Integer32(1))                           # ssIndex
        add(UCDSS + (2, 0), octet("systemStats"))                           # ssErrorName

    # laTable（Load Averages）：**Windows 沒有負載平均**。
    # Linux 的 loadavg 是「可執行 + 不可中斷睡眠的行程數之指數移動平均」，
    # Windows 的排程模型沒有對應概念。以處理器佇列長度硬湊會產生
    # 看似合理但語意不同的數字——那正是 §6.9 禁止的捏造。
    # LibreNMS 的 Load Averages 圖表因此在 Windows 上不會出現，這是正確的。

    # --- SNMPv2-MIB snmp 群組（agent 自身的封包統計）---
    # 這組不是從 OS 取得，而是 agent 自己累計的。LibreNMS 的
    # netstats-snmp 圖表用它，同時也是 §3.2 閘門丟棄量的對外出口。
    g = _gate
    drops = (sum(v for k, v in g.counters.items() if k != "passed") if g else 0)
    passed = g.counters["passed"] if g else 0
    add(SNMPG + (1, 0), rfc1902.Counter32(passed & U32))        # snmpInPkts
    add(SNMPG + (2, 0), rfc1902.Counter32(passed & U32))        # snmpOutPkts
    add(SNMPG + (3, 0), rfc1902.Counter32(0))                   # snmpInBadVersions
    add(SNMPG + (4, 0), rfc1902.Counter32(0))                   # snmpInBadCommunityNames
    add(SNMPG + (5, 0), rfc1902.Counter32(0))                   # snmpInBadCommunityUses
    add(SNMPG + (6, 0), rfc1902.Counter32(drops & U32))         # snmpInASNParseErrs
    add(SNMPG + (30, 0), rfc1902.Integer32(2))                  # snmpEnableAuthenTraps: disabled(2)

    # --- JT 自我健康 OID（spec §7）---
    # §7.3：即使在降級模式下，這組 OID 與 system group 仍必須可回應。
    # 這是判斷「服務活著但壞了」與「服務死了」的唯一依據。
    svc_uptime = int((time.monotonic() - _health["start_monotonic"]) * 100)
    snap_age = (int(time.monotonic() - _health["snapshot_built_monotonic"])
                if _health["snapshot_built_monotonic"] else 0)
    add(JTAGENT + (1, 0), octet(AGENT_VERSION))                      # jtAgentVersion
    add(JTAGENT + (2, 0), octet(AGENT_BUILD_DATE))                   # jtAgentBuildDate
    add(JTAGENT + (3, 0), rfc1902.TimeTicks(svc_uptime & U32))       # jtAgentServiceUptime
    # 取不到就不輸出該 OID（spec §6.9：絕不捏造數值）
    _rss = _proc_rss_bytes()
    if _rss is not None:
        add(JTAGENT + (6, 0), rfc1902.Gauge32(_rss))                 # jtAgentRssBytes
    add(JTAGENT + (7, 0), rfc1902.Gauge32(_proc_thread_count()))     # jtAgentThreadCount
    _hc = _proc_handle_count()
    if _hc is not None:
        add(JTAGENT + (8, 0), rfc1902.Gauge32(_hc))                  # jtAgentHandleCount
    add(JTAGENT + (9, 0), rfc1902.Gauge32(snap_age))                 # jtAgentSnapshotAge
    add(JTAGENT + (10, 0), rfc1902.Gauge32(_health["snapshot_build_ms"]))   # jtAgentSnapshotBuildMs
    add(JTAGENT + (11, 0), rfc1902.Integer32(1))                     # jtAgentConfigValid 1=valid
    add(JTAGENT + (12, 0), octet(CFG.get("config_source", "file")))  # jtAgentConfigSource
    add(JTAGENT + (13, 0), octet("none"))                            # jtAgentVacmPreset（尚未實作）
    add(JTAGENT + (20, 0), octet(CFG_PATH))                          # jtAgentConfigPath
    add(JTAGENT + (21, 0), octet(LOG_DIR))                           # jtAgentLogPath
    add(JTAGENT + (22, 0), octet(_install_dir()))                    # jtAgentInstallPath
    add(JTAGENT + (23, 0), octet(_config_warnings()))                # jtAgentConfigWarnings
    add(JTAGENT + (30, 0), rfc1902.Counter32(_health["snapshot_failures"] & U32))

    # jtAgentCollectorTable：每個 collector 的健康狀態
    now = time.monotonic()
    for ci, (cname, st) in enumerate(sorted(_health["collectors"].items()), start=1):
        since = int((now - st["last_ok"]) * 100) if st["last_ok"] else 0
        add(JTCOLL + (1, ci), rfc1902.Integer32(ci))                       # jtCollectorIndex
        add(JTCOLL + (2, ci), octet(cname))                                # jtCollectorName
        add(JTCOLL + (3, ci), rfc1902.Integer32(1))                        # jtCollectorEnabled
        add(JTCOLL + (4, ci), rfc1902.Integer32(st["status"]))             # 1=ok 2=degraded 3=failed
        add(JTCOLL + (5, ci), rfc1902.TimeTicks(since & U32))              # jtCollectorLastSuccess
        add(JTCOLL + (6, ci), rfc1902.Gauge32(st["duration_ms"]))          # jtCollectorLastDurationMs
        add(JTCOLL + (7, ci), rfc1902.Counter32(st["errors"] & U32))       # jtCollectorErrorCount
        add(JTCOLL + (8, ci), octet(st["last_error"]))                     # jtCollectorLastError

    pairs.sort(key=lambda p: p[0])

    # 護欄：snapshot + bisect 的正確性建立在「無重複 OID」之上（spec §36）。
    # 重複會讓 bisect 定位錯位，症狀是某些值莫名其妙變成別的欄位的值。
    for a, b in zip(pairs, pairs[1:]):
        if a[0] == b[0]:
            raise AssertionError(f"重複 OID: {a[0]}")

    return tuple(p[0] for p in pairs), tuple(p[1] for p in pairs)


# --------------------------------------------------------------- MIB 控制器
class SnapshotController(AbstractMibInstrumController):
    """spec §4.3。不覆寫 write_variables → 自動成為唯讀 agent（spec §2.12）。"""

    def __init__(self, oids: tuple, vals: tuple):
        self.oids, self.vals = oids, vals

    def read_variables(self, *varBinds, **context):
        oids, vals = self.oids, self.vals
        out = []
        for vb in varBinds:
            name = vb[0]
            i = bisect_left(oids, tuple(name))
            if i < len(oids) and oids[i] == tuple(name):
                out.append((name, vals[i]))
            else:
                out.append((name, rfc1905.noSuchInstance))
        return out

    def read_next_variables(self, *varBinds, **context):
        oids, vals, n = self.oids, self.vals, len(self.oids)
        out = []
        for vb in varBinds:
            i = bisect_right(oids, tuple(vb[0]))
            if i < n:
                out.append((v2c.ObjectIdentifier(oids[i]), vals[i]))
            else:
                out.append((vb[0], rfc1905.endOfMibView))
        return out


class CappedBulkResponder(cmdrsp.BulkCommandResponder):
    """spec §4.4：伺服器端對 max-repetitions 設上限（預設 25），忽略更大的請求值。

    pysnmp 原生實作只有 varbind 筆數上限（max_varbinds=64），沒有位元組上限，
    且走到 MIB 結尾時會用 endOfMibView 把回應塞滿到 max-repetitions 筆。
    """
    MAXREP_CAP = 25

    def handle_management_operation(self, snmpEngine, stateReference, contextName, PDU):
        try:
            cur = int(v2c.apiBulkPDU.get_max_repetitions(PDU))
            if cur > self.MAXREP_CAP:
                v2c.apiBulkPDU.set_max_repetitions(PDU, self.MAXREP_CAP)
        except Exception as exc:  # noqa: BLE001 - 上限設定失敗不應讓請求失敗
            # 記錄但不中斷：截斷仍由 pysnmp 的 max_varbinds 與回應大小把關
            log(f"max-repetitions 上限設定失敗: {exc!r}")
        return super().handle_management_operation(snmpEngine, stateReference, contextName, PDU)


# --------------------------------------------------------------- 執行
class GatedUdpTransport(udp.UdpTransport):
    """在 pysnmp 之前攔截每個 datagram（spec §3.2）。

    這是整個資安設計的第一道防線：被擋下的封包**完全不會進入
    BER decoder**，因此深度巢狀、超長長度欄位、OID 放大等攻擊
    根本碰不到 pyasn1。

    覆寫 handle_datagram 而非在 pysnmp 內部下手，是為了保證順序：
    pysnmp 一旦拿到位元組，解析就已經發生了。
    """

    def datagram_received(self, datagram, transportAddress):
        """pysnmp 7.x 的實際掛點。

        UdpAsyncioTransport → DgramAsyncioProtocol → asyncio.DatagramProtocol。
        DgramAsyncioProtocol.datagram_received 會把 datagram 交給
        loop.call_soon(callback) 進入 pysnmp 的訊息處理鏈。
        在此處攔截，位元組就到不了 BER decoder。
        """
        gate = _gate
        if gate is not None:
            src_ip = transportAddress[0] if transportAddress else ""
            allowed, _reason = gate.check(bytes(datagram), src_ip)
            if not allowed:
                # 丟棄事件必須限流，否則攻擊者可用它灌爆 log 與 Graylog 授權
                # （spec §3.8）。此處僅計數，彙總輸出由週期性工作處理。
                return
        return super().datagram_received(datagram, transportAddress)


def run_agent(host: str, port: int, community: str, stop_event: threading.Event) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def main_co():
        # Settings are loaded by the entry point, before this function's
        # arguments were bound. Loading them here would be too late: the caller
        # already read CFG["community"] to pass it in, so the agent would listen
        # with whatever the value was *before* the file was read — which is how
        # the first attempt at this fix ended up serving with an empty community
        # while the log cheerfully reported the config had loaded.
        if not CFG["community"]:
            log("no community configured — refusing to serve. Set \"community\" in "
                f"{CFG_PATH} and restart the service.", error=True)
            raise SystemExit(1)
        if not CFG["allowed_networks"]:
            # Deny-all rather than serve-all. The pre-auth gate used to treat an
            # empty list as "no filtering", which is fail-open: a hand-edited
            # config with the list emptied would quietly expose the agent to
            # every source. To serve everything deliberately, list 0.0.0.0/0.
            log("no management networks configured — only loopback will be "
                f"answered. Set \"allowed_networks\" in {CFG_PATH} and restart.",
                error=True)

        # 關鍵：transport 必須在 running event loop 內建立。若在 loop 啟動前呼叫
        # open_server_mode，socket 不會真的綁定 —— 服務顯示 Running 但不回應任何
        # 請求（spec §6.5 的「假活著」）。這個 bug 實測發生過。
        ok = lower_process_priority()
        log(f"程序優先權降為 BELOW_NORMAL: {ok}")
        ident = load_system_identity()
        CFG["contact"], CFG["location"] = ident["contact"], ident["location"]
        srcs = {ident["contact_source"], ident["location_source"]} - {"none"}
        CFG["config_source"] = ("merged" if len(srcs) > 1
                                else (srcs.pop() if srcs else "default"))
        log(f"sysContact={ident['contact']!r} (來源: {ident['contact_source']}) "
            f"sysLocation={ident['location']!r} (來源: {ident['location_source']})")
        _t0 = time.monotonic()
        oids, vals = build_snapshot()
        _health["snapshot_build_ms"] = int((time.monotonic() - _t0) * 1000)
        _health["snapshot_built_monotonic"] = time.monotonic()
        _health["snapshot_generation"] = 1
        eng = engine.SnmpEngine()
        global _gate
        _gate = PreAuthGate(
            allowed_networks=PreAuthGate.parse_networks(CFG["allowed_networks"]),
            rate_pps=CFG["rate_pps"], burst=CFG["rate_burst"])
        nets = CFG["allowed_networks"] or ("(未設定 —— 不做 IP 過濾)",)
        log(f"pre-auth gate 啟用: networks={list(nets)} "
            f"rate={CFG['rate_pps']}pps burst={CFG['rate_burst']}")
        config.add_transport(eng, udp.DOMAIN_NAME,
                             GatedUdpTransport().open_server_mode((host, port)))
        config.add_v1_system(eng, "area", community)
        config.add_vacm_user(eng, 2, "area", "noAuthNoPriv", (1, 3, 6))
        ctx = context.SnmpContext(eng)
        ctrl = SnapshotController(oids, vals)
        ctx.context_names[b""] = ctrl
        cmdrsp.GetCommandResponder(eng, ctx)
        cmdrsp.NextCommandResponder(eng, ctx)
        CappedBulkResponder(eng, ctx)
        log(f"LISTENING {host}:{port} community={community} varbinds={len(oids)}")
        _script = os.path.abspath(__file__) if "__file__" in globals() else "<frozen>"
        log(f"exe={sys.executable!r} script={_script!r} frozen={_is_frozen()} "
            f"state_dir={STATE_DIR!r} fs_encoding={sys.getfilesystemencoding()}")

        while not stop_event.is_set():
            await asyncio.sleep(5)
            try:
                t0 = time.monotonic()
                no, nv = build_snapshot()
                # 原子換手：Python 參考指派在 GIL 下為原子操作，
                # 故走訪中的請求不會看到半套快照（spec §4.3 效益 3）。
                ctrl.oids, ctrl.vals = no, nv
                _health["snapshot_build_ms"] = int((time.monotonic() - t0) * 1000)
                _health["snapshot_built_monotonic"] = time.monotonic()
                _health["snapshot_generation"] += 1
                if _gate is not None:
                    _gate.prune()
            except Exception as exc:  # noqa: BLE001
                _health["snapshot_failures"] += 1
                log(f"快照重建失敗（累計 {_health['snapshot_failures']} 次）: {exc!r}")

    try:
        loop.run_until_complete(main_co())
    except Exception as exc:  # noqa: BLE001
        import traceback
        log(f"agent 異常終止：{exc!r} | {traceback.format_exc()}", error=True)
    finally:
        log("agent 結束")


# pywin32 的 pythonservice.exe 會 import 本模組，並在**模組層級**尋找服務類別。
# 若把 class 定義在函式內部，會得到
#   AttributeError: module 'jt_snmpd' has no attribute '...'
# 且服務直接啟動失敗、沒有任何 log。實測踩過。
try:
    import win32event
    import win32service
    import win32serviceutil
    import servicemanager

    class JTSnmpdService(win32serviceutil.ServiceFramework):
        _svc_name_ = "jt-snmpd"
        _svc_display_name_ = "JT SNMP Agent"
        _svc_description_ = "以標準 MIB 提供 Windows 主機監控資料的 SNMP Agent"

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.hstop = win32event.CreateEvent(None, 0, 0, None)
            # agent 執行緒結束時觸發，讓 SvcDoRun 不必輪詢就能察覺（見下）
            self.hdead = win32event.CreateEvent(None, 0, 0, None)
            self.stop_event = threading.Event()

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            log("SvcStop")
            self.stop_event.set()
            win32event.SetEvent(self.hstop)

        def SvcDoRun(self):
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                                  servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
            # Before anything reads CFG. Everything below — including the values
            # handed to run_agent — must see the operator's settings, not the
            # built-in defaults.
            load_config()
            log(f"SvcDoRun port={CFG['port']} community={CFG['community']!r} "
                f"version={AGENT_VERSION}")

            def _worker():
                try:
                    run_agent("0.0.0.0", CFG["port"], CFG["community"], self.stop_event)
                finally:
                    win32event.SetEvent(self.hdead)

            threading.Thread(target=_worker, daemon=True).start()

            # 只等 hstop 是不夠的：agent 執行緒若在啟動階段就死掉（綁定失敗、
            # MIB 載入失敗、快照建置失敗），服務會永遠停在 Running 卻沒有任何
            # 監聽器——spec §6.5 的「假活著」。SCM 看到 Running、監控看到逾時，
            # 兩邊說法不一致是現場最難查的狀況。
            #
            # 改等「停止」與「agent 已死」兩個事件；後者以非零碼結束，
            # 讓已設定的 sc failure 自動復原真正生效（否則那段設定形同虛設）。
            rc = win32event.WaitForMultipleObjects(
                [self.hstop, self.hdead], 0, win32event.INFINITE)
            if rc == win32event.WAIT_OBJECT_0 + 1 and not self.stop_event.is_set():
                log("agent 執行緒非預期結束，服務以失敗狀態退出以觸發自動復原",
                    error=True)
                # 1064 = ERROR_EXCEPTION_IN_SERVICE，SCM 會據此套用復原動作
                self.ReportServiceStatus(win32service.SERVICE_STOPPED,
                                         win32ExitCode=1064, waitHint=0)
                os._exit(1)

    _HAVE_SERVICE = True
except ImportError:      # pywin32 不在（例如僅做前景除錯）
    _HAVE_SERVICE = False


def _is_frozen() -> bool:
    """PyInstaller 打包後 sys.frozen 為 True，且 sys.executable 是我們自己的 exe。"""
    return getattr(sys, "frozen", False)


def _service_main() -> None:
    """服務進入點。

    未打包時走 HandleCommandLine（pythonservice.exe 代跑）。
    打包成 exe 後 **必須**改走 PrepareToHostSingle + StartServiceCtrlDispatcher，
    因為此時服務主程式就是我們自己的 exe（spec §1.4 硬性規則），
    沒有 pythonservice.exe 可以代為 host。
    """
    if not _HAVE_SERVICE:
        raise SystemExit("需要 pywin32 才能以服務模式執行")

    # --selftest / --foreground 由 __main__ 先攔截；此處只處理服務相關 argv。
    if _is_frozen() and len(sys.argv) == 1:
        # SCM 直接啟動我們的 exe（無參數）→ 進入服務派遣迴圈
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(JTSnmpdService)
        servicemanager.StartServiceCtrlDispatcher()
        return

    if _is_frozen():
        # install/remove/start/stop：讓 pywin32 把 binPath 指向我們的 exe 本身
        win32serviceutil.HandleCommandLine(
            JTSnmpdService, argv=sys.argv,
            customInstallOptions="", customOptionHandler=None)
        return

    win32serviceutil.HandleCommandLine(JTSnmpdService)


def _arg(name: str, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def selftest() -> int:
    """打包完整性煙霧測試。

    只驗「exe 產出了」是不夠的——pysnmp 的 MIB 資料檔漏打包時 exe 照樣產出，
    但一啟動就 MibNotFoundError，而**服務狀態仍顯示 Running**（§6.5 假活著）。
    實測踩過一次。因此建置後必須實際初始化一次 SNMP engine 並建 snapshot。
    """
    try:
        oids, vals = build_snapshot()
        if len(oids) < 10:
            print(f"SELFTEST_FAIL snapshot 過小: {len(oids)}")
            return 1
        # 真正初始化 pysnmp engine —— MIB 載入失敗會在這裡爆
        eng = engine.SnmpEngine()
        config.add_v1_system(eng, "selftest", "public")
        config.add_vacm_user(eng, 2, "selftest", "noAuthNoPriv", (1, 3, 6))
        ctx = context.SnmpContext(eng)
        ctx.context_names[b""] = SnapshotController(oids, vals)
        # 驗 GET / GETNEXT 兩條路徑
        ctrl = ctx.context_names[b""]
        got = ctrl.read_variables((v2c.ObjectIdentifier(SYS + (1, 0)), None))
        nxt = ctrl.read_next_variables((v2c.ObjectIdentifier(SYS), None))
        if not got or not nxt:
            print("SELFTEST_FAIL GET/GETNEXT 無回應")
            return 1
        print(f"SELFTEST_OK varbinds={len(oids)} frozen={_is_frozen()} "
              f"sysDescr_len={len(bytes(got[0][1]))}")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"SELFTEST_FAIL {exc!r}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    # 這兩個必須在 _service_main() 之前攔截。frozen 後若讓它們落到
    # win32serviceutil.HandleCommandLine，會得到 "option not recognized"
    # 並印出服務用法——實測踩過。
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())

    # Config file first, command line second — the command line is an override,
    # so it has to be applied after the file has been read.
    load_config()
    CFG["port"] = int(_arg("--port", CFG["port"]))
    CFG["community"] = _arg("--community", CFG["community"])
    if "--foreground" in sys.argv:
        print(f"foreground 0.0.0.0:{CFG['port']} community={CFG['community']}")
        # noqa: S104 —— 綁 0.0.0.0 是設計本意：SNMP agent 必須在所有管理網段
        # 上可達。存取控制由 §3.2 的 pre-auth gate（來源 IP 白名單）與防火牆
        # 規則（安裝時強制輸入管理網段，預設 deny）兩層負責，不靠綁定位址。
        run_agent("0.0.0.0", CFG["port"], CFG["community"], threading.Event())
    else:
        _service_main()
