"""A hand-written BER varbind encoder, and the optimisation gate C turned on.

Why it exists: building a response through pysnmp and pyasn1 was measured at
**about 125 µs per varbind**, split as

    pyasn1 object construction (apiPDU.set_varbinds)   84 µs/vb
    BER encoding itself                                49 µs/vb

which is far past the 80 µs per varbind budget, and it is linear: raising
max-repetitions does not amortise it (127 µs/vb at 25, 124 µs/vb at 100).

With snapshot + bisect the MIB layer costs 8 µs/vb, so the whole bottleneck sits
in encoding. This was the risk named in advance as "the performance of BER in
pure Python".

The answer follows from the snapshot being immutable: encode every varbind to
BER bytes **when the snapshot is built**, and leave the request path with a
slice and a concatenation. It also replaces the earlier approach of storing only
the sizes, because with the wire bytes in hand the size is len().
"""

from __future__ import annotations

# ASN.1 / SNMP tags
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQUENCE = 0x30
TAG_IPADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43
TAG_OPAQUE = 0x44
TAG_COUNTER64 = 0x46


def enc_len(n: int) -> bytes:
    """The BER length field: short form under 128, long form otherwise."""
    if n < 0x80:
        return bytes((n,))
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(b),)) + b


def tlv(tag: int, content: bytes) -> bytes:
    return bytes((tag,)) + enc_len(len(content)) + content


def enc_int_content(v: int) -> bytes:
    """Integer content, deliberately matching what pyasn1 emits rather than
    DER's shortest encoding: at negative boundaries pyasn1 adds a redundant
    leading byte. Predicting pyasn1 is the point, so this follows pyasn1."""
    n = v.bit_length() // 8 + 1
    return v.to_bytes(n, "big", signed=True)


def enc_oid_content(oid: tuple[int, ...]) -> bytes:
    """OID content: the first two sub-identifiers combine as 40*a+b, and each of
    the rest is base-128 variable length."""
    if len(oid) < 2:
        raise ValueError(f"an OID needs at least two sub-identifiers: {oid}")
    out = bytearray()
    for sub in (oid[0] * 40 + oid[1], *oid[2:]):
        if sub < 0x80:
            out.append(sub)
            continue
        chunks = []
        while sub:
            chunks.append(sub & 0x7F)
            sub >>= 7
        chunks.reverse()
        for c in chunks[:-1]:
            out.append(c | 0x80)
        out.append(chunks[-1])
    return bytes(out)


def enc_oid(oid: tuple[int, ...]) -> bytes:
    return tlv(TAG_OID, enc_oid_content(oid))


def enc_value(val) -> bytes:
    """Encode one pyasn1/pysnmp value object to BER.

    The order of the type checks matters. Counter32, Gauge32 and TimeTicks are
    all subclasses of Integer, so they have to be tested first or they are
    encoded with the wrong tag.
    """
    from pysnmp.proto import rfc1902

    if isinstance(val, rfc1902.Counter64):
        return tlv(TAG_COUNTER64, enc_int_content(int(val)))
    if isinstance(val, rfc1902.Counter32):
        return tlv(TAG_COUNTER32, enc_int_content(int(val)))
    if isinstance(val, rfc1902.Gauge32):  # Unsigned32 shares the tag
        return tlv(TAG_GAUGE32, enc_int_content(int(val)))
    if isinstance(val, rfc1902.TimeTicks):
        return tlv(TAG_TIMETICKS, enc_int_content(int(val)))
    if isinstance(val, rfc1902.IpAddress):
        return tlv(TAG_IPADDRESS, val.asOctets())
    if isinstance(val, rfc1902.Opaque):
        return tlv(TAG_OPAQUE, val.asOctets())
    if isinstance(val, rfc1902.ObjectIdentifier):
        return enc_oid(tuple(val))
    if isinstance(val, rfc1902.OctetString):
        return tlv(TAG_OCTET_STRING, val.asOctets())
    if isinstance(val, rfc1902.Integer32) or isinstance(val, rfc1902.Integer):
        return tlv(TAG_INTEGER, enc_int_content(int(val)))
    raise TypeError(f"unsupported type: {type(val).__name__}")


def enc_varbind(oid: tuple[int, ...], val) -> bytes:
    """A complete VarBind ::= SEQUENCE { name ObjectName, value ObjectSyntax }"""
    return tlv(TAG_SEQUENCE, enc_oid(oid) + enc_value(val))


def precompute_wire(oids, values) -> tuple[bytes, ...]:
    """Pre-encode every varbind when the snapshot is built.

    The memory cost at real scale (3,000 to 6,500 varbinds) is 90 to 180 KB,
    which is not worth weighing against the time it saves.
    """
    return tuple(enc_varbind(o, v) for o, v in zip(oids, values))
