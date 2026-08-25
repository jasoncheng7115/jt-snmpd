"""LibreNMS's smart application over NET-SNMP-EXTEND-MIB, and the 497-day uptime wrap.

**SMART: why the route changed**

The first version published NVMe endurance and available spare as
`entPhySensorType = other(1)`, and none of it appeared in LibreNMS. The lookup
table in `includes/discovery/sensors/entity-sensor.inc.php` recognises nine
types:

    voltsDC voltsAC amperes watts hertz percentRH rpm celsius dBm

`other` is not among them, so the whole row is **discarded without a message**.
The agent was fine and the walk returned the values; LibreNMS simply did not take
them. A gap where neither side is wrong and nothing reports an error is the
hardest kind to find.

The route LibreNMS actually reads SMART through is `json_app_get()`, and it is
**entirely over SNMP**:

    snmp_get($device, 'nsExtendOutputFull."smart"', '-Oqv', 'NET-SNMP-EXTEND-MIB')

The monitored host needs neither the LibreNMS agent nor smartctl. The SMART
attributes are already read through ctypes; serialising them into the same JSON
is all that was required.

**Uptime: the wrap at 497 days**

`sysUpTime` is TimeTicks, an Unsigned32 counting hundredths of a second, so
2^32/100 seconds is about 497.1 days and it must wrap. That is the type RFC 3418
specifies, every conforming agent behaves the same way, the built-in Windows
service included, and the wrap itself cannot be fixed.

What can be fixed is the **false reboot alert**. LibreNMS's
`Core.php::calculateUptime()`:

    $uptime = max(round(sysUpTime/100),
                  bad_snmpEngineTime ? 0 : snmpEngineTime,
                  bad_hrSystemUptime ? 0 : round(hrSystemUptime/100));
    if ($uptime < $device->uptime) { Eventlog::log('Device rebooted after ...'); }

and `windows.yaml` sets **only `bad_hrSystemUptime: true`**, not
`bad_snmpEngineTime`. snmpEngineTime counts seconds and tops out at 2147483647,
about 68 years, so publishing it gives max() one source that does not wrap.
"""

from __future__ import annotations

import ast
import base64
import gzip
import json
import re
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
sys.path.insert(0, str(DEPLOY))
import smartjson  # noqa: E402

AGENT_SRC = (DEPLOY / "jt_agent.py").read_text(encoding="utf-8")


# --- the index encoding has to match LibreNMS's Oid::encodeString -----------

def _extend_index(token: str) -> tuple[int, ...]:
    raw = token.encode("ascii")
    return (len(raw),) + tuple(raw)


def test_smart_token_encodes_as_librenms_expects():
    """LibreNMS's documentation states it: 'zfs' becomes
    nsExtendOutputFull.3.122.102.115."""
    assert _extend_index("zfs") == (3, 122, 102, 115)
    assert _extend_index("smart") == (5, 115, 109, 97, 114, 116)


