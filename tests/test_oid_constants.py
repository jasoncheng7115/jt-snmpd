"""Base OID constants pinned to their RFC values.

**Why this exists**

A mistyped OID fails **silently**. It happened once: ifXTable was written as
`1.3.6.1.31.1.1.1`, missing the `2.1`. The agent started, the walk answered, and
the entire table hung off a branch that does not exist. From LibreNMS the symptom
was "the Ports page has no names and no 64-bit counters", and from the agent
there was nothing to see at all.

LibreNMS's windows.yaml sets `ifname: true`, so port labels come straight from
ifXTable's ifName. Getting that table wrong disables half of what Ports does.

This file pins every base OID to its RFC definition. Changing an OID constant
means changing this too, and changing this forces a look at the RFC rather than
a guess from memory.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"

# Sources: RFC 1213 / RFC 2863 (IF-MIB) / RFC 2790 (HOST-RESOURCES-MIB) / UCD-SNMP-MIB
EXPECTED = {
    "SYS":    ((1, 3, 6, 1, 2, 1, 1),                  "SNMPv2-MIB::system"),
    "IFT":    ((1, 3, 6, 1, 2, 1, 2, 2, 1),            "IF-MIB::ifEntry"),
    "IFX":    ((1, 3, 6, 1, 2, 1, 31, 1, 1, 1),        "IF-MIB::ifXEntry"),
    "HR":     ((1, 3, 6, 1, 2, 1, 25),                 "HOST-RESOURCES-MIB::host"),
    "HRSTOR": ((1, 3, 6, 1, 2, 1, 25, 2, 3, 1),        "HOST-RESOURCES-MIB::hrStorageEntry"),
    "HRDEV":  ((1, 3, 6, 1, 2, 1, 25, 3, 2, 1),        "HOST-RESOURCES-MIB::hrDeviceEntry"),
    "HRPROC": ((1, 3, 6, 1, 2, 1, 25, 3, 3, 1),        "HOST-RESOURCES-MIB::hrProcessorEntry"),
    "DIO":    ((1, 3, 6, 1, 4, 1, 2021, 13, 15, 1, 1), "UCD-SNMP-MIB::diskIOEntry"),
}


def _module_constants() -> dict[str, tuple[int, ...]]:
    """Read the base OID constants out of the agent source, statically.

    ast rather than import: the agent needs winreg and ctypes.windll, which do
    not exist on the Linux CI runner. Parsing keeps this test runnable anywhere.
    """
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    env: dict[str, tuple[int, ...]] = {}

    def resolve(node) -> tuple[int, ...] | None:
        if isinstance(node, ast.Tuple):
            vals = []
            for el in node.elts:
                if isinstance(el, ast.Constant) and isinstance(el.value, int):
                    vals.append(el.value)
                else:
                    return None
            return tuple(vals)
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = resolve(node.left), resolve(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name):
                got = resolve(stmt.value)
                if got is not None:
                    env[target.id] = got
    return env


@pytest.mark.parametrize("name,expected,rfc_name", [
    (k, v[0], v[1]) for k, v in EXPECTED.items()
])
def test_base_oid_matches_rfc(name: str, expected: tuple[int, ...], rfc_name: str):
    consts = _module_constants()
    assert name in consts, f"{name} is not defined in the agent"
    got = consts[name]
    assert got == expected, (
        f"{name} should be {rfc_name} = {'.'.join(map(str, expected))}, "
        f"but is {'.'.join(map(str, got))}"
)


def test_all_base_oids_are_under_iso_org_dod_internet():
    """Every base OID lives under .1.3.6.1. A wrong prefix hides the whole table
    on a branch nothing will ever walk."""
    consts = _module_constants()
    for name in EXPECTED:
        got = consts[name]
        assert got[:4] == (1, 3, 6, 1), f"{name} has the wrong prefix: {got[:4]}"


def test_enterprise_oids_use_registered_pen():
    """A private branch has to use a registered PEN. 2021 is UCD-SNMP, which is
    where LibreNMS looks for diskIO. Substituting our own PEN would put the table
    somewhere LibreNMS never reads."""
    consts = _module_constants()
    assert consts["DIO"][:6] == (1, 3, 6, 1, 4, 1), "DIO has to sit under enterprises"
    assert consts["DIO"][6] == 2021, "diskIO has to use UCD-SNMP's PEN, 2021"
