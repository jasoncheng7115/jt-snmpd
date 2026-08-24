# -*- coding: utf-8 -*-
"""Build the JSON that LibreNMS's `smart` application expects from disk health data.

**Why NET-SNMP-EXTEND-MIB rather than entPhySensorTable**

The first attempt published NVMe endurance and available spare as
`entPhySensorType = other(1)`, and they were **invisible** in LibreNMS. The
reason is in `includes/discovery/sensors/entity-sensor.inc.php`:

    $entitysensor = ['voltsDC'=>'voltage', 'voltsAC'=>'voltage', 'amperes'=>'current',
                     'watts'=>'power', 'hertz'=>'freq', 'percentRH'=>'humidity',
                     'rpm'=>'fanspeed', 'celsius'=>'temperature', 'dBm'=>'dbm'];
    ...
    if (isset($entitysensor[$entry['entPhySensorType']]) && ...)

`other` is not in that map, so the whole row is skipped. Only temperature
(`celsius`) survived, which is why the field showed a temperature and no SMART
metrics at all. entPhySensorTable is a dead end for counter-style data — not
because the values were wrong, but because the table has no semantics for them.

LibreNMS reads SMART through `json_app_get()`:

    snmp_get($device, 'nsExtendOutputFull."smart"', '-Oqv', 'NET-SNMP-EXTEND-MIB')

That is **entirely over SNMP**: beyond jt-snmpd itself the monitored host needs
neither the LibreNMS agent, nor smartctl, nor any external script. The SMART
attributes are already read directly through ctypes; serialising them into the
same JSON and serving it from `nsExtendOutputFull` is all that is required.

**Why it has to be compressed**

Responses are capped at 1400 bytes and never fragmented. Uncompressed, the SMART
JSON for a single disk is already close to that cap and two disks exceed it.
LibreNMS's `json_app_get()` accepts base64(gzip(json)):

    if (preg_match('/^[A-Za-z0-9\\/\\+\\n]+\\=*\\n*$/', $output)
        && ! preg_match('/^[0-9]+\\n/', $output)) {
        $output = gzdecode(base64_decode($output));
    }

The JSON is highly repetitive, so in practice it compresses to about a third.
"""

from __future__ import annotations

import base64
import gzip
import json

# The SMART attribute IDs LibreNMS's smart application reads (see its RRD
# definition). Every one has to be present in the JSON: the PHP side indexes
# `$disk['5']` directly, a missing key raises a warning, and null is rejected by
# is_numeric() so the field is stored as U (unknown) — which is what we mean.
LIBRENMS_SMART_IDS = ("5", "9", "10", "173", "177", "183", "184", "187", "188",
                      "190", "194", "196", "197", "198", "199", "231", "232", "233")

# Self-test log counters. The SMART self-test log (SMART_READ_LOG 0x06) is not
# read yet, so these stay null — reporting 0 would show up in LibreNMS as "every
# test passed", which would be a fabrication.
SELFTEST_KEYS = ("completed", "interrupted", "read_failure", "unknown_failure",
                 "extended", "short", "conveyance", "selective")

# NVMe has no ATA attribute table. Only fields whose meaning is unambiguous are
# mapped; the rest stay null. Under-reporting is preferable to mislabelling — an
# ID mapped to the wrong thing means the field acts on a false indicator.
NVME_TO_SMART_ID = {
    "power_on_hours": "9",      # Power_On_Hours
    "temp_c": "194",            # Temperature_Celsius
    "media_errors": "187",      # Reported_Uncorrect
    "avail_spare_pct": "232",   # Available_Reserved_Space
}

# ATA: diskhealth records attributes by name, so translate back to the IDs
# LibreNMS wants.
ATA_NAME_TO_SMART_ID = {
    "reallocated_sectors": "5",
    "power_on_hours": "9",
    "wear_leveling_count": "177",
    "airflow_temperature_c": "190",
    "temperature_c": "194",
    "pending_sectors": "197",
    "uncorrectable_sectors": "198",
    "ssd_life_left_pct": "231",
    "available_reserved_space_pct": "232",
    "media_wearout_indicator": "233",
}


def _blank_disk() -> dict:
    d: dict = {i: None for i in LIBRENMS_SMART_IDS}
    d.update({k: None for k in SELFTEST_KEYS})
    return d


def build_disk_entry(health: dict, max_temp: int | None = None) -> dict:
    """Turn one disk's probe() result into LibreNMS's attribute dictionary.

    Anything unknown stays None (→ JSON null → U in the RRD). Zero is never used
    to mean "not measured": a 0 in reallocated sectors reads as "this disk is
    healthy", when the truth is "this attribute was never read".
    """
    out = _blank_disk()
    if not health:
        return out

    # Prefer the ATA attribute table — it carries the most detail
    for name, attr in (health.get("smart") or {}).items():
        sid = ATA_NAME_TO_SMART_ID.get(name)
        if sid and isinstance(attr, dict) and isinstance(attr.get("raw"), int):
            out[sid] = attr["raw"]

    # Attributes taken straight from their IDs, covering the ones with no name
    # mapping (10, 183, 184, 188, 196, 199 and so on)
    for aid, raw in (health.get("smart_by_id") or {}).items():
        sid = str(int(aid))
        if sid in out and out[sid] is None and isinstance(raw, int):
            out[sid] = raw

    # NVMe fields fill in what the ATA table does not have
    for key, sid in NVME_TO_SMART_ID.items():
        if out.get(sid) is None and isinstance(health.get(key), int):
            out[sid] = health[key]

    # Temperature may come from StorageProperty, which is neither the ATA nor the
    # NVMe attribute table
    if out["194"] is None and isinstance(health.get("temp_c"), int):
        out["194"] = health["temp_c"]

    # LibreNMS's smart application renders a "Max Temp(C)" panel from the
    # `max_temp` key (`if (isset($disk['max_temp']))`). Without it the panel is
    # still drawn and its graph is a broken image — on every installation.
    #
    # Windows' storage APIs expose *thresholds* (warning, critical), not "the
    # highest this disk has ever been", and putting a threshold there would
    # mislabel the line. So this is the highest temperature actually observed,
    # persisted across restarts: "the maximum seen since jt-snmpd was installed"
    # is a real measurement rather than a stand-in.
    if isinstance(max_temp, int):
        out["max_temp"] = max_temp

    return out


