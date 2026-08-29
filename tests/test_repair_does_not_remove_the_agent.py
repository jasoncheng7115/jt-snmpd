"""A repair must not leave the machine with no agent and report success.

**Why this file exists**

`msiexec /i jt-snmpd.msi /qn REINSTALL=ALL REINSTALLMODE=vomus` -- the ordinary
repair, and the first thing an administrator reaches for -- deleted the service,
removed the firewall rules, restored the built-in Windows SNMP Service to
Automatic, and **exited 0**. Measured on a real machine on 2026-08-29; the
installer log shows the uninstall branch running to completion and the install
branch never starting.

The cause is a condition that reads as though it means one thing and means
another. `REMOVE="ALL"` is not "the product is being uninstalled": Windows
Installer sets it during a repair too, because a repair removes and re-adds the
components. So `UnconfigureAgent` ran on its `REMOVE="ALL"` condition, and
`ConfigureAgent` did not, because its `NOT REMOVE` was false. Both halves were
individually reasonable and together they took the machine apart.

Exit 0 is what makes this bad rather than merely wrong. A failure that reports
failure gets retried; this one reports success, and a GPO that repairs on a
schedule would quietly disarm a fleet.

The same shape guards the major upgrade: during one, the old package's removal
sequence runs with UPGRADINGPRODUCTCODE set, and it must not undo what the new
package is installing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WXS = Path(__file__).resolve().parents[1] / "packaging" / "wix" / "jt-snmpd.wxs"


@pytest.fixture(scope="module")
def sequence() -> str:
    s = WXS.read_text(encoding="utf-8")
    return s[s.index("<InstallExecuteSequence>"):s.index("</InstallExecuteSequence>")]


def _condition(sequence: str, action: str) -> str:
    m = re.search(rf'<Custom Action="{action}"[^>]*?Condition="(.*?)"', sequence, re.S)
    assert m, f"{action} is not scheduled at all"
    return m.group(1).replace("&quot;", '"')


def test_a_repair_still_configures_the_agent(sequence: str):
    """The half that was missing: nothing re-registered the service."""
    cond = _condition(sequence, "ConfigureAgent")
    assert "REINSTALL" in cond, (
        "ConfigureAgent is skipped during a repair, so nothing re-registers the "
        "service, recreates the firewall rules, or re-disables the built-in "
        "SNMP service")


def test_a_repair_does_not_run_the_uninstall_branch(sequence: str):
    """The half that did the damage."""
    cond = _condition(sequence, "UnconfigureAgent")
    assert "NOT REINSTALL" in cond, (
        'REMOVE="ALL" is also set during a repair; without excluding it, a '
        "repair deletes the service and restores the built-in SNMP service")


def test_an_upgrade_does_not_run_the_uninstall_branch(sequence: str):
    """The old package's removal sequence runs during a major upgrade and must
    not undo what the new one is installing."""
    cond = _condition(sequence, "UnconfigureAgent")
    assert "NOT UPGRADINGPRODUCTCODE" in cond


def test_the_two_conditions_cannot_both_be_false(sequence: str):
    """The property that actually matters, checked over every combination
    rather than by reading the two strings and hoping.

    Whatever Windows Installer sets, at least one of the branches has to run.
    Neither running is how a machine ends up with the files on disk, no service,
    and exit 0."""
    conf = _condition(sequence, "ConfigureAgent")
    unconf = _condition(sequence, "UnconfigureAgent")

    def evaluate(expr: str, props: dict) -> bool:
        # A small evaluator for the subset of MSI condition syntax used here.
        e = expr
        e = re.sub(r'REMOVE="ALL"', repr(props["REMOVE"] == "ALL"), e)
        for name in ("REINSTALL", "UPGRADINGPRODUCTCODE", "REMOVE"):
            e = re.sub(rf"\b{name}\b", repr(bool(props.get(name))), e)
        return eval(e.replace(" AND ", " and ").replace(" OR ", " or ")
                     .replace("NOT ", "not "))

    for remove in ("", "ALL"):
        for reinstall in ("", "ALL"):
            props = {"REMOVE": remove, "REINSTALL": reinstall,
                     "UPGRADINGPRODUCTCODE": ""}
            ran = evaluate(conf, props) or evaluate(unconf, props)
            assert ran, (
                f"neither branch runs for {props}: the files change and "
                "nothing configures or unconfigures the service")

    # UPGRADINGPRODUCTCODE is set only in the **old** package's sequence during
    # a major upgrade, and there doing nothing is the point: the new package
    # configures the service in its own sequence, where REMOVE is empty. So this
    # is the one combination where both branches are correctly silent, and
    # asserting it stops a future "fix" from making the old package act.
    both_silent = {"REMOVE": "ALL", "REINSTALL": "", "UPGRADINGPRODUCTCODE": "{GUID}"}
    assert not evaluate(unconf, both_silent), (
        "the outgoing package would tear down the service the incoming one is "
        "installing")
