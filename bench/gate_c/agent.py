"""閘門 C 測試 agent：把 pysnmp 的 MIB 層換成 snapshot + bisect。

用法：
    python -m bench.gate_c.agent --varbinds 10000 --port 11161 [--stock-bulk]

--stock-bulk 使用 pysnmp 原生的 BulkCommandResponder（一次 repetition 一次
read_next_variables 呼叫），用來對照批次化版本的差異。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from bisect import bisect_right

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.proto import rfc1905
from pysnmp.proto.api import v2c

from .snapshot import Snapshot, SnapshotMibInstrumController, build_synthetic_snapshot

# spec §4.4：回應上限 1400 bytes，避免 IP 分片。扣掉 SNMP 訊息外層
# （version / community / PDU header / request-id / error status+index /
# 各層 SEQUENCE 標頭）的保留額度。
MAX_RESPONSE_BYTES = 1400
MESSAGE_OVERHEAD_RESERVE = 120
MAX_REPETITIONS_CAP = 25  # spec §4.4：伺服器端對 max-repetitions 設上限


class SliceableSnapshotController(SnapshotMibInstrumController):
    """在 §4.3 的 GET/GETNEXT 之外，多開一條「連續切片」路徑給 GETBULK。"""

    def read_next_slice(self, name, max_count: int, max_bytes: int, acFun, context):
        """從 name 之後取最多 max_count 筆，且累計編碼大小不超過 max_bytes。

        這是 §4.3 說的「GETBULK 退化為陣列切片」。一次 bisect 定位，
        之後純粹是陣列前進，沒有任何樹走訪。
        """
        snap = self.snapshot
        oids, values, sizes = snap.oids, snap.values, snap.sizes
        n = len(oids)
        i = bisect_right(oids, tuple(name))

        out = []
        used = 0
        while i < n and len(out) < max_count:
            val = values[i]
            oid = oids[i]
            if acFun is not None:
                if acFun("read", (v2c.ObjectIdentifier(oid), val), **context) is False:
                    i += 1
                    continue
            sz = sizes[i] if sizes else 64
            if out and used + sz > max_bytes:
                break  # 截斷：回應少於請求的 repetition 數，manager 端必定會處理
            used += sz
            out.append((v2c.ObjectIdentifier(oid), val))
            i += 1

        if not out:
            out.append((name, rfc1905.endOfMibView))
        return out


class BatchedBulkCommandResponder(cmdrsp.BulkCommandResponder):
    """取代 pysnmp 原生的 GETBULK 處理。

    原生實作（cmdrsp.py:436）是 `while M and R: rspVarBinds.extend(mgmtFun(...))`
    ——每個 repetition 都是一次獨立的 read_next_variables 呼叫，pysnmp 原始碼
    自己也留著 `TODO: manage all PDU var-binds in a single call`。

    對 snapshot + bisect 架構而言這是純浪費：M=25 就是 25 次 bisect，
    而正確答案是 1 次 bisect + 1 次切片。同時原生實作只有 varbind 筆數上限
    （max_varbinds=64），**沒有任何位元組上限**，所以 §4.4 的 1400 bytes
    截斷也只能在這裡實作。
    """

    def handle_management_operation(self, snmpEngine, stateReference, contextName, PDU):
        nonRepeaters = max(int(v2c.apiBulkPDU.get_non_repeaters(PDU)), 0)
        maxRepetitions = max(int(v2c.apiBulkPDU.get_max_repetitions(PDU)), 0)
        reqVarBinds = v2c.apiPDU.get_varbinds(PDU)

        N = min(nonRepeaters, len(reqVarBinds))
        R = max(len(reqVarBinds) - N, 0)
        M = min(maxRepetitions, MAX_REPETITIONS_CAP)

        instrum = self.snmpContext.get_mib_instrum(contextName)
        ctx = dict(snmpEngine=snmpEngine, acFun=self.verify_access, cbCtx=self.cbCtx)

        # 只有單一 repeater（bulkwalk 的常態）走快速路徑；多 repeater 需交錯
        # 輸出，語意較複雜且實務上罕見，交回原生實作。
        if N == 0 and R == 1 and isinstance(instrum, SliceableSnapshotController):
            rspVarBinds = instrum.read_next_slice(
                reqVarBinds[0][0],
                M,
                MAX_RESPONSE_BYTES - MESSAGE_OVERHEAD_RESERVE,
                self.verify_access,
                ctx,
            )
        else:
            rspVarBinds = list(instrum.read_next_variables(*reqVarBinds[:N], **ctx)) if N else []
            varBinds = reqVarBinds[-R:] if R else []
            budget = MAX_RESPONSE_BYTES - MESSAGE_OVERHEAD_RESERVE
            while M and R and budget > 0:
                got = instrum.read_next_variables(*varBinds, **ctx)
                rspVarBinds.extend(got)
                varBinds = rspVarBinds[-R:]
                M -= 1

        if rspVarBinds:
            self.send_varbinds(snmpEngine, stateReference, 0, 0, rspVarBinds)
            self.release_state_information(stateReference)


def build_agent(snapshot: Snapshot, host: str, port: int, community: str, stock_bulk: bool):
    snmpEngine = engine.SnmpEngine()
    config.add_transport(
        snmpEngine, udp.DOMAIN_NAME, udp.UdpTransport().open_server_mode((host, port))
    )
    config.add_v1_system(snmpEngine, "bench-area", community)
    config.add_vacm_user(snmpEngine, 2, "bench-area", "noAuthNoPriv", (1, 3, 6))

    instrum = SliceableSnapshotController(snapshot)
    snmpCtx = context.SnmpContext(snmpEngine)
    snmpCtx.context_names[b""] = instrum  # 整個換掉預設 context 的 MIB 層

    cmdrsp.GetCommandResponder(snmpEngine, snmpCtx)
    cmdrsp.NextCommandResponder(snmpEngine, snmpCtx)
    if stock_bulk:
        cmdrsp.BulkCommandResponder(snmpEngine, snmpCtx)
    else:
        BatchedBulkCommandResponder(snmpEngine, snmpCtx)
    return snmpEngine


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--varbinds", type=int, default=10000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=11161)
    ap.add_argument("--community", default="bench")
    ap.add_argument("--stock-bulk", action="store_true")
    ap.add_argument("--profile-seconds", type=float, default=0.0,
                    help="剖析請求路徑 N 秒後印出熱點並結束")
    args = ap.parse_args()

    t = time.perf_counter()
    snap = build_synthetic_snapshot(args.varbinds)
    build_ms = (time.perf_counter() - t) * 1000

    build_agent(snap, args.host, args.port, args.community, args.stock_bulk)
    mode = "stock" if args.stock_bulk else "batched"
    print(
        f"READY varbinds={len(snap)} build_ms={build_ms:.1f} "
        f"bulk={mode} listen={args.host}:{args.port}",
        flush=True,
    )
    await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
