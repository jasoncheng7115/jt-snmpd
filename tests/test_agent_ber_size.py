"""The shipped agent's varbind size arithmetic, against the real encoder.

`_varbind_size` decides when to stop filling a GETBULK response. Every byte it
under-estimates is a byte the response goes over 1400, and over 1400 the
datagram fragments — which is the defect this arithmetic was added to fix. So it
is not enough for it to be approximately right.

The equivalent test for the prototype is tests/test_ber_size.py. This one exists
because those are two different implementations in two different files, and only
this one is in the program customers install.

The arithmetic deliberately tracks **what pyasn1 emits**, not DER's shortest
encoding: at negative boundaries pyasn1 adds a redundant leading byte, so -128
encodes as `ff 80` rather than `80`. Following the standard instead of the
encoder would under-count exactly where it matters.
"""

from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest
from pyasn1.codec.ber import encoder as ber
from pysnmp.proto import rfc1902
from pysnmp.proto.api import v2c

AGENT = Path(__file__).resolve().parents[1] / "deploy" / "jt_agent.py"


def _extract() -> dict:
    """Pull the size helpers out of the agent source.

    Importing the agent fails on Linux: winreg, ctypes.windll and iphlpapi are
    not there. These functions are pure arithmetic, so extracting them keeps
    the check runnable in CI.
    """
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    ns: dict = {"rfc1902": rfc1902, "v2c": v2c}
    wanted = {"_tlv_len", "_oid_content_len", "_int_content_len", "_varbind_size"}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            exec(compile(ast.Module([node], []), "<agent>", "exec"), ns)  # noqa: S102
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") in ("MAX_RESPONSE_BYTES",
                                            "MESSAGE_OVERHEAD_RESERVE"):
                    exec(compile(ast.Module([node], []), "<agent>", "exec"), ns)  # noqa: S102
                    found.add(t.id)
    missing = wanted - set(ns)
    assert not missing, f"not found in the agent: {missing}"
    assert found == {"MAX_RESPONSE_BYTES", "MESSAGE_OVERHEAD_RESERVE"}
    return ns


NS = _extract()
size_of = NS["_varbind_size"]


def _real_size(oid: tuple, val) -> int:
    vb = v2c.VarBind()
    v2c.apiVarBind.set_oid_value(vb, (v2c.ObjectIdentifier(oid), val))
    return len(ber.encode(vb))


SYS = (1, 3, 6, 1, 2, 1, 1)
ENT = (1, 3, 6, 1, 4, 1, 99999, 1, 1)


@pytest.mark.parametrize("val", [
    rfc1902.Integer32(0), rfc1902.Integer32(1), rfc1902.Integer32(127),
    rfc1902.Integer32(128), rfc1902.Integer32(255), rfc1902.Integer32(256),
    rfc1902.Integer32(-1), rfc1902.Integer32(-127),
    # pyasn1 emits a redundant leading byte at these two
    rfc1902.Integer32(-128), rfc1902.Integer32(-2147483648),
    rfc1902.Integer32(2147483647),
    rfc1902.Gauge32(0), rfc1902.Gauge32(4294967295),
    rfc1902.Counter32(4294967295), rfc1902.Counter64(18446744073709551615),
    rfc1902.TimeTicks(0), rfc1902.TimeTicks(4294967295),
    rfc1902.OctetString(b""), rfc1902.OctetString(b"x" * 127),
    rfc1902.OctetString(b"x" * 128), rfc1902.OctetString(b"x" * 255),
    rfc1902.OctetString(b"x" * 256), rfc1902.OctetString(b"x" * 1100),
    rfc1902.OctetString("乙太網路".encode("utf-8")),
    rfc1902.ObjectIdentifier((1, 3, 6, 1, 4, 1, 311, 1, 1, 3, 1, 3)),
])
def test_size_matches_the_encoder_at_the_boundaries(val):
    assert size_of(SYS + (1, 0), val) == _real_size(SYS + (1, 0), val)


def test_size_matches_for_deep_and_large_sub_identifiers():
    """hrDeviceIndex runs to 327680 and entPhysicalIndex to 4000-plus, so
    sub-identifiers well past 127 are ordinary here, not exotic."""
    for oid in [ENT + (1, 0), (1, 3, 6, 1, 2, 1, 25, 3, 2, 1, 3, 327680),
                (1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, 2, 4000),
                (1, 3, 6, 1, 4, 1, 8072, 1, 3, 2, 3, 1, 2, 5, 115, 109, 97, 114, 116)]:
        val = rfc1902.OctetString(b"payload")
        assert size_of(oid, val) == _real_size(oid, val), oid


def test_size_never_under_estimates_across_random_values():
    """The property that matters. Over-estimating costs a varbind in a response;
    under-estimating puts the datagram over 1400 and fragments it."""
    rnd = random.Random(20260827)
    for _ in range(4000):
        kind = rnd.randrange(5)
        if kind == 0:
            val = rfc1902.Integer32(rnd.randint(-2**31, 2**31 - 1))
        elif kind == 1:
            val = rfc1902.Gauge32(rnd.randint(0, 2**32 - 1))
        elif kind == 2:
            val = rfc1902.Counter64(rnd.randint(0, 2**64 - 1))
        elif kind == 3:
            val = rfc1902.OctetString(bytes(rnd.randrange(256)
                                            for _ in range(rnd.randrange(600))))
        else:
            val = rfc1902.TimeTicks(rnd.randint(0, 2**32 - 1))
        oid = SYS + tuple(rnd.randrange(1, 400000) for _ in range(rnd.randrange(1, 6)))
        computed, actual = size_of(oid, val), _real_size(oid, val)
        assert computed >= actual, f"under-estimated {oid} {val!r}: {computed} < {actual}"
        assert computed == actual, f"drifted on {oid} {val!r}: {computed} != {actual}"


def test_the_budget_leaves_room_for_the_v3_envelope():
    """authPriv carries the USM security parameters and privacy padding on top
    of the PDU, and that has to come out of the same 1400 bytes."""
    assert NS["MAX_RESPONSE_BYTES"] == 1400
    assert NS["MESSAGE_OVERHEAD_RESERVE"] >= 200, (
        "v3 authPriv envelopes measured around 190 bytes; leaving less than "
        "that is how a response that fits in the budget still leaves the wire "
        "over 1400")
