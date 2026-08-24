---
layout: default
title: Full Comparison
description: Compared with the built-in SNMP Service, table by table
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/comparison-vs-builtin-snmp_zh-TW.html)

# jt-snmpd compared with the Windows built-in SNMP Service

| Measurement | Value |
|---|---|
| Date | 2026-08-24 |
| Poller | LibreNMS 26.8.1, with no LibreNMS-side modifications |
| Control | Windows 10 22H2, **Windows built-in SNMP Service** |
| Subject | Windows 10 22H2, **jt-snmpd** |

> Addresses use the RFC 5737 documentation range (`192.0.2.0/24`). Only the
> addresses and serial numbers were changed; the counts are as measured.

---

## What LibreNMS shows

This is the difference an operator actually sees.

| LibreNMS table | Built-in SNMP | **jt-snmpd** | Notes |
|---|---:|---:|---|
| **entPhysical** (Inventory) | **0** | **5** | The built-in service has no inventory at all. We parse chassis, mainboard, CPU, DIMMs and disks out of SMBIOS |
| **ucd_diskio** (Disk I/O) | **0** | **2** | No disk I/O in the built-in service. We serve it from `IOCTL_DISK_PERFORMANCE` |
| **sensors** (Temperature) | **0** | **2** | No sensors at all in the built-in service. We provide disk temperature and ACPI thermal zones (33 °C / 25 °C measured on real hardware) |
| **applications** (SMART) | **0** | **1** | Not available in the built-in service. We serve LibreNMS's `smart` application through NET-SNMP-EXTEND-MIB (requires `discovery_modules.applications`) |
| **mempools** (Memory) | 2 | **4** | The built-in service has physical and virtual only. We add cached and swap |
| storage (Disk Usage) | 2 | 2 | The same, except our descriptions carry the real volume labels, including non-ASCII ones |
| processors | 8 | 6 | Each machine's actual core count; no difference in behaviour |
| ipv4_addresses | 2 | 2 | The same |
| **ports** | 9 | **1** | **Deliberately different**, see below |
| **hrDevice** | 68 | 9 | **Deliberately different**, see below |

## Why ports and hrDevice are so much smaller

The built-in service publishes every NDIS filter driver as a separate interface.
The measured `ports` list on the control machine:

```
ethernet_32777    ethernet_32770    ppp_32768 (down)
ethernet_8   ethernet_9   ethernet_11   ethernet_12   ethernet_13   ethernet_15
```

Every one is auto-named and says nothing about what it is, and `ppp_32768` is a
WAN Miniport that is down. Of the 68 `hrDevice` rows, **51 are
`hrDeviceNetwork`** — again WFP filter drivers, the QoS scheduler and tunnel
interfaces.

On a Hyper-V host that number grows to somewhere between 40 and 80 interfaces.
Each one becomes a port and a set of RRDs in LibreNMS, and virtual interfaces
come and go — when one goes, its RRDs are left with nothing pointing at them.

jt-snmpd publishes only interfaces where `HardwareInterface = TRUE` and
`FilterInterface` is not set, and excludes loopback and NIC team members. On the
same machine it picked exactly one physical adapter out of eleven interfaces,
correctly excluding three WFP filter drivers, two VPN virtual adapters (PANGP and
F5), Kernel Debug, Loopback, and Teredo / IP-HTTPS / 6to4.

**This is a design decision, not an omission.** Set `interface_filter.mode` to
`all` when the complete list is what you want.

## Total OIDs: 6,457 against 575

The gap is concentrated in a handful of tables. Each is accounted for below.

### Deliberately withheld (information disclosure, spec §3.5)

| Subtree | Built-in | jt-snmpd | Why it is withheld |
|---|---:|---:|---|
| `hrSWInstalled` | 407 | 0 | Exact version of every package = a ready-made CVE list |
| `hrSWRun` | 1,394 | 0 | Which EDR is running and where = tailored evasion |
| `hrSWRunPerf` | 398 | 0 | As above |
| `tcpConnTable` | 460 | 0 | The full connection list |
| `udpTable` | 68 | 0 | The service list |
| `ipNetToMedia` (ARP) | 448 | 0 | The internal ARP table = a target list for lateral movement |

