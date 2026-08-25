"""Gate C prototype: the snapshot + bisect architecture.

pysnmp's MibTableColumn / MibScalarInstance object model is deliberately not
used. The whole MIB is one array sorted in OID lexicographic order; GET is a
`bisect_left` and GETNEXT a `bisect_right`. Lexicographic ordering, absence of
duplicate OIDs, absence of GETNEXT loops and a correct endOfMibView all follow
from that one property, so none of them has to be maintained by hand.
"""

from __future__ import annotations

import random
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from pysnmp.proto import rfc1902, rfc1905
from pysnmp.proto.api import v2c
from pysnmp.smi.instrum import AbstractMibInstrumController

OidTuple = tuple[int, ...]


@dataclass(frozen=True)
class Snapshot:
    """One immutable snapshot, shared by a whole walk.

    A walk that saw ifTable grow or shrink partway through would leave LibreNMS
    with duplicated or vanished ports; sharing one snapshot makes that
    impossible rather than unlikely.
    """

    oids: tuple[OidTuple, ...]  # sorted, so bisect works
    values: tuple[object, ...]  # same length, positionally aligned, ASN.1 objects
    sizes: tuple[int, ...] = ()  # BER byte count per varbind, computed at build time
    generation: int = 0
    built_at_monotonic: float = 0.0
    collector_health: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.oids)


class SnapshotMibInstrumController(AbstractMibInstrumController):
    """Replace pysnmp's MIB layer entirely, keeping message, USM, VACM and transport.

    pysnmp 7.1.29's AbstractMibInstrumController has exactly three methods.
    Leaving write_variables unimplemented makes this a read-only agent by
    construction rather than by a check.
    """

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot

    # --- GET ---------------------------------------------------------------
    def read_variables(self, *varBinds, **context):
        snap = self.snapshot  # read once, so a swap mid-walk cannot be observed
        acFun = context.get("acFun")
        out = []
        for vb in varBinds:
            name = vb[0]
            target: OidTuple = tuple(name)
            i = bisect_left(snap.oids, target)
            if i < len(snap.oids) and snap.oids[i] == target:
                val = snap.values[i]
                # VACM has to apply to GET as well as GETNEXT
                if acFun and acFun("read", (name, val), **context) is False:
                    out.append((name, rfc1905.noSuchObject))
                    continue
                out.append((name, val))
            else:
                out.append((name, rfc1905.noSuchInstance))
        return out

    # --- GETNEXT -----------------------------------------------------------
    def read_next_variables(self, *varBinds, **context):
        snap = self.snapshot
        acFun = context.get("acFun")
        oids, values, n = snap.oids, snap.values, len(snap.oids)
        out = []
        for vb in varBinds:
            name = vb[0]
            i = bisect_right(oids, tuple(name))
            # VACM has to apply along the walk, not only to GET. A denied entry
            # means keep looking, not return an error: returning one ends the
            # walk there, which is the classic way this is got wrong.
            while i < n:
                nxt = v2c.ObjectIdentifier(oids[i])
                val = values[i]
                if acFun is None or acFun("read", (nxt, val), **context) is not False:
                    out.append((nxt, val))
                    break
                i += 1
            else:
                out.append((name, rfc1905.endOfMibView))
        return out


# --- synthetic data ---------------------------------------------------------

_BASE: OidTuple = (1, 3, 6, 1, 4, 1, 99999, 1)


