"""前置解析閘門（spec §3.2）— 位於 pysnmp 之前的第一道防線。

**為什麼這是最高優先的資安項目**

agent 以 LocalSystem 常駐，任何 RCE 直接等同 SYSTEM。而 UDP/161 上
**每一個位元組都會先經過純 Python 的 BER decoder，才輪到認證**。
攻擊者不需要任何 community 或 v3 帳號，就能讓 LocalSystem 程序解析任意封包。

已知攻擊面：
  - 深度巢狀 SEQUENCE → pyasn1 遞迴 → RecursionError，最壞情況是
    asyncio 事件迴圈上的未捕捉例外
  - 超長 BER length 欄位 → 記憶體配置放大
  - 含數千個 sub-identifier 的 OID → CPU 放大
  - SNMPv3 的 engineID discovery 是未認證的，可藉 usmStatsUnknownUserNames
    的 report PDU 列舉有效帳號

**順序是關鍵。** spec §3.2 指出原計畫把 ACL 放在
`snmp.v2c.communities[].source`，代表 ACL 在**解析之後**才生效——順序錯誤。
本模組的每一道檢查都在 pysnmp 拿到位元組之前執行：

    socket recv
      ↓
    ① 來源 IP 白名單（不在名單直接 drop，零解析）
      ↓
    ② 封包大小上限（> 4096 直接丟；正常請求 < 300 bytes）
      ↓
    ③ 每來源 token bucket 速率限制
      ↓
    ④ 外層 TLV 粗略合法性（第一個 byte 必須 0x30；宣告長度需與實際相符）
      ↓
    交給 pysnmp

③ 必須在 USM 密碼學處理**之前**——v3 讓 DoS 更便宜，因為每個封包都要做 HMAC。
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field

# spec §3.2：正常請求 < 300 bytes，4096 已經非常寬鬆
MAX_PACKET_BYTES = 4096
# 每來源每秒允許的封包數（token bucket）
DEFAULT_RATE_PPS = 50
DEFAULT_BURST = 100


class DropReason:
    """丟棄原因。每一種都對應一個 jtAgent*Drops 計數器與一個 Event ID。"""
    ACL = "acl"                  # Event 2001
    OVERSIZE = "oversize"        # Event 2002
    RATE_LIMIT = "rate_limit"    # Event 2003
    MALFORMED = "malformed"      # Event 2002


@dataclass
class _Bucket:
    tokens: float
    last: float


@dataclass
class PreAuthGate:
    """pysnmp 之前的閘門。約 100 行，自行撰寫（spec §3.2）。

    allowed_networks 為空代表**不做 IP 過濾**——這只應該在明確設定下發生。
    spec §3.3 要求安裝時必須輸入管理網段，預設 deny，不允許 Any/Any。
    """

    allowed_networks: tuple = ()
    rate_pps: float = DEFAULT_RATE_PPS
    burst: float = DEFAULT_BURST
    max_bytes: int = MAX_PACKET_BYTES

    _buckets: dict = field(default_factory=dict)
    counters: dict = field(default_factory=lambda: {
        DropReason.ACL: 0, DropReason.OVERSIZE: 0,
        DropReason.RATE_LIMIT: 0, DropReason.MALFORMED: 0, "passed": 0,
    })

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def parse_networks(specs) -> tuple:
        """把 ['192.168.1.0/24', '10.0.0.5'] 解析成 ip_network 物件。

        單一 IP 不帶遮罩時視為 /32（或 IPv6 的 /128）。
        """
        out = []
        for spec in specs or ():
            s = str(spec).strip()
            if not s:
                continue
            out.append(ipaddress.ip_network(s, strict=False))
        return tuple(out)

    def _ip_allowed(self, src_ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(src_ip)
        except ValueError:
            return False

        # loopback 永遠放行。spec §6.5 的 loopback 自我測試是唯一能偵測
        # 「服務 Running 但事件迴圈卡死」的機制，安裝程式的健康檢查
        # （spec §5.7 第 7 步）也靠它。若被 ACL 擋住，每個站台的安裝
        # 都會在最後一步失敗——實測踩過。
        #
        # 安全性：本機行程本來就在這台機器上，且 community/USM 認證照常適用；
        # 放行 loopback 不會擴大外部攻擊面。
        if addr.is_loopback:
            return True

        if not self.allowed_networks:
            return True          # 未設定 = 不過濾（安裝程式必須避免這個狀態）
        for net in self.allowed_networks:
            # IPv4 位址不可能落在 IPv6 網段，version 不同直接跳過
            if addr.version == net.version and addr in net:
                return True
        return False

    def _rate_ok(self, src_ip: str, now: float) -> bool:
        """Token bucket。每來源獨立，避免單一來源耗盡全域配額。"""
        b = self._buckets.get(src_ip)
        if b is None:
            self._buckets[src_ip] = _Bucket(tokens=self.burst - 1, last=now)
            return True
        elapsed = now - b.last
        if elapsed > 0:
            b.tokens = min(self.burst, b.tokens + elapsed * self.rate_pps)
            b.last = now
        if b.tokens >= 1:
            b.tokens -= 1
            return True
        return False

    @staticmethod
    def _tlv_sane(data: bytes) -> bool:
        """外層 TLV 粗略合法性檢查。

        不做完整解析——那正是我們要避免的。只驗三件事：
          1. 第一個 byte 必須是 0x30（SEQUENCE）
          2. BER 長度欄位本身要能讀完
          3. 宣告的長度要與實際 payload 相符（允許尾端有多餘位元組時視為畸形）
        """
        if len(data) < 2 or data[0] != 0x30:
            return False
        first = data[1]
        if first < 0x80:
            declared, header = first, 2
        else:
            n = first & 0x7F
            if n == 0 or n > 4 or len(data) < 2 + n:
                return False          # 不定長度或過長的長度欄位一律拒絕
            declared = int.from_bytes(data[2:2 + n], "big")
            header = 2 + n
        # 宣告長度必須正好等於剩餘位元組
        return declared == len(data) - header

    # ------------------------------------------------------------------- main
    def check(self, data: bytes, src_ip: str, now: float | None = None):
        """回傳 (allowed: bool, reason: str | None)。

        呼叫順序即防禦順序，不可調換：IP 比對最便宜且零解析，速率限制必須
        在任何密碼學處理之前。
        """
        now = time.monotonic() if now is None else now

        if not self._ip_allowed(src_ip):
            self.counters[DropReason.ACL] += 1
            return False, DropReason.ACL

        if len(data) > self.max_bytes:
            self.counters[DropReason.OVERSIZE] += 1
            return False, DropReason.OVERSIZE

        if not self._rate_ok(src_ip, now):
            self.counters[DropReason.RATE_LIMIT] += 1
            return False, DropReason.RATE_LIMIT

        if not self._tlv_sane(data):
            self.counters[DropReason.MALFORMED] += 1
            return False, DropReason.MALFORMED

        self.counters["passed"] += 1
        return True, None

    def prune(self, now: float | None = None, idle_seconds: float = 300.0) -> int:
        """清掉閒置的 token bucket，避免被大量偽造來源 IP 撐爆記憶體。

        這本身就是一個攻擊面：不清理的話，攻擊者用隨機來源 IP 洗一輪
        就能讓 dict 無限成長。回傳清掉的數量。
        """
        now = time.monotonic() if now is None else now
        stale = [ip for ip, b in self._buckets.items() if now - b.last > idle_seconds]
        for ip in stale:
            del self._buckets[ip]
        return len(stale)
