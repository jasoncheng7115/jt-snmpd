"""How the sensor parsing holds up against a hostile or damaged buffer.

**Why this whole file exists**

Every offset and length in a WMI data block **comes out of the buffer itself**,
and the buffer comes from firmware and drivers. Python is memory-safe, so there
is no classic heap corruption here; the risk simply takes a different shape:

1. A nonsense `InstanceCount`, say 0xFFFFFFFF, turns into four billion
   iterations. Under a hard requirement never to slow the host down, that is a
   self-inflicted denial of service.
2. A nonsense `BufferSize` lets the buffer allocation be any size at all.
3. Firmware-supplied strings go straight into an SNMP OCTET STRING, where
   control characters and excessive lengths deform the response or break the
   1400-byte cap.
4. `0` and `0xFFFFFFFF` are how ACPI says "unknown". Converted to Celsius they
   are -273 and four hundred million degrees, and either one reaches LibreNMS as
   a run of false alarms.

So parsing is separated from acquisition entirely: `parse_wnode_all_data()` is a
pure function that can be fed arbitrary bytes on Linux. That is what these tests
do.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
import sensors  # noqa: E402


def _wnode(*, instances: int, flags: int = 0x0200, data_off: int = 64,
           inst_size: int = 76, name_off: int = 0, payload: bytes = b"",
           buffer_size: int | None = None, total: int | None = None) -> bytes:
    """A WNODE_ALL_DATA buffer we control, so it can be damaged deliberately."""
    body = bytearray(total if total is not None else max(64 + instances * inst_size,
                                                         64 + len(payload)))
    struct.pack_into("<I", body, 0, buffer_size if buffer_size is not None else len(body))
    struct.pack_into("<I", body, 44, flags)
    struct.pack_into("<III", body, 48, data_off, instances, name_off)
    struct.pack_into("<I", body, 60, inst_size)
    if payload:
        body[data_off:data_off + len(payload)] = payload
    return bytes(body)


def _zone_payload(current_tenths_k: int, critical: int = 3800,
                  passive: int = 3600) -> bytes:
    """The nine ULONGs of MSAcpi_ThermalZoneTemperature."""
    return struct.pack("<9I", 0, 0, 0, 0, 4, current_tenths_k, passive, critical, 0)


# --- temperature conversion: unknown has to vanish, not become a number -----

@pytest.mark.parametrize("tenths,expected", [
    (2981, 25.0),       # 25.0 °C
    (3081, 35.0),
    (2732, 0.0),        # freezing
    (3448, 71.7),       # the hot end of the range seen on a real disk
])
def test_valid_temperatures_convert(tenths, expected):
    assert sensors.tenths_kelvin_to_celsius(tenths) == pytest.approx(expected, abs=0.1)


@pytest.mark.parametrize("bad", [0, 1, 0xFFFFFFFF, 0x80000000, -1, 2331, 4733, 10**9])
def test_unknown_or_absurd_temperatures_are_rejected(bad):
    """ACPI says "unknown" with 0 or 0xFFFFFFFF. Converted, those are -273 and
    four hundred million degrees, and either reaches LibreNMS as a false alarm."""
    assert sensors.tenths_kelvin_to_celsius(bad) is None


def test_non_integer_temperature_rejected():
    assert sensors.tenths_kelvin_to_celsius("2981") is None
    assert sensors.tenths_kelvin_to_celsius(None) is None


# --- parsing: no number the buffer states about itself is trusted -----------

def test_empty_and_short_buffers():
    for raw in (b"", b"\x00", b"\x00" * 63):
        assert sensors.parse_wnode_all_data(raw) == []


def test_random_garbage_never_raises():
    """No byte sequence may make the parser raise; a raise is a failed snapshot build."""
    for pattern in (b"\xff", b"\x00", b"\xaa", b"\x7f"):
        for length in (64, 100, 512, 4096):
            sensors.parse_wnode_all_data(pattern * length)


def test_absurd_instance_count_is_capped():
    """The central assertion: four billion claimed instances must not become four
    billion iterations."""
    raw = _wnode(instances=0xFFFFFFFF, total=4096)
    out = sensors.parse_wnode_all_data(raw)
    assert len(out) <= sensors.MAX_INSTANCES


def test_instance_count_cap_is_configurable_and_enforced():
    raw = _wnode(instances=1000, inst_size=76, total=64 + 1000 * 76)
    assert len(sensors.parse_wnode_all_data(raw, max_instances=3)) == 3


def test_data_offset_beyond_buffer_yields_nothing():
    raw = _wnode(instances=4, data_off=100000, total=1024)
    assert sensors.parse_wnode_all_data(raw) == []


def test_instance_size_zero_is_rejected():
    """inst_size=0 puts every instance at the same offset, and the loop reads
    nothing for ever."""
    assert sensors.parse_wnode_all_data(_wnode(instances=5, inst_size=0)) == []


def test_instance_size_larger_than_buffer_is_rejected():
    assert sensors.parse_wnode_all_data(
        _wnode(instances=2, inst_size=1 << 30, total=1024)) == []


def test_buffer_size_field_larger_than_actual_bytes():
    """The buffer claims a megabyte and holds a kilobyte; what we have is what counts."""
    raw = _wnode(instances=2, inst_size=76, buffer_size=1 << 20,
                 total=64 + 2 * 76, payload=_zone_payload(2981) * 2)
    out = sensors.parse_wnode_all_data(raw)
    for inst in out:
        assert len(inst.data) <= len(raw)


def test_partial_last_instance_is_dropped_not_truncated():
    """A truncated final instance is dropped rather than half-reported."""
    raw = _wnode(instances=3, inst_size=76, total=64 + 76 * 2 + 10,
                 payload=_zone_payload(2981) * 2)
    out = sensors.parse_wnode_all_data(raw)
    assert len(out) == 2
    assert all(len(i.data) == 76 for i in out)


def test_variable_length_instances_with_bad_entries_skip_only_those():
    """One bad instance must not invalidate the whole batch."""
    total = 4096
    body = bytearray(total)
    struct.pack_into("<I", body, 0, total)
    struct.pack_into("<I", body, 44, 0)          # not fixed-length
    struct.pack_into("<III", body, 48, 0, 3, 0)
    good = _zone_payload(2981)
    body[200:200 + len(good)] = good
    body[400:400 + len(good)] = good
    struct.pack_into("<II", body, 60, 200, len(good))          # good
    struct.pack_into("<II", body, 68, 999999, len(good))       # offset out of bounds
    struct.pack_into("<II", body, 76, 400, len(good))          # good
    out = sensors.parse_wnode_all_data(bytes(body))
    assert len(out) == 2


# --- instance names: firmware strings have to be cleaned --------------------

def test_control_characters_are_stripped():
    assert sensors.sanitise_name("THM_\x00\x07\x1b[31m0") == "THM_[31m0"
    assert "\n" not in sensors.sanitise_name("a\nb")


def test_name_length_is_capped():
    assert len(sensors.sanitise_name("A" * 10000)) == sensors.MAX_NAME_CHARS


def test_bad_name_offsets_do_not_raise():
    for name_off in (1, 63, 100000, 0xFFFFFFFF):
        raw = _wnode(instances=2, name_off=name_off, total=1024,
                     payload=_zone_payload(2981) * 2)
        sensors.parse_wnode_all_data(raw)


def test_absurd_name_length_is_rejected():
    """A garbage name-length field must not become a slice that size."""
    total = 2048
    body = bytearray(total)
    struct.pack_into("<I", body, 0, total)
    struct.pack_into("<I", body, 44, 0x0200)
    struct.pack_into("<III", body, 48, 200, 1, 400)
    struct.pack_into("<I", body, 60, 76)
    body[200:276] = _zone_payload(2981)
    struct.pack_into("<I", body, 400, 500)       # the name lives at offset 500
    struct.pack_into("<H", body, 500, 0xFFFF)    # claims a length of 65535
    out = sensors.parse_wnode_all_data(bytes(body))
    assert len(out) == 1
    assert out[0].name == ""                     # the length is not credible, so no name


# --- thermal zone fields ----------------------------------------------------

def test_thermal_zone_round_trip():
    raw = _wnode(instances=1, payload=_zone_payload(2981, critical=3800),
                 total=1024)
    inst = sensors.parse_wnode_all_data(raw)[0]
    z = sensors.parse_thermal_zone(inst)
    assert z is not None
    assert z.celsius == pytest.approx(25.0, abs=0.1)
    assert z.critical_c == pytest.approx(106.85, abs=0.1)


def test_thermal_zone_with_unknown_temperature_is_dropped():
    """An unknown temperature removes the row rather than reporting 0 or -273."""
    raw = _wnode(instances=1, payload=_zone_payload(0), total=1024)
    inst = sensors.parse_wnode_all_data(raw)[0]
    assert sensors.parse_thermal_zone(inst) is None


def test_thermal_zone_too_short_is_dropped():
    assert sensors.parse_thermal_zone(
        sensors.WnodeInstance(0, b"\x00" * 20, "x")) is None


def test_critical_trip_point_may_be_absent_without_dropping_reading():
    """An implausible trip point must not cost us the temperature, which is the
    reading that matters."""
    raw = _wnode(instances=1, payload=_zone_payload(2981, critical=0, passive=0),
                 total=1024)
    z = sensors.parse_thermal_zone(sensors.parse_wnode_all_data(raw)[0])
    assert z is not None and z.celsius == pytest.approx(25.0, abs=0.1)
    assert z.critical_c is None and z.passive_c is None


# --- allocation ceilings, for when a driver reports an enormous size --------

def test_buffer_allocation_is_bounded():
    assert 0 < sensors.MAX_WMI_BUFFER <= 16 << 20
    assert 0 < sensors.MAX_INSTANCES <= 4096
    assert 0 < sensors.MAX_PROCESSORS <= 4096


@pytest.mark.skipif(sys.platform == "win32",
                    reason="on Windows these call the real APIs and return real data")
def test_module_imports_on_non_windows():
    """The agent's tests run on Linux, so this module has to import and return
    empty values there.

    Skipped on Windows rather than rewritten: what is being checked is that it
    imports on a non-Windows platform, and checking that on Windows means
    nothing. CI runs on both a Linux and a Windows runner, and the first version
    of this condition was missing: Linux was green and Windows failed outright.
    """
    assert sensors.read_thermal_zones() == []
    assert sensors.read_battery() is None
    assert sensors.read_cpu_frequencies() == []


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
def test_windows_collectors_return_sane_shapes():
    """On Windows, check the **type and range** instead of expecting emptiness.

    A virtual machine having no thermal zone and no battery is normal, so there
    is no asserting that a value exists; but any value that does exist has to be
    plausible.
    """
    for z in sensors.read_thermal_zones():
        assert -40 <= z.celsius <= 200
        assert isinstance(z.name, str)
    bat = sensors.read_battery()
    if bat is not None:
        assert 0 <= bat.percent <= 100
    for f in sensors.read_cpu_frequencies():
        assert 0 < f.current_mhz <= 100_000
        assert 0 < f.max_mhz <= 100_000


def test_processor_buffer_uses_group_aware_count():
    """os.cpu_count() reports only the caller's processor group and under-reports
    past 64 processors, while the kernel writes back according to the **real**
    count. Undersize the buffer and that is genuine heap corruption. This was a
    real defect in the prototype, and the fix has to stay in the code."""
    src = (Path(__file__).resolve().parent.parent / "deploy"
           / "sensors.py").read_text(encoding="utf-8")
    assert "GetActiveProcessorCount" in src, "the processor count has to be group-aware"
    assert "_ALL_PROCESSOR_GROUPS" in src
    # Only the executable statements. The prose mentions os.cpu_count() on
    # purpose, explaining why it must **not** be used.
    import ast
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "read_cpu_frequencies")
    stmts = [x for x in fn.body
             if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant))]
    body = "\n".join(ast.unparse(x) for x in stmts)
    assert "os.cpu_count" not in body, "os.cpu_count() must not size the buffer"
    assert "MAX_PROCESSORS" in body, "without a ceiling, an absurd API result allocates an absurd buffer"