def _disk_status_fields(health: dict, over_temp: bool) -> dict:
    """LibreNMS's smart application page decides which status markers to show
    from these per-disk keys
    (`includes/html/pages/device/apps/smart.inc.php`)::

        health_pass  1 → " (OK)"          0 → " (FAIL)"
        over_temp    1 → " (Overheating)"
        dev_error    1 → " (Polling Error)"

    All three go through a `?? null` branch, so **omitting them shows nothing**.
    The first version omitted them, and the drive list was a bare
    `PhysicalDrive0` that said nothing about the disk's health.

    `health_pass` is only emitted when the disk **answered for itself** — ATA
    SMART RETURN STATUS, or the NVMe critical warning bitmap. Deriving it from
    attributes would be wrong: zero reallocated sectors does not mean healthy,
    since the firmware may already be predicting failure on some other attribute,
    and a handful of reallocated sectors is normal on some models. The firmware
    makes that judgement, so the firmware is what gets asked.
    """
    out: dict = {"dev_error": 0}
    if isinstance(health.get("health_pass"), bool):
        out["health_pass"] = 1 if health["health_pass"] else 0
    elif "critical_warning" in health:
        # NVMe: critical warning bitmap; any bit set means the controller has
        # raised a warning
        cw = health["critical_warning"]
        if isinstance(cw, int):
            out["health_pass"] = 1 if cw == 0 else 0
    out["over_temp"] = 1 if over_temp else 0
    return out


def build_smart_json(disks: list[dict], *, over_temp_c: int = 70) -> dict:
    """Assemble the standard envelope for a LibreNMS JSON application.

    Each entry in `disks` needs `name` (part of the RRD file name, so it must be
    stable and usable as a file name) and `health` (the result of
    `diskhealth.probe()`); `max_temp` is the optional observed maximum.
    """
    entries: dict[str, dict] = {}
    over_temp: list[str] = []
    unhealthy: list[str] = []

    for d in disks:
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        # The name becomes part of an RRD file name, so it has to be safe
        name = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)[:64]
        health = d.get("health") or {}
        entry = build_disk_entry(health, d.get("max_temp"))

        t = entry.get("194")
        hot = isinstance(t, int) and t >= over_temp_c
        if hot:
            over_temp.append(name)
        entry.update(_disk_status_fields(health, hot))

        # The detail table (`$diskFields` in smart.inc.php). Replacing a disk in
        # the field needs the model and serial to identify which one. This is the
        # customer's own asset information and stays inside their own monitoring
        # system.
        for key, src in (("disk", "name"), ("serial", "serial"),
                         ("vendor", "vendor"), ("product", "model")):
            v = d.get(src) if src == "name" else health.get(src) or d.get(src)
            if isinstance(v, str) and v.strip():
                entry[key] = v.strip()[:64]
        entries[name] = entry
        # A non-zero reallocated or pending sector count means this disk is failing
        for sid in ("5", "197", "198"):
            v = entry.get(sid)
            if isinstance(v, int) and v > 0 and name not in unhealthy:
                unhealthy.append(name)

    return {
        "version": 1,
        "error": 0,
        "errorString": "",
        "data": {
            "disks": entries,
            "exit_nonzero": 0,
            "unhealthy": len(unhealthy),
            "dev_error": 0,
            "disks_with_failed_tests": [],
            "disks_with_failed_health": unhealthy,
            "disks_with_over_temp": over_temp,
            "disks_with_dev_error": [],
        },
    }


def encode_extend_output(payload: dict) -> bytes:
    """Serialise, gzip, base64 — the value served from `nsExtendOutputFull`.

    `mtime=0` is deliberate: the gzip header carries the current time by default,
    so identical data would produce different bytes on every build, the snapshot
    would churn every five seconds for no reason, and tests could not compare it.
    """
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, compresslevel=9, mtime=0))


def looks_like_librenms_base64(value: bytes) -> bool:
    """Check the output matches what LibreNMS detects as base64, since anything
    else is treated as plain text.

    Mirrors `json_app_get()`:

        preg_match('/^[A-Za-z0-9\\/\\+\\n]+\\=*\\n*$/', $output)
        && ! preg_match('/^[0-9]+\\n/', $output)
    """
    if not value:
        return False
    body = value.rstrip(b"=")
    if not body:
        return False
    allowed = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/+\n")
    if not set(body) <= allowed:
        return False
    # Must not look like the legacy format (digits followed by a newline)
    head = value.split(b"\n", 1)[0]
    return not head.isdigit()
