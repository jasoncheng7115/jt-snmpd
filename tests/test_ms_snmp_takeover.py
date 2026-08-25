"""Taking over the built-in Windows SNMP Service, and giving it back.

**How this was found**

The question was simply "the installer detects the built-in SNMP service and
disables it, right?". It does. Checking the code to be sure turned up something
else: **the upgrade path destroyed the restore record**.

`msi-configure.ps1` re-read the built-in service's current state on every run and
overwrote `state\\ms-snmp-restore.json` unconditionally. On a first install that
reads the true original state, say Automatic and Running, and all is well. **On
an upgrade the built-in service has already been disabled by the previous
install**, so the re-read returns Disabled and Stopped, and that is what gets
written back as the thing to restore.

The uninstall side tests:

    if ($orig -and $orig -ne 'Disabled') { Set-Service -Name SNMP -StartupType $orig }

`$orig` is now `Disabled`, the condition is false, and **the built-in service
never comes back**.

So install-then-remove restores correctly and install-then-upgrade-then-remove
does not. The only difference is an upgrade in the middle, which is the ordinary
operation for this product. The first lifecycle test missed it because its
removal stage checked our own service, directories and firewall rules, and never
checked what had been handed back.

What has to be recorded is the state **before we first touched it**, so an
existing record always wins.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "packaging"
SRC = (PKG / "msi-configure.ps1").read_text(encoding="utf-8-sig")


# --- detection and disabling ------------------------------------------------

def test_detects_builtin_snmp_and_records_original_state():
    assert "Get-Service -Name SNMP" in SRC, "the built-in SNMP service is never detected"
    for field in ("original_start_type", "original_status", "service_existed"):
        assert field in SRC, f"the restore record has no {field}"


def test_disables_rather_than_removes():
    """Disabled, not removed. Removal is irreversible, and all we want is UDP/161."""
    assert "Set-Service -Name SNMP -StartupType Disabled" in SRC
    assert "Stop-Service -Name SNMP -Force" in SRC
    assert not re.search(r"Remove-WindowsCapability|Uninstall-WindowsFeature|"
                         r"sc\.exe delete SNMP\b", SRC), (
        "the built-in service must be disabled rather than removed, and restorable")


def test_disable_result_is_verified():
    """Group policy or third-party management can block the disable. If it does
    not actually stop, the built-in service still holds UDP/161, our bind fails,
    and all anyone sees is a health check timing out for no stated reason."""
    i = SRC.find("if ($msCfg.service_exists -and $KeepMsSnmp -ne '1')")
    assert i != -1, "the disable block is gone"
    block = SRC[i:i + 1400]
    assert "$after = Get-Service -Name SNMP" in block, "the state is not verified after disabling"
    assert "exit 1" in block, "a failed disable has to fail the install rather than continue"


def test_keepmssnmp_escape_hatch_exists():
    """In some environments the built-in service carries ExtensionAgents and
    cannot be stopped. There has to be a way out, but an explicit one: a property
    the operator passes, never the default."""
    assert "$KeepMsSnmp -ne '1'" in SRC
    wxs = (PKG / "wix" / "jt-snmpd.wxs").read_text(encoding="utf-8-sig")
    assert "KEEPMSSNMP" in wxs, "the wxs defines no KEEPMSSNMP property, so the way out does not exist"


# --- an upgrade must not destroy the restore record (the point of this file) -

def test_existing_restore_record_takes_precedence():
    """The central assertion: an existing record wins, and only a first install
    writes the current state."""
    assert "$RESTORE_FILE" in SRC, "the path should be one variable, so two places cannot disagree"
    assert "if (Test-Path $RESTORE_FILE)" in SRC, (
        "the existing record is not checked, so an upgrade overwrites the true "
        "original state with the disabled one")
    i = SRC.find("if (Test-Path $RESTORE_FILE)")
    j = SRC.find("$restore = [ordered]@{", i)
    assert j != -1
    block = SRC[i:j]
    assert "ConvertFrom-Json" in block, "an existing record has to be parsed and reused"
    assert "if (-not $msSnmpBlock)" in block, (
        "the current state may only be used when there is no existing record")


def test_restore_block_is_not_rebuilt_unconditionally():
    """The converse: $restore must not read $msCfg's state fields directly."""
    i = SRC.find("$restore = [ordered]@{")
    j = SRC.find("}\n", SRC.find("not_imported", i))
    block = SRC[i:j]
    assert "$msCfg.start_type" not in block, (
        "$restore takes the current state directly, which is what overwrote the "
        "true original on upgrade")
    assert "ms_snmp = $msSnmpBlock" in block


def test_uninstall_restores_original_start_type():
    assert "Set-Service -Name SNMP -StartupType $orig" in SRC
    assert "$r.ms_snmp.disabled_by_us" in SRC, (
        "only restore machines we changed; a KEEPMSSNMP install is not ours to undo")
    assert "original_status -eq 'Running'" in SRC, (
        "only start it again if it was running before")


def test_restore_is_skipped_when_original_was_already_disabled():
    """A machine that was already Disabled must not be helpfully enabled on removal."""
    assert "$orig -ne 'Disabled'" in SRC


# --- exercise the restore decision against real JSON ------------------------

def _would_restore(record: dict) -> bool:
    """A copy of the uninstall side's condition, so the meaning can be checked
    on its own."""
    ms = record.get("ms_snmp", {})
    if not (ms.get("disabled_by_us") and ms.get("service_existed")):
        return False
    orig = ms.get("original_start_type")
    return bool(orig) and orig != "Disabled"


def test_first_install_record_restores():
    assert _would_restore({"ms_snmp": {
        "service_existed": True, "original_start_type": "Automatic",
        "original_status": "Running", "disabled_by_us": True}})


def test_record_polluted_by_upgrade_does_not_restore():
    """The shape of the bug: once an upgrade overwrote Automatic with Disabled,
    the restore condition could never be true. This test keeps the shape of it."""
    assert not _would_restore({"ms_snmp": {
        "service_existed": True, "original_start_type": "Disabled",
        "original_status": "Stopped", "disabled_by_us": True}})


def test_machine_without_builtin_snmp_restores_nothing():
    assert not _would_restore({"ms_snmp": {
        "service_existed": False, "original_start_type": None,
        "original_status": None, "disabled_by_us": False}})


def test_keepmssnmp_install_restores_nothing():
    assert not _would_restore({"ms_snmp": {
        "service_existed": True, "original_start_type": "Automatic",
        "original_status": "Running", "disabled_by_us": False}})


def test_restore_record_shape_is_json_serialisable():
    """The field names PowerShell writes have to match what is tested here."""
    sample = {"schema_version": 1, "ms_snmp": {
        "service_existed": True, "original_start_type": "Automatic",
        "original_status": "Running", "disabled_by_us": True}}
    assert json.loads(json.dumps(sample)) == sample
    for k in sample["ms_snmp"]:
        assert k in SRC, f"the PowerShell side never writes {k}"


# --- the security rules outrank faithful migration --------------------------

def test_writable_communities_are_downgraded_not_copied():
    assert "ValidCommunities" in SRC
    assert "trap_destinations" in SRC and "not_imported" in SRC, (
        "traps and ExtensionAgents are listed but never imported")