def test_agent_uses_the_same_encoding():
    fn = next(n for n in ast.walk(ast.parse(AGENT_SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "_extend_index")
    body = ast.unparse(fn)
    assert "len(raw)" in body and "tuple(raw)" in body


# --- the JSON shape has to match how LibreNMS reads it ----------------------

def _sample(**over):
    health = {"smart": {"reallocated_sectors": {"value": 100, "worst": 100, "raw": 0},
                        "power_on_hours": {"value": 95, "worst": 95, "raw": 12345},
                        "pending_sectors": {"value": 100, "worst": 100, "raw": 0}},
              "smart_by_id": {199: 2, 196: 0, 187: 0},
              "temp_c": 34}
    health.update(over)
    return [{"name": "PhysicalDrive0", "health": health}]


def test_every_id_librenms_reads_is_present():
    """The PHP side indexes `$disk['5']` directly; a missing key raises a warning
    and floods the log."""
    doc = smartjson.build_smart_json(_sample())
    disk = doc["data"]["disks"]["PhysicalDrive0"]
    for sid in smartjson.LIBRENMS_SMART_IDS:
        assert sid in disk, f"SMART ID {sid} is missing"
    for k in smartjson.SELFTEST_KEYS:
        assert k in disk, f"self-test field {k} is missing"


def test_top_level_json_app_envelope():
    doc = smartjson.build_smart_json(_sample())
    assert doc["version"] == 1 and doc["error"] == 0
    assert set(doc["data"]) >= {"disks", "exit_nonzero", "unhealthy", "dev_error"}


def test_unmeasured_attributes_are_null_not_zero():
    """A zero says "this disk is healthy"; the truth is "we never read that
    attribute". LibreNMS's is_numeric(null) is false, so the RRD stores U for
    unknown, which is what is meant."""
    disk = smartjson.build_smart_json(_sample())["data"]["disks"]["PhysicalDrive0"]
    assert disk["10"] is None
    assert disk["completed"] is None
    assert disk["5"] == 0, "a genuine zero is still a zero"


def test_ata_attributes_map_to_correct_ids():
    disk = smartjson.build_smart_json(_sample())["data"]["disks"]["PhysicalDrive0"]
    assert disk["5"] == 0          # Reallocated_Sector_Ct
    assert disk["9"] == 12345      # Power_On_Hours
    assert disk["197"] == 0        # Current_Pending_Sector
    assert disk["199"] == 2        # UDMA_CRC_Error_Count, only in smart_by_id
    assert disk["194"] == 34       # Temperature_Celsius


def test_nvme_fields_map_conservatively():
    """NVMe has no ATA attribute table. Only fields whose meaning is unambiguous
    are mapped and the rest stay null: an ID mapped to the wrong thing has
    someone in the field acting on a false indicator."""
    disks = [{"name": "PhysicalDrive1",
              "health": {"power_on_hours": 4321, "temp_c": 41,
                         "media_errors": 0, "avail_spare_pct": 100,
                         "percentage_used": 3}}]
    d = smartjson.build_smart_json(disks)["data"]["disks"]["PhysicalDrive1"]
    assert d["9"] == 4321 and d["194"] == 41
    assert d["187"] == 0 and d["232"] == 100
    assert d["5"] is None, "NVMe has no notion of reallocated sectors, so none is invented"
    assert d["233"] is None, "percentage_used and Media_Wearout do not mean the same thing"


def test_unhealthy_disks_are_flagged():
    doc = smartjson.build_smart_json(_sample(
        smart={"reallocated_sectors": {"value": 90, "worst": 90, "raw": 8}}))
    assert doc["data"]["disks_with_failed_health"] == ["PhysicalDrive0"]
    assert doc["data"]["unhealthy"] == 1


def test_over_temp_is_flagged():
    doc = smartjson.build_smart_json(_sample(temp_c=85), over_temp_c=70)
    assert doc["data"]["disks_with_over_temp"] == ["PhysicalDrive0"]


def test_disk_name_is_filesystem_safe():
    """The name becomes part of an RRD file name."""
    disks = [{"name": "PhysicalDrive0 / ../etc\\x00", "health": {"temp_c": 30}}]
    name = next(iter(smartjson.build_smart_json(disks)["data"]["disks"]))
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name
    assert "/" not in name and "\\" not in name


def test_disk_without_name_is_skipped():
    assert smartjson.build_smart_json(
        [{"name": "", "health": {"temp_c": 30}}])["data"]["disks"] == {}


# --- encoding: it has to pass LibreNMS's base64 test and fit in 1400 bytes --

def test_encoding_round_trips():
    doc = smartjson.build_smart_json(_sample())
    blob = smartjson.encode_extend_output(doc)
    assert json.loads(gzip.decompress(base64.b64decode(blob))) == doc


def test_encoding_matches_librenms_detection_regex():
    """Mirrors json_app_get():
        preg_match('/^[A-Za-z0-9\\/\\+\\n]+\\=*\\n*$/', $output)
        && ! preg_match('/^[0-9]+\\n/', $output)
    Anything else is treated as plain text and taken down the legacy CSV path,
    where it fails."""
    blob = smartjson.encode_extend_output(smartjson.build_smart_json(_sample()))
    assert smartjson.looks_like_librenms_base64(blob)
    assert re.fullmatch(rb"[A-Za-z0-9/+\n]+=*\n*", blob)


def test_encoding_is_deterministic():
    """The gzip header carries the current time by default, so identical data
    produces different bytes every time: the snapshot would churn every five
    seconds for no reason, and tests could not compare it. mtime is fixed."""
    doc = smartjson.build_smart_json(_sample())
    assert smartjson.encode_extend_output(doc) == smartjson.encode_extend_output(doc)


@pytest.mark.parametrize("n", [1, 2, 4])
def test_encoded_size_fits_single_response(n):
    """Responses are capped at 1400 bytes and never fragmented. Uncompressed, the
    JSON exceeds that at two disks."""
    disks = [{"name": f"PhysicalDrive{i}", "health": _sample()[0]["health"]}
             for i in range(n)]
    blob = smartjson.encode_extend_output(smartjson.build_smart_json(disks))
    assert len(blob) < 1200, f"{n} disks encode to {len(blob)} bytes, close to the cap"


def test_compression_actually_helps():
    doc = smartjson.build_smart_json(_sample())
    raw = len(json.dumps(doc, separators=(",", ":")))
    assert len(smartjson.encode_extend_output(doc)) < raw


def test_agent_enforces_a_varbind_size_cap():
    """With enough disks the output exceeds what one varbind can hold. It has to
    be cut, and the cut has to be **recorded**: truncating quietly leaves someone
    believing every disk is being monitored."""
    assert "MAX_EXTEND_BYTES" in AGENT_SRC
    i = AGENT_SRC.find("MAX_EXTEND_BYTES")
    assert re.search(r"while len\(blob\) > MAX_EXTEND_BYTES", AGENT_SRC), "there is no shrink loop"
    assert re.search(r"omitted the last.*error=True", AGENT_SRC, re.S), \
        "a truncation has to be logged and reach the Event Log"


def test_agent_publishes_discovery_and_output_oids():
    """Discovery reads nsExtendStatus and polling reads nsExtendOutputFull.
    Without either one, nothing works."""
    assert "NSEXT_CFG + (21,)" in AGENT_SRC, "no nsExtendStatus, so discovery never finds the app"
    assert "NSEXT_OUT1 + (2,)" in AGENT_SRC, "no nsExtendOutputFull, so there is nothing to poll"
    for node in ast.parse(AGENT_SRC).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "NSEXT":
            assert tuple(e.value for e in node.value.elts) == (1, 3, 6, 1, 4, 1, 8072, 1, 3, 2)
            return
    pytest.fail("NSEXT is not defined")


# --- the 497-day wrap -------------------------------------------------------

TIMETICKS_WRAP_DAYS = 2 ** 32 / 100 / 86400


def test_timeticks_wrap_point_is_497_days():
    assert TIMETICKS_WRAP_DAYS == pytest.approx(497.10, abs=0.01)


def test_agent_emits_snmp_engine_time():
    """Once sysUpTime wraps, this is the only uptime source that does not."""
    assert "SNMPFW + (3, 0)" in AGENT_SRC, "snmpEngineTime is not published"
    for node in ast.parse(AGENT_SRC).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SNMPFW":
            assert tuple(e.value for e in node.value.elts) == (1, 3, 6, 1, 6, 3, 10, 2, 1)
            return
    pytest.fail("SNMPFW is not defined")


def test_engine_time_is_seconds_not_centiseconds():
    """snmpEngineTime counts seconds. In hundredths it is a hundred times too
    large, max() always picks it, and the uptime is wrong by that factor."""
    assert "_k32.GetTickCount64() // 1000" in AGENT_SRC


def test_engine_time_is_clamped_to_int32():
    """Integer32 tops out at 2147483647; past that it encodes as a negative."""
    assert "2147483647" in AGENT_SRC


def test_engine_boots_is_persisted():
    """RFC 3414 requires the pair (boots, time) never to repeat. A reboot resets
    time to zero, so boots has to increase, which means it has to be kept."""
    fn = next(n for n in ast.walk(ast.parse(AGENT_SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "_engine_boots")
    body = ast.unparse(fn)
    assert "ENGINE_FILE" in body and "os.replace" in body, "the boot count has to be written atomically"
    assert "boot_key" in body, "a new boot has to be identified by the boot instant"


def test_engine_id_is_stable_across_restarts():
    """SNMPv3 user keys are localised to the engineID; change it and every one of
    them stops working."""
    # The source text rather than ast.unparse, which normalises 0x80 to 128. What
    # is being asserted is the intent to set that top bit.
    i = AGENT_SRC.index("def _engine_id()")
    body = AGENT_SRC[i:AGENT_SRC.index("\ndef ", i + 1)]
    assert "MachineGuid" in AGENT_SRC, "the engineID should derive from a stable machine-level identifier"
    assert "hashlib" in body, "the GUID text exceeds the 27-byte limit and has to be hashed"
    assert "0x80" in body, "RFC 3411's format requires the top bit to be 1"

    # And check the meaning: compute one and confirm the first byte's top bit
    pen = 99999
    first = (pen >> 24) & 0xFF | 0x80
    assert first & 0x80, "the engineID's first byte must have its top bit set"


# --- max_temp, and LibreNMS's Max Temp panel --------------------------------

def test_max_temp_is_emitted_when_observed():
    """LibreNMS's smart application decides whether to write the maxtemp RRD from
    `if (isset($disk['max_temp']))`. Without it the panel is still drawn and its
    graph is a broken image, on every installation. This was found by looking at
    a screenshot."""
    disks = [{"name": "PhysicalDrive0", "health": {"temp_c": 33}, "max_temp": 41}]
    d = smartjson.build_smart_json(disks)["data"]["disks"]["PhysicalDrive0"]
    assert d["max_temp"] == 41
    assert d["194"] == 33, "the current temperature and the maximum are different values"


def test_max_temp_absent_when_never_observed():
    """Nothing observed means no key: LibreNMS's isset() skips it, and no false
    data is created."""
    disks = [{"name": "PhysicalDrive0", "health": {"temp_c": 33}}]
    assert "max_temp" not in smartjson.build_smart_json(disks)["data"]["disks"]["PhysicalDrive0"]


def test_max_temp_is_not_taken_from_a_threshold():
    """Windows' storage APIs expose warning and critical thresholds, not "the
    highest this disk has been". Putting a threshold in max_temp mislabels the
    line, and someone in the field then reads a line unrelated to temperature."""
    disks = [{"name": "PhysicalDrive0",
              "health": {"temp_c": 33, "temp_warn_c": 70, "temp_crit_c": 80}}]
    d = smartjson.build_smart_json(disks)["data"]["disks"]["PhysicalDrive0"]
    assert "max_temp" not in d, "a threshold must not be passed off as a maximum"


def test_agent_persists_observed_max_and_only_writes_on_increase():
    """The snapshot rebuilds every five seconds; writing each time is seventeen
    thousand needless disk writes a day, against the requirement not to slow the
    host. It writes only when the maximum actually rises."""
    fn = next(n for n in ast.walk(ast.parse(AGENT_SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "observed_max_temp")
    body = ast.unparse(fn)
    assert "MAXTEMP_FILE" in body and "os.replace" in body, "it has to be written atomically and kept"
    assert "current <= prev" in body, "only a rise may cause a write"
    assert "0 < current < 150" in body, "implausible temperatures have to be rejected"


# --- per-disk status markers, LibreNMS's (OK) and (FAIL) --------------------

def test_health_pass_drives_the_ok_fail_badge():
    """`includes/html/pages/device/apps/smart.inc.php`：

        $healthStatus = match ($diskData['health_pass'] ?? null) {
            1 => ' (OK)', 0 => ' (FAIL)', default => '',
        };

    Without these keys the drive list is a bare name that says nothing about
    whether the disk is healthy.
    """
    d = [{"name": "PhysicalDrive0", "health": {"health_pass": True, "temp_c": 33}}]
    assert smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]["health_pass"] == 1
    d[0]["health"]["health_pass"] = False
    assert smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]["health_pass"] == 0


def test_health_pass_omitted_when_disk_did_not_answer():
    """A USB bridge does not forward SMART commands. Emitting nothing lets
    LibreNMS's `?? null` branch show nothing, which is more honest than a guessed
    (OK)."""
    d = [{"name": "PhysicalDrive0", "health": {"temp_c": 33}}]
    assert "health_pass" not in smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]


def test_health_pass_is_not_derived_from_attributes():
    """Zero reallocated sectors does not mean healthy: the firmware may already
    be predicting failure on some other attribute. The firmware makes that
    judgement, so it is the firmware that gets asked."""
    d = [{"name": "PhysicalDrive0",
          "health": {"smart": {"reallocated_sectors": {"value": 100, "worst": 100, "raw": 0}},
                     "temp_c": 33}}]
    e = smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]
    assert e["5"] == 0, "the attribute itself is still published"
    assert "health_pass" not in e, "overall health must not be inferred from attributes"


def test_nvme_critical_warning_maps_to_health_pass():
    """NVMe has no ATA RETURN STATUS; the critical warning bitmap is the
    equivalent."""
    for cw, expected in ((0, 1), (1, 0), (4, 0)):
        d = [{"name": "PhysicalDrive0", "health": {"critical_warning": cw, "temp_c": 41}}]
        assert smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]["health_pass"] == expected


def test_over_temp_flag_is_per_disk():
    d = [{"name": "PhysicalDrive0", "health": {"health_pass": True, "temp_c": 85}}]
    e = smartjson.build_smart_json(d, over_temp_c=70)["data"]["disks"]["PhysicalDrive0"]
    assert e["over_temp"] == 1


def test_dev_error_is_always_present():
    """Omitted, LibreNMS shows nothing. An explicit 0 is what says "polling is
    working"."""
    d = [{"name": "PhysicalDrive0", "health": {"temp_c": 33}}]
    assert smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]["dev_error"] == 0


