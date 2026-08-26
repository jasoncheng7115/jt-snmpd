"""jt-snmpd — jt-snmpd for Windows (Phase 0.5, deployable end to end)

Self-contained in one file. Runs in the foreground for debugging, or is
registered by pywin32 as a Windows service (automatic start, LocalSystem).

The architecture follows snapshot + bisect. The entire MIB is one
array sorted in OID lexicographic order; GET uses bisect_left and GETNEXT uses
bisect_right, which makes the §36 requirements — ordering, no duplicate OIDs, no
GETNEXT loops, correct endOfMibView — structural rather than something to
remember.

Usage:
    python jt_agent.py --foreground [--port 161] [--community <community>]
    python jt_agent.py install|start|stop|remove       (pywin32 service)
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

import usm

from preauth import PreAuthGate

# ------------------------------------------------------------ config / logging
STATE_DIR = r"C:\ProgramData\jt-snmpd"
LOG_DIR = os.path.join(STATE_DIR, "logs")
STATE_FILE = os.path.join(STATE_DIR, "state", "index-map.json")

# Defaults only. The real values come from config.json, written by the
# installer and editable afterwards (edit, then restart the service).
#
# `community` and `allowed_networks` are deliberately empty rather than carrying
# sensible-looking values. An earlier version shipped the development lab's own
# community and "192.168.1.0/24" as defaults *and never read the config file at
# all*: the installer wrote the operator's answers to config.json, the agent
# ignored them, and every install that did not happen to use those exact two
# values failed its loopback health check with MSI error 1603. The defaults were
# what made the bug survive testing — our own lab used precisely those values.
CFG = {"port": 161, "community": "", "contact": "", "location": "",
       # deny by default, never Any/Any. Empty means "not configured"
       # and is treated as deny-all (loopback excepted); to serve every source
       # deliberately, set 0.0.0.0/0 and ::/0 explicitly.
       "allowed_networks": (), "rate_pps": 50, "rate_burst": 100,
       # SNMPv3 is added beside v2c, not in place of it: an upgrade that stopped
       # answering v2c would take every existing deployment off the map at the
       # moment it was installed. Sites that have to certify "no v2c" set this
       # once their v3 users are provisioned.
       "v3_only": False,
       # ipNetToPhysicalTable is the local ARP table, which is a
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
        return          # already loaded; both entry points call this, and
                        # re-reading would only log the same line twice
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

# The version comes from deploy/version.py, the single source. Hardcoding it here
# drifts from the MSI version — which happened: the MSI reached 0.1.6 while
# jtAgentVersion still reported 0.1.0-dev.
try:
    from version import VERSION as AGENT_VERSION, BUILD_DATE as AGENT_BUILD_DATE
except ImportError:                     # version.py is bundled after packaging,
                                        # so this should not happen
    AGENT_VERSION, AGENT_BUILD_DATE = "unknown", "unknown"

try:
    import sensors as _sensors          # ACPI thermal zones, battery, CPU freq
except ImportError:
    _sensors = None
try:
    import smartjson as _smartjson      # JSON for LibreNMS's smart application
except ImportError:
    _smartjson = None


LOG_MAX_BYTES = 5 * 1024 * 1024     # cap per file
LOG_KEEP = 3                        # keep .1 through .3


def _rotate_log(path: str) -> None:
    """Rotate the log file.

    Unbounded growth fills the system drive on a deployment of hundreds of
    machines running for years — **a monitoring agent taking down the host it
    monitors** is the least acceptable failure there is. A repeated snapshot
    failure writes a line every five seconds, which is seventeen thousand lines a
    day; this is not hypothetical.
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
        # A failed rotation only means the log keeps growing; it must not
        # affect the agent itself.
        pass


def _event_log_error(msg: str) -> None:
    """Also write errors to the Windows Event Log, under the source jt-snmpd.

    Field staff and audit tooling look at the Event Viewer first, not at a text
    file under %ProgramData%. Diagnosing hundreds of machines remotely,
    `Get-WinEvent` can collect centrally; log files scattered across machines
    cannot. servicemanager is imported further down this module, so it is fetched
    lazily through globals().
    """
    sm = globals().get("servicemanager")
    if sm is None:
        return
    try:
        sm.LogErrorMsg(f"jt-snmpd: {msg}")
    except Exception:   # noqa: BLE001, S110
        # Failing to write to the Event Log (permissions, unregistered event
        # source) must not bring the agent down with it.
        pass


def log(msg: str, *, error: bool = False) -> None:
    """All file I/O states encoding="utf-8" explicitly.

    The installation path may contain non-ASCII characters. Python's open() on
    Windows defaults to the system ANSI code page (cp950 for Traditional
    Chinese), and writing content outside that page raises UnicodeEncodeError.
    The path itself is safe — Python 3 handles it as str, internally UTF-16 — as
    long as nothing encodes it by hand. What actually breaks is content encoding
    and argument passing to subprocesses.

    `error=True` additionally writes to the Event Log. That is reserved for
    things the operator needs to know, not for every small collector failure —
    otherwise the Event Log fills with noise and stops being worth reading.
    """
    size = 0
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "jt-snmpd.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} "
                     f"{'ERROR ' if error else ''}{msg}\n")
            size = fh.tell()        # tell() is free; no extra stat needed
    except (OSError, UnicodeError):
        pass
    if size > LOG_MAX_BYTES:
        _rotate_log(os.path.join(LOG_DIR, "jt-snmpd.log"))
    if error:
        _event_log_error(msg)


# ------------------------------------------------- Win32 function signatures
# Rule: every Win32 call declares argtypes and restype. Without them ctypes
# truncates 64-bit return values through its default c_int — observed as drive C:
# reporting 0 GB, and GetTickCount64 overflowing past 24.8 days.
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

# Priority constants used to get out of the way
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
THREAD_MODE_BACKGROUND_END = 0x00020000


def lower_process_priority() -> bool:
    """Drop the whole agent process to BELOW_NORMAL.

    A hard requirement: polling must not make Windows feel slower. Measured at
    7,000 times the real load, a fixed workload on the host degraded by 4.2%
    without this (the target is under 3%).

    An SNMP agent is not latency-sensitive — LibreNMS polls every five minutes
    and its timeouts are in seconds — so yielding CPU to foreground work is the
    right trade.
    """
    return bool(_k32.SetPriorityClass(_k32.GetCurrentProcess(),
                                      BELOW_NORMAL_PRIORITY_CLASS))


def begin_background_mode() -> bool:
    """Put the current thread into background mode, lowering both CPU **and disk
    I/O** priority.

    Only for collector threads. The SNMP response path does not use it, since
    that would slow responses down.

    Note that THREAD_MODE_BACKGROUND_BEGIN can only be called on the calling
    thread itself.
    """
    return bool(_k32.SetThreadPriority(_k32.GetCurrentThread(),
                                       THREAD_MODE_BACKGROUND_BEGIN))

INVALID_HANDLE = ctypes.c_void_p(-1).value
INT32_MAX = 2147483647
U32 = 0xFFFFFFFF


def octet(s) -> rfc1902.OctetString:
    """An SNMP OCTET STRING is bytes, not text. pyasn1 encodes str as latin-1 by
    default and raises PyAsn1UnicodeEncodeError on anything outside it — and a
    network adapter alias on a Traditional Chinese Windows install is Chinese
    ("乙太網路"), so this is guaranteed to be hit in the target environment.
    Everything is encoded explicitly as UTF-8."""
    if isinstance(s, bytes):
        return rfc1902.OctetString(s)
    return rfc1902.OctetString(str(s).encode("utf-8"))


def _reg(path: str, name: str, root=winreg.HKEY_LOCAL_MACHINE):
    with winreg.OpenKey(root, path) as key:
        return winreg.QueryValueEx(key, name)[0]


# --- Own process resources  ---------
class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def _proc_rss_bytes() -> int | None:
    """Own RSS. Returns None when it cannot be read — **not 0**.

    never fabricate a value. Returning 0 makes the LibreNMS graph show
    "RSS = 0", which looks like a valid reading when it is actually a measurement
    failure — worse than the OID not existing at all. It would also mean the
    §6.4 self-restart threshold (RSS > 250 MB) could never fire.

    Bandit S110 flagged this try/except/pass, and on investigation it was a real
    violation of the spec rather than a false positive.
    """
    try:
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        c = _PROCESS_MEMORY_COUNTERS()
        c.cb = ctypes.sizeof(c)
        if psapi.GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return min(int(c.WorkingSetSize), U32)
        log("GetProcessMemoryInfo failed; jtAgentRssBytes will not be emitted")
    except Exception as exc:  # noqa: BLE001
        log(f"_proc_rss_bytes failed: {exc!r}")
    return None


def _proc_handle_count() -> int | None:
    """Own handle count. None when unreadable, for the same reason as
    _proc_rss_bytes."""
    try:
        _k32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        _k32.GetProcessHandleCount.restype = wintypes.BOOL
        n = wintypes.DWORD(0)
        if _k32.GetProcessHandleCount(_k32.GetCurrentProcess(), ctypes.byref(n)):
            return int(n.value)
        log("GetProcessHandleCount failed; jtAgentHandleCount will not be emitted")
    except Exception as exc:  # noqa: BLE001
        log(f"_proc_handle_count failed: {exc!r}")
    return None


def _proc_thread_count() -> int:
    return threading.active_count()


# ------------------------- Configuration sources (ADMX policy / MS SNMP migration)
POLICY_KEY = r"SOFTWARE\Policies\JasonTools\JTSNMPD"
MSSNMP_KEY = r"SYSTEM\CurrentControlSet\Services\SNMP\Parameters"


def _reg_opt(path: str, name: str, default=None):
    """Read a single registry value; return default if absent, never raise."""
    try:
        return _reg(path, name)
    except OSError:
        return default


def load_system_identity() -> dict:
    """Decide the effective sysContact and sysLocation.

    Precedence (policy **overrides** local settings, matching how the
    rest of Windows behaves):

      1. ADMX policy at HKLM\\SOFTWARE\\Policies\\JasonTools\\JTSNMPD
      2. Whatever the built-in Windows SNMP service already had
      3. Empty string

    Point 2 is deliberate. The customer was already running the built-in service;
    switching over should not make them retype sysContact and sysLocation — that
    is the core of the migration experience. The registry values remain even
    after the built-in service is disabled, and are still worth carrying over.
    """
    out = {"contact": "", "location": "", "contact_source": "none",
           "location_source": "none"}

    # 2) Start from whatever MS SNMP already had
    ms = MSSNMP_KEY + r"\RFC1156Agent"
    v = _reg_opt(ms, "sysContact")
    if v:
        out["contact"], out["contact_source"] = str(v), "ms-snmp"
    v = _reg_opt(ms, "sysLocation")
    if v:
        out["location"], out["location_source"] = str(v), "ms-snmp"

    # 1) ADMX policy overrides it
    v = _reg_opt(POLICY_KEY, "SysContact")
    if v:
        out["contact"], out["contact_source"] = str(v), "policy"
    v = _reg_opt(POLICY_KEY, "SysLocation")
    if v:
        out["location"], out["location_source"] = str(v), "policy"

    return out


