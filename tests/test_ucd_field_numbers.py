"""UCD-SNMP-MIB systemStats field numbers, pinned to the MIB.

**Why this exists**

Implementing UCD systemStats, 57 to 63 were laid out from intuition as
SwapIn / SwapOut / IOSent / IOReceived / Contexts / Interrupts，
while UCD-SNMP-MIB actually orders them
IOSent(57) / IOReceived(58) / Interrupts(59) / Contexts(60) / SwapIn(62) / SwapOut(63)。

**Nothing about the mistake is visible.** The agent starts, the walk answers,
LibreNMS draws lines, the numbers move. Context switches are simply plotted on
the I/O graph. None of the existing tests for duplicate OIDs, ordering or
response size can catch it, because the structure is entirely valid and only the
meaning is wrong.

The only way to see it is to resolve our output through the MIB names:

    snmpwalk -m UCD-SNMP-MIB -O QUs <host> systemStats

This test pins the numbers. Changing them means changing this too, and changing
this forces a look at the MIB. The authority is:

    snmptranslate -m UCD-SNMP-MIB -On UCD-SNMP-MIB::<name>
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"
SRC = AGENT.read_text(encoding="utf-8")

# Source: snmptranslate -m UCD-SNMP-MIB -On UCD-SNMP-MIB::<name>
# taken from the UCD-SNMP-MIB shipped with LibreNMS 26.8.1
UCD_SYSTEMSTATS = {
    1: "ssIndex",
    2: "ssErrorName",
    50: "ssCpuRawUser",
    51: "ssCpuRawNice",
    52: "ssCpuRawSystem",
    53: "ssCpuRawIdle",
    54: "ssCpuRawWait",
    55: "ssCpuRawKernel",
    56: "ssCpuRawInterrupt",
    57: "ssIORawSent",
    58: "ssIORawReceived",
    59: "ssRawInterrupts",
    60: "ssRawContexts",
    61: "ssCpuRawSoftIRQ",
    62: "ssRawSwapIn",
    63: "ssRawSwapOut",
    64: "ssCpuRawSteal",
    65: "ssCpuRawGuest",
}

# The comment beside each UCDSS field in the agent has to be the right MIB name.
# A call may wrap across lines, so this looks forward from add(UCDSS + (N, 0))
# for the nearest `# ss<Name>` comment rather than only at the same line.
_EMIT = re.compile(r"add\(UCDSS \+ \((\d+), 0\)(.{0,200}?)#\s*(ss\w+)", re.S)


def _emitted() -> dict[int, str]:
    """Field number to the name in its comment, read from the source."""
    out: dict[int, str] = {}
    for m in _EMIT.finditer(SRC):
        num = int(m.group(1))
        # Must not run into the next add(UCDSS: that would mean this field has
        # no comment of its own
        if "add(UCDSS" in m.group(2):
            continue
        out.setdefault(num, m.group(3))
    return out


def test_ucd_base_oid_is_correct():
    """UCDSS has to be .1.3.6.1.4.1.2021.11, UCD-SNMP-MIB::systemStats."""
    tree = ast.parse(SRC)
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "UCDSS":
            got = tuple(e.value for e in node.value.elts)
            assert got == (1, 3, 6, 1, 4, 1, 2021, 11), (
                f"UCDSS should be .1.3.6.1.4.1.2021.11, but is {got}")
            return
    pytest.fail("UCDSS is not defined")


def test_every_emitted_field_has_correct_mib_name():
    """The name beside each emitted field has to match the MIB's name for that
    number.

    This is the assertion that stops numbers being assigned from intuition.
    """
    emitted = _emitted()
    assert emitted, "no UCDSS fields are emitted at all"
    wrong = {n: (name, UCD_SYSTEMSTATS.get(n))
             for n, name in emitted.items()
             if UCD_SYSTEMSTATS.get(n) != name}
    assert not wrong, (
        "field numbers disagree with the MIB:\n"
        + "\n".join(f"  {n}: the code says {a}, the MIB says {b}" for n, (a, b) in wrong.items()))


@pytest.mark.parametrize("field,expected_num", [
    ("ssCpuRawUser", 50), ("ssCpuRawNice", 51), ("ssCpuRawSystem", 52),
    ("ssCpuRawIdle", 53), ("ssCpuRawInterrupt", 56),
    ("ssIORawSent", 57), ("ssIORawReceived", 58),
    ("ssRawInterrupts", 59), ("ssRawContexts", 60),
    ("ssRawSwapIn", 62), ("ssRawSwapOut", 63),
])
def test_required_field_emitted_at_correct_number(field: str, expected_num: int):
    """The fields LibreNMS actually reads have to carry the right numbers."""
    emitted = _emitted()
    assert expected_num in emitted, f"{field} (field {expected_num}) is not emitted"
    assert emitted[expected_num] == field


def test_cpu_four_fields_all_present_for_librenms():
    """LibreNMS's ucd-mib poller needs user, nice, system and idle **all four**
    before it creates the Detailed Processor Usage graph:

        if (isset($ss['ssCpuRawUser']) && isset($ss['ssCpuRawNice'])
            && isset($ss['ssCpuRawSystem']) && isset($ss['ssCpuRawIdle']))

    Windows has no nice, but emitting 0 states something true -- there is never
    any nice time on Windows -- which is different from iowait and steal, which
    cannot be measured at all. Omit it and the whole graph never appears.
    """
    emitted = _emitted()
    for num, name in ((50, "ssCpuRawUser"), (51, "ssCpuRawNice"),
                      (52, "ssCpuRawSystem"), (53, "ssCpuRawIdle")):
        assert emitted.get(num) == name, (
            f"{name} is not emitted, so LibreNMS will not create the Detailed "
            "Processor Usage graph")


def test_unmeasurable_fields_are_not_emitted():
    """Fields that cannot be measured on Windows are **not emitted**, rather
    than filled with 0.

    A zero makes LibreNMS create the graph and draw a flat line, which reads as
    "measured, and it was zero" when the truth is "not measurable at all".
    """
    emitted = _emitted()
    for num, name in ((54, "ssCpuRawWait"), (64, "ssCpuRawSteal"),
                      (61, "ssCpuRawSoftIRQ"), (65, "ssCpuRawGuest")):
        assert num not in emitted, (
            f"{name} (field {num}) cannot be measured on Windows and must not be emitted")


def test_userhz_conversion_documented():
    """UCD's ssCpuRaw* fields are in USER_HZ, hundredths of a second, while
    Windows counts in 100 ns units: a factor of 10^5. Get it wrong and every
    percentage is meaningless."""
    assert "100_000" in SRC or "100000" in SRC, "the USER_HZ conversion is missing"
    assert "USER_HZ" in SRC, "the conversion factor should say where it comes from"
