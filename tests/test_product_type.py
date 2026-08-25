"""The three sysObjectID branches, and Windows Server.

**Why all three branches have to exist**

LibreNMS's `LibreNMS/OS/Windows.php` picks one of three version tables from the
sysObjectID:

    .1.3.6.1.4.1.311.1.1.3.1.1  → getClientVersion()
    .1.3.6.1.4.1.311.1.1.3.1.2  → getServerVersion()
    .1.3.6.1.4.1.311.1.1.3.1.3  → getDatacenterVersion()

The same build number maps to different strings in each: 26100 is `11 (24H2)` in
the client table and `Server 2025 (24H2)` in the server one. Without the domain
controller branch a DC is classified as a server and gets a different version
string, and the mistake is silent: the agent works, LibreNMS displays, and the
version is wrong.

**The Server Core trap**

`InstallationType` is not consistent across Windows releases: `Server`,
`Server Core` and `Windows Server Core` have all been seen. An equality test
misclassifies Server Core as a workstation, and Server Core is on the list of
platforms this has to support.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"
SRC = AGENT.read_text(encoding="utf-8")

MS_PREFIX = "(1, 3, 6, 1, 4, 1, 311, 1, 1, 3, 1, "


def _sysobjid_map() -> dict[str, tuple[int, ...]]:
    """Read the ptype to sysObjectID mapping out of the source.

    ast rather than import: the agent needs winreg and ctypes.windll, which do
    not exist on Linux.
    """
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if {"client", "server", "domain_controller"} <= set(keys):
            out = {}
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Tuple):
                    out[k.value] = tuple(e.value for e in v.elts)
            return out
    pytest.fail("no client/server/domain_controller sysObjectID mapping found in the agent")


@pytest.mark.parametrize("ptype,expected_last", [
    ("client", 1),
    ("server", 2),
    ("domain_controller", 3),
])
def test_sysobjectid_branch_matches_librenms(ptype: str, expected_last: int):
    """The last sub-identifier has to be 1, 2 and 3 respectively.

    LibreNMS uses it to choose between getClientVersion, getServerVersion and
    getDatacenterVersion. Get it wrong and it shows the wrong Windows version.
    """
    m = _sysobjid_map()
    assert ptype in m, f"the {ptype} branch is missing"
    expected = (1, 3, 6, 1, 4, 1, 311, 1, 1, 3, 1, expected_last)
    assert m[ptype] == expected, (
        f"{ptype} should be .1.3.6.1.4.1.311.1.1.3.1.{expected_last}, but is {m[ptype]}")


def test_all_three_branches_are_distinct():
    m = _sysobjid_map()
    assert len(set(m.values())) == 3, f"the three branches have to differ: {m}"


def test_all_branches_use_microsoft_pen():
    """Microsoft's PEN, 311, is what has to be used.

    Until this project has a PEN of its own, sysObjectID stays
    Microsoft-compatible. Otherwise all three LibreNMS branches miss and the
    Version field is blank.
    """
    for ptype, oid in _sysobjid_map().items():
        assert oid[:6] == (1, 3, 6, 1, 4, 1), f"{ptype} has the wrong prefix"
        assert oid[6] == 311, f"{ptype} has to use Microsoft's PEN 311, not {oid[6]}"


def test_installation_type_uses_prefix_match_not_equality():
    """Server Core reports an InstallationType of "Server Core", not "Server".

    An equality test misclassifies it as a workstation, and Server Core is on
    the list of platforms this has to support.
    """
    assert 'startswith("server")' in SRC or "startswith('server')" in SRC, (
        "InstallationType has to be matched with startswith, or Server Core is "
        "misclassified")
    assert '== "Server"' not in SRC, "InstallationType must not be compared for equality"


def test_domain_controller_detection_exists():
    """The DC check has to call DsRoleGetPrimaryDomainInformation.

    That API rather than WMI, which this project does not use.
    """
    assert "DsRoleGetPrimaryDomainInformation" in SRC
    assert "DsRoleFreeMemory" in SRC, "memory allocated by the DsRole APIs has to be freed"
    # Both PDC and BDC count as a domain controller
    assert "DSROLE_PRIMARY_DC" in SRC and "DSROLE_BACKUP_DC" in SRC


def test_product_type_has_fallback_when_installation_type_missing():
    """Older or trimmed installations may have no InstallationType.

    The fallback is ProductOptions\\ProductType:
      WinNT is a workstation, LanmanNT a domain controller, ServerNT a server.
    Without it, all of those machines are classified as workstations.
    """
    assert "ProductOptions" in SRC, "the ProductType fallback is missing"
    assert "LanmanNT" in SRC, "LanmanNT, a domain controller, has to be recognised"
    assert "ServerNT" in SRC, "ServerNT, a server, has to be recognised"


def test_dc_detection_failure_does_not_raise():
    """Outside a domain, or where the API is unavailable, the check returns
    False quietly.

    Startup does not fail hard. An agent that cannot work out its role should
    carry on as a workstation rather than refuse to start.
    """
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_is_domain_controller":
            body = ast.unparse(node)
            assert "except" in body, "_is_domain_controller has to catch exceptions"
            assert "return False" in body, "a failure has to return False"
            return
    pytest.fail("_is_domain_controller not found")
