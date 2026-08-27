"""The installer's health check must speak whatever the agent is configured to.

**Why this file exists**

1.1.1 made machines with `v3_only` set impossible to upgrade. The installer ends
by proving the service does not just start but actually answers SNMP -- a good
check, and the reason a whole class of "alive but dead" installs gets caught.
It did that with a hand-built SNMPv2c GET. On a host that had taken the security
advice and refused v2c, nothing answered, the custom action failed, and Windows
Installer rolled the entire transaction back. `msiexec` exit 1603, every time,
on exactly the sites that had been most careful.

The defect was older than 1.1.1 but unreachable: until then an upgrade **reset**
`v3_only` to false, and the reset happened before the probe, so the probe always
had v2c to talk to. 1.1.1's fix -- preserving operator settings across an
upgrade -- is what made the older bug reachable. Two changes that are each
correct can still combine into a failure, and neither one's tests would show it.

The replacement probe for that case is an SNMPv3 engine discovery: an empty user
name at noAuthNoPriv, which RFC 3414 requires an agent to answer with a report
PDU before any credential has been presented. It needs no account, so it works
on a machine whose SNMPv3 users the installer has never seen, and it is still a
real round trip rather than a look at the service state.

Measured on a live agent before this was written: under `v3_only`, the v2c probe
gets nothing and the discovery probe is answered.

These assertions pin the shape of the fix, not its wording:

  1. the probe is chosen from the configuration, not fixed at v2c
  2. a v3-only host is probed with something that does not need a community
  3. the port comes from the configuration too -- a host moved off 161 failed
     the check for the same reason
  4. the check still fails closed
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "msi-configure.ps1"


@pytest.fixture(scope="module")
def text() -> str:
    raw = SCRIPT.read_bytes()
    # PowerShell 5.1 reads a BOM-less file as the ANSI code page and the script
    # contains non-ASCII, so the BOM is load-bearing, not decoration.
    assert raw[:3] == b"\xef\xbb\xbf", "msi-configure.ps1 must be UTF-8 with BOM"
    return raw.decode("utf-8-sig")


def test_the_probe_depends_on_v3_only(text: str):
    """The whole defect in one assertion: a single unconditional v2c probe."""
    assert re.search(r"if\s*\(\s*\$cfg\.v3_only\s*\)", text), (
        "the health check must branch on the configuration it just wrote; "
        "a fixed SNMPv2c probe cannot pass on a host that refuses v2c")


def test_a_v3_only_host_is_probed_without_a_community(text: str):
    branch = text[text.index("if ($cfg.v3_only)"):]
    branch = branch[:branch.index("} else {")]
    assert "New-SnmpV3Discovery" in branch
    assert "Community" not in branch, (
        "the v3_only branch must not need a community: that is the one thing "
        "the host has refused to accept")


def test_the_discovery_probe_carries_no_credentials(text: str):
    """An engine discovery is answerable precisely because it is empty. If this
    grows a user name or an engine ID it stops being a discovery and starts
    needing an account the installer does not have."""
    fn = text[text.index("function New-SnmpV3Discovery"):]
    fn = fn[:fn.index("\nfunction ")]
    assert "0x02,0x01,0x03" in fn, "msgSecurityModel must be USM (3)"
    assert "0x04,0x01,0x04" in fn, "msgFlags must be reportable, noAuthNoPriv"
    # Empty engine ID, empty user name, empty auth and priv parameters.
    assert fn.count("[byte[]](0x04,0x00)") >= 4


def test_the_port_is_not_hardcoded(text: str):
    """Same failure, different setting. A host moved off 161 was probed on 161
    and rolled back for it."""
    assert "$probePort = [int]$cfg.port" in text
    check = text[text.index("function Test-SnmpLoopback"):]
    check = check[:check.index("\n}")]
    assert "161" not in check, "the probe must take the port, not assume it"


def test_the_check_still_fails_closed(text: str):
    """The point of the check is that it can fail the installation. A probe made
    permissive enough to always pass would be worse than none, because the log
    would then assert something nobody verified."""
    tail = text[text.index("if (-not $healthy)"):]
    assert "exit 1" in tail[:400]
