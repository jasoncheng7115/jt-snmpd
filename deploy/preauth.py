"""Pre-parse gate  — the first line of defence, ahead of pysnmp.

**Why this is the highest-priority security item**

The agent runs continuously as LocalSystem, so any remote code execution is
immediately SYSTEM. And on UDP/161 **every byte reaches a pure-Python BER
decoder before authentication happens at all**. An attacker needs no community
string and no v3 account to make a LocalSystem process parse arbitrary input.

Known attack surface:
  - Deeply nested SEQUENCEs → pyasn1 recursion → RecursionError, in the worst
    case as an uncaught exception on the asyncio event loop
  - Oversized BER length fields → amplified memory allocation
  - OIDs with thousands of sub-identifiers → amplified CPU
  - SNMPv3 engineID discovery is unauthenticated, and the
    usmStatsUnknownUserNames report PDU can be used to enumerate valid accounts

**The order matters.** The original plan put the ACL in
`snmp.v2c.communities[].source`, which applies it *after* parsing — backwards.
Every check here runs before pysnmp sees a single byte:

    socket recv
      ↓
    1. source IP allow-list (not listed → dropped, nothing parsed)
      ↓
    2. packet size cap (> 4096 dropped; a normal request is under 300 bytes)
      ↓
    3. per-source token bucket
      ↓
    4. rough outer TLV sanity (first byte must be 0x30; declared length must
       match what actually arrived)
      ↓
    hand off to pysnmp

Step 3 has to come before USM cryptography: v3 makes denial of service cheaper,
because every packet costs an HMAC.
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field

# a normal request is under 300 bytes, so 4096 is already generous
MAX_PACKET_BYTES = 4096
# Packets per second allowed from a single source (token bucket).
#
# The sustained rate is what stops a flood; the burst is what a legitimate walk
# needs, and the two are sized from measurement rather than from a round number.
# A GETBULK walk sends one request per 25 varbinds as fast as the manager can
# turn them around: 34 requests in 0.32 s on a 766-varbind laptop, an
# instantaneous 106 pps. A 2,400-varbind server — 64 cores, 40 interfaces — is
# about 96 requests, and some sites poll every minute rather than every five,
# with discovery running alongside polling and sometimes a second manager.
#
# A burst of 100 therefore sat right on top of one ordinary walk. Two managers,
# or one large host, and packets were dropped: measured as walks that finished
# in 0.2 s taking 5 s on a retry, on v2c and v3 alike. That is the worst kind of
# fault to leave in, because it looks like a network problem.
#
# 300 covers three concurrent large walks. It does not weaken the control worth
# mentioning: the sustained rate is unchanged, and 300 packets is nothing to an
# attacker who is limited to 50 per second after them.
DEFAULT_RATE_PPS = 50
DEFAULT_BURST = 300


class DropReason:
    """Why a packet was dropped. Each maps to a jtAgent*Drops counter and an
    Event ID."""
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
    """The gate in front of pysnmp. Around a hundred lines, written here rather
    than pulled in.

    An empty `allowed_networks` means "not configured", and is treated as deny —
    see `_ip_allowed`. The installer is required to ask for the management
    networks; the default is deny, and Any/Any is never acceptable.
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
        """Turn ['192.0.2.0/24', '198.51.100.5'] into ip_network objects.

        A bare address with no prefix is treated as /32 (or /128 for IPv6).
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

        # Loopback is always allowed. The loopback self-test is the
        # only thing that detects "service reports Running but the event loop is
        # wedged", and the installer's health check  relies on
        # it too. With loopback behind the ACL, every site's installation fails
        # at the final step — which is exactly what happened once.
        #
        # Security: a local process is already on this machine, and the community
        # string or USM credentials still apply. Allowing loopback does not widen
        # the external attack surface.
        if addr.is_loopback:
            return True

        if not self.allowed_networks:
            # Not configured means deny, not allow. This used to return True —
            # "no list, no filtering" — which is fail-open. That was tolerable
            # only while the installer was the sole author of the config; now
            # that operators are expected to edit the file by hand, an emptied
            # list would quietly expose the agent to every source on the network.
            # Loopback is already allowed above, so the health check and local
            # diagnosis still work and the operator sees monitoring stop rather
            # than silently over-sharing.
            return False
        for net in self.allowed_networks:
            # An IPv4 address cannot fall inside an IPv6 network, so a version
            # mismatch is skipped outright
            if addr.version == net.version and addr in net:
                return True
        return False

    def _rate_ok(self, src_ip: str, now: float) -> bool:
        """Token bucket, one per source, so a single source cannot exhaust a
        shared allowance."""
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
        """Rough sanity check on the outer TLV.

        Deliberately not a full parse — parsing is the thing being avoided.
        Three checks only:
          1. the first byte must be 0x30 (SEQUENCE)
          2. the BER length field must itself be readable
          3. the declared length must match the payload exactly (trailing bytes
             count as malformed)
        """
        if len(data) < 2 or data[0] != 0x30:
            return False
        first = data[1]
        if first < 0x80:
            declared, header = first, 2
        else:
            n = first & 0x7F
            if n == 0 or n > 4 or len(data) < 2 + n:
                return False          # indefinite or over-long length: rejected
            declared = int.from_bytes(data[2:2 + n], "big")
            header = 2 + n
        # The declared length must be exactly the remaining bytes
        return declared == len(data) - header

    # ------------------------------------------------------------------- main
    def check(self, data: bytes, src_ip: str, now: float | None = None):
        """Return (allowed: bool, reason: str | None).

        The call order *is* the defence order and must not be rearranged: the
        address comparison is the cheapest and parses nothing, and rate limiting
        has to precede any cryptography.
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
        """Drop idle token buckets so spoofed source addresses cannot exhaust
        memory.

        This is an attack surface in its own right: without pruning, a flood from
        random source addresses grows the dict without bound. Returns how many
        were removed.
        """
        now = time.monotonic() if now is None else now
        stale = [ip for ip, b in self._buckets.items() if now - b.last > idle_seconds]
        for ip in stale:
            del self._buckets[ip]
        return len(stale)
