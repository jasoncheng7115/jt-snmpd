# -*- coding: utf-8 -*-
"""Driverless hardware sensors: ACPI thermal zones, battery, CPU frequency.

**Why CPU package temperature is not here**

Reading it requires MSR access (Intel `IA32_THERM_STATUS`, AMD SMN), which
requires a kernel driver. WinRing0 — the one LibreHardwareMonitor and
OpenHardwareMonitor use — is on Microsoft's vulnerable-driver blocklist and will
not load under the HVCI/WDAC configurations our customers run. Even where it
would load, installing something that can read and write arbitrary MSRs and
physical memory across hundreds of government and hospital machines, for one
temperature reading, turns a monitoring tool into a privilege-escalation path.
CLAUDE.md rule 8 forbids it.

What this module exposes is what the firmware already publishes:

- **ACPI thermal zones** (`MSAcpi_ThermalZoneTemperature`) — the temperature near
  the mainboard or CPU, with the passive and critical trip points ACPI itself
  declares, usable directly as thresholds.
- **Battery** — `GetSystemPowerStatus` (charge percentage, AC line status).
- **CPU frequency** — `CallNtPowerInformation(ProcessorInformation)`.

All three are documented public APIs: no driver, no extra privilege, no
subprocess.

**Why the parsing here is so defensive**

Every offset and length in a WMI data block is **read from the buffer itself**,
and the buffer comes from firmware and drivers. Python is memory-safe, so there
is no classic overflow; the real risks are different:

1. An implausible `InstanceCount` turns into a loop running millions of times —
   which, under a hard requirement never to slow the host down, is a
   self-inflicted denial of service.
2. An implausible `BufferSize` leads to an enormous allocation.
3. Firmware-supplied instance names go straight into an SNMP OCTET STRING, where
   control characters and over-long strings deform the response or push it past
   the 1400-byte cap.
4. `0` and `0xFFFFFFFF` are how ACPI says "unknown"; converted to Celsius they
   are -273 °C and four hundred million degrees, and in LibreNMS they are a
   stream of false alerts.

So **parsing is separated from acquisition**: `parse_wnode_all_data()` is a pure
function from bytes to structures, which is what makes it testable against
hostile buffers on Linux (see tests/test_sensors_parsing.py).
"""

from __future__ import annotations

import struct
import sys
from typing import NamedTuple

# --- Defensive limits --------------------------------------------------------
# These are not "should be enough"; they are "past this, the data is wrong and
# is better discarded".
MAX_WMI_BUFFER = 1 << 20        # 1 MB; thermal zone data is a few hundred bytes
MAX_INSTANCES = 64              # thermal zones; a real machine has 1 to 8
MAX_NAME_CHARS = 128            # cap on instance name length
MAX_PROCESSORS = 512            # cap on the CallNtPowerInformation buffer

# ACPI temperatures are in tenths of a kelvin. The plausible range is taken as
# -40 °C to 200 °C: below that is almost always "unknown" (ACPI signals it with 0
# or 0xFFFFFFFF), above it means the parse is misaligned. Both are discarded
# rather than reported.
TENTHS_K_MIN = 2332             # -40.0 °C
TENTHS_K_MAX = 4732             # 200.0 °C


def tenths_kelvin_to_celsius(v: int) -> float | None:
    """Tenths of a kelvin to Celsius; None for implausible values (never
    fabricate — spec §6.9)."""
    if not isinstance(v, int) or not (TENTHS_K_MIN <= v <= TENTHS_K_MAX):
        return None
    return round(v / 10.0 - 273.15, 1)


def sanitise_name(raw: str, *, limit: int = MAX_NAME_CHARS) -> str:
    """Clean a firmware-supplied string before it enters SNMP.

    Control characters deform both the log file and the LibreNMS display;
    over-long strings eat into the 1400-byte response cap. Both are handled at
    the source rather than left for something downstream.
    """
    cleaned = "".join(c for c in raw if c.isprintable() and c not in "\r\n\t")
    return cleaned[:limit]


