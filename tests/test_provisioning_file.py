"""SNMPv3 accounts delivered by a deployment tool, consumed once, then gone.

**Why this exists**

Hundreds of machines cannot be visited to run `jt-snmpd.exe user add`, so
something has to carry the passphrases to them. Every obvious route is worse
than it looks:

  * An MSI property is written to the msiexec log and to Event IDs 1033 and
    11707, where it stays on every machine it reached. This is why the installer
    accepts no SNMPv3 parameter, and that does not change.
  * A Group Policy startup script keeps the passphrase in SYSVOL, readable by
    every domain computer. That is the shape of the Group Policy Preferences
    password problem, which Microsoft eventually removed the feature over.

The file route does not make a passphrase safe to distribute; it bounds how long
and where it exists on the monitored host. The properties that give it any value
at all are the ones asserted here, and losing any one of them quietly would turn
a bounded exposure into a permanent one:

  1. it is read from the data directory, whose ACL the installer restricts to
     SYSTEM and Administrators
  2. it is consumed before the store is read, so one restart is enough
  3. the passphrases become localized keys under DPAPI, as `user add` does
  4. **the file is deleted afterwards, including when parsing it failed** --
     a typo must not leave plain text on disk for ever
  5. failing to delete it is reported loudly, because a file that was meant to
     be gone and is not is an exposure nobody would otherwise learn of
  6. no passphrase is ever written to a log
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1] / "deploy" / "jt_agent.py"


@pytest.fixture(scope="module")
def src() -> str:
    return AGENT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tree(src: str) -> ast.Module:
    return ast.parse(src)


def _func(tree: ast.Module, name: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{name} is gone")


def test_the_file_lives_in_the_hardened_data_directory(src: str):
    """%ProgramData%\\jt-snmpd is the one directory the installer has already
    restricted to SYSTEM and Administrators. Anywhere else and the passphrase is
    readable by any user on the machine for as long as the file exists."""
    assert 'PROVISION_FILE = os.path.join(STATE_DIR, "provision.json")' in src


def test_it_is_consumed_before_the_store_is_read(tree: ast.Module):
    """Otherwise a freshly deployed machine needs a second restart before it
    serves v3, and whoever deployed it sees a host that is not answering."""
    body = _func(tree, "_register_v3_users") if any(
        isinstance(n, ast.FunctionDef) and n.name == "_register_v3_users"
        for n in ast.walk(tree)) else None
    if body is None:
        # The registration function has been renamed; find the one that loads
        # the store instead.
        body = next(ast.unparse(n) for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and "usm.load_store(USM_STORE" in ast.unparse(n)
                    and "_consume_provisioning" in ast.unparse(n))
    consume = body.index("_consume_provisioning")
    load = body.index("usm.load_store")
    assert consume < load, "the file must be consumed before the store is read"


def test_the_passphrases_become_localized_keys(tree: ast.Module):
    body = _func(tree, "_consume_provisioning")
    assert "usm.localize" in body
    assert "usm.save_store" in body
    assert "usm.check_passphrase" in body, (
        "the same minimum applied to `user add` has to apply here; a rollout is "
        "exactly where a weak passphrase would be set on every machine at once")


def test_the_file_is_deleted_even_when_it_could_not_be_used(tree: ast.Module):
    """The assertion this file exists for. A malformed provisioning file that is
    left behind turns a typo into plain-text passphrases sitting on disk for
    ever, and the operator has no reason to go looking."""
    body = _func(tree, "_consume_provisioning")
    assert "finally" in body, (
        "deletion must be in a finally block; on any other path a failure "
        "leaves the passphrases on disk")
    tail = body[body.rindex("finally"):]
    assert "_shred" in tail


def test_failing_to_delete_it_is_reported_loudly(tree: ast.Module):
    body = _func(tree, "_shred")
    assert "error=True" in body
    assert "plain text" in body, (
        "the log line has to say what is still on the disk, or nobody will act "
        "on it")


def test_no_passphrase_is_ever_logged(tree: ast.Module):
    """Reading the log must never be a way to recover a credential. The names
    and algorithms are logged on purpose: an operator rolling out to hundreds of
    machines has to be able to answer 'did it take?' from Get-WinEvent."""
    body = _func(tree, "_consume_provisioning")
    for line in body.splitlines():
        if "log(" not in line:
            continue
        for forbidden in ("auth_pass", "priv_pass", "passphrase'", 'passphrase"'):
            assert forbidden not in line, f"a log line may be carrying a secret: {line.strip()}"


def test_a_rerun_converges_rather_than_failing(tree: ast.Module):
    """A rollout gets re-run. On the machines it already reached, the user
    exists, and refusing there would leave the operator unable to tell a real
    failure from a machine that was already done."""
    body = _func(tree, "_consume_provisioning")
    assert "replacing the existing user" in body


def test_the_installer_still_takes_no_snmpv3_property():
    """The counterweight. This file makes deployment possible, and must not
    become an argument for putting a passphrase back into the MSI."""
    wxs = (AGENT.parents[1] / "packaging" / "wix" / "jt-snmpd.wxs").read_text(encoding="utf-8")
    dlg = wxs[wxs.index('<Dialog Id="JtSettingsDlg"'):]
    dlg = dlg[:dlg.index("</Dialog>")]
    props = {ln.split('Property="')[1].split('"')[0] for ln in dlg.splitlines()
             if 'Property="' in ln}
    assert props == {"MANAGEMENTNETWORKS", "COMMUNITY", "KEEPMSSNMP"}


def test_a_file_written_by_powershell_can_be_read(tree: ast.Module):
    """`Set-Content -Encoding UTF8` in Windows PowerShell 5.1 writes a BOM, and
    so does Notepad. Read as plain utf-8 that raises "Unexpected UTF-8 BOM", the
    provisioning fails, and a rollout provisions nothing on every machine while
    reporting success at the deployment tool.

    This is the second time in this file's history: config.json already carried
    a comment about it, and the first version of the provisioning reader made
    the same mistake anyway. It was caught by writing the file the way a
    deployment tool would, rather than the way Python does."""
    body = _func(tree, "_consume_provisioning")
    assert "utf-8-sig" in body, (
        "the provisioning file is written by an operator's tooling, so it has "
        "to tolerate a byte-order mark")


def test_every_operator_supplied_json_file_tolerates_a_bom(src: str):
    """The general form. Files the agent writes itself may be read strictly;
    files a person or their tooling writes may not."""
    for path_const in ("CFG_PATH", "PROVISION_FILE"):
        idx = src.index(f"open({path_const}")
        window = src[idx:idx + 120]
        assert "utf-8-sig" in window, f"{path_const} is read without utf-8-sig"
