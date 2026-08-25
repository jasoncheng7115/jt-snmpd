"""The data directory has to survive being renamed.

0.9.6 renamed everything to `jt-snmpd` so the product, the service, the paths and
the repository finally agree. That moved the data directory from
`C:\\ProgramData\\JT-SNMP` to `C:\\ProgramData\\jt-snmpd`, and the data directory
is the one thing here that cannot simply be recreated:

  - `state\\index-map.json` holds the ifIndex assignments. Lose it and LibreNMS
    deletes every port and rediscovers, taking the historical RRDs with it. That
    is the most expensive failure this project has.
  - `state\\engine.json` holds the SNMP engine identity and boot counter.
  - `state\\ms-snmp-restore.json` is the only record of what the built-in
    Windows SNMP Service looked like before we disabled it. Lose it and there is
    nothing to restore on uninstall.

This nearly went wrong while making the change: a repository-wide replacement of
the old directory name rewrote the migration's *source* path too, leaving it
pointing at its own destination. The migration would have run, found nothing to
do, reported success, and every upgraded machine would have started from an
empty state directory.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIGURE = ROOT / "packaging" / "msi-configure.ps1"
AGENT = ROOT / "deploy" / "jt_agent.py"

SRC = CONFIGURE.read_text(encoding="utf-8")


def _migration_block() -> str:
    """Just the migration, so an assertion here cannot be satisfied or broken by
    the purge branch, which legitimately does test whether a directory is gone."""
    start = SRC.index("# --- Carry the data directory across")
    end = SRC.index("# --- Data directory and ACL")
    return SRC[start:end]


def _assign(name: str) -> str:
    m = re.search(rf"^\${name}\s*=\s*(.+)$", SRC, re.M)
    if not m:
        pytest.fail(f"${name} is not assigned in msi-configure.ps1")
    return m.group(1).strip()


def test_the_two_paths_are_actually_different():
    """The central assertion, and the one that nearly failed.

    A migration whose source equals its destination is not a migration. It runs,
    finds nothing, and reports success.
    """
    new, old = _assign("DATA_DIR"), _assign("DATA_DIR_OLD")
    assert new != old, (
        f"DATA_DIR and DATA_DIR_OLD are both {new}. The migration would be a "
        "no-op and every upgraded machine would start with an empty state "
        "directory, losing the ifIndex map")
    assert "JT-SNMP" in old, (
        f"DATA_DIR_OLD is {old}; it has to name the pre-0.9.6 directory, "
        "which was C:\\ProgramData\\JT-SNMP")
    assert "jt-snmpd" in new, f"DATA_DIR is {new}; it should be the renamed path"


def test_agent_reads_the_new_location():
    """The agent and the installer have to agree on where the data lives."""
    m = re.search(r'^STATE_DIR\s*=\s*r?"([^"]+)"', AGENT.read_text(encoding="utf-8"), re.M)
    assert m, "STATE_DIR is not assigned in jt_agent.py"
    assert m.group(1).lower().endswith("jt-snmpd"), (
        f"the agent reads {m.group(1)}, which is not the renamed data directory")


def test_migration_moves_rather_than_leaving_two_copies():
    """One directory afterwards, so there is no question which one is live."""
    assert "Move-Item" in SRC and "$DATA_DIR_OLD" in SRC, \
        "no move from the old data directory to the new one"


def test_migration_falls_back_to_copying_rather_than_failing_silently():
    """A duplicated directory is recoverable; a lost one is not."""
    assert "Copy-Item" in SRC, \
        "there is no fallback if the move fails, which would lose the data"


def test_migration_is_not_gated_on_the_destination_directory_existing():
    """The check that could never fire.

    The first version guarded the whole migration on
    `-not (Test-Path $DATA_DIR)`. That can never be true here: this script writes
    its own log inside $LOG_DIR, so the destination is created by the first Log
    call, long before the check runs. Every upgrade skipped the migration, left
    the old data behind, and started the agent with a fresh index map -- exactly
    the failure the migration exists to prevent. Found on the first real upgrade.

    The condition has to be about the individual items, not the directory.
    """
    assert not re.search(r"-not\s*\(Test-Path \$DATA_DIR\)", _migration_block()), (
        "the migration is gated on $DATA_DIR being absent, and it never is: "
        "the log file creates it before the check runs")


def test_migration_decides_per_item():
    """Item by item, so a half-finished migration finishes next time."""
    assert re.search(r"foreach \(\$rel in @\('config\.json'", SRC), \
        "the migration no longer walks the individual items"
    assert "if (Test-Path $dst)" in SRC, \
        "the migration does not check the destination item before moving"


def test_migration_never_overwrites_live_data():
    """On a reinstall the new location holds current data; it must win."""
    i = SRC.index("if (Test-Path $dst)")
    tail = SRC[i:i + 400]
    assert "leaving it alone" in tail or "continue" in tail, \
        "an existing item at the destination must not be overwritten"


def test_migration_stops_rather_than_continuing_with_partial_state():
    """Half a state directory is worse than a failed install, which rolls back."""
    assert re.search(r"\$failed -gt 0", SRC), \
        "a failed item does not stop the installation"


def test_purge_removes_both_locations():
    """A purge that leaves the old directory behind is not a purge.

    The next installation would find it and migrate it straight back in.
    """
    m = re.search(r"if \(\$Purge -eq '1'\)(.{0,2000})", SRC, re.S)
    assert m, "the purge branch is gone"
    block = m.group(1)
    assert "Remove-Item $DATA_DIR " in block or "Remove-Item $DATA_DIR\n" in block, \
        "purge does not remove the current data directory"
    assert "$DATA_DIR_OLD" in block, (
        "purge does not remove the pre-rename data directory, so a later "
        "install would migrate it back")


def test_the_expensive_file_is_named_in_the_reasoning():
    """Whoever edits this next has to know why it matters."""
    assert "index-map" in SRC, (
        "the comment no longer says what is lost; the next person to simplify "
        "this needs to know it costs every port's history")
