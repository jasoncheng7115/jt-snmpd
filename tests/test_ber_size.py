"""Calculated BER sizes checked against the real encoder.

**Why this exists**

The 1400-byte truncation decides when to stop from the varbind sizes stored in
the snapshot. Those sizes are calculated rather than encoded, because encoding
for real costs 93% of the snapshot build and would break the 500 ms budget, and
they deliberately match what pyasn1 emits rather than DER's shortest form.

If pyasn1 ever changes how it encodes, the predictions drift silently, responses
exceed 1400 bytes, and the firewall drops the fragments. The symptom is
"LibreNMS intermittently gets no data", which is close to undiagnosable from that
end. So this comparison belongs in CI.
"""

import random

import pytest
from pyasn1.codec.ber import encoder as ber
from pysnmp.proto import rfc1902
from pysnmp.proto.api import v2c

from bench.gate_c.snapshot import precompute_sizes


def _real_size(oid, val) -> int:
    vb = v2c.VarBind()
    v2c.apiVarBind.set_oid_value(vb, (v2c.ObjectIdentifier(oid), val))
    return len(ber.encode(vb))


OIDS = [
    (1, 3, 6, 1, 2, 1, 1, 1, 0),
    (1, 3, 6, 1, 4, 1, 311, 1, 1, 3, 1, 1),
    (1, 3, 6, 1, 2, 1, 2, 2, 1, 10, 2147483647),
    (1, 3, 6, 1, 4, 1, 99999, 1, 1, 1, 20, 5000),
    tuple([1, 3, 6, 1, 4, 1, 99999] + [128] * 10),  # forces multi-byte base-128 sub-identifiers
    (0, 0),
    (2, 999, 1),
]

# Boundaries: each one is where two's complement or the leading-byte rule turns over
INT32 = [0, 1, 127, 128, 255, 256, 32767, 32768, 65535, 2147483647,
         -1, -127, -128, -129, -255, -256, -32768, -32769, -2147483648]
UINT32 = [0, 1, 127, 128, 255, 256, 65535, 65536, 2147483647, 2147483648, 4294967295]
UINT64 = UINT32 + [2**32, 2**63 - 1, 2**63, 2**64 - 1, 10**18]
STR_LENS = [0, 1, 127, 128, 255, 256, 300, 1000]


def _cases():
    for oid in OIDS:
        for v in INT32:
            yield oid, rfc1902.Integer32(v)
        for v in UINT32:
            yield oid, rfc1902.Counter32(v)
            yield oid, rfc1902.Gauge32(v)
            yield oid, rfc1902.TimeTicks(v)
        for v in UINT64:
            yield oid, rfc1902.Counter64(v)
        for n in STR_LENS:
            yield oid, rfc1902.OctetString(b"x" * n)
        yield oid, rfc1902.ObjectIdentifier((1, 3, 6, 1, 4, 1, 311, 1, 1, 3, 1, 2))


@pytest.mark.parametrize("oid,val", list(_cases()))
def test_analytic_size_matches_encoder(oid, val):
    assert precompute_sizes([oid], [val])[0] == _real_size(oid, val)


def test_analytic_size_matches_encoder_randomized():
    """A random sweep, to cover combinations the boundary table does not list."""
    rnd = random.Random(20260823)
    mismatches = []
    for _ in range(4000):
        oid = tuple([1, 3, 6, 1, 4, 1] + [rnd.randint(0, 300000) for _ in range(rnd.randint(1, 8))])
        val = rnd.choice([
            lambda: rfc1902.Integer32(rnd.randint(-2147483648, 2147483647)),
            lambda: rfc1902.Counter32(rnd.randint(0, 4294967295)),
            lambda: rfc1902.Gauge32(rnd.randint(0, 4294967295)),
            lambda: rfc1902.TimeTicks(rnd.randint(0, 4294967295)),
            lambda: rfc1902.Counter64(rnd.randint(0, 2**64 - 1)),
            lambda: rfc1902.OctetString(bytes(rnd.randint(0, 255) for _ in range(rnd.randint(0, 400)))),
        ])()
        got, want = precompute_sizes([oid], [val])[0], _real_size(oid, val)
        if got != want:
            mismatches.append((oid, type(val).__name__, val, got, want))
    assert not mismatches, f"{len(mismatches)} mismatches, first 5: {mismatches[:5]}"
