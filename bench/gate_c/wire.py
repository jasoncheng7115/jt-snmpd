"""手寫 BER varbind 編碼器（閘門 C 的關鍵優化）。

為什麼需要這個：實測顯示 pysnmp/pyasn1 產生一個回應的成本是
**每 varbind 約 125 µs**，其中

    pyasn1 物件建構（apiPDU.set_varbinds）   84 µs/vb
    純 BER 編碼                              49 µs/vb

——遠超 §4.2 的 80 µs/varbind 總預算，而且是線性成本，調高
max-repetitions 攤不掉（25 筆時 127 µs/vb，100 筆時 124 µs/vb）。

snapshot + bisect 已經把 MIB 層降到 8 µs/vb，瓶頸完全在編碼層，
正是 §1.3 預先點名的風險②「純 Python BER 的效能」。

解法：既然 snapshot 是不可變的，就在**建立時**把每筆 varbind 編成 BER bytes，
請求路徑上只做「切片 + 串接」。這也自然取代了原本只預存大小的做法——
有了 wire bytes，長度就是 len()。
"""

from __future__ import annotations

# ASN.1 / SNMP 標籤
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
    """BER 長度欄位：< 128 為短式，否則長式。"""
    if n < 0x80:
        return bytes((n,))
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(b),)) + b


def tlv(tag: int, content: bytes) -> bytes:
    return bytes((tag,)) + enc_len(len(content)) + content


def enc_int_content(v: int) -> bytes:
    """整數內容。刻意對齊 pyasn1 的行為（負數邊界會多一個冗餘前導位元組），
    而非 DER 最短編碼——見 docs/phase0-findings.md §2.4。"""
    n = v.bit_length() // 8 + 1
    return v.to_bytes(n, "big", signed=True)


def enc_oid_content(oid: tuple[int, ...]) -> bytes:
    """OID 內容：前兩個 sub-id 併為 40*a+b，其後各自 base-128 變長編碼。"""
    if len(oid) < 2:
        raise ValueError(f"OID 至少需兩個 sub-identifier: {oid}")
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
    """把一個 pyasn1/pysnmp 值物件編成 BER。

    型別判定順序重要：Counter32 / Gauge32 / TimeTicks 都是 Integer 的子類別，
    必須先於 Integer 判定，否則會被編成錯誤的 tag。
    """
    from pysnmp.proto import rfc1902

    if isinstance(val, rfc1902.Counter64):
        return tlv(TAG_COUNTER64, enc_int_content(int(val)))
    if isinstance(val, rfc1902.Counter32):
        return tlv(TAG_COUNTER32, enc_int_content(int(val)))
    if isinstance(val, rfc1902.Gauge32):  # Unsigned32 同 tag
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
    raise TypeError(f"不支援的型別：{type(val).__name__}")


def enc_varbind(oid: tuple[int, ...], val) -> bytes:
    """完整的 VarBind ::= SEQUENCE { name ObjectName, value ObjectSyntax }"""
    return tlv(TAG_SEQUENCE, enc_oid(oid) + enc_value(val))


def precompute_wire(oids, values) -> tuple[bytes, ...]:
    """snapshot 建立時把每筆 varbind 預先編碼。

    記憶體代價：真實規模（3,000～6,500 varbind）約 90～180 KB，可忽略。
    """
    return tuple(enc_varbind(o, v) for o, v in zip(oids, values))