def _install_dir() -> str:
    """the installation directory must be answerable over SNMP,
    without logging in to the machine."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _config_warnings() -> str:
    """Security warnings about the effective configuration
    .

    §5.9.4: importing a community during migration from Windows SNMP turns v2c on
    where it was off by default. That is an explicit downgrade in security and
    has to be surfaced here.
    """
    warns = []
    if CFG["community"]:
        warns.append("v2c enabled")
        if CFG["community"] in ("public", "private"):
            warns.append(f"default community '{CFG['community']}'")
    if not warns:
        return "none"
    return "; ".join(warns)


# ----------------------------------------------------------------------- Memory
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class PERFORMANCE_INFORMATION(ctypes.Structure):
    """psapi GetPerformanceInfo — every memory figure Windows has, in one call.

    Beyond GlobalMemoryStatusEx it also gives SystemCache, KernelPaged,
    KernelNonpaged, and ProcessCount / ThreadCount / HandleCount.

    The last of those means hrSystemProcesses no longer needs a Toolhelp32
    snapshot, which enumerates every process and costs 50-300 ms with 300
    processes running. This is a single call taking tens of microseconds.
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


# ---------------------------------------------------------------------- Volumes
DRIVE_FIXED = 3


def _volume_info(root: str) -> tuple[str, str, str]:
    """Return (volume label, serial in hex, file system).

    The label may be non-ASCII. GetVolumeInformationW is a wide-character API
    returning UTF-16, so Python gets a str directly; the risk comes later, when
    it is encoded into an OCTET STRING. That has to go through octet() to become
    explicit UTF-8, or pyasn1 encodes it as latin-1 and raises
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
    """Fixed disks only. never enumerate network drives or optical
    media — a disconnected network share blocks for 30 seconds or more."""
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
    """hrStorageSize and hrStorageUsed are Integer32, so the
    allocation unit has to be scaled up dynamically."""
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

# hardware inventory is read once at startup and **cached for the
# lifetime of the process**. SMBIOS does not change after boot, and neither do
# physical disk models or capacities. Re-reading it on every snapshot rebuild
# would be pure waste.
_inventory_cache: dict | None = None


def get_inventory() -> dict:
    global _inventory_cache
    if _inventory_cache is None:
        try:
            import smbios
            info = smbios.collect()
        except Exception as exc:  # noqa: BLE001
            log(f"SMBIOS read failed: {exc!r}")
            info = {}
        info["disks"] = get_physical_disks()
        _inventory_cache = info
    return _inventory_cache

# --- Self-health  ---------------------------------------------------
# This agent fails quietly: the service reports Running while the LibreNMS graphs
# go flat. These values let LibreNMS monitor the agent itself, and they are the
# only way to tell "alive but broken" from "dead".
_health = {
    "start_monotonic": time.monotonic(),
    "snapshot_generation": 0,
    "snapshot_built_monotonic": 0.0,
    "snapshot_build_ms": 0,
    "snapshot_failures": 0,
    "collectors": {},        # name -> dict(status, last_ok_monotonic, duration_ms, errors, last_error)
}


def _collector(name: str, fn, default):
    """Run one collector and record its health.

    Every new collector must also implement its entry in jtAgentCollectorTable.
    On failure this returns the default rather than raising: startup never fails
    hard, and when a collector fails its rows disappear from the snapshot rather
    than being filled with a fabricated value.
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
        log(f"collector {name} failed: {exc!r}")
        result = default
    st["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return result


def get_cpu_loads() -> list[int]:
    """Per-core utilisation as a percentage. NtQuerySystemInformation
    returns every CPU in one call, far cheaper than PDH wildcard expansion on a
    many-core machine."""
    ncpu = os.cpu_count() or 1
    buf = (_SPPI * ncpu)()
    ret = wintypes.ULONG(0)
    if _ntdll.NtQuerySystemInformation(8, ctypes.byref(buf), ctypes.sizeof(buf),
                                       ctypes.byref(ret)) != 0:
        return [0] * ncpu
    loads = []
    for i in range(ncpu):
        idle = buf[i].IdleTime.QuadPart
        total = buf[i].KernelTime.QuadPart + buf[i].UserTime.QuadPart  # KernelTime includes idle
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
    """IOCTL_DISK_PERFORMANCE. Needs administrative rights, which the
    service has as LocalSystem. Returns (drive_no, BytesRead, BytesWritten,
    ReadCount, WriteCount), all cumulative."""
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


# ------------------------------- Network protocol statistics (IP/TCP/UDP/ICMP)
# These feed the whole LibreNMS "Netstats" graph group. All through iphlpapi: one
# call returns an entire counter block, at negligible cost (§10-32: no wmic, no
# PowerShell).
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


# --------------------------------- IP address table / neighbour table (ARP, ND)
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
    """Convert a SOCKADDR_INET into (family, address string, address bytes)."""
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
    """Local unicast IP addresses, both IPv4 and IPv6.

    Feeds LibreNMS's ipv4-addresses and ipv6-addresses discovery modules. These
    are **this machine's own addresses**, so the disclosure risk is low. It is the
    ARP table that maps the internal network (see get_ip_neighbors).
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


# ipNetToPhysicalState, per RFC 4293: reachable(1) stale(2) delay(3)
# probe(4) invalid(5) unknown(6) incomplete(7)
_NDSTATE_TO_MIB = {0: 7, 1: 7, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 1}


def get_ip_neighbors() -> list[dict]:
    """Neighbour cache (ARP and IPv6 ND).

    This one needs stating explicitly: ipNetToPhysicalTable is the **local
    ARP table**, which is a ready-made target list for lateral movement and is of
    real value to an attacker. It is therefore **off by default** and has to be
    switched on deliberately (the VACM standard preset, not librenms-minimal).
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


# ------------------------------------------------------------------ Route table
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
    """IPv4 route table.

    ipForwardTable counts as "the complete internal routing
    topology" and of value to an attacker, which places it in the VACM standard
    preset. It is not, however, a direct target list the way the ARP table is —
    routes describe subnets, ARP describes hosts — and parts of LibreNMS use it.
    So it is emitted by default and can be turned off in the configuration.
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
                # A directly-connected route has no next hop, and RFC1213
                # represents ipRouteNextHop as 0.0.0.0. This is not a bind
                # address, so Bandit's B104 is a false positive here.
                "next_hop": nexthop if nv == 4 else "0.0.0.0",  # nosec B104
                "if_index": int(r.InterfaceIndex), "metric": int(r.Metric),
                # RFC1213 ipRouteProto: other(1) local(2) netmgmt(3) ...
                # Windows NL_ROUTE_PROTOCOL: 1=Other 2=Local 3=NetMgmt
                "proto": 2 if r.Protocol == 2 else (3 if r.Protocol == 3 else 1),
                # ipRouteType: direct(3) means the destination is on a local
                # subnet; indirect(4) means it goes via a gateway
                "type": 3 if nv != 4 or nexthop == "0.0.0.0" else 4,  # nosec B104
            })
        return out
    finally:
        _iph.FreeMibTable(ptr)


# ------------------- UCD-SNMP systemStats (the LibreNMS System graph group)
class _SYSTEM_PERFORMANCE_INFORMATION(ctypes.Structure):
    """NtQuerySystemInformation(SystemPerformanceInformation)。

    Windows does not document this structure, but its layout has been stable
    since NT and both Task Manager and perfmon depend on it. Measured on Win11
    26200 it returns 312 bytes, matching this definition. If a future layout
    changes, the returned length will not match and the data is refused rather
    than misread.
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
    """Machine-wide performance counters. None when unavailable — nothing is
    fabricated."""
    buf = _SYSTEM_PERFORMANCE_INFORMATION()
    ret = wintypes.ULONG(0)
    st = _ntdll.NtQuerySystemInformation(
        SYSTEM_PERFORMANCE_INFORMATION_CLASS, ctypes.byref(buf),
        ctypes.sizeof(buf), ctypes.byref(ret))
    if st != 0:
        return None
    # If the layout changes in a future Windows the returned length will not
    # match, and the data is refused rather than misread.
    if ret.value != ctypes.sizeof(buf):
        log(f"SystemPerformanceInformation length mismatch ({ret.value} != "
            f"{ctypes.sizeof(buf)}); UCD systemStats will not be emitted")
        return None
    return buf


def get_cpu_times_total() -> dict | None:
    """Machine-wide cumulative CPU time and interrupt counts, in 100 ns units.

    UCD's ssCpuRaw* counters are in **USER_HZ (hundredths of a second)**, which
    differs from Windows' 100 ns unit by a factor of 10^5. Getting the conversion
    wrong makes LibreNMS's Detailed Processor Usage show nonsensical percentages.
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
    # KernelTime includes idle (a Windows convention), so idle is subtracted to
    # get system time
    return {"idle": idle, "system": max(kernel - idle, 0), "user": user,
            "interrupt": intr, "interrupt_count": icount}


# ------------------------------------------- Process count (hrSystemProcesses)
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
    """hrSystemProcesses. Counts only; nothing is enumerated.

    a full hrSWRunTable is an information-disclosure source — which
    EDR is running and where it is installed — and is off by default. A **bare
    count** discloses nothing, and LibreNMS's System → Processes graph needs it.
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


# --------------------------------------------- Physical disks (hrDiskStorage)
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
    """Read a NUL-terminated string at an offset into a descriptor buffer. An
    offset of 0 means the field is absent."""
    if not off or off >= len(buf):
        return ""
    end = buf.find(b"\x00", off)
    return buf[off:end if end >= 0 else len(buf)].decode("ascii", "replace").strip()


def get_physical_disks() -> list[dict]:
    """Enumerate physical disks and read model, serial, capacity and bus type.

    handles should be cached, since repeatedly opening and closing a
    physical disk device is wasteful. This is inventory data, so once is enough
    (§2.7: hardware inventory is cached for the lifetime of the process).
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
            # Device descriptor: model, serial, bus
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
            # Capacity: IOCTL_DISK_GET_LENGTH_INFO needs FILE_READ_ACCESS, but
            # the handle is deliberately opened with dwDesiredAccess=0 (least
            # privilege). GET_DRIVE_GEOMETRY_EX works with no access
            # rights at all, so it is the primary source.
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
            # Temperature and health. Firmware support for these IOCTLs varies enormously, and
            # one misbehaving device must not fail the whole enumeration
            # . The work is delegated to the
            # diskhealth module, which tries several paths — see its docstring.
            #
            # The handle is not shared: diskhealth needs READ|WRITE to issue
            # SMART commands, while this function deliberately opens with least
            # privilege.
            try:
                import diskhealth
                hl = diskhealth.probe(n)
                if hl:
                    if hl.get("temp_c"):
                        info["temp_c"] = hl["temp_c"]
                    info["health"] = hl
            except Exception as exc:  # noqa: BLE001
                log(f"PhysicalDrive{n} temperature/health query failed: {exc!r}")
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


# ------------------------------------------- SNMP engine identity and time
ENGINE_FILE = os.path.join(STATE_DIR, "state", "engine.json")
USM_STORE = os.path.join(STATE_DIR, "secrets", "usm.dat")


def _extend_index(token: str) -> tuple[int, ...]:
    """The NET-SNMP-EXTEND-MIB tables are indexed by nsExtendToken, an OCTET
    STRING.

    SMI encodes a string index as the length followed by one sub-identifier per
    byte,
    so "smart" becomes (5, 115, 109, 97, 114, 116). On the LibreNMS side this is
    `Oid::encodeString('smart')`.
    """
    raw = token.encode("ascii", errors="ignore")
    return (len(raw),) + tuple(raw)


def _machine_guid() -> str:
    """Read Windows' MachineGuid as a stable basis for the engineID.

    RFC 3411 requires the engineID to stay the same across reboots and service
    restarts. SNMPv3 user keys are localised against it, so if it changes every
    key stops working.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return str(winreg.QueryValueEx(k, "MachineGuid")[0])
    except Exception:  # noqa: BLE001 - fall back to the hostname if the
                       # registry is unavailable
        return socket.gethostname()


# RFC 3414 §2.2: snmpEngineBoots saturates here, and a new engineID is required
# once it does.
ENGINE_BOOTS_MAX = 2147483647

# The enterprise number is not registered. IANA has assigned up to 66639 and
# issues them in sequence, so 99999 is unclaimed today and will stay unclaimed
# for years, but it is a squat and docs/snmpv3.md says so plainly. Note that the
# uniqueness of an engineID does not rest on it: two hosts are told apart by the
# MachineGuid digest below, not by the enterprise number they share.
ENGINE_PEN = 99999


def _new_engine_id(machine_guid: str) -> str:
    """A fresh SnmpEngineID, hex encoded, as defined in RFC 3411 §5.

    Format: the top bit set marks the RFC 3411 format, the remaining 31 bits are
    the enterprise number, the fifth byte is a format code (4 = administratively
    assigned text), and up to 27 bytes follow.

    A hash of the MachineGuid is used rather than the GUID itself: the GUID is 36
    characters, past the length limit, while the hash is equally stable and a
    fixed size.
    """
    head = bytes([(ENGINE_PEN >> 24) & 0xFF | 0x80, (ENGINE_PEN >> 16) & 0xFF,
                  (ENGINE_PEN >> 8) & 0xFF, ENGINE_PEN & 0xFF, 4])
    digest = hashlib.sha256(machine_guid.encode("utf-8")).digest()[:16]
    return (head + digest).hex()


def _plan_engine_state(prev: object, machine_guid: str, boot_key: int,
                       fresh_engine_id: str) -> tuple[dict, list[str]]:
    """Decide what engine.json should contain next.

    Pure on purpose: no registry, no clock, no disk. The awkward cases here are
    a cloned VM, a corrupted state file and a saturated counter, none of which
    can be produced on demand on a real machine, so the decision is separated
    from the I/O and tested directly.

    Three properties have to hold at once.

    **An engineID must not be shared between machines.** Customer estates are
    built from Proxmox and Hyper-V templates. Capture a template after the agent
    has run once and every clone answers with the same engineID; the manager's
    USM cache then holds one boots/time pair for what it believes is a single
    engine, and authentication fails intermittently across the whole estate for
    reasons nothing in the logs explains. Recording the MachineGuid the identity
    was generated for is what makes the clone detectable.

    **snmpEngineBoots must never go backwards.** It is half of the pair RFC 3414
    uses for replay protection; restarting the count reopens a window that was
    already closed.

    **The counter must stop at 2^31-1**, at which point RFC 3414 requires a new
    engineID rather than a wrap.
    """
    prev = prev if isinstance(prev, dict) else {}
    reasons: list[str] = []

    def _whole(value: object) -> int:
        # bool is an int subclass; a JSON `true` here would otherwise count as 1
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    engine_id = prev.get("engine_id")
    boots = max(0, _whole(prev.get("boots")))

    if not isinstance(engine_id, str) or not engine_id:
        # Note what is *not* reset here. Up to 1.0.0 the file was schema 1: it
        # carried the boot count but no identity, because the engineID was
        # derived from the MachineGuid on every snapshot instead of being
        # written down. That derivation is the one still used, so an upgrade
        # produces the identical engineID and the machine's identity has not
        # changed. Restarting its counter would hand back the replay window for
        # nothing.
        engine_id = fresh_engine_id
        reasons.append("no engine identity on file, generated one")
    elif prev.get("machine_guid") != machine_guid:
        # Resetting the counter is right rather than merely tidy: the identity
        # is new, so its counter has never been used and cannot be replayed.
        engine_id, boots = fresh_engine_id, 0
        reasons.append(
            "MachineGuid does not match the one this engineID was generated "
            "for, so the machine was cloned or reimaged; generated a new "
            "engineID and reset snmpEngineBoots. Any SNMPv3 user localised "
            "against the old engineID has to be provisioned again")
    elif boots >= ENGINE_BOOTS_MAX:
        engine_id, boots = fresh_engine_id, 0
        reasons.append("snmpEngineBoots reached its ceiling, so RFC 3414 "
                       "requires a new engineID; generated one")

    # A change of boot instant is what marks a new boot, so the counter moves
    # then and not on a service restart. snmpEngineTime is measured from the
    # boot instant too, so the pair still increases strictly across a restart.
    if prev.get("boot_key") != boot_key:
        boots += 1

    return ({"schema_version": 2, "machine_guid": machine_guid,
             "engine_id": engine_id, "boot_key": boot_key,
             "boots": max(1, min(boots, ENGINE_BOOTS_MAX))}, reasons)


def _load_engine_state() -> dict:
    """Read engine.json, falling back to the .bak when the main file is damaged.

    Falling back matters more here than for most state: treating a corrupt file
    as "no state" would restart snmpEngineBoots at 1, which is precisely the
    replay window the counter exists to close.
    """
    for path in (ENGINE_FILE, ENGINE_FILE + ".bak"):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                if path.endswith(".bak"):
                    log("engine.json was unreadable; recovered from engine.json.bak")
                return data
        except (OSError, ValueError, UnicodeError):
            continue
    return {}


def _save_engine_state(data: dict) -> None:
    """temp file, flush, fsync, atomic replace, keep a .bak."""
    os.makedirs(os.path.dirname(ENGINE_FILE), exist_ok=True)
    tmp = ENGINE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    if os.path.exists(ENGINE_FILE):
        try:
            os.replace(ENGINE_FILE, ENGINE_FILE + ".bak")
        except OSError:
            pass
    os.replace(tmp, ENGINE_FILE)


_engine_cache: dict | None = None


def _engine_state() -> dict:
    """The engine identity and boot counter, resolved once per process.

    Both values are constant for the lifetime of the service, and the snapshot
    is rebuilt every five seconds. Reading the registry, hashing and touching
    the disk on every rebuild bought nothing; this is resolved on the first
    rebuild and reused, which is also what keeps the agent off the disk while
    it is only answering queries.
    """
    global _engine_cache
    if _engine_cache is not None:
        return _engine_cache
    try:
        guid = _machine_guid()
        # Tolerate sub-second jitter, or every sample would look like a new boot
        boot_key = (int(time.time() * 1000) - int(_k32.GetTickCount64())) // 10000
        prev = _load_engine_state()
        state, reasons = _plan_engine_state(prev, guid, boot_key,
                                            _new_engine_id(guid))
        for reason in reasons:
            log(f"engine identity: {reason}")
        if state != prev:
            _save_engine_state(state)
        _engine_cache = state
    except Exception as exc:  # noqa: BLE001 - must never stop a snapshot build
        log(f"engine state read/write failed, serving a volatile identity: {exc!r}")
        guid = "unknown"
        try:
            guid = _machine_guid()
        except Exception:  # noqa: BLE001
            pass
        _engine_cache = {"schema_version": 2, "machine_guid": guid,
                         "engine_id": _new_engine_id(guid), "boot_key": 0,
                         "boots": 1}
    return _engine_cache


def _engine_id() -> bytes:
    """SnmpEngineID, stable for as long as the machine keeps its MachineGuid."""
    return bytes.fromhex(_engine_state()["engine_id"])


def _engine_boots() -> int:
    """snmpEngineBoots, incremented once per system boot."""
    return int(_engine_state()["boots"])


def _register_v3_users(eng) -> int:
    """Load the SNMPv3 users and register them. Returns how many are usable.

    Nothing here is fatal on its own. A site that has not provisioned v3 yet is
    the normal case, and a store that cannot be used is reported rather than
    allowed to stop an agent that is still serving v2c perfectly well. What is
    not acceptable is silence: every reason a user did not load is logged,
    because the symptom at the other end is an authentication failure that says
    nothing about the cause.
    """
    try:
        users, problems = usm.load_store(USM_STORE, _engine_id())
    except Exception as exc:  # noqa: BLE001 - never stop the agent starting
        log(f"[!] the SNMPv3 store could not be loaded: {exc!r}")
        return 0
    for problem in problems:
        log(f"[!] SNMPv3: {problem}")
    registered = 0
    for user in users:
        try:
            for warning in usm.check_algorithms(user.auth, user.priv):
                log(f"[!] SNMPv3 user {user.name!r}: {warning}")
            config.add_v3_user(
                eng, user.name,
                usm.AUTH_PROTOCOLS[user.auth], user.auth_key,
                usm.PRIV_PROTOCOLS[user.priv], user.priv_key,
                authKeyType=config.USM_KEY_TYPE_LOCALIZED,
                privKeyType=config.USM_KEY_TYPE_LOCALIZED)
            # authPriv only. A read-only agent still discloses an inventory, a
            # software list and an ARP table, so there is no level below this
            # worth offering.
            config.add_vacm_user(eng, 3, user.name, "authPriv", (1, 3, 6))
            registered += 1
            log(f"SNMPv3 user {user.name!r} registered ({user.auth} + {user.priv})")
        except Exception as exc:  # noqa: BLE001
            log(f"[!] SNMPv3 user {user.name!r} could not be registered: {exc!r}")
    if not users and not problems:
        log("SNMPv3: no users provisioned; run `jt-snmpd.exe user add` to add one")
    return registered

MAXTEMP_FILE = os.path.join(STATE_DIR, "state", "disk-maxtemp.json")
_maxtemp_cache: dict[str, int] | None = None


def observed_max_temp(name: str, current: int | None) -> int | None:
    """Record and return the highest temperature **observed** for a disk.

    LibreNMS's smart application has a Max Temp graph fed by the `max_temp` key
    in the JSON. Windows' storage APIs only expose thresholds (warning,
    critical), never "the highest this disk has ever been", and putting a
    threshold there would mislabel the line. So this tracks the highest value
    actually measured: "the maximum observed since jt-snmpd was installed" is a
    real number rather than a stand-in.

    The file is written only when the maximum genuinely rises. The snapshot
    rebuilds every five seconds, so writing every time would be seventeen
    thousand needless disk writes a day — against the requirement never to slow
    the host down.
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
        log(f"disk-maxtemp write failed (nothing else is affected): {exc!r}")
    return current


def _load_index_map() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, UnicodeError):
        return {"schema_version": 1, "interfaces": {}, "next_if_index": 1}


def _save_index_map(m: dict) -> None:
    """temp file, flush, atomic replace, keep a .bak. A corrupted
    index-map is the most expensive way this can fail."""
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
        log(f"index-map write failed: {exc!r}")


def get_interfaces() -> list[dict]:
    """hardware interfaces only by default. A Hyper-V host reports 40
    to 80 interfaces including WFP LightWeight Filters, Teredo and isatap;
    publishing all of them creates a mass of useless ports and orphaned RRDs.

    ifIndex is assigned from the persistent NET_LUID, because Windows'
    own InterfaceIndex is not guaranteed stable across reboots."""
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
            # NIC teaming / SET: both the team interface and its members report
            # HardwareInterface=TRUE, and publishing both makes LibreNMS count
            # the same traffic twice. NDIS marks team members with
            # ConnectionType Passive(2), and ordinary interfaces and the team
            # itself with Dedicated(1).
            # NET_IF_CONNECTION_TYPE: Dedicated=1 Passive=2 Demand=3
            if r.ConnectionType == 2:
                log(f"interface {r.Alias!r} is a team member "
                    f"(ConnectionType=Passive); not emitted")
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
                # Windows' native InterfaceIndex. The IP, ARP and neighbour
                # tables are all indexed by it, while our ifTable uses the
                # persistent LUID-derived index. The two have to be
                # mapped, or LibreNMS binds addresses to the wrong port or finds
                # nothing at all — which happened: 127.0.0.1 has Windows index 1,
                # and ended up attached to the Ethernet adapter.
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


# ------------------------------------------------------------ System identity
# Role codes from DsRoleGetPrimaryDomainInformation
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
    """Determine whether this machine is a domain controller, via
    DsRoleGetPrimaryDomainInformation.

    Three sysObjectID branches are required: workstation, server, domain
    controller — and LibreNMS's Windows.php uses the third to call
    getDatacenterVersion().
    Splitting only into client and server puts a domain controller in the server
    branch, which produces a different version string.

    The API lives in netapi32.dll and is safe to call outside a domain, where it
    reports a standalone role.
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
        log(f"domain controller check failed; treating as not a DC: {exc!r}")
        return False


def get_product_type() -> str:
    """Return 'client', 'server' or 'domain_controller'.

    InstallationType can be Client, Server, Server Core or Windows Server Core
    depending on the release, so this uses startswith("server") rather than an
    equality test — Server Core has to be recognised as a server (the platform
    definition of done).
    """
    is_server = False
    try:
        it = str(_reg(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
                      "InstallationType"))
        is_server = it.lower().startswith("server")
    except OSError:
        # Older or trimmed installations may not have the value; fall back to
        # ProductType: 1=WinNT (workstation) 2=LanmanNT (DC) 3=ServerNT (server)
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
    """mirror the Microsoft SNMP Service format exactly, or the regex
    in LibreNMS's Windows.php will not match and the Hardware, Version and
    Features fields come out blank.

    Measured: the real MS SNMP service on Win11 build 26200 reports NT version
    6.3, not 10.0."""
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
    """hrSystemNumUsers — the real number of interactive sessions.

    This used to return a hardcoded 1, which is simply wrong on a Remote Desktop
    Session Host where a single machine may have dozens of users. It surfaced
    while working through the Windows Server scenarios: not "needs verifying",
    but wrong as written.

    Only Active and Disconnected sessions are counted. Disconnected means the
    user is still logged on with resources still held, which is what RFC 2790's
    hrSystemNumUsers ("number of user sessions") is meant to include. System
    sessions such as Listen and Idle are not counted.
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
        log(f"WTSEnumerateSessions failed: {exc!r}")
        return 0


def _hr_system_date() -> bytes:
    """hrSystemDate is DateAndTime (RFC 2579): 8 or 11 binary bytes, not text.
    Layout: year(2), month, day, hour, minute, second, deciseconds, and
    optionally the UTC offset direction, hours and minutes."""
    lt = time.localtime()
    off = -(time.altzone if lt.tm_isdst else time.timezone)   # seconds, east positive
    sign = b"+" if off >= 0 else b"-"
    off = abs(off)
    return (lt.tm_year.to_bytes(2, "big")
            + bytes([lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, min(lt.tm_sec, 59), 0])
            + sign + bytes([off // 3600, (off % 3600) // 60]))


def uptime_centis() -> int:
    return int(_k32.GetTickCount64() // 10)


# --------------------------------------------------------------- Snapshot
# The JT private subtree. No IANA PEN has been assigned yet, so this borrows a
# reserved branch under a Microsoft-compatible prefix; once a PEN is assigned the
# tree moves and a mapping is published.
JT = (1, 3, 6, 1, 4, 1, 99999, 1)
JTAGENT = JT + (1,)          # scalars
JTCOLL = JT + (2, 1)         # jtAgentCollectorTable
# jtDiskHealthTable — per-disk health, so LibreNMS can produce a state-class
# sensor (the green/red indicator). LibreNMS's device overview page has a sensors
# section but **no** applications section, so the smart application's (OK)/(FAIL)
# only appears under the Apps tab. Showing health on the overview needs a state
# sensor, and that needs an OID to map to.
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
HRDEVTYPE = HR + (3, 1)                    # hrDeviceTypes prefix
DIO = (1, 3, 6, 1, 4, 1, 2021, 13, 15, 1, 1)  # UCD-DISKIO
UCDLA = (1, 3, 6, 1, 4, 1, 2021, 10, 1)    # UCD laTable (load average)
UCDSS = (1, 3, 6, 1, 4, 1, 2021, 11)       # UCD systemStats
ENTPHY = (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1)   # ENTITY-MIB entPhysicalEntry
IPG = (1, 3, 6, 1, 2, 1, 4)                # IP-MIB ip group
ICMPG = (1, 3, 6, 1, 2, 1, 5)              # IP-MIB icmp group
TCPG = (1, 3, 6, 1, 2, 1, 6)               # TCP-MIB tcp group
UDPG = (1, 3, 6, 1, 2, 1, 7)               # UDP-MIB udp group
SNMPG = (1, 3, 6, 1, 2, 1, 11)             # SNMPv2-MIB snmp group
IPADDR = (1, 3, 6, 1, 2, 1, 4, 20, 1)      # RFC1213 ipAddrTable (IPv4; what
                                           # LibreNMS actually reads)
IPADDRESS = (1, 3, 6, 1, 2, 1, 4, 34, 1)   # IP-MIB ipAddressTable（IPv4 + IPv6）
IPNETPHYS = (1, 3, 6, 1, 2, 1, 4, 35, 1)   # IP-MIB ipNetToPhysicalTable（ARP / ND）
ENTSENS = (1, 3, 6, 1, 2, 1, 99, 1, 1, 1)  # ENTITY-SENSOR-MIB entPhySensorEntry
ENT_SENSOR_BASE = 5000                     # entPhysicalIndex range: sensors
ENT_THERMAL_BASE = 5500                    # entPhysicalIndex range: thermal zones
ENT_CPUFREQ_BASE = 5900                    # entPhysicalIndex range: CPU frequency

# The SNMP-FRAMEWORK-MIB snmpEngine group.
# **This is not optional.** LibreNMS's Core.php takes the max() of three sources
# to decide uptime:
#
#     max(round(sysUpTime/100),
#         bad_snmpEngineTime ? 0 : snmpEngineTime,
#         bad_hrSystemUptime ? 0 : round(hrSystemUptime/100))
#
# and windows.yaml sets **only bad_hrSystemUptime**, never bad_snmpEngineTime.
#
# sysUpTime and hrSystemUptime are both TimeTicks (Unsigned32, hundredths of a
# second), so they wrap after 2^32/100 seconds — about 497.1 days. After the wrap
# the value drops sharply, LibreNMS concludes "Device rebooted", and a false
# alert fires. snmpEngineTime is counted in **seconds** up to 2147483647 (about
# 68 years), so providing it gives max() a source that does not wrap and the
# false reboot alert disappears.
SNMPFW = (1, 3, 6, 1, 6, 3, 10, 2, 1)      # snmpEngine group

# NET-SNMP-EXTEND-MIB. Every LibreNMS application (smart and the rest) comes
# through here:
#   discovery: walk nsExtendStatus
#   polling:   get nsExtendOutputFull."<token>"
# All over SNMP: beyond jt-snmpd itself the monitored host needs neither the
# LibreNMS agent nor smartctl.
NSEXT = (1, 3, 6, 1, 4, 1, 8072, 1, 3, 2)
NSEXT_CFG = NSEXT + (2, 1)                 # nsExtendConfigTable
NSEXT_OUT1 = NSEXT + (3, 1)                # nsExtendOutput1Table
NSEXT_OUT2 = NSEXT + (4, 1)                # nsExtendOutput2Table

# Byte cap for a single varbind. Responses are capped at 1400 bytes and never
# fragmented, so an oversized OCTET STRING simply will not fit in a GET response.
# The compressed SMART JSON is usually 400-600 bytes, but it grows with the disk
# count — past the cap, disks are dropped, and that **must be logged**. Truncating
# quietly would leave the impression that every disk is being monitored.
MAX_EXTEND_BYTES = 1100

# hrDeviceIndex ranges.
# 196608 upwards is the Microsoft SNMP Service convention for CPUs, kept for
# compatibility.
DEV_BASE_CPU = 196608
DEV_BASE_NET = 262144
DEV_BASE_DISK = 327680

# entPhysicalIndex ranges
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
    # three branches matching the three version lookup paths in
    # LibreNMS's Windows.php
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
    add(SYS + (3, 0), rfc1902.TimeTicks(up & U32))     # wraps naturally
    # sysUpTime is TimeTicks, so it wraps after 2^32 hundredths of a second —
    # about 497.1 days. That is the type RFC 3418 mandates and every conforming
    # agent behaves the same way, the built-in Windows service included.
    #
    # The wrap cannot be avoided; the **false reboot alert** can. LibreNMS takes
    # the max() of three sources, and snmpEngineTime counts in seconds up to
    # 2147483647 (about 68 years) without wrapping. With it available, max()
    # switches to snmpEngineTime when sysUpTime wraps, the value keeps rising,
    # and LibreNMS's `if ($uptime < $device->uptime)` never becomes true.
    _engine_secs = min(int(_k32.GetTickCount64() // 1000), 2147483647)
    add(SNMPFW + (1, 0), rfc1902.OctetString(_engine_id()))   # snmpEngineID
    add(SNMPFW + (2, 0), rfc1902.Integer32(_engine_boots()))  # snmpEngineBoots
    add(SNMPFW + (3, 0), rfc1902.Integer32(_engine_secs))     # snmpEngineTime
    add(SNMPFW + (4, 0), rfc1902.Integer32(1400))             # snmpEngineMaxMessageSize
    add(SYS + (4, 0), octet(CFG["contact"]))
    add(SYS + (5, 0), octet(host))
    add(SYS + (6, 0), octet(CFG["location"]))
    add(SYS + (7, 0), rfc1902.Integer32(76))           # always 76
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
        # ifXTable — LibreNMS's windows.yaml does not set bad_ifXEntry, so the
        # 64-bit counters are used
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

    # --- HOST-RESOURCES: hrSystem (complete) ---
    perf = _collector("perf_info", get_perf_info, None)
    add(HR + (1, 1, 0), rfc1902.TimeTicks(up & U32))                    # hrSystemUptime
    add(HR + (1, 2, 0), octet(_hr_system_date()))                       # hrSystemDate
    add(HR + (1, 3, 0), rfc1902.Integer32(0))                           # hrSystemInitialLoadDevice
    add(HR + (1, 4, 0), octet(""))                                      # hrSystemInitialLoadParameters
    add(HR + (1, 5, 0), rfc1902.Gauge32(
        _collector("sessions", get_session_count, 0)))                  # hrSystemNumUsers
    # hrSystemProcesses — what LibreNMS's System → Processes graph reads.
    # GetPerformanceInfo is preferred: a single call, tens of microseconds. Only
    # when that is unavailable does this fall back to a Toolhelp32 snapshot,
    # which enumerates every process and costs 50-300 ms with 300 of them
    # .
    nproc = perf.ProcessCount if perf is not None else _collector(
        "processes", get_process_count, 0)
    add(HR + (1, 6, 0), rfc1902.Gauge32(nproc))
    add(HR + (1, 7, 0), rfc1902.Integer32(0))                           # hrSystemMaxProcesses (0 = no limit)

    mem = _collector("memory", get_memory, None)
    if mem is None:
        mem = MEMORYSTATUSEX()
    add(HR + (2, 2, 0), rfc1902.Integer32(min(mem.ullTotalPhys // 1024, INT32_MAX)))

    # --- hrStorageTable ---
    # The memory pool names deliberately match net-snmp's wording, so LibreNMS's
    # mempool discovery files them under the right categories on the Memory page
    # (system, virtual, cached, buffers, shared, swap).
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

        # Cached Memory — the Windows system file cache, matching net-snmp's
        # "Cached memory". A cache is by nature "used but reclaimable", so used
        # equals total, as net-snmp reports it.
        rows.append(("Cached Memory", HR + (2, 1, 1), cache, cache))

        # Swap space — the page file portion. Windows' commit limit is physical
        # memory plus the page file, so the page file size is the commit limit
        # minus physical memory. This is a different concept from "Virtual
        # Memory" (the commit charge) and the two must not be conflated
        # .
        swap_total = max(commit_limit - phys_total, 0)
        swap_used = max(commit_total - (phys_total - mem.ullAvailPhys), 0)
        if swap_total:
            rows.append(("Swap Space", HR + (2, 1, 3),
                         swap_total, min(swap_used, swap_total)))

        # Kernel pools. Windows-specific, but hrStorageOther is where RFC 2790
        # puts things like this.
        if kpaged:
            rows.append(("Kernel Paged Pool", HR + (2, 1, 1), kpaged, kpaged))
        if knonpaged:
            rows.append(("Kernel Nonpaged Pool", HR + (2, 1, 1), knonpaged, knonpaged))
    _vols = _collector("volumes", get_fixed_volumes, [])
    for vol in _vols:
        # The description format deliberately does **not** follow the Microsoft
        # SNMP Service's
        #   "C: Label:xxx  Serial Number 1A2B3C4D"
        # A serial number means nothing for monitoring and "Label:" is just
        # noise. Instead:
        #   with a label    -> "C: System"
        #   without a label -> "C:"
        # The serial is still available from ENTITY-MIB's entPhysicalSerialNum,
        # so nothing is lost. Labels may be non-ASCII and always go through
        # octet() to become UTF-8.
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
        """A human-recognisable name for a disk.

        "PhysicalDrive0" on its own says nothing about which disk it is on a
        machine with several — reported from the field. The model is the most
        recognisable piece of information, the serial second; but the serial is
        long, so it belongs in ENTITY-MIB's entPhysicalSerialNum rather than the
        display name.
        """
        n = disk["index"]
        model = (disk.get("model") or "").strip()
        if not model or model == f"PhysicalDrive{n}":
            return f"PhysicalDrive{n}"
        # Following net-snmp's style on Linux ("/dev/sda: SATA CVB-CD256"):
        # "device: model", with no capacity. LibreNMS's column is narrow, and
        # anything longer gets truncated to something like "...M.2 2280 256G"
        # which shows none of the identifying part — reported from the field.
        # Repeated vendor words and the capacity suffix are dropped, leaving the
        # most recognisable form of the model.
        words = model.split()
        if len(words) > 1 and words[0].upper() == words[1].upper():
            words = words[1:]                       # "QEMU QEMU HARDDISK" → "QEMU HARDDISK"
        model = " ".join(words)
        for suffix in (" 2280", " M.2"):            # form factor does not aid
                                                # recognition
            model = model.replace(suffix, "")
        model = model.strip()
        if len(model) > 28:
            model = model[:28].rstrip()
        return f"PhysicalDrive{n}: {model}"

    # --- The whole hrDeviceTable family ---
    # every table derived from hrDevice (hrProcessor, hrNetwork,
    # hrDiskStorage) shares one hrDeviceIndex space rather than inventing its own.
    inv = _collector("inventory", get_inventory, {})

    # (a) Processors -> hrProcessorTable
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

    # (b) Network interfaces -> hrNetworkTable
    for nic in ifaces:
        di = DEV_BASE_NET + nic["idx"]
        add(HRDEV + (1, di), rfc1902.Integer32(di))
        add(HRDEV + (2, di), rfc1902.ObjectIdentifier(HRDEVTYPE + (4,)))  # hrDeviceNetwork
        add(HRDEV + (3, di), octet(nic["descr"]))
        add(HRDEV + (4, di), rfc1902.ObjectIdentifier((0, 0)))
        # hrDeviceStatus: running(2) only when the interface is up, else down(5)
        add(HRDEV + (5, di), rfc1902.Integer32(2 if nic["oper"] == 1 else 5))
        add(HRDEV + (6, di), rfc1902.Counter32((nic["in_err"] + nic["out_err"]) & U32))
        add(HRNET + (1, di), rfc1902.Integer32(nic["idx"]))               # hrNetworkIfIndex

    # (c) Physical disks -> hrDiskStorageTable
    for disk in inv.get("disks", []):
        di = DEV_BASE_DISK + disk["index"]
        add(HRDEV + (1, di), rfc1902.Integer32(di))
        add(HRDEV + (2, di), rfc1902.ObjectIdentifier(HRDEVTYPE + (6,)))  # hrDeviceDiskStorage
        add(HRDEV + (3, di), octet(_disk_label(disk)))
        add(HRDEV + (4, di), rfc1902.ObjectIdentifier((0, 0)))
        add(HRDEV + (5, di), rfc1902.Integer32(2))
        add(HRDEV + (6, di), rfc1902.Counter32(0))
        add(HRDISK + (1, di), rfc1902.Integer32(1))                       # readWrite
        # hrDiskStorageMedia: 3 = hardDisk. Removable devices are other(1); the
        # medium type is not guessed.
        add(HRDISK + (2, di), rfc1902.Integer32(1 if disk["removable"] else 3))
        add(HRDISK + (3, di), rfc1902.Integer32(1 if disk["removable"] else 2))  # TruthValue
        # hrDiskStorageCapacity is Integer32 in KB, so anything above 2 TB
        # overflows. The RFC provides no allocation-unit mechanism here, so the
        # value is clamped and the limitation documented (same root cause as
        # §2.1).
        add(HRDISK + (4, di), rfc1902.Integer32(min(disk["size_bytes"] // 1024, INT32_MAX)))

    # --- ENTITY-MIB entPhysicalTable (the LibreNMS Inventory page) ---
    # the data comes from GetSystemFirmwareTable('RSMB'), needing
    # neither WMI nor any special privilege.
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

    # --- IP / ICMP / TCP / UDP groups (the whole LibreNMS Netstats set) ---
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
        # icmp group: 1-13 are the In* counters, 14-26 the Out*, ordered per
        # RFC 1213
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
        # MaxConn of -1 means dynamically allocated; Windows returns 0xFFFFFFFF,
        # which has to be converted back
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
        """Expand address bytes into an OID suffix, one sub-identifier per byte."""
        return tuple(raw)

    def _prefix_mask(plen: int) -> str:
        """IPv4 prefix length to a dotted-decimal mask, as ipAdEntNetMask wants."""
        m = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
        return ".".join(str((m >> sh) & 0xFF) for sh in (24, 16, 8, 0))

    # Windows' native InterfaceIndex mapped to our persistent ifIndex.
    # Addresses not in this map — loopback, tunnels, filtered-out virtual
    # interfaces — are not emitted at all:
    # pointing at a non-existent ifIndex only produces orphaned data in
    # LibreNMS.
    _win2if = {n["win_idx"]: n["idx"] for n in ifaces if "win_idx" in n}

    addrs = _collector("ip_addresses", get_ip_addresses, [])
    for a in addrs:
        our_if = _win2if.get(a["if_index"])
        if our_if is None:
            continue
        idx = _oid_addr(a["raw"])
        if a["version"] == 4:
            # RFC1213 ipAddrTable — what LibreNMS's ipv4-addresses mostly reads
            add(IPADDR + (1,) + idx, rfc1902.IpAddress(a["addr"]))        # ipAdEntAddr
            add(IPADDR + (2,) + idx, rfc1902.Integer32(our_if))           # ipAdEntIfIndex
            add(IPADDR + (3,) + idx,
                rfc1902.IpAddress(_prefix_mask(a["prefix_len"])))         # ipAdEntNetMask
            add(IPADDR + (4,) + idx, rfc1902.Integer32(1))                # ipAdEntBcastAddr
            add(IPADDR + (5,) + idx, rfc1902.Integer32(65535))            # ipAdEntReasmMaxSize
        # IP-MIB ipAddressTable — shared by IPv4 and IPv6, indexed by
        # (addrType, addr)
        atype = 1 if a["version"] == 4 else 2
        aidx = (atype, len(a["raw"])) + idx
        add(IPADDRESS + (3,) + aidx, rfc1902.Integer32(our_if))           # ipAddressIfIndex
        add(IPADDRESS + (4,) + aidx, rfc1902.Integer32(1))                # ipAddressType unicast
        add(IPADDRESS + (5,) + aidx, rfc1902.Integer32(a["prefix_len"]))  # prefix length (simplified)
        add(IPADDRESS + (6,) + aidx, rfc1902.Integer32(1))                # ipAddressOrigin
        add(IPADDRESS + (7,) + aidx, rfc1902.Integer32(1))                # ipAddressStatus preferred
        add(IPADDRESS + (10,) + aidx, rfc1902.Integer32(1))               # ipAddressRowStatus

    # --- ipNetToPhysicalTable（ARP / IPv6 ND）---
    # off by default. The local ARP table is a target list for
    # lateral movement.
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
    # The built-in service has both of these tables (20 and 27 rows as measured)
    # and this agent originally had neither. They raise no disclosure concern —
    # they describe this machine's own volumes — and are not on the withheld
    # list, so they are emitted by default.
    HRFS_TYPE_NTFS = HR + (3, 9, 4)      # hrFSNTFS
    HRFS_TYPE_FAT32 = HR + (3, 9, 3)     # hrFSFat32 (approximate; the RFC does
                                         # not distinguish FAT from FAT32)
    HRFS_TYPE_OTHER = HR + (3, 9, 1)     # hrFSOther
    _FS_TYPES = {"NTFS": HRFS_TYPE_NTFS, "FAT32": HRFS_TYPE_FAT32,
                 "FAT": HRFS_TYPE_FAT32, "REFS": HRFS_TYPE_OTHER}

    # hrPartition is indexed by (hrDeviceIndex, hrPartitionIndex)
    # . There is no reliable mapping from a volume to the
    # physical disk it lives on — Storage Spaces, dynamic disks and multiple
    # mount points all break the one-to-one assumption — so everything is
    # attached to the first disk's hrDeviceIndex and the limitation documented.
    # Reporting the wrong parent is worse than reporting an unknown one.
    _disks = inv.get("disks", [])
    _part_dev = DEV_BASE_DISK + (_disks[0]["index"] if _disks else 0)

    for pi, vol in enumerate(_vols, start=1):
        label = vol["label"] or vol["root"].rstrip("\\")
        add(HRPART + (1, _part_dev, pi), rfc1902.Integer32(pi))          # hrPartitionIndex
        add(HRPART + (2, _part_dev, pi), octet(label))                   # hrPartitionLabel
        add(HRPART + (3, _part_dev, pi), octet(vol["serial"]))           # hrPartitionID
        # hrPartitionSize is in KB as Integer32, so it caps at 2 TB and is clamped
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
        add(HRFS + (7, pi), rfc1902.Integer32(2))                        # hrFSStorageIndex placeholder
        add(HRFS + (8, pi), rfc1902.Integer32(0))                        # hrFSLastFullBackupDate
        add(HRFS + (9, pi), rfc1902.Integer32(0))                        # hrFSLastPartialBackupDate

    # --- ipRouteTable（RFC1213）---
    # RFC1213's ipRouteTable is indexed by **destination address alone**, so a
    # destination can appear only once. Real hosts routinely have a multicast
    # route (224.0.0.0) and a broadcast route (255.255.255.255) per adapter, and
    # sometimes equal-cost multipath. Measured on a laptop with seven addresses,
    # 224.0.0.0 appeared several times, tripping the duplicate-OID guard and
    # stopping the agent from starting.
    #
    # The fix: keep only the entry with the lowest metric for each destination,
    # which is the route that would actually be used. This is inherent to
    # RFC1213; the newer ipForwardTable and inetCidrRouteTable include the
    # interface in the index. Surplus routes are dropped rather than emitted
    # under a wrong index.
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

    # --- ENTITY-SENSOR-MIB (the LibreNMS sensors module) ---
    # sensor data does **not** come from LibreHardwareMonitor, which
    # depends on WinRing0 — on Microsoft's vulnerable-driver blocklist, and
    # enough to trigger Defender on an HVCI endpoint. Native
    # IOCTL_STORAGE_QUERY_PROPERTY with StorageDeviceTemperatureProperty is used
    # instead. Virtual disks usually have no temperature sensor, in which case
    # the row does not appear (§6.9: never fabricate a value).
    def ent_sensor(idx, sensor_type, scale, precision, value, status, unit_descr):
        add(ENTSENS + (1, idx), rfc1902.Integer32(sensor_type))   # entPhySensorType
        add(ENTSENS + (2, idx), rfc1902.Integer32(scale))         # entPhySensorScale
        add(ENTSENS + (3, idx), rfc1902.Integer32(precision))     # entPhySensorPrecision
        add(ENTSENS + (4, idx), rfc1902.Integer32(value))         # entPhySensorValue
        add(ENTSENS + (5, idx), rfc1902.Integer32(status))        # 1=ok 2=unavailable 3=nonoperational
        # entPhySensorValueTimeStamp means "the sysUpTime when this reading was
        # taken", not the age of the sensor. Readings are taken as the snapshot
        # is rebuilt, so the current sysUpTime is the correct value.
        add(ENTSENS + (6, idx), rfc1902.TimeTicks(up & U32))      # entPhySensorValueTimeStamp
        add(ENTSENS + (7, idx), rfc1902.Integer32(60))            # entPhySensorValueUpdateRate
        add(ENTSENS + (8, idx), octet(unit_descr))                # entPhySensorUnitsDisplay

    # entPhySensorType (RFC 3433): other(1), celsius(8), percentRH(9), rpm(10),
    # cmm(11), truthvalue(12), volts/amps/watts/hertz…
    #
    # **Warning: LibreNMS accepts only these types**
    # (includes/discovery/sensors/entity-sensor.inc.php):
    #   voltsDC voltsAC amperes watts hertz percentRH rpm celsius dBm
    # `other(1)` is not in that map and the whole row is **silently discarded**.
    # The first version published NVMe endurance and available spare as other, so
    # the field saw a temperature and no SMART metrics at all while this side
    # looked perfectly healthy — it took a long time to find that the problem was
    # the map. Counter-style SMART metrics therefore go through
    # NET-SNMP-EXTEND-MIB instead (see the smart application below).
    SENSOR_CELSIUS, SENSOR_OTHER, SENSOR_HERTZ = 8, 1, 7
    SCALE_UNITS, SCALE_MEGA, STATUS_OK = 9, 11, 1
    for disk in inv.get("disks", []):
        base = ENT_SENSOR_BASE + disk["index"] * 10
        name = _disk_label(disk)

        temp = disk.get("temp_c")
        if temp is not None:
            ent_sensor(base, SENSOR_CELSIUS, SCALE_UNITS, 0, int(temp),
                       STATUS_OK, "C")
            # The sensor name does not repeat the full disk name: LibreNMS uses
            # entPhysicalName directly as the sensor label, and repeating the
            # model only overflows the column (reported from the field). The
            # parent entry is already that disk, so the hierarchy says which one.
            ent(base, ENT_CLASS_OTHER, descr=f"Temperature ({name})",
                name=f"PhysicalDrive{disk['index']} Temp",
                parent=ENT_DISK_BASE + disk["index"], relpos=1)

        health = disk.get("health") or {}
        # NVMe endurance: Percentage Used (0-255; above 100 means the estimated
        # life has been exceeded)
        if "percentage_used" in health:
            ent_sensor(base + 1, SENSOR_OTHER, SCALE_UNITS, 0,
                       int(health["percentage_used"]), STATUS_OK, "%")
            ent(base + 1, ENT_CLASS_OTHER, descr=f"Endurance Used ({name})",
                name=f"PhysicalDrive{disk['index']} Wear",
                parent=ENT_DISK_BASE + disk["index"], relpos=2)
        # Available spare, as a percentage
        if "avail_spare_pct" in health:
            ent_sensor(base + 2, SENSOR_OTHER, SCALE_UNITS, 0,
                       int(health["avail_spare_pct"]), STATUS_OK, "%")
            ent(base + 2, ENT_CLASS_OTHER, descr=f"Available Spare ({name})",
                name=f"PhysicalDrive{disk['index']} Spare",
                parent=ENT_DISK_BASE + disk["index"], relpos=3)

    # --- ACPI thermal zones (system / mainboard temperature) ---
    # CPU package temperature needs MSR access, which needs a kernel driver
    # (forbidden by rule 8). ACPI thermal zones are the alternative the firmware
    # already publishes: most laptops and some desktops have them, virtual
    # machines do not — and where there are none these rows simply do not appear
    # (§6.9: never fabricate).
    for zi, tz in enumerate(_collector("thermal_zones",
                                       lambda: (_sensors.read_thermal_zones()
                                                if _sensors else []), [])):
        idx = ENT_THERMAL_BASE + zi
        ent_sensor(idx, SENSOR_CELSIUS, SCALE_UNITS, 0, int(round(tz.celsius)),
                   STATUS_OK, "C")
        ent(idx, ENT_CLASS_OTHER, descr=f"Thermal Zone ({tz.name})",
            name=f"ThermalZone{zi}", parent=ENT_MAINBOARD, relpos=10 + zi)

    # --- CPU frequency ---
    # One sensor, not one per logical processor: CallNtPowerInformation reports a
    # package-level P-state, and every core returns the same value in practice
    # (all six cores at 3600 on one test machine, 2501 on another). Sixty-four
    # identical graphs on a 64-core host have no value and only slow LibreNMS
    # down.
    #
    # The mega scale is necessary: entPhySensorValue is Integer32, and 3600 MHz
    # expressed in Hz is 3.6e9, which overflows immediately.
    _freqs = _collector("cpu_frequency",
                        lambda: (_sensors.read_cpu_frequencies() if _sensors else []), [])
    if _freqs:
        cur = max(f.current_mhz for f in _freqs)
        ent_sensor(ENT_CPUFREQ_BASE, SENSOR_HERTZ, SCALE_MEGA, 0, int(cur),
                   STATUS_OK, "MHz")
        ent(ENT_CPUFREQ_BASE, ENT_CLASS_OTHER, descr="CPU Frequency",
            name="CPU Frequency", parent=ENT_MAINBOARD, relpos=20)

    # --- Battery (private OIDs only) ---
    # LibreNMS's entity-sensor map has no charge or percent type, so publishing
    # it as a standard sensor would produce nothing. It lives in the private
    # subtree for walking and for our own diagnosis, without pretending a graph
    # will appear.
    _bat = _collector("battery",
                      lambda: (_sensors.read_battery() if _sensors else None), None)
    if _bat is not None:
        add(JTAGENT + (40, 0), rfc1902.Gauge32(_bat.percent))          # jtBatteryPercent
        add(JTAGENT + (41, 0), rfc1902.Integer32(1 if _bat.on_ac else 2))  # jtBatteryOnAC
        if _bat.seconds_left is not None:
            add(JTAGENT + (42, 0), rfc1902.Gauge32(_bat.seconds_left))  # jtBatterySecondsLeft

    # --- jtDiskHealthTable: per-disk health (a LibreNMS state sensor) ---
    # The grading is deliberately conservative:
    #   ok(1)       the firmware's self-assessment passed and there is no known
    #               sign of degradation
    #   warning(2)  reallocated or pending sectors have appeared, or the
    #               temperature is over the threshold — the disk still works, but
    #               it belongs on a replacement plan
    #   critical(3) the firmware itself says failure is imminent (SMART RETURN
    #               STATUS reports a threshold exceeded)
    #   unknown(4)  the disk did not answer — a USB bridge that does not pass
    #               SMART commands through, for instance. Saying "unknown"
    #               explicitly, rather than defaulting to healthy.
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
                           for a in (5, 197, 198))     # reallocated / pending /
                                                       # uncorrectable
            t = d.get("temp_c")
            if degraded or (isinstance(t, int) and t >= 70):
                st = DISK_STATE_WARNING
        add(JTDISK + (1, di), rfc1902.Integer32(di))                    # jtDiskHealthIndex
        add(JTDISK + (2, di), octet(f"PhysicalDrive{di}"))              # jtDiskHealthName
        add(JTDISK + (3, di), rfc1902.Integer32(st))                    # jtDiskHealthState
        add(JTDISK + (4, di), octet(_disk_label(d)[:64]))               # jtDiskHealthDescr

    # --- NET-SNMP-EXTEND-MIB: LibreNMS's smart application ---
    # The supported way LibreNMS reads SMART (json_app_get):
    #   discovery  walk nsExtendStatus
    #   polling    get  nsExtendOutputFull."smart"
    # The value is base64(gzip(json)) — explicitly supported by LibreNMS, and
    # necessary: responses cap at 1400 bytes and are never fragmented, so
    # uncompressed JSON exceeds it at two disks.
    _smart_disks = []
    for d in inv.get("disks", []):
        if not d.get("health"):
            continue
        nm = f"PhysicalDrive{d['index']}"
        _smart_disks.append({
            "name": nm, "health": d["health"],
            "max_temp": observed_max_temp(nm, d.get("temp_c")),
            # Replacing a disk in the field needs the model and serial to know
            # which one
            "model": d.get("model"), "serial": d.get("serial"),
            "vendor": d.get("vendor"),
        })
    if _smart_disks and _smartjson is not None:
        payload = _smartjson.build_smart_json(_smart_disks)
        blob = _smartjson.encode_extend_output(payload)
        # With many disks this can exceed the single-varbind cap. Drop entries
        # until it fits, but **log how many were dropped** — truncating quietly
        # leaves the impression that every disk is being monitored.
        dropped = 0
        while len(blob) > MAX_EXTEND_BYTES and len(_smart_disks) > 1:
            _smart_disks.pop()
            dropped += 1
            payload = _smartjson.build_smart_json(_smart_disks)
            blob = _smartjson.encode_extend_output(payload)
        if dropped:
            log(f"smart application output exceeded {MAX_EXTEND_BYTES} bytes; "
                f"omitted the last {dropped} of "
                f"{len(_smart_disks) + dropped} disks",
                error=True)
        if len(blob) <= MAX_EXTEND_BYTES:
            tok = _extend_index("smart")
            # nsExtendConfigTable: LibreNMS discovery only walks
            # nsExtendStatus, but the remaining columns are filled in so the row
            # looks complete to any other SNMP tool.
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
            # nsExtendOutput2Table (line by line; there is only one line here)
            add(NSEXT_OUT2 + (2,) + tok + (1,), rfc1902.OctetString(blob))  # nsExtendOutLine
            add(NSEXT + (1, 0), rfc1902.Integer32(1))                  # nsExtendNumEntries

    # --- UCD-SNMP systemStats (the LibreNMS System graph group) ---
    # Detailed Processor Usage, Context Switches, Interrupts, I/O and Swap I/O —
    # everything a Linux device shows in that group on LibreNMS comes from here.
    #
    # The field numbers **must come from UCD-SNMP-MIB**, never from memory. This
    # was learned the hard way: 57-63 were assigned by intuition as
    # SwapIn/SwapOut/IOSent/IOReceived/Contexts/Interrupts, when the real order
    # is IOSent(57)/IOReceived(58)/Interrupts(59)/Contexts(60)/SwapIn(62)/
    # SwapOut(63). The result was context switches plotted as I/O — the graphs
    # still had lines, the numbers still moved, and nothing looked wrong.
    #     snmptranslate -m UCD-SNMP-MIB -On UCD-SNMP-MIB::ssRawContexts
    sp = _collector("sys_perf", get_system_perf, None)
    ct = _collector("cpu_times", get_cpu_times_total, None)

    if ct is not None:
        # UCD's ssCpuRaw* counters are in USER_HZ (hundredths of a second);
        # Windows uses 100 ns. The conversion divides by 10^5, and getting the
        # factor wrong makes the percentages meaningless.
        def _hz(v100ns: int) -> int:
            return (v100ns // 100_000) & U32

        add(UCDSS + (50, 0), rfc1902.Counter32(_hz(ct["user"])))            # ssCpuRawUser
        # ssCpuRawNice: Windows has no nice. But LibreNMS's ucd-mib poller
        # requires **all four** of user, nice, system and idle to be present
        # before it creates the Detailed Processor Usage graph (the isset
        # condition in includes/polling/ucd-mib.inc.php).
        #
        # Emitting 0 here is a correct statement — there is never any nice time
        # on Windows — not a fabricated measurement. That is different from
        # iowait and steal below, which are genuinely unmeasurable.
        add(UCDSS + (51, 0), rfc1902.Counter32(0))                          # ssCpuRawNice
        add(UCDSS + (52, 0), rfc1902.Counter32(_hz(ct["system"])))          # ssCpuRawSystem
        add(UCDSS + (53, 0), rfc1902.Counter32(_hz(ct["idle"])))            # ssCpuRawIdle
        # ssCpuRawWait(54): Windows has no iowait — I/O waiting is part of a
        #   thread's wait state, not a separate category of CPU time. It is
        #   **unmeasurable and therefore not emitted**, so LibreNMS shows no I/O
        #   Wait graph. That is the honest outcome.
        # ssCpuRawKernel(55): UCD's definition overlaps ssCpuRawSystem and is
        #   usually 0 on Linux
        add(UCDSS + (56, 0), rfc1902.Counter32(_hz(ct["interrupt"])))       # ssCpuRawInterrupt
        # ssCpuRawSoftIRQ(61) / ssCpuRawSteal(64) / ssCpuRawGuest(65,66)：
        #   no Windows equivalent, so not emitted.

    if sp is not None:
        # I/O in blocks; net-snmp counts 512-byte blocks on Linux
        add(UCDSS + (57, 0), rfc1902.Counter32(
            (sp.IoWriteTransferCount // 512) & U32))                        # ssIORawSent
        add(UCDSS + (58, 0), rfc1902.Counter32(
            (sp.IoReadTransferCount // 512) & U32))                         # ssIORawReceived
        if ct is not None:
            add(UCDSS + (59, 0), rfc1902.Counter32(
                ct["interrupt_count"] & U32))                               # ssRawInterrupts
        add(UCDSS + (60, 0), rfc1902.Counter32(sp.ContextSwitches & U32))   # ssRawContexts
        # Paging activity feeds Swap I/O Activity. Page file reads and writes on
        # Windows are the equivalent of swap on Linux.
        add(UCDSS + (62, 0), rfc1902.Counter32(sp.PageReadCount & U32))     # ssRawSwapIn
        add(UCDSS + (63, 0), rfc1902.Counter32(
            sp.DirtyPagesWriteCount & U32))                                 # ssRawSwapOut

        # The older per-second instantaneous values (ssSwapIn, ssSwapOut,
        # ssIOSent, ssIOReceive, ssSysInterrupts, ssSysContext — fields 3 to 9)
        # are not emitted: LibreNMS reads only the Raw variants, and these would
        # require keeping state between samples.

        # ssIndex and ssErrorName are identification fields that net-snmp
        # provides on Linux. Some tools use them to decide whether UCD is
        # supported at all, and they cost almost nothing.
        add(UCDSS + (1, 0), rfc1902.Integer32(1))                           # ssIndex
        add(UCDSS + (2, 0), octet("systemStats"))                           # ssErrorName

    # laTable (Load Averages): **Windows has no load average.** Linux's loadavg
    # is an exponential moving average of runnable plus uninterruptible-sleep
    # processes, and the Windows scheduler has no equivalent. Substituting the
    # processor queue length would produce a plausible-looking number meaning
    # something else — exactly the fabrication §6.9 forbids. So LibreNMS shows no
    # Load Averages graph on Windows, which is correct.

    # --- SNMPv2-MIB snmp group (the agent's own packet statistics) ---
    # Not read from the OS; accumulated by the agent itself. LibreNMS's
    # netstats-snmp graphs use it, and it is also how the §3.2 gate's drop counts
    # reach the outside world.
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

    # --- JT self-health OIDs  ---
    # §7.3: these and the system group must stay answerable even in degraded
    # mode. They are the only way to tell "the service is alive but broken" from
    # "the service is dead".
    svc_uptime = int((time.monotonic() - _health["start_monotonic"]) * 100)
    snap_age = (int(time.monotonic() - _health["snapshot_built_monotonic"])
                if _health["snapshot_built_monotonic"] else 0)
    add(JTAGENT + (1, 0), octet(AGENT_VERSION))                      # jtAgentVersion
    add(JTAGENT + (2, 0), octet(AGENT_BUILD_DATE))                   # jtAgentBuildDate
    add(JTAGENT + (3, 0), rfc1902.TimeTicks(svc_uptime & U32))       # jtAgentServiceUptime
    # Unreadable means the OID is not emitted
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
    add(JTAGENT + (13, 0), octet("none"))                            # jtAgentVacmPreset (not yet implemented)
    add(JTAGENT + (20, 0), octet(CFG_PATH))                          # jtAgentConfigPath
    add(JTAGENT + (21, 0), octet(LOG_DIR))                           # jtAgentLogPath
    add(JTAGENT + (22, 0), octet(_install_dir()))                    # jtAgentInstallPath
    add(JTAGENT + (23, 0), octet(_config_warnings()))                # jtAgentConfigWarnings
    add(JTAGENT + (30, 0), rfc1902.Counter32(_health["snapshot_failures"] & U32))

    # jtAgentCollectorTable: the health of each collector
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

    # Guard: the correctness of snapshot + bisect rests on there being no
    # duplicate OIDs. A duplicate makes bisect land in the wrong
    # place, and the symptom is values inexplicably showing another field's data.
    for a, b in zip(pairs, pairs[1:]):
        if a[0] == b[0]:
            raise AssertionError(f"duplicate OID: {a[0]}")

    return tuple(p[0] for p in pairs), tuple(p[1] for p in pairs)


# ------------------------------------------------------------- MIB controller
class SnapshotController(AbstractMibInstrumController):
    """Not overriding write_variables makes this a read-only agent by
    construction."""

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
    """cap max-repetitions server-side (25 by default), ignoring any
    larger value a request asks for.

    pysnmp's own implementation caps only the varbind count (max_varbinds=64),
    has no byte cap, and pads the response with endOfMibView up to
    max-repetitions once it reaches the end of the MIB.
    """
    MAXREP_CAP = 25

    def handle_management_operation(self, snmpEngine, stateReference, contextName, PDU):
        try:
            cur = int(v2c.apiBulkPDU.get_max_repetitions(PDU))
            if cur > self.MAXREP_CAP:
                v2c.apiBulkPDU.set_max_repetitions(PDU, self.MAXREP_CAP)
        except Exception as exc:  # noqa: BLE001 - failing to apply the cap must
                                  # not fail the request
            # Log and continue: pysnmp's max_varbinds and the response size
            # limit still bound the result
            log(f"failed to apply max-repetitions cap: {exc!r}")
        return super().handle_management_operation(snmpEngine, stateReference, contextName, PDU)


# ------------------------------------------------------------------- Runtime
class GatedUdpTransport(udp.UdpTransport):
    """Intercept every datagram before pysnmp sees it.

    This is the first line of the whole security design: a packet that is stopped
    here **never reaches the BER decoder**, so deep nesting, oversized length
    fields and OID amplification never touch pyasn1.

    Overriding at the transport rather than somewhere inside pysnmp is what
    guarantees the ordering: once pysnmp has the bytes, parsing has already
    happened.
    """

    def datagram_received(self, datagram, transportAddress):
        """The actual hook point in pysnmp 7.x.

        UdpAsyncioTransport → DgramAsyncioProtocol → asyncio.DatagramProtocol。
        DgramAsyncioProtocol.datagram_received hands the datagram to
        loop.call_soon(callback), which enters pysnmp's message processing chain.
        Intercepting here keeps the bytes away from the BER decoder.
        """
        gate = _gate
        if gate is not None:
            src_ip = transportAddress[0] if transportAddress else ""
            allowed, _reason = gate.check(bytes(datagram), src_ip)
            if not allowed:
                # Drop events have to be rate-limited, or an attacker can use
                # them to flood the log and a Graylog licence. Only
                # counters are updated here; a periodic task emits summaries.
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

        # Critical: the transport has to be created inside a running event loop.
        # Calling open_server_mode before the loop starts leaves the socket
        # unbound — the service reports Running and answers nothing (the "alive
        # but dead" case). This actually happened.
        ok = lower_process_priority()
        log(f"process priority lowered to BELOW_NORMAL: {ok}")
        ident = load_system_identity()
        CFG["contact"], CFG["location"] = ident["contact"], ident["location"]
        srcs = {ident["contact_source"], ident["location_source"]} - {"none"}
        CFG["config_source"] = ("merged" if len(srcs) > 1
                                else (srcs.pop() if srcs else "default"))
        log(f"sysContact={ident['contact']!r} (from {ident['contact_source']}) "
            f"sysLocation={ident['location']!r} (from {ident['location_source']})")
        _t0 = time.monotonic()
        oids, vals = build_snapshot()
        _health["snapshot_build_ms"] = int((time.monotonic() - _t0) * 1000)
        _health["snapshot_built_monotonic"] = time.monotonic()
        _health["snapshot_generation"] = 1
        # The engineID is handed over rather than left to pysnmp: SNMPv3 keys
        # are localized against it, and a pysnmp-generated one would not be
        # the value served as snmpEngineID or the value the keys were made
        # for. It also has to be the persisted one so it survives a restart.
        eng = engine.SnmpEngine(rfc1902.OctetString(_engine_id()))
        global _gate
        _gate = PreAuthGate(
            allowed_networks=PreAuthGate.parse_networks(CFG["allowed_networks"]),
            rate_pps=CFG["rate_pps"], burst=CFG["rate_burst"])
        nets = CFG["allowed_networks"] or ("(none configured; loopback only)",)
        log(f"pre-auth gate active: networks={list(nets)} "
            f"rate={CFG['rate_pps']}pps burst={CFG['rate_burst']}")
        config.add_transport(eng, udp.DOMAIN_NAME,
                             GatedUdpTransport().open_server_mode((host, port)))
        if CFG["v3_only"]:
            log("v3_only is set: SNMPv2c is not registered on this agent")
        else:
            config.add_v1_system(eng, "area", community)
            config.add_vacm_user(eng, 2, "area", "noAuthNoPriv", (1, 3, 6))
        v3_count = _register_v3_users(eng)
        if CFG["v3_only"] and not v3_count:
            # Refusing to start is the lesser harm. Listening with no way in
            # would look healthy from Windows while answering nobody, and the
            # operator would go looking at the network for a fault that is
            # in a configuration file.
            raise SystemExit("v3_only is set but no SNMPv3 user could be "
                             "loaded; the agent would answer nobody")
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
                # Atomic handover: reference assignment in Python is atomic
                # under the GIL, so a walk in progress never sees half a
                # snapshot.
                ctrl.oids, ctrl.vals = no, nv
                _health["snapshot_build_ms"] = int((time.monotonic() - t0) * 1000)
                _health["snapshot_built_monotonic"] = time.monotonic()
                _health["snapshot_generation"] += 1
                if _gate is not None:
                    _gate.prune()
            except Exception as exc:  # noqa: BLE001
                _health["snapshot_failures"] += 1
                log(f"snapshot rebuild failed "
                    f"({_health['snapshot_failures']} so far): {exc!r}")

    try:
        loop.run_until_complete(main_co())
    except Exception as exc:  # noqa: BLE001
        import traceback
        log(f"agent terminated abnormally: {exc!r} | {traceback.format_exc()}",
            error=True)
    finally:
        log("agent stopped")


# pywin32's pythonservice.exe imports this module and looks for the service class
# at **module level**. Defining it inside a function produces
#   AttributeError: module 'jt_snmpd' has no attribute '...'
# and the service fails to start with nothing in the log. This happened.
try:
    import win32event
    import win32service
    import win32serviceutil
    import servicemanager

    class JTSnmpdService(win32serviceutil.ServiceFramework):
        _svc_name_ = "jt-snmpd"
        _svc_display_name_ = "jt-snmpd"
        _svc_description_ = ("SNMP agent serving Windows host monitoring data "
                             "over standard MIBs")

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.hstop = win32event.CreateEvent(None, 0, 0, None)
            # Signalled when the agent thread ends, so SvcDoRun notices without
            # polling (see below)
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

            # Waiting on hstop alone is not enough: if the agent thread dies
            # during startup — a bind failure, a MIB load failure, a snapshot
            # build failure — the service sits at Running forever with nothing
            # listening (the "alive but dead" case). The Service
            # Control Manager saying Running while monitoring reports a timeout
            # is the hardest state to diagnose in the field.
            #
            # So wait on both "stop" and "the agent died". The second exits with
            # a non-zero code, which is what makes the configured sc failure
            # recovery actually fire — otherwise that configuration is inert.
            rc = win32event.WaitForMultipleObjects(
                [self.hstop, self.hdead], 0, win32event.INFINITE)
            if rc == win32event.WAIT_OBJECT_0 + 1 and not self.stop_event.is_set():
                log("agent thread ended unexpectedly; exiting with a failure "
                    "status to trigger automatic recovery",
                    error=True)
                # 1064 = ERROR_EXCEPTION_IN_SERVICE; the SCM applies the
                # configured recovery actions on this
                self.ReportServiceStatus(win32service.SERVICE_STOPPED,
                                         win32ExitCode=1064, waitHint=0)
                os._exit(1)

    _HAVE_SERVICE = True
except ImportError:      # pywin32 absent (foreground debugging, for instance)
    _HAVE_SERVICE = False


def _is_frozen() -> bool:
    """After PyInstaller packaging sys.frozen is True and sys.executable is our
    own exe."""
    return getattr(sys, "frozen", False)


def _service_main() -> None:
    """Service entry point.

    Unpackaged, this goes through HandleCommandLine and pythonservice.exe hosts
    it. Once packaged as an exe it **must** use PrepareToHostSingle and
    StartServiceCtrlDispatcher instead, because the service binary is then our
    own exe (a hard rule) and there is no pythonservice.exe to host
    it.
    """
    if not _HAVE_SERVICE:
        raise SystemExit("pywin32 is required to run as a service")

    # --selftest and --foreground are intercepted in __main__; only
    # service-related argv is handled here.
    if _is_frozen() and len(sys.argv) == 1:
        # The SCM launched our exe directly with no arguments: enter the
        # service dispatch loop
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(JTSnmpdService)
        servicemanager.StartServiceCtrlDispatcher()
        return

    if _is_frozen():
        # install/remove/start/stop: let pywin32 point binPath at our own exe
        win32serviceutil.HandleCommandLine(
            JTSnmpdService, argv=sys.argv,
            customInstallOptions="", customOptionHandler=None)
        return

    win32serviceutil.HandleCommandLine(JTSnmpdService)


def _arg(name: str, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def selftest() -> int:
    """Smoke test that the package is complete.

    "The exe was produced" is not enough. When pysnmp's MIB data files were left
    out of a build the exe appeared as usual, raised MibNotFoundError on startup,
    and **the service still reported Running** (the "alive but dead" case in
    §6.5). That happened once, which is why every build now actually initialises
    an SNMP engine and constructs a snapshot.
    """
    try:
        oids, vals = build_snapshot()
        if len(oids) < 10:
            print(f"SELFTEST_FAIL snapshot too small: {len(oids)}")
            return 1
        # Really initialise the pysnmp engine — a MIB load failure surfaces here
        eng = engine.SnmpEngine()
        config.add_v1_system(eng, "selftest", "public")
        config.add_vacm_user(eng, 2, "selftest", "noAuthNoPriv", (1, 3, 6))
        ctx = context.SnmpContext(eng)
        ctx.context_names[b""] = SnapshotController(oids, vals)
        # Exercise both the GET and GETNEXT paths
        ctrl = ctx.context_names[b""]
        got = ctrl.read_variables((v2c.ObjectIdentifier(SYS + (1, 0)), None))
        nxt = ctrl.read_next_variables((v2c.ObjectIdentifier(SYS), None))
        if not got or not nxt:
            print("SELFTEST_FAIL no response from GET/GETNEXT")
            return 1
        print(f"SELFTEST_OK varbinds={len(oids)} frozen={_is_frozen()} "
              f"sysDescr_len={len(bytes(got[0][1]))}")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"SELFTEST_FAIL {exc!r}")
        traceback.print_exc()
        return 1


def _usm_cli(argv: list[str]) -> int:
    """`jt-snmpd.exe user add|list|remove` — SNMPv3 account management.

    **Passphrases are prompted for, never taken from the command line.** A
    passphrase in an argument is visible in the process list to every user on
    the machine while the command runs, and lands in the console history and in
    the transcripts some sites turn on by policy. This is the same reason the
    installer does not accept keys as MSI properties: those end up in the
    msiexec log and in Event IDs 1033 and 11707.
    """
    import getpass

    action = argv[0] if argv else "list"
    engine_id = _engine_id()
    try:
        users, problems = usm.load_store(USM_STORE, engine_id)
    except OSError as exc:
        print(f"the SNMPv3 store could not be read: {exc}")
        return 1
    for problem in problems:
        print(f"[!] {problem}")

    if action == "list":
        print(f"engineID {engine_id.hex()}")
        if not users:
            print("no SNMPv3 users are provisioned")
        for user in users:
            print(f"  {user.name}  {user.auth} + {user.priv}")
        return 0

    if action == "remove":
        if len(argv) < 2:
            print("usage: user remove <name>")
            return 2
        kept = [u for u in users if u.name != argv[1]]
        if len(kept) == len(users):
            print(f"no such user: {argv[1]}")
            return 1
        usm.save_store(USM_STORE, engine_id, kept)
        print(f"removed {argv[1]}")
        return 0

    if action != "add":
        print("usage: user add|list|remove")
        return 2

    name = argv[1] if len(argv) > 1 else input("user name: ").strip()
    auth = _arg("--auth", usm.DEFAULT_AUTH)
    priv = _arg("--priv", usm.DEFAULT_PRIV)
    try:
        for warning in usm.check_algorithms(auth, priv):
            print(f"[!] {warning}")
        if not name:
            raise usm.UsmError("a user name is required")
        if any(u.name == name for u in users):
            raise usm.UsmError(f"{name!r} already exists; remove it first")
        auth_pass = getpass.getpass("authentication passphrase: ")
        usm.check_passphrase("authentication", auth_pass)
        if getpass.getpass("confirm: ") != auth_pass:
            raise usm.UsmError("the passphrases did not match")
        priv_pass = getpass.getpass("privacy passphrase: ")
        usm.check_passphrase("privacy", priv_pass)
        if getpass.getpass("confirm: ") != priv_pass:
            raise usm.UsmError("the passphrases did not match")
        if priv_pass == auth_pass:
            raise usm.UsmError("use different passphrases for authentication "
                               "and privacy; one compromise should not be two")
        auth_key, priv_key = usm.localize(auth, priv, auth_pass, priv_pass,
                                          engine_id)
    except usm.UsmError as exc:
        print(f"[!] {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\ncancelled")
        return 1

    users.append(usm.UsmUser(name, auth, priv, auth_key, priv_key))
    try:
        usm.save_store(USM_STORE, engine_id, users)
    except OSError as exc:
        print(f"[!] the SNMPv3 store could not be written: {exc}")
        return 1
    print(f"added {name} ({auth} + {priv}).")
    print("Only the localized keys were stored; the passphrases were not.")
    print("Restart the service for it to take effect: sc stop jt-snmpd && "
          "sc start jt-snmpd")
    return 0


if __name__ == "__main__":
    # These two have to be intercepted before _service_main(). Once frozen,
    # letting them reach win32serviceutil.HandleCommandLine produces "option not
    # recognized" and a printout of the service usage — which happened.
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())

    if len(sys.argv) > 1 and sys.argv[1] == "user":
        load_config()
        raise SystemExit(_usm_cli(sys.argv[2:]))

    # Config file first, command line second — the command line is an override,
    # so it has to be applied after the file has been read.
    load_config()
    CFG["port"] = int(_arg("--port", CFG["port"]))
    CFG["community"] = _arg("--community", CFG["community"])
    if "--foreground" in sys.argv:
        print(f"foreground 0.0.0.0:{CFG['port']} community={CFG['community']}")
        # noqa: S104 — binding 0.0.0.0 is intentional: an SNMP agent has to be
        # reachable from every management network. Access control comes from the
        # §3.2 pre-auth gate (source address allow-list) and the firewall rule
        # (management networks are mandatory at install time, deny by default),
        # not from the bind address.
        run_agent("0.0.0.0", CFG["port"], CFG["community"], threading.Event())
    else:
        _service_main()