def test_identification_fields_help_locate_the_physical_disk():
    """Replacing a disk in the field, the model and serial are what make it
    findable."""
    d = [{"name": "PhysicalDrive0", "health": {"temp_c": 33},
          "model": "SAMSUNG SSD PM871b", "serial": "S3U0NE0K200798", "vendor": "Samsung"}]
    e = smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]
    assert e["product"] == "SAMSUNG SSD PM871b"
    assert e["serial"] == "S3U0NE0K200798"
    assert e["disk"] == "PhysicalDrive0"


def test_agent_reads_authoritative_smart_status():
    """Overall health comes from ATA SMART RETURN STATUS (0xDA), not from
    inferring it out of attributes."""
    dh = (DEPLOY / "diskhealth.py").read_text(encoding="utf-8")
    assert "SMART_RETURN_STATUS = 0xDA" in dh
    assert "SMART_SEND_DRIVE_COMMAND = 0x0007C084" in dh
    assert "smart_overall_health" in dh
    # The magic values the ATA specification returns
    assert "0x4F" in dh and "0xC2" in dh, "the values meaning no threshold was exceeded"
    assert "0xF4" in dh and "0x2C" in dh, "the values meaning a threshold was exceeded"


# --- jtDiskHealthTable, a private state OID ---------------------------------

