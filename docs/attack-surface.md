---
layout: default
title: Security Assessment
description: What installing jt-snmpd adds to a Windows host, measured
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)

# Security assessment: what installing jt-snmpd adds to a Windows host

| Measurement | Value |
|---|---|
| Date | 2026-08-24 |
| Version | jt-snmpd 0.9.2 |
| Host under test | Dell Latitude E5270 / Windows 10 22H2 |
| Poller | LibreNMS 26.8.1 |

> Addresses in this document use the RFC 5737 documentation range
> (`192.0.2.0/24`). The measurements were taken on a real internal network; only
> the addresses were changed, never the numbers.

The failure a monitoring agent can least afford is **becoming the way in**. This
document measures what the installation opens, how far it can be pushed, and
which layer stops it. Every figure can be reproduced with the commands in §7.

---

## 1. New network exposure: UDP/161

Installing the agent opens one UDP listener. SNMP is **the textbook reflective
DDoS protocol**: an attacker spoofs a source address, sends a small request, and
the victim receives a large reply.

### Measured amplification

```
16:51:53.019679 192.0.2.10.58590 > 192.0.2.63.161: UDP, length 39
16:51:53.037594 192.0.2.63.161 > 192.0.2.10.58590: UDP, length 774
```

**39 → 774 bytes = 19.8×**, and that is with the attacker deliberately raising
GETBULK's `max-repetitions` to **1000**.

An agent with no cap would keep filling the reply until it hit the message size
limit. At this project's scale of roughly 6,000 OIDs that reply reaches tens of
kilobytes, putting amplification into the 1000× range. Two mechanisms hold it
down:

| Mechanism | Value | Effect |
|---|---|---|
| `MAXREP_CAP` | 25 | However many repetitions are requested, at most 25 are served |
| Response byte cap | 1400 | No fragmentation; the response is truncated instead |

**The theoretical ceiling is therefore 1400/39 ≈ 36×, and 19.8× was measured.**

### The reflection cannot actually leave the host

Amplification only matters if the reply can be aimed at a victim. To do that the
attacker has to spoof a source address that lies **inside the management
network**, and two layers stand in the way:

```
JT SNMP Agent (UDP 161): Allow proto=UDP port=161 from=192.0.2.0/255.255.255.0
JT SNMP Agent (ICMPv4):  Allow proto=ICMPv4         from=192.0.2.0/255.255.255.0
```

- **Windows Firewall**: the installer requires the management networks up front
  and denies everything else. Packets are dropped by the operating system before
  they reach our process at all.
- **The pre-parse gate** (`preauth.py`): source allow-list → packet size cap →
  per-source token bucket → outer TLV sanity check, **all of it ahead of
  pysnmp's BER decoder**.

The second layer exists because the first can be widened — customers do edit
their own firewall rules — and for defence in depth: the BER decoder is the
largest single piece of attack surface, so keeping unauthorised packets away
from it is worth doing twice.

---

## 2. Code execution risk: the service runs as LocalSystem

This is the item that most needs stating plainly. **A parser vulnerability here
is remote code execution as SYSTEM.**

### Why LocalSystem is unavoidable

Disk SMART needs IOCTLs such as `SMART_RCV_DRIVE_DATA`, and `\\.\PhysicalDriveN`
has to be opened with `GENERIC_READ | GENERIC_WRITE` — which requires
administrative rights. A virtual service account cannot do it.

### Mitigation: the token is stripped to the minimum

```
SERVICE_NAME: jt-snmpd
        PRIVILEGES : SeChangeNotifyPrivilege
                   : SeSystemProfilePrivilege
                   : SeIncreaseQuotaPrivilege
```

