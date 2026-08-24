"""閘門 C 原型：spec.md §4.3 的 snapshot + bisect 架構。

刻意不使用 pysnmp 的 MibTableColumn / MibScalarInstance 物件模型（spec §10-22）。
整份 MIB 是一個「已依 OID 字典序排好的陣列」，GET 用 bisect_left、
GETNEXT 用 bisect_right。lexicographic ordering / 無重複 OID / 無 GETNEXT loop /
正確的 endOfMibView 因此成為結構保證，不需人工維護（spec §4.3 效益 2）。
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
    """spec §4.3 的不可變快照。整趟 walk 共用同一份，故 LibreNMS 不會
    遇到「walk 到一半 ifTable 列數改變」導致 port 重複或消失。"""

    oids: tuple[OidTuple, ...]  # 已排序，供 bisect 使用
    values: tuple[object, ...]  # 與 oids 等長且對位，已是 ASN.1 物件
    sizes: tuple[int, ...] = ()  # 每筆 varbind 的 BER 編碼位元組數，建立時預算好
    generation: int = 0
    built_at_monotonic: float = 0.0
    collector_health: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.oids)


class SnapshotMibInstrumController(AbstractMibInstrumController):
    """把 pysnmp 的 MIB 層整個換掉，只留 message / USM / VACM / transport。

    pysnmp 7.1.29 的 AbstractMibInstrumController 只有三個方法，
    write_variables 不覆寫即自動成為唯讀 agent（spec §2.12：v1.0 不支援 SET）。
    """

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot

    # --- GET ---------------------------------------------------------------
    def read_variables(self, *varBinds, **context):
        snap = self.snapshot  # 一次取用，避免走訪中途被換手
        acFun = context.get("acFun")
        out = []
        for vb in varBinds:
            name = vb[0]
            target: OidTuple = tuple(name)
            i = bisect_left(snap.oids, target)
            if i < len(snap.oids) and snap.oids[i] == target:
                val = snap.values[i]
                # VACM：spec §3.5 要求白名單過濾，GET 與 GETNEXT 都要生效
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
            # VACM 必須在「走訪路徑」上生效，不能只擋 GET（spec §3.5 實作陷阱 2）。
            # 被拒的項目要繼續往下找，不是回傳錯誤，否則 walk 會在此中斷。
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


# --- 合成資料 --------------------------------------------------------------

_BASE: OidTuple = (1, 3, 6, 1, 4, 1, 99999, 1)


def build_synthetic_snapshot(n_varbinds: int, seed: int = 1) -> Snapshot:
    """產生 n 個 varbind 的合成 table（spec §1.3 閘門 C 的效能實驗）。

    型別分布刻意混合，貼近真實 ifTable/ifXTable：整數、計數器、
    64-bit 計數器、字串、Gauge、TimeTicks。純 Integer 會低估 BER 編碼成本。
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

    # 一律重新排序：真實 collector 的輸出順序不保證，排序是 snapshot builder 的職責
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
    """tag(1) + BER 長度欄位 + 內容。長度 < 128 為短式單位元組。"""
    if content_len < 0x80:
        return 1 + 1 + content_len
    n = (content_len.bit_length() + 7) // 8
    return 1 + 1 + n + content_len


def _oid_content_len(oid: OidTuple) -> int:
    """OID 內容長度：前兩個 sub-id 併為 40*a+b，其後各自 base-128 變長編碼。"""
    if len(oid) < 2:
        return 1
    total = 0
    first = oid[0] * 40 + oid[1]
    for sub in (first, *oid[2:]):
        total += 1 if sub < 0x80 else (sub.bit_length() + 6) // 7
    return total


def _int_content_len(v: int) -> int:
    """整數的 BER 內容長度。正負號共用同一條公式。

    注意：這是對齊 **pyasn1 實際行為**，不是 DER 的最短編碼。pyasn1 在負數
    邊界會多送一個冗餘前導位元組（-128 編成 ff 80 而非 80，-2147483648 編成
    5 bytes 而非 4）。本函式的用途是預測 pyasn1 會吐出多少位元組，
    好在 §4.4 的 1400 bytes 上限前截斷，因此必須跟著 pyasn1 走。
    tests/ 需有 property test 對照真實編碼器，pyasn1 升版時才不會無聲漂掉。
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
    return None  # 未知型別 → 交給呼叫端退回實際編碼


def precompute_sizes(oids, values) -> tuple[int, ...]:
    """在 snapshot 建立時算好每筆 varbind 的 BER 大小。

    §4.4 要求回應超過 1400 bytes 即截斷。若在請求路徑上反覆試編碼再回退，
    成本很高；改為建立時付一次代價，請求路徑只剩累加與比較。

    這裡用解析式計算而非實際編碼：實測完整 BER 編碼是 115 µs/varbind，
    佔快照建立成本的 93%，會直接撞破 §4.2「快照重建 < 500 ms」的預算。
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
        sizes.append(_tlv_len(inner))  # 外層 SEQUENCE
    return tuple(sizes)