class WnodeInstance(NamedTuple):
    index: int
    data: bytes
    name: str


def parse_wnode_all_data(raw: bytes, *,
                         max_instances: int = MAX_INSTANCES) -> list[WnodeInstance]:
    """Parse a WNODE_ALL_DATA buffer.

    A pure function that calls no Win32 API — which is the point, because it
    makes the parser testable against hostile input. Anything implausible ends
    with "return what parsed successfully so far": it never raises, and it never
    trusts a number the buffer states about itself.

    Layout (wmistr.h)::

        WNODE_HEADER          48 bytes
          +0  BufferSize      ULONG
          +4  ProviderId      ULONG
          +8  Version/HistoricalContext
          +16 TimeStamp       LARGE_INTEGER
          +24 Guid            GUID (16)
          +40 KernelHandle/ProviderPtr
          +44 Flags           ULONG        (WNODE_HEADER is 48 bytes)
        WNODE_ALL_DATA
          +48 DataBlockOffset            ULONG
          +52 InstanceCount              ULONG
          +56 OffsetInstanceNameOffsets  ULONG
          +60 FixedInstanceSize, or OffsetInstanceDataAndLength[]
    """
    out: list[WnodeInstance] = []
    if len(raw) < 64:
        return out

    buffer_size = struct.unpack_from("<I", raw, 0)[0]
    # The size the buffer claims cannot exceed the bytes actually held.
    limit = min(buffer_size, len(raw)) if buffer_size >= 64 else len(raw)

    flags = struct.unpack_from("<I", raw, 44)[0]
    data_off, inst_count, name_off = struct.unpack_from("<III", raw, 48)

    if inst_count == 0:
        return out
    # Truncate rather than trust: a real machine has single-digit thermal
    # zones, so a count in the millions means the data is corrupt.
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
                break                   # out of bounds: stop, keep what parsed
            spans.append((off, inst_size))
    else:
        # OffsetInstanceDataAndLength[] is 8 bytes per entry (Offset + Length)
        table_end = 60 + count * 8
        if table_end > limit:
            count = max(0, (limit - 60) // 8)
        for i in range(count):
            off, length = struct.unpack_from("<II", raw, 60 + i * 8)
            if length == 0 or off < 0 or length > limit or off + length > limit:
                continue                # skip the bad entry, keep the batch
            spans.append((off, length))

    names = _parse_instance_names(raw, name_off, len(spans), limit)
    for i, (off, length) in enumerate(spans):
        out.append(WnodeInstance(index=i, data=raw[off:off + length],
                                 name=names[i] if i < len(names) else ""))
    return out


def _parse_instance_names(raw: bytes, name_off: int, count: int,
                          limit: int) -> list[str]:
    """Instance names are an array of offsets, each pointing at a counted
    UNICODE string.

    Every level of indirection is re-checked — the offset of the name table, the
    offset of the string, the length of the string — because any of them can be
    garbage.
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
            # The character cap covers both "far too long" and "the length
            # field is garbage"
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
    """Turn one thermal zone record into a temperature.

    Field order of MSAcpi_ThermalZoneTemperature (from the ACPI driver's MOF)::

        ThermalStamp, ThermalConstant1, ThermalConstant2, Reserved,
        SamplingPeriod, CurrentTemperature, PassiveTripPoint,
        CriticalTripPoint, ActiveTripPointCount, ActiveTripPoint[10]

    Nine ULONGs followed by ten more. Only the first nine are needed, and a
    record too short for them is not forced.
    """
    if len(inst.data) < 36:             # 9 * 4
        return None
    try:
        f = struct.unpack_from("<9I", inst.data, 0)
    except struct.error:
        return None
    celsius = tenths_kelvin_to_celsius(f[5])
    if celsius is None:
        return None                     # unknown or implausible: the row
                                        # disappears from the snapshot
    return ThermalZone(
        name=inst.name or "ThermalZone",
        celsius=celsius,
        critical_c=tenths_kelvin_to_celsius(f[7]),
        passive_c=tenths_kelvin_to_celsius(f[6]),
    )


# --- Below here needs Windows; importing this module on Linux stays safe ----

if sys.platform == "win32":  # pragma: no cover - only runs on Windows
    import ctypes
    from ctypes import wintypes

    _adv = ctypes.windll.advapi32
    _k32 = ctypes.windll.kernel32
    _pwr = ctypes.windll.powrprof

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    # Rule 11: every Win32 call declares argtypes and restype.
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
    # MSAcpi_ThermalZoneTemperature (the ACPI driver's WMI data block)
    _TZ_GUID = _GUID(0xA1BC18C0, 0xA7C8, 0x11D1,
                     (ctypes.c_ubyte * 8)(0xBF, 0x3C, 0x00, 0xA0, 0xC9, 0x06, 0x29, 0x10))

    def read_thermal_zones() -> list[ThermalZone]:
        """Read the ACPI thermal zones. Machines without any — virtual
        machines, most desktops — return an empty list.

        `WmiOpenBlock` returns 4200 (ERROR_WMI_GUID_NOT_FOUND) when there are no
        zones. That is normal, not an error, and is not logged.
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
            # Cap the size the driver asks for as well; otherwise a broken
            # driver can make us allocate arbitrarily much memory.
            if not (0 < size.value <= MAX_WMI_BUFFER):
                return []
            buf = (ctypes.c_ubyte * size.value)()
            if _adv.WmiQueryAllDataW(h, ctypes.byref(size), ctypes.byref(buf)) != 0:
                return []
            # The second call may report less than was allocated; take the
            # smaller of the two.
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
        """Battery state. Desktops and virtual machines have none, so None."""
        st = _SYSTEM_POWER_STATUS()
        if not _k32.GetSystemPowerStatus(ctypes.byref(st)):
            return None
        # BATTERY_FLAG_NO_SYSTEM_BATTERY = 0x80; 255 percent means unknown.
        if st.BatteryFlag & 0x80 or st.BatteryLifePercent > 100:
            return None
        secs = st.BatteryLifeTime
        return Battery(
            percent=int(st.BatteryLifePercent),
            on_ac=(st.ACLineStatus == 1),
            # 0xFFFFFFFF means unknown, and on AC power it has no meaning anyway
            seconds_left=None if secs == 0xFFFFFFFF else int(secs),
        )

    class CpuFreq(NamedTuple):
        number: int
        current_mhz: int
        max_mhz: int

    def read_cpu_frequencies() -> list[CpuFreq]:
        """Current and maximum frequency for each logical processor.

        **The buffer must be sized with
        GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)**, not `os.cpu_count()`.
        The latter reflects only the caller's processor group and under-reports
        on machines with more than 64 processors, while the kernel writes back
        according to the **actual** processor count — so an undersized buffer is
        a genuine heap corruption. ctypes is exactly where Python's memory safety
        stops applying.
        """
        n = _k32.GetActiveProcessorCount(_ALL_PROCESSOR_GROUPS)
        if n <= 0:
            return []
        # The cap guards against an anomalous return from the API and against
        # an over-large allocation.
        n = min(int(n), MAX_PROCESSORS)
        arr = (_PROCESSOR_POWER_INFORMATION * n)()
        if _pwr.CallNtPowerInformation(_PROCESSOR_INFORMATION, None, 0,
                                       ctypes.byref(arr), ctypes.sizeof(arr)) != 0:
            return []
        out: list[CpuFreq] = []
        for p in arr:
            # 0 MHz means the kernel left it unset; an implausibly high value
            # means the parse is misaligned. Both are discarded.
            if 0 < p.CurrentMhz <= 100_000 and 0 < p.MaxMhz <= 100_000:
                out.append(CpuFreq(number=int(p.Number),
                                   current_mhz=int(p.CurrentMhz),
                                   max_mhz=int(p.MaxMhz)))
        return out

else:   # Non-Windows: same names, so the agent's imports and the tests do not
        # have to branch
    def read_thermal_zones() -> list[ThermalZone]:
        return []

    def read_battery():
        return None

    def read_cpu_frequencies() -> list:
        return []