LocalSystem carries around 30 privileges by default; three are kept. **Among
those removed** are `SeDebugPrivilege` (read and write any process's memory),
`SeTcbPrivilege`, `SeImpersonatePrivilege` (token theft, the standard privilege
escalation route), `SeLoadDriverPrivilege` (load a kernel driver) and
`SeBackupPrivilege` / `SeRestorePrivilege` (read and write any file, bypassing
ACLs).

Even with the parser compromised, what the attacker holds is a SYSTEM context
that cannot debug another process, cannot impersonate another user, and cannot
load a driver.

### Mitigation: read-only, with no oracle

```
$ snmpset -v2c -c <community> 192.0.2.63 .1.3.6.1.2.1.1.6.0 s "PWNED"
Timeout: No Response from 192.0.2.63
$ snmpget -v2c -c <community> -Oqv 192.0.2.63 .1.3.6.1.2.1.1.6.0
"LAB"
```

SET is not answered with an error, it is **dropped**: no reply means no signal
an attacker can probe with. In the implementation this is achieved by never
overriding `write_variables()`, so read-only is not enforced by a check — **the
code path does not exist**.

### Mitigation: no kernel driver is introduced

CPU core temperature requires reading MSRs, which requires a kernel driver. The
industry's usual choice, WinRing0, is on Microsoft's vulnerable driver blocklist.
**Installing a driver that can read and write arbitrary MSRs and physical memory
across hundreds of government and hospital machines, in exchange for one
temperature reading, turns a monitoring tool into a privilege escalation
channel.** The project therefore does without CPU core temperature. It is one of
the project's hard rules: no kernel driver is introduced for the sake of any
single value.

### Mitigation: the parsers assume hostile input

Every parse whose **length or offset comes out of the buffer being parsed** is
treated as hostile input, and is separated from acquisition into a pure function
so it can be property-tested with malicious bytes
(`tests/test_sensors_parsing.py`, 34 cases).

ctypes is where Python's memory safety ends: undersize a buffer and the kernel
writes past it. One instance has already been fixed — the
`CallNtPowerInformation` call sized its buffer from `os.cpu_count()`, which
under-reports on machines with more than 64 logical processors and leads to the
kernel writing out of bounds. It now uses
`GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)`.

---

## 3. Denial of service against ourselves

A nonsense length field will not take Python out of bounds, but it will run a
loop four billion times — and under a hard requirement never to slow the host
down, that is a self-inflicted denial of service. Every parser carries explicit
ceilings (`MAX_INSTANCES`, `MAX_WMI_BUFFER`, `MAX_PROCESSORS`,
`MAX_NAME_CHARS`).

The load polling imposes has been measured on real hardware: at 7,000× the real
polling rate, a fixed benchmark workload degrades by **0.41%** (with the process
set to `BELOW_NORMAL_PRIORITY_CLASS`). A single full walk costs 12.5 ms of CPU;
at LibreNMS's one poll every five minutes that is roughly 0.004% CPU.

The per-source token bucket limits request rate, and `prune()` clears expired
entries periodically so that the source table cannot itself become a route to
memory exhaustion.

---

## 4. Information disclosure: what is deliberately withheld

The threat model  treats the primary adversary as **someone already
inside the network**. If a single unauthenticated read-only walk yields a
complete vulnerability assessment and an internal network map, the agent is an
asset to the attacker rather than to the operator. The following are therefore
**off by default** (3,175 OIDs in total):

| Subtree | Built-in SNMP | jt-snmpd | Why it is withheld |
|---|---:|---:|---|
| `hrSWInstalled` | 407 | 0 | Exact version of every package = a ready-made CVE list |
| `hrSWRun` / `hrSWRunPerf` | 1,792 | 0 | Which EDR is running and where = tailored evasion |
| `tcpConnTable` | 460 | 0 | The full connection list |
| `udpTable` | 68 | 0 | The service list |
| `ipNetToMedia` (ARP) | 448 | 0 | The internal ARP table = a target list for lateral movement |

Interface filtering reduces disclosure as a side effect: only physical adapters
are published, so VPN virtual adapters, WFP filter drivers and tunnel interfaces
never appear.

---

## 5. Risks not yet mitigated (stated honestly)

| Risk | Today | Plan |
|---|---|---|
| **Community string in clear text** | v2c has neither encryption nor authentication and can be sniffed | SNMPv3 (SHA-256 + AES-128, keys stored with DPAPI) is a v1.0 requirement |
| **Source addresses can be spoofed** | UDP is connectionless; the allow-list stops reflection but not blind sends | v3 authentication resolves it; for now rate limiting and read-only bound the impact |
| **The executable is unsigned** | No Authenticode certificate yet | A certificate through an open-source code-signing programme is planned. Until then integrity comes from the published SHA-256 — [Code signing](https://jasoncheng7115.github.io/jt-snmpd/code-signing.html) covers trusting it manually, WDAC hash rules, and signing with your own certificate |
| **pysnmp's BER decoder** | The pre-parse gate sits in front of it, but authorised sources still reach it | A small purpose-built parser (Phase 1) to shrink this surface |
| **LocalSystem** | Reduced to three privileges | It cannot go lower: the SMART IOCTLs require administrative rights |

---

## 6. Compared with the alternatives

| Option | UDP/161 | Runs as | Writes | Source control | Rate limit | Support status |
|---|---|---|---|---|---|---|
| **Built-in Windows SNMP** | Open | LocalSystem, privileges not stripped | **SET supported** | `PermittedManagers`, applied **after parsing** | None | Deprecated by Microsoft |
| **jt-snmpd** | Open | LocalSystem, 3 privileges | Read-only | Pre-parse gate, **ahead of the BER decoder** | Per-source token bucket | Maintained |
| No SNMP monitoring | Closed | — | — | — | — | No monitoring |

**Replacing the built-in service is a net reduction in attack surface**: no SET,
27 fewer privileges, 3,175 fewer disclosure OIDs, plus rate limiting and a
source check that runs before anything is parsed.

---

## 7. Reproducing these measurements

```bash
# Amplification
tcpdump -i any -n -q "udp port 161 and host <target>" &
snmpbulkget -v2c -c <community> -Cr1000 -Cn0 <target> .1.3.6.1.2.1

# Read-only
snmpset -v2c -c <community> <target> .1.3.6.1.2.1.1.6.0 s "TEST"   # should time out
snmpget -v2c -c <community> -Oqv <target> .1.3.6.1.2.1.1.6.0        # should be unchanged
```

```powershell
# Token privileges
sc qprivs jt-snmpd
# Firewall scope
Get-NetFirewallRule -DisplayName 'JT SNMP Agent*' | Get-NetFirewallAddressFilter
```

---

## Related documentation

- [Documentation home](https://jasoncheng7115.github.io/jt-snmpd/)
- [Compared with the built-in SNMP Service](https://jasoncheng7115.github.io/jt-snmpd/comparison-vs-builtin-snmp.html)
- [Security scanning toolchain](https://jasoncheng7115.github.io/jt-snmpd/security-scanning.html)
- [Release checklist](https://jasoncheng7115.github.io/jt-snmpd/release-checklist.html)
