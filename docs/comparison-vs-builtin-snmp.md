---
layout: default
title: Full Comparison
description: Compared with the built-in SNMP Service, table by table
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/comparison-vs-builtin-snmp_zh-TW.html)

# jt-snmpd compared with the Windows built-in SNMP Service

> **Why not rebuild Net-SNMP for Windows instead?** That option was
> evaluated. The conclusion and the reasoning, including Net-SNMP's own
> published failure history, are in
> [ADR-0001](https://jasoncheng7115.github.io/jt-snmpd/adr/0001-why-not-net-snmp.html).

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
| **System graphs** | 3 | **8** | The built-in service gives Processes, Users and Uptime only, because those three come from HOST-RESOURCES. The other five come from UCD-SNMP-MIB on Linux, and the built-in Windows service does not implement that MIB |
| storage (Disk Usage) | 2 | 2 | Same row count, different descriptions: we read the real volume label and serial number, including non-ASCII labels |
| processors | 8 | 6 | Each machine's actual core count; no difference in behaviour |
| ipv4_addresses | 2 | 2 | The same |
| **ports** | 9 | **1** | **Deliberately different**, see below |
| **hrDevice** | 68 | 9 | **Deliberately different**, see below |

## System graphs: Windows only ever had three

LibreNMS's System graph group shows eight graphs for a Linux device and three for
Windows. That is not weak Windows support in LibreNMS: the other five are drawn
from **UCD-SNMP-MIB's `systemStats`**, a net-snmp enterprise MIB that the built-in
Windows SNMP Service does not implement.

| Graph | Source | Built-in SNMP | jt-snmpd |
|---|---|---|---|
| Processes | HOST-RESOURCES `hrSystemProcesses` | ✅ | ✅ |
| Users | HOST-RESOURCES `hrSystemNumUsers` | ✅ | ✅ |
| Uptime | `sysUpTime` | ✅ | ✅ |
| Detailed Processor Usage | UCD `ssCpuRawUser/Nice/System/Idle` | ❌ | ✅ |
| Context Switches | UCD `ssRawContexts` | ❌ | ✅ |
| Interrupts | UCD `ssRawInterrupts` | ❌ | ✅ |
| I/O | UCD `ssIORawSent` / `ssIORawReceived` | ❌ | ✅ |
| Swap I/O | UCD `ssRawSwapIn` / `ssRawSwapOut` | ❌ | ✅ |

The data comes from `NtQuerySystemInformation`
(`SystemPerformanceInformation` plus per-CPU times); no WMI and no subprocess.

Fields that cannot be measured on Windows (`ssCpuRawWait`, `ssCpuRawSteal`,
`ssCpuRawSoftIRQ`, `ssCpuRawGuest`) are **not emitted** rather than filled with
zero. A zero would make LibreNMS create the graph and draw a flat line at zero,
which reads as "measured, and it was zero" when the truth is "not measurable at
all".

`ssCpuRawNice` is the exception: Windows has no nice, but zero is emitted,
because "there is never any nice time on Windows" is a true statement, and
LibreNMS's ucd-mib poller requires user, nice, system and idle to be **all four
present** before it creates the Detailed Processor Usage graph. Omit one and the
whole graph never appears.

## Volume label encoding

`hrStorageDescr` carries the volume label, and in the field those labels are
frequently non-ASCII. This is a place that genuinely breaks: pysnmp's
`rfc1902.OctetString(str)` raises `PyAsn1UnicodeEncodeError` on non-ASCII input,
the snapshot fails to build, and the agent presents as "started, but no data".

The fix is that every OCTET STRING is encoded to UTF-8 bytes before it reaches
pysnmp, rather than letting pysnmp guess an encoding. The rule covers volume
labels, interface descriptions, `sysContact` and `sysLocation`, and the strings
parsed out of SMBIOS.

Measured result: a volume labelled 乙太網路 renders correctly on the LibreNMS Disk
Usage page, with no mojibake and no question marks. This is verified end to end
rather than only in an encoding unit test.

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

## Total OIDs: 7,582 against 767

The gap is concentrated in a handful of tables. Each is accounted for below.

### Deliberately withheld (information disclosure)

| Subtree | Built-in | jt-snmpd | Why it is withheld |
|---|---:|---:|---|
| `hrSWInstalled` | 660 | 0 | Exact version of every package = a ready-made CVE list |
| `hrSWRun` | 1,449 | 0 | Which EDR is running and where = tailored evasion |
| `hrSWRunPerf` | 414 | 0 | As above |
| `tcpConnTable` | 1,230 | 0 | The full connection list |
| `udpTable` | 50 | 0 | The service list |
| `ipNetToMedia` (ARP) | 196 | 0 | The internal ARP table = a target list for lateral movement |

That is **3,999 OIDs**, well over half of the difference. These counts were taken on the test-bed machine and move with it: how much software is installed, how many processes are running and how many connections are open all feed straight into the built-in service's total.

All of these are **implemented or implementable**; they are off by default. The
threat model  treats the primary adversary as someone already inside
the network: a single unauthenticated read-only walk would otherwise yield a
complete vulnerability assessment and an internal network map, from a process
running as LocalSystem.

### What the withheld OIDs would actually buy you

"Deliberately withheld" carries an implication that publishing them would be
useful. Checked against the LibreNMS 26.8.1 source, three of the four categories
have **no consumer in LibreNMS at all**: publishing them produces no page, no
graph and no table row.

| Subtree | OIDs | Consumer in LibreNMS | What publishing it gets you |
|---|---:|---|---|
| `hrSWInstalled` | 407 | Only `LibreNMS/OS/Junos.php`, which reads two specific instances to parse a JUNOS version string | No software inventory page exists. On Windows, nothing |
| `hrSWRun` / `hrSWRunPerf` | 1,792 | Only `LibreNMS/OS/Edgeos.php` and `Edgeosolt.php` | LibreNMS has no processes module. Nothing |
| `tcpConnTable` / `udpTable` | 528 | **None.** Zero references in the entire source tree | Nothing |
| `ipNetToMedia` / `ipNetToPhysical` | 448 | **Yes**: `LibreNMS/Modules/ArpTable.php` walks both and stores them in `ipv4_mac` | ARP search, FDB search, and per-port neighbour data |

Put plainly: 2,727 of those OIDs would disclose a vulnerability list and a
connection state table in exchange for **no LibreNMS functionality whatsoever**.
That is not a security-versus-features trade-off; there is simply no reason to
publish them.

ARP is the one that genuinely is useful, and it is **already implemented** and
off by default. To turn it on, edit `C:\ProgramData\jt-snmpd\config.json`:

```json
{
  "enable_arp_table": true
}
```

Save it, restart the service (`Restart-Service jt-snmpd`), and it appears on the
next discovery.

Weigh it before enabling: the ARP table is a list of this host's neighbours, and
to an attacker already inside the network that is a target list for lateral
movement. On a Windows endpoint it is usually a handful of same-subnet entries,
and its value to LibreNMS is mostly MAC-to-IP resolution, which pays off far
better on routers and switches.

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
is the route LibreNMS actually reads SMART from, and it is entirely over SNMP.
On other platforms that route is fed by a helper script on the host that shells
out to smartmontools; here the agent reads the attributes itself through
`IOCTL_STORAGE_QUERY_PROPERTY`, so jt-snmpd is the only thing installed.

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
| Development status | Deprecated by Microsoft, no longer in active development | Maintained |
| SNMP version | v1 / v2c / v3 | v2c (v3 in development) |
| Writes | SET supported | **Read-only** |
| Traps | Supported | Not supported (out of scope for v1.0) |
| Source access control | `PermittedManagers`, applied after parsing | Pre-parse gate, **ahead of** the BER decoder |
| Rate limiting | None | Per-source token bucket |
| Response size control | None | Capped at 1400 bytes, never fragmented |
| Interface filtering | None; everything is published | Physical adapters only |
| ifIndex stability | Windows' native index, **not guaranteed stable across reboots** | Kept against the NET_LUID |
| `sysUpTime` | Time since the **SNMP service** started | Time since the **machine** booted |
| Restarting the agent | Reads to LibreNMS as a reboot (see below) | No effect on reported uptime |
| Self-health monitoring | None | A private OID subtree |
| Deployment | A Windows capability (DISM / Add-WindowsCapability) | MSI (GPO / Intune / SCCM) |

> **Deprecated is not the same as unsupported.** By Microsoft's own
> definition, deprecation means a feature is no longer in active development
> and might be removed in a future release; a deprecated component still
> ships, **is supported for production deployments**, and continues to
> receive security and quality updates for the product lifecycle. Replacing
> it is therefore planning, not an emergency. The reason to replace it is how
> little it reports, which is the rest of this document, not a support cliff.

The `ifIndex` row deserves particular attention. Replacing a driver, removing
and reinserting an adapter, or rebuilding a vSwitch can all make Windows
renumber, and LibreNMS matches ports by ifIndex. When the number changes, the old
port is marked deleted and a new one is created, and the historical RRDs are left
with nothing pointing at them. jt-snmpd keys the assignment on the NET_LUID and
keeps it: an interface gets an ifIndex the first time it is seen and never
changes afterwards.

### Restarting the built-in service reads as a reboot

Measured on one machine, minutes apart, under each agent in turn:

| OID | Built-in SNMP Service | jt-snmpd |
|---|---|---|
| `sysUpTime.0` | **19 seconds** | 179 days |
| `hrSystemUptime.0` | 179 days | 179 days |
| `snmpEngineTime.0` | not served | 179 days |

The built-in service follows RFC 3418 literally: `sysUpTime` counts from the
last re-initialisation of the network management portion, which is the SNMP
service itself. jt-snmpd reports the host's uptime, from `GetTickCount64`.

LibreNMS takes the largest of `sysUpTime`, `snmpEngineTime` and
`hrSystemUptime`, but `windows.yaml` sets `bad_hrSystemUptime: true` and the
built-in service serves no `snmpEngineTime`. So for the built-in service that
maximum is 19 seconds, it is lower than the uptime recorded at the previous
poll, and LibreNMS raises **Device rebooted** for a machine that has been up for
half a year. Any restart of the SNMP service does it — a Windows Update, a
service recovery, an uninstall.

jt-snmpd avoids this twice over: its `sysUpTime` does not restart when the
service does, and `snmpEngineTime` gives LibreNMS a second stable source. That
matters beyond false alerts, because `sysUpTime` is a TimeTicks counter and
wraps at 497 days by definition of the type; `snmpEngineTime` is in seconds and
does not.


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