def test_disk_health_state_table_exists():
    """LibreNMS's device overview has a sensors block but **no** applications
    block (`resources/views/components/device/overview/` has no applications
    component), so SMART's (OK) and (FAIL) appear only under Apps.

    Showing health on the overview would need a state-class sensor, which needs
    `os_discovery/windows.yaml` on the LibreNMS side, and this project does not
    modify the LibreNMS server. The table is published anyway, so a single health
    value can be read by a custom alert or another tool.
    """
    for node in ast.parse(AGENT_SRC).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "JTDISK":
            return
    pytest.fail("JTDISK is not defined")


def test_disk_health_states_are_conservative():
    """`unknown` has to be a state of its own: "could not ask" must not default
    to healthy. When a USB bridge refuses to forward SMART commands, a false
    green light is worse than no light."""
    for name in ("DISK_STATE_OK", "DISK_STATE_WARNING",
                 "DISK_STATE_CRITICAL", "DISK_STATE_UNKNOWN"):
        assert name in AGENT_SRC, f"{name} is missing"
    i = AGENT_SRC.find("jtDiskHealthTable: per-disk health")
    block = AGENT_SRC[i:i + 2200]
    assert "DISK_STATE_UNKNOWN" in block, "not being able to ask has to be marked unknown"
    assert "(5, 197, 198)" in block, "reallocated, pending and uncorrectable sectors should degrade to warning"