That is **3,175 OIDs**, the large majority of the difference.

All of these are **implemented or implementable**; they are off by default. The
threat model (spec §3.1) treats the primary adversary as someone already inside
the network: a single unauthenticated read-only walk would otherwise yield a
complete vulnerability assessment and an internal network map, from a process
running as LocalSystem. The ARP table is already implemented and can be switched
on in the configuration.

### Since filled in (genuinely missing at first)

The first comparison found three tables we really did not have. They were added
in 0.1.2:

| Subtree | Built-in | jt-snmpd 0.1.2 | Source |
|---|---:|---:|---|
| `hrFSTable` | 27 | **18** | `GetVolumeInformationW`, mount points `C:` and `D:`, type NTFS |
| `hrPartitionTable` | 20 | **10** | As above |
| `ipRouteTable` | 130 | **42** | `GetIpForwardTable2`, including the default gateway and directly connected networks |

The counts are lower than the built-in service because it lists routes for every
virtual interface, while we list only routes that map to a physical interface.

### Only in jt-snmpd

| Subtree | Built-in | jt-snmpd | Contents |
|---|---:|---:|---|
| `entPhysicalTable` | 0 | 80 | Parsed from SMBIOS: chassis, mainboard, CPU, DIMMs (with part numbers and speeds), disks |
| `entPhySensorTable` | 0 | 24 | Disk temperature, ACPI thermal zones, CPU frequency |
| `diskIOEntry` (UCD) | 0 | 20 | Bytes and operations read and written, including the 64-bit forms |
| JT self-health OIDs | 0 | 65 | Version, RSS, snapshot age, per-collector health table |
| `ipAddressTable` (IPv6) | 0 | 24 | The built-in service has IPv4's `ipAddrTable` only |

### Why SMART is not in entPhySensorTable

The first version published NVMe endurance and available spare as
`entPhySensorType = other(1)`, and none of it appeared in LibreNMS. The lookup
table in `includes/discovery/sensors/entity-sensor.inc.php` recognises nine
types:

    voltsDC voltsAC amperes watts hertz percentRH rpm celsius dBm

`other` is not among them, so the whole row is discarded without a message. The
agent was working, `snmpwalk` returned the values, and LibreNMS simply did not
take them — the kind of gap where neither side is wrong and nothing reports an
error is the hardest to find.

Counter-style SMART metrics therefore go through **NET-SNMP-EXTEND-MIB**, which
is the route LibreNMS actually reads SMART from, and it is entirely over SNMP:
beyond jt-snmpd itself the monitored host needs neither the LibreNMS agent nor
smartctl.

One upstream LibreNMS defect is worth recording while we are here:
`entity-sensor.inc.php:47` maps `hertz` to the class `freq`, but the valid class
defined in `LibreNMS/Enum/Sensor.php:24` is `frequency`. Every sensor reported as
`hertz` is therefore discarded. The same pattern appears in
`cisco-entity-sensor.inc.php:56` and `openbsd.inc.php:28`, so the impact reaches
well beyond this project.

## Inventory as measured on real hardware

The Inventory page for a Dell Latitude E5270 running Windows 10 22H2, entirely
from SMBIOS parsing — **the built-in service provides none of it**:

```
Latitude E5270 (DESKTOP-9PNNQ34)        Serial ****
└── 0DV5YH (Mainboard)                  Dell Inc.
    ├── Intel(R) Core(TM) i5-6300U @ 2.40GHz (U3E1)   2 cores, 2400 MHz
    └── HMA82GS6AFR8N-UH (DIMM A)        16384 MB 2133 MT/s   Serial ****
└── SAMSUNG SSD PM871b M.2 2280 256GB    238 GB (RAID)   Serial ****
    └── PhysicalDrive0 Temp              34 °C
```

> Serial numbers are replaced with `****` in this document. The agent itself
> **does** report the real ones — when someone has to decide which disk or which
> memory module to replace in the field, the serial is what makes it findable,
> and that data stays inside the customer's own monitoring system. It is masked
> here only because this document is public.

## Behavioural differences

