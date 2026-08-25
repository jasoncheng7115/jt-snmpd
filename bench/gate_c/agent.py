"""Gate C test agent: pysnmp's MIB layer replaced with snapshot + bisect.

Usage:
    python -m bench.gate_c.agent --varbinds 10000 --port 11161 [--stock-bulk]

--stock-bulk uses pysnmp's own BulkCommandResponder, which makes one
read_next_variables call per repetition, so the batched version can be measured
against it.
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

# Responses are capped at 1400 bytes to avoid IP fragmentation. The allowance
# （version / community / PDU header / request-id / error status+index /
# below subtracts the SNMP message envelope and the nested SEQUENCE headers.
MAX_RESPONSE_BYTES = 1400
MESSAGE_OVERHEAD_RESERVE = 120
MAX_REPETITIONS_CAP = 25  # the server's own ceiling on max-repetitions


class SliceableSnapshotController(SnapshotMibInstrumController):
    """A contiguous-slice path for GETBULK, alongside GET and GETNEXT."""

    def read_next_slice(self, name, max_count: int, max_bytes: int, acFun, context):
        """Take up to max_count entries after `name`, stopping before max_bytes.

        This is what "GETBULK degenerates to an array slice" means: one bisect to
        find the start, then walking the array. No tree traversal at all.
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
                break  # truncated: fewer repetitions than asked for, which every manager handles
            used += sz
            out.append((v2c.ObjectIdentifier(oid), val))
            i += 1

        if not out:
            out.append((name, rfc1905.endOfMibView))
        return out


class BatchedBulkCommandResponder(cmdrsp.BulkCommandResponder):
    """Replace pysnmp's own GETBULK handling.

    The stock implementation (cmdrsp.py:436) is
    `while M and R: rspVarBinds.extend(mgmtFun(...))`: one independent
    read_next_variables call per repetition. pysnmp's own source carries a
    `TODO: manage all PDU var-binds in a single call` beside it.

    Against snapshot + bisect that is pure waste: M=25 becomes 25 bisects where
    the answer is one bisect and one slice. The stock version also caps only the
    number of varbinds (max_varbinds=64) and has **no byte cap at all**, so the
    1400-byte truncation has to live here too.
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

        # Only the single-repeater case takes the fast path, which is what
        # bulkwalk does. Several repeaters have to interleave their output, which
        # is more involved and rare in practice, so that falls back to the stock
        # implementation.
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
    snmpCtx.context_names[b""] = instrum  # replace the default context's MIB layer wholesale

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
                    help="profile the request path for N seconds, print the hot spots and exit")
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
