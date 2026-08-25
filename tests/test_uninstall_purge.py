"""Uninstall and what PURGE has to leave behind, which is nothing.

**How this was found**

The full lifecycle run -- install, upgrade, remove, reinstall, purge -- failed on
its last step: after `PURGE=1` the data directory was still there, holding
`logs\\msi-configure.log`.

The custom action writes its log **inside the directory it is purging**.
`Remove-Item` did delete the whole thing, and the next two `Log` lines recreated
`logs\\`. The removal was undone by its own closing messages.

**Why ordinary testing misses it**

`Remove-Item` succeeded, the exit code was 0, and the log said the data directory
had been completely removed. From msiexec, from the service state, from the
program directory, everything looked right; only opening the data directory shows
the debris. And the consequence is delayed: the next installation inherits the
old state, which defeats remove-and-reinstall, the first thing a customer tries.

The same few lines had a second problem. `Remove-Item ... -ErrorAction
SilentlyContinue` swallows "the file is locked and cannot be deleted" as well,
and still reports success. Just after the service stops, a DPAPI blob or the log
file may still be held briefly; that is not hypothetical.

This file pins two things: file logging stops before the removal, and the result
is verified afterwards rather than assumed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "packaging"
CONFIGURE = PKG / "msi-configure.ps1"
SRC = CONFIGURE.read_text(encoding="utf-8-sig")


def _purge_block() -> str:
    """The then branch of `if ($Purge -eq '1') { ... }`.

    Matched by counting braces rather than by finding the first `} else {`: the
    branch contains a nested if/else of its own, verifying the removal, and the
    first version of this helper tripped over exactly that.
    """
    i = SRC.find("if ($Purge -eq '1')")
    assert i != -1, "the purge branch is gone"
    start = SRC.index("{", i)
    depth = 0
    for k in range(start, len(SRC)):
        if SRC[k] == "{":
            depth += 1
        elif SRC[k] == "}":
            depth -= 1
            if depth == 0:
                return SRC[start:k + 1]
    pytest.fail("the purge branch's braces never close")


# --- the log file recreating the directory ----------------------------------

def test_log_writes_are_gated_by_a_flag():
    """Logging has to be switchable, or any line after the removal recreates the
    directory."""
    assert "$script:LogToFile" in SRC, (
        "Log has no flag to turn it off, so the closing messages after a purge "
        "recreate logs\\")
    i = SRC.find("function Log {")
    body = SRC[i:SRC.find("\n}", i)]
    assert "if ($script:LogToFile)" in body, "the file write is not guarded by the flag"
    assert "Add-Content" in body


def test_file_logging_is_disabled_before_the_delete():
    """The order carries the meaning: logging off, then delete. The other way
    round achieves nothing."""
    block = _purge_block()
    off = block.find("$script:LogToFile = $false")
    rm = block.find("Remove-Item $DATA_DIR")
    assert off != -1, "file logging is not turned off before the purge"
    assert rm != -1, "the purge branch never deletes the data directory"
    assert off < rm, "file logging has to be turned off **before** the delete"


def test_log_dir_lives_inside_data_dir():
    """The premise of the two assertions above: the log really is inside the
    directory being purged.

    If the log ever moves to %TEMP% they stop being necessary, but that should be
    a decision someone made rather than a drift nobody noticed.
    """
    assert "$LOG_DIR     = Join-Path $DATA_DIR 'logs'" in SRC.replace("  ", " ").replace(
        "$LOG_DIR = Join-Path $DATA_DIR 'logs'", "$LOG_DIR     = Join-Path $DATA_DIR 'logs'"
) or re.search(r"\$LOG_DIR\s*=\s*Join-Path \$DATA_DIR 'logs'", SRC), (
        "if the log location changes, revisit whether the purge still needs to "
        "turn logging off")


# --- success must not be claimed without checking ---------------------------

def test_purge_verifies_the_directory_is_actually_gone():
    """Nothing is reported that has not been verified."""
    block = _purge_block()
    assert "Test-Path $DATA_DIR" in block, (
        "the directory has to be confirmed gone, not inferred from Remove-Item "
        "not raising")


def test_purge_retries_because_files_may_still_be_locked():
    """Just after the service stops, a DPAPI blob or the log may still be held."""
    block = _purge_block()
    assert re.search(r"foreach \(\$attempt in 1\.\.\d+\)", block), "the purge does not retry"
    assert "Start-Sleep" in block, "there is no wait between retries"


def test_failed_purge_is_reported_not_swallowed():
    """A failure has to leave a warning: debris makes the next installation
    inherit the old state."""
    block = _purge_block()
    assert "WARN" in block, "a failed purge has to be reported as a warning"
    ok = block.find('Log "data directory completely removed')
    assert ok != -1, "the success message is gone"
    # The success message has to sit inside the branch where $purged is true
    assert "if ($purged)" in block, "the success message is not conditional on the check"
    assert block.find("if ($purged)") < ok, "the success message must not be unconditional"


# --- the default, without PURGE ---------------------------------------------

def test_default_uninstall_keeps_data_dir():
    """Keeping it by default is deliberate.

    Customers troubleshoot by removing and reinstalling. Clearing the index map
    makes LibreNMS rediscover everything and orphans the existing RRDs.
    """
    i = SRC.find("if ($Purge -eq '1')")
    assert i != -1, "the purge branch is gone"
    # Anchor on the end of the uninstall block. This used to look for a Chinese
    # log line that had since been translated, so find() returned -1, the slice
    # ran to the end of the file, and the assertion below was checking the whole
    # script rather than the else branch. It passed for the wrong reason.
    j = SRC.find('Log "=== uninstall complete ==="', i)
    assert j != -1, "the uninstall-complete marker moved; this test is looking at the wrong region"
    tail = SRC[i:j]
    else_i = tail.find("} else {")
    assert else_i != -1
    else_block = tail[else_i:]
    # $DATA_DIR_OLD starts with $DATA_DIR, so a plain substring test matches the
    # migration's own cleanup and reports a deletion that is not there.
    assert not re.search(r"Remove-Item \$DATA_DIR(?!_)", else_block), (
        "a default uninstall must not delete the data directory")
    assert "data directory kept" in else_block


def test_uninstall_restores_builtin_snmp():
    """On a machine whose built-in SNMP we took over, removal has to put back
    both its start type and whether it was running."""
    assert "Set-Service -Name SNMP -StartupType $orig" in SRC
    assert "original_status" in SRC, "the restore ignores whether it had been running"


def test_purge_property_is_secured_or_documented():
    """PURGE is destructive, and it has to be declared in the wxs or the custom
    action never sees it."""
    wxs = (PKG / "wix" / "jt-snmpd.wxs").read_text(encoding="utf-8-sig")
    assert "PURGE" in wxs, "the wxs declares no PURGE property, so PURGE=1 does nothing"