| Aspect | Built-in SNMP | jt-snmpd |
|---|---|---|
| Support status | Deprecated by Microsoft | Maintained |
| SNMP version | v1 / v2c / v3 | v2c (v3 in development) |
| Writes | SET supported | **Read-only** |
| Traps | Supported | Not supported (out of scope for v1.0) |
| Source access control | `PermittedManagers`, applied after parsing | Pre-parse gate, **ahead of** the BER decoder |
| Rate limiting | None | Per-source token bucket |
| Response size control | None | Capped at 1400 bytes, never fragmented |
| Interface filtering | None; everything is published | Physical adapters only |
| ifIndex stability | Windows' native index, **not guaranteed stable across reboots** | Kept against the NET_LUID |
| Self-health monitoring | None | A private OID subtree |
| Deployment | A Windows capability (DISM / Add-WindowsCapability) | MSI (GPO / Intune / SCCM) |

The `ifIndex` row deserves particular attention. Replacing a driver, removing
and reinserting an adapter, or rebuilding a vSwitch can all make Windows
renumber, and LibreNMS matches ports by ifIndex. When the number changes, the old
port is marked deleted and a new one is created, and the historical RRDs are left
with nothing pointing at them. jt-snmpd keys the assignment on the NET_LUID and
keeps it: an interface gets an ifIndex the first time it is seen and never
changes afterwards.

## Migration behaviour

At install time these are carried over from the built-in service's registry:

| Source | Target | Handling |
|---|---|---|
| `ValidCommunities` right 4 (read-only) | community | Imported as-is |
| `ValidCommunities` rights 8 / 16 (writable) | community | **Downgraded to read-only** with a warning |
| `ValidCommunities` rights 1 / 2 | — | Not imported (meaningless for a read-only agent) |
| `PermittedManagers` | Source ACL and firewall scope | Host names are resolved to addresses; failures are listed as warnings |
| `PermittedManagers` empty | — | **Installation stops.** Never migrated as Any/Any |
| `RFC1156Agent\sysContact` | `sysContact` | Carried over as-is |
| `RFC1156Agent\sysLocation` | `sysLocation` | Carried over as-is |
| `RFC1156Agent\sysServices` | — | Not imported (fixed at 76); a differing value is noted in the report |
| `TrapConfiguration` | — | Not imported. **Listed in full, with a warning that traps will stop** |
| `ExtensionAgents` | — | Not imported. Names are listed, with a warning that their OIDs will no longer be available |

When the built-in service is disabled, its original start type and state are
recorded and restored on uninstall.

## Reproducing this comparison

```bash
# On the LibreNMS host
for oid in 1.3.6.1.2.1.25.4 1.3.6.1.2.1.25.6 1.3.6.1.2.1.47.1.1.1.1 \
           1.3.6.1.2.1.4.21 1.3.6.1.2.1.25.3.8; do
  echo -n "$oid  "
  echo -n "built-in=$(snmpbulkwalk -v2c -c COMMUNITY -On -Cr20 <builtin-host> $oid 2>/dev/null | wc -l)  "
  echo    "jt-snmpd=$(snmpbulkwalk -v2c -c COMMUNITY -On -Cr20 <jt-host> $oid 2>/dev/null | wc -l)"
done
```

Row counts on the LibreNMS side:

```sql
SELECT 'ports', COUNT(*) FROM ports WHERE device_id=? AND deleted=0
UNION ALL SELECT 'entPhysical', COUNT(*) FROM entPhysical WHERE device_id=?
UNION ALL SELECT 'sensors', COUNT(*) FROM sensors WHERE device_id=?
UNION ALL SELECT 'mempools', COUNT(*) FROM mempools WHERE device_id=?
UNION ALL SELECT 'ucd_diskio', COUNT(*) FROM ucd_diskio WHERE device_id=?;
```

---

## Related documentation

- [Documentation home](https://jasoncheng7115.github.io/jt-snmpd/)
- [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
- [Code signing](https://jasoncheng7115.github.io/jt-snmpd/code-signing.html)
- [Release checklist](https://jasoncheng7115.github.io/jt-snmpd/release-checklist.html)