def build_synthetic_snapshot(n_varbinds: int, seed: int = 1) -> Snapshot:
    """A synthetic table of n varbinds.

    The type mix is deliberate, and close to a real ifTable/ifXTable: integers,
    counters, 64-bit counters, strings, gauges and timeticks. Measuring with
    integers alone understates the cost of BER encoding.
    """
    rnd = random.Random(seed)
    cols = 20
    rows = max(1, n_varbinds // cols)

    makers = [
        lambda: rfc1902.Integer32(rnd.randint(0, 2_000_000_000)),
        lambda: rfc1902.Counter32(rnd.randint(0, 4_000_000_000)),
        lambda: rfc1902.Counter64(rnd.randint(0, 10**18)),
        lambda: rfc1902.OctetString(f"Ethernet {rnd.randint(0, 9999)} Gigabit Adapter"),
        lambda: rfc1902.Gauge32(rnd.randint(0, 10_000_000)),
        lambda: rfc1902.TimeTicks(rnd.randint(0, 4_000_000_000)),
    ]

    pairs: list[tuple[OidTuple, object]] = []
    for col in range(1, cols + 1):
        mk = makers[(col - 1) % len(makers)]
        for row in range(1, rows + 1):
            pairs.append((_BASE + (1, 1, col, row), mk()))

    # Always re-sort: a real collector makes no ordering promise, and sorting is
    # the snapshot builder's job
    pairs.sort(key=lambda p: p[0])
    oids = tuple(p[0] for p in pairs)
    values = tuple(p[1] for p in pairs)
    return Snapshot(
        oids=oids,
        values=values,
        sizes=precompute_sizes(oids, values),
        generation=1,
)


def _tlv_len(content_len: int) -> int:
    """tag(1) + the BER length field + the content. Under 128 the length is one byte."""
    if content_len < 0x80:
        return 1 + 1 + content_len
    n = (content_len.bit_length() + 7) // 8
    return 1 + 1 + n + content_len


def _oid_content_len(oid: OidTuple) -> int:
    """OID content length: the first two sub-identifiers combine as 40*a+b, and
    each of the rest is base-128 variable length."""
    if len(oid) < 2:
        return 1
    total = 0
    first = oid[0] * 40 + oid[1]
    for sub in (first, *oid[2:]):
        total += 1 if sub < 0x80 else (sub.bit_length() + 6) // 7
    return total


def _int_content_len(v: int) -> int:
    """BER content length for an integer; one formula covers both signs.

    This tracks **what pyasn1 actually emits**, not DER's shortest encoding. At
    negative boundaries pyasn1 emits a redundant leading byte: -128 encodes as
    `ff 80` rather than `80`, and -2147483648 takes five bytes rather than four.
    The purpose here is to predict how many bytes pyasn1 will produce, so the
    response can be truncated before the 1400-byte cap, which means following
    pyasn1 rather than the standard. A property test compares this against the
    real encoder, so a pyasn1 upgrade cannot drift it silently.
    """
    return v.bit_length() // 8 + 1


_uint_content_len = _int_content_len


def _value_content_len(val) -> int:
    if isinstance(val, rfc1902.OctetString):
        return len(val.asOctets())
    if isinstance(val, (rfc1902.Counter64, rfc1902.Counter32, rfc1902.Gauge32, rfc1902.TimeTicks)):
        return _uint_content_len(int(val))
    if isinstance(val, rfc1902.ObjectIdentifier):
        return _oid_content_len(tuple(val))
    if isinstance(val, rfc1902.Integer32) or isinstance(val, rfc1902.Integer):
        return _int_content_len(int(val))
    return None  # unknown type: the caller falls back to encoding it for real


def precompute_sizes(oids, values) -> tuple[int, ...]:
    """Compute each varbind's BER size once, when the snapshot is built.

    Responses are truncated at 1400 bytes. Encoding speculatively on the request
    path and backing off is expensive; paying once at build time leaves the
    request path with an addition and a comparison.

    The sizes are calculated rather than encoded. Encoding for real was measured
    at 115 µs per varbind, 93% of the cost of building a snapshot, which would
    break the budget of rebuilding one in under 500 ms.
    """
    from pyasn1.codec.ber import encoder as ber

    sizes = []
    for oid, val in zip(oids, values):
        vlen = _value_content_len(val)
        if vlen is None:
            vb = v2c.VarBind()
            v2c.apiVarBind.set_oid_value(vb, (v2c.ObjectIdentifier(oid), val))
            sizes.append(len(ber.encode(vb)))
            continue
        inner = _tlv_len(_oid_content_len(oid)) + _tlv_len(vlen)
        sizes.append(_tlv_len(inner))  # the enclosing SEQUENCE
    return tuple(sizes)
