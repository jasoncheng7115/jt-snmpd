"""The pre-auth gate has to be attached to pysnmp's receive path.

**Why this test exists**

The first implementation named the override `handle_datagram`. pysnmp 7.x calls
`datagram_received`. Python does not complain when a subclass overrides a method
that does not exist: the subclass simply gains a method nobody calls, and the
original keeps running.

The result was an agent that started, answered, passed its tests, and logged
"pre-auth gate enabled" — with **the gate doing nothing at all**. Every source
check, the rate limit and the malformed-packet check were bypassed, and an
attacker's bytes went straight into the BER decoder.

That is the worst failure mode a security control has: **it looks like
protection and is not**. So the attachment point itself is pinned here, not just
the gate's logic.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from pysnmp.carrier.asyncio.dgram import udp

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"


def _gated_transport_methods() -> set[str]:
    """Parse the agent and list what GatedUdpTransport overrides.

    ast rather than import: the agent needs winreg and ctypes.windll, which do
    not exist on Linux.
    """
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GatedUdpTransport":
            return {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
    pytest.fail("GatedUdpTransport is not defined in the agent")


def _gated_transport_bases() -> list[str]:
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GatedUdpTransport":
            return [ast.unparse(b) for b in node.bases]
    pytest.fail("GatedUdpTransport is not defined in the agent")


def test_gated_transport_subclasses_pysnmp_udp_transport():
    bases = _gated_transport_bases()
    assert any("UdpTransport" in b for b in bases), (
        f"GatedUdpTransport has to inherit pysnmp's UdpTransport; bases={bases}")


def test_overridden_methods_actually_exist_on_parent():
    """Every override has to exist on the parent.

    Overriding a method that does not exist is legal and silent in Python, which
    is precisely what this stops.
    """
    overridden = _gated_transport_methods() - {"__init__"}
    assert overridden, "GatedUdpTransport overrides nothing"

    parent_attrs = set()
    for cls in udp.UdpTransport.__mro__:
        parent_attrs |= set(vars(cls).keys())

    bogus = {m for m in overridden if m not in parent_attrs}
    assert not bogus, (
        f"these methods do not exist anywhere on pysnmp's UdpTransport chain, "
        f"so overriding them has no effect: {sorted(bogus)}. "
        f"The available receive hooks are: "
        f"{sorted(a for a in parent_attrs if 'datagram' in a.lower())}")


def test_datagram_received_is_the_hook_point():
    """Pin the hook name. If pysnmp renames it, this fails first, rather than a
    security control failing silently in production."""
    assert "datagram_received" in _gated_transport_methods(), (
        "datagram_received has to be overridden; it is pysnmp 7.x's receive hook")
    assert hasattr(udp.UdpTransport, "datagram_received")


def test_override_signature_matches_parent():
    """A signature mismatch only fails at run time, and only once a packet arrives."""
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    ours = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GatedUdpTransport":
            for n in node.body:
                if isinstance(n, ast.FunctionDef) and n.name == "datagram_received":
                    ours = [a.arg for a in n.args.args]
    assert ours is not None, "no datagram_received override found"

    parent = list(inspect.signature(udp.UdpTransport.datagram_received).parameters)
    assert len(ours) == len(parent), (
        f"parameter count differs: ours {ours} against pysnmp's {parent}")


def test_override_calls_super():
    """An allowed packet has to be handed back to the parent. Otherwise the
    agent receives and never answers: another way to be Running and useless."""
    src = AGENT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GatedUdpTransport":
            for n in node.body:
                if isinstance(n, ast.FunctionDef) and n.name == "datagram_received":
                    body = ast.unparse(n)
                    assert "super().datagram_received" in body, (
                        "the allow path has to call super().datagram_received")
                    return
    pytest.fail("datagram_received not found")


def test_gate_is_instantiated_and_assigned_to_module_global():
    """The gate has to be built and assigned to the module-level _gate.

    Otherwise `if gate is not None` inside datagram_received is always None and
    the whole gate is short-circuited."""
    src = AGENT.read_text(encoding="utf-8")
    assert "_gate = PreAuthGate(" in src, "a PreAuthGate has to be built and assigned to _gate"
    assert "global _gate" in src, "without a global declaration the assignment is local"
