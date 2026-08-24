"""同處理程序 profile：把「解碼請求 → controller → 組裝回應 → 編碼」整條請求路徑
用 cProfile 剖析，找出 §4.2 的 80 µs/varbind 預算花在哪。

不經過 socket / asyncio，直接呼叫我們自訂的 command responder 邏輯，
因此量到的是「處理成本」本身，排除網路與事件迴圈排程的雜訊。
"""

from __future__ import annotations

import cProfile
import io
import pstats
import sys

from pyasn1.codec.ber import decoder as ber_dec
from pyasn1.codec.ber import encoder as ber_enc
from pysnmp.proto.api import v2c

sys.path.insert(0, ".")
from bench.gate_c.snapshot import build_synthetic_snapshot  # noqa: E402
from bench.gate_c.wire import (  # noqa: E402
    TAG_INTEGER, TAG_OCTET_STRING, TAG_SEQUENCE, enc_int_content, tlv,
)

N = 50000
ITERS = 20000
snap = build_synthetic_snapshot(N)

# 預先算好 wire bytes（snapshot 建立時的一次性成本，不計入請求路徑）
from bench.gate_c.wire import precompute_wire  # noqa: E402

WIRE = precompute_wire(snap.oids, snap.values)
OIDS = snap.oids

# 預先做好一個 GETBULK 請求封包（模擬 client 送來的位元組）
_req = v2c.GetBulkRequestPDU()
v2c.apiBulkPDU.set_defaults(_req)
v2c.apiBulkPDU.set_non_repeaters(_req, 0)
v2c.apiBulkPDU.set_max_repetitions(_req, 25)
v2c.apiBulkPDU.set_varbinds(_req, [(v2c.ObjectIdentifier(OIDS[len(OIDS) // 3]), v2c.Null(""))])
_m = v2c.Message()
v2c.apiMessage.set_defaults(_m)
v2c.apiMessage.set_community(_m, "bench")
v2c.apiMessage.set_pdu(_m, _req)
REQ_BYTES = ber_enc.encode(_m)

from bisect import bisect_right  # noqa: E402

MSG_SPEC = v2c.Message()


def handle_full_wire(raw: bytes) -> bytes:
    """完整請求路徑（wire 預編碼版本）。"""
    # ① 解碼請求
    msg, _ = ber_dec.decode(raw, asn1Spec=MSG_SPEC)
    pdu = v2c.apiMessage.get_pdu(msg)
    community = v2c.apiMessage.get_community(msg)
    reqid = v2c.apiBulkPDU.get_request_id(pdu)
    maxrep = min(int(v2c.apiBulkPDU.get_max_repetitions(pdu)), 25)
    start = tuple(v2c.apiBulkPDU.get_varbinds(pdu)[0][0])

    # ② controller：一次 bisect + 切片（含 1400 bytes 預算）
    i = bisect_right(OIDS, start)
    budget = 1400 - 120
    used = 0
    parts = []
    end = min(i + maxrep, len(OIDS))
    while i < end:
        w = WIRE[i]
        if parts and used + len(w) > budget:
            break
        used += len(w)
        parts.append(w)
        i += 1

    # ③ 組裝回應 + 編碼（純位元組串接）
    vbl = tlv(TAG_SEQUENCE, b"".join(parts))
    rpdu = tlv(0xA2, tlv(TAG_INTEGER, enc_int_content(int(reqid)))
               + tlv(TAG_INTEGER, enc_int_content(0))
               + tlv(TAG_INTEGER, enc_int_content(0)) + vbl)
    return tlv(TAG_SEQUENCE, tlv(TAG_INTEGER, enc_int_content(1))
               + tlv(TAG_OCTET_STRING, bytes(community)) + rpdu)


def handle_full_pysnmp(raw: bytes) -> bytes:
    """完整請求路徑（pysnmp 物件模型版本），作為對照。"""
    msg, _ = ber_dec.decode(raw, asn1Spec=MSG_SPEC)
    pdu = v2c.apiMessage.get_pdu(msg)
    community = v2c.apiMessage.get_community(msg)
    reqid = v2c.apiBulkPDU.get_request_id(pdu)
    maxrep = min(int(v2c.apiBulkPDU.get_max_repetitions(pdu)), 25)
    start = tuple(v2c.apiBulkPDU.get_varbinds(pdu)[0][0])

    i = bisect_right(OIDS, start)
    end = min(i + maxrep, len(OIDS))
    vbs = [(v2c.ObjectIdentifier(OIDS[j]), snap.values[j]) for j in range(i, end)]

    rpdu = v2c.ResponsePDU()
    v2c.apiPDU.set_defaults(rpdu)
    v2c.apiPDU.set_request_id(rpdu, int(reqid))
    v2c.apiPDU.set_varbinds(rpdu, vbs)
    rmsg = v2c.Message()
    v2c.apiMessage.set_defaults(rmsg)
    v2c.apiMessage.set_community(rmsg, community)
    v2c.apiMessage.set_pdu(rmsg, rpdu)
    return ber_enc.encode(rmsg)


def _time(fn) -> float:
    import time
    fn(REQ_BYTES)
    t = time.perf_counter()
    for _ in range(ITERS):
        fn(REQ_BYTES)
    return (time.perf_counter() - t) / ITERS


def main() -> None:
    # 正確性：兩條路徑必須解出相同 varbind
    a = handle_full_wire(REQ_BYTES)
    b = handle_full_pysnmp(REQ_BYTES)
    ma, _ = ber_dec.decode(a, asn1Spec=v2c.Message())
    mb, _ = ber_dec.decode(b, asn1Spec=v2c.Message())
    va = [tuple(n) for n, _ in v2c.apiPDU.get_varbinds(v2c.apiMessage.get_pdu(ma))]
    vb = [tuple(n) for n, _ in v2c.apiPDU.get_varbinds(v2c.apiMessage.get_pdu(mb))]
    assert va == vb, "兩條路徑結果不一致"
    print(f"正確性：wire 與 pysnmp 兩條路徑解出相同 {len(va)} 筆 varbind ✓\n")

    for name, fn in (("wire 預編碼", handle_full_wire), ("pysnmp 物件模型", handle_full_pysnmp)):
        d = _time(fn)
        print(f"{name:<16} {d*1e6:8.1f} µs/封包   {d*1e6/25:7.2f} µs/varbind")

    print("\n=== cProfile：wire 路徑熱點（前 12）===")
    pr = cProfile.Profile()
    pr.enable()
    for _ in range(ITERS):
        handle_full_wire(REQ_BYTES)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(12)
    print(s.getvalue())


if __name__ == "__main__":
    main()
