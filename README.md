# jt-snmpd v0.9.0

[![License](https://img.shields.io/badge/License-GPL--3.0--or--later-blue)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011%20%2F%20Server%202016%2B-0078D6?logo=windows&logoColor=white)
![SNMP](https://img.shields.io/badge/SNMP-v2c-orange)
![LibreNMS](https://img.shields.io/badge/LibreNMS-ready-1e88e5)
![Read only](https://img.shields.io/badge/agent-read--only-success)
![No outbound](https://img.shields.io/badge/outbound%20connections-none-success)

> A read-only SNMP agent for Windows that serves host monitoring data over standard
> MIBs, built to replace the deprecated built-in Microsoft SNMP Service and to feed
> LibreNMS without patching LibreNMS.
>
> By Jason Cheng (Jason Tools) · License: GPL-3.0-or-later · 繁體中文: [README_zh-TW.md](README_zh-TW.md)

---

## Why jt-snmpd?

Microsoft deprecated the built-in SNMP Service, and Net-SNMP has no current
official Windows build. That leaves Windows hosts either unmonitored or monitored
through an agent that pushes to a time-series database — which does not help when
the NMS speaks SNMP.

jt-snmpd fills that gap with a few deliberate constraints, all driven by the target
environment (government agencies and hospitals: no outbound internet, Defender /
HVCI / WDAC commonly enforced, tens to hundreds of hosts deployed by GPO):

- **Read-only.** No SNMP SET, no traps, no writes of any kind.
- **No outbound connections, ever.** No update checks, no telemetry, no code fetched
  at runtime. The installer is fully self-contained.
- **No kernel drivers.** Disk temperature comes from native Windows IOCTLs, not from
  LibreHardwareMonitor — its WinRing0 driver is on the Microsoft vulnerable driver
  blocklist and triggers Defender on HVCI endpoints.
- **No WMI, no PowerShell subprocesses** in the data path. Collectors call Win32 APIs
  directly through ctypes.
- **Migrates from the built-in service.** Community strings, permitted managers,
  sysContact and sysLocation are picked up from the existing SNMP Service registry,
  so switching over does not mean re-entering configuration on every host.
- **Designed not to slow the host down.** Measured, not asserted — see
  [Host impact](#host-impact).

## What LibreNMS sees

Everything below is verified against a production LibreNMS 26.8.1 instance with no
LibreNMS-side patches.

| LibreNMS page | Source | Status |
|---|---|---|
| OS detection (Hardware / Version / Features) | `sysDescr` / `sysObjectID` mimicking the Microsoft format | ✅ |
| Ports | IF-MIB `ifTable` + `ifXTable` (64-bit counters), persistent `ifIndex` | ✅ |
| Processor | `hrProcessorTable` from `NtQuerySystemInformation` | ✅ |
| Memory | `hrStorage` — Physical, Virtual, Cached, Swap | ✅ |
| Disk Usage | `hrStorage` fixed disks with real volume labels | ✅ |
| Disk I/O | UCD-DISKIO from `IOCTL_DISK_PERFORMANCE` | ✅ |
| Temperature | ENTITY-SENSOR-MIB — disk temperature via SMART / NVMe | ✅ |
| Inventory | ENTITY-MIB `entPhysicalTable` parsed from SMBIOS | ✅ |
| Devices | `hrDeviceTable` — processors, NICs, physical disks | ✅ |
| IP addresses | `ipAddrTable` + `ipAddressTable` (IPv4 + IPv6) | ✅ |
| Netstats | `ip` / `icmp` / `tcp` / `udp` / `snmp` groups | ✅ |
| Agent self-health | JT private OIDs (see below) | ✅ |
| ARP / neighbours | `ipNetToPhysicalTable` | ⚙️ off by default |

### Agent self-health OIDs

This agent's failure mode is silent: the service shows *Running* while the graphs
go flat. A private OID subtree exposes the agent's own state so LibreNMS can
monitor the agent itself — version, service uptime, RSS, thread and handle counts,
snapshot age and build time, configuration source and paths, plus a per-collector
health table with status, last success, duration and cumulative error count.

Practical value: after upgrading several hundred hosts, one SNMP walk tells you
which ones did not take the new version.

### Compared with the built-in SNMP Service

A measured, side-by-side comparison against a Windows 10 host still running the
built-in Microsoft SNMP Service — including where jt-snmpd deliberately reports
*less* and why — is in
[`docs/comparison-vs-builtin-snmp.md`](docs/comparison-vs-builtin-snmp.md).

Headline: the built-in service exposes 6,507 OIDs and jt-snmpd 776, but 3,175 of
that gap is information disclosure that is off by default (installed software,
running processes, connection tables, ARP), while jt-snmpd adds inventory,
disk I/O, sensors, disk SMART and self-health that the built-in service does not
provide at all.

Both hosts below are physical machines running Windows 10 22H2, polled by the
same LibreNMS instance with no LibreNMS-side customisation.

#### Sensors

The built-in service reports no sensors at all, so LibreNMS never creates a
Temperature tab. jt-snmpd provides disk temperature and the ACPI thermal zone,
both with the thresholds the firmware itself declares.

![Sensors comparison](docs/images/temperature-en.png)

#### Disk SMART

SMART reaches LibreNMS entirely over SNMP, through `NET-SNMP-EXTEND-MIB` — no
LibreNMS agent and no `smartctl` on the monitored host. Attributes that were not
measured stay `null` rather than being reported as zero.

> Requires `discovery_modules.applications` to be enabled in LibreNMS; it is
> `false` by default. (The `Proxmox` entry on the built-in host is an unrelated
> false positive from an earlier discovery — the built-in service provides no
> SMART data.)

![SMART comparison](docs/images/smart-en.png)

#### Ports

The built-in service publishes every NDIS filter driver as a separate interface;
nine of them here, automatically named and mostly meaningless. jt-snmpd
publishes hardware interfaces only, and assigns `ifIndex` from the persistent
`NET_LUID` so a driver update does not orphan the history.

![Ports comparison](docs/images/ports-en.png)

#### Memory

The built-in service exposes physical and virtual memory. jt-snmpd adds cached
memory and swap, which is what fills the rest of the LibreNMS Memory page.

![Memory comparison](docs/images/memory-en.png)

## Architecture

The MIB is a single array of `(OID, value)` pairs kept in lexicographic order.
`GET` is a `bisect_left`, `GETNEXT` a `bisect_right`, `GETBULK` a contiguous slice.

This is not a micro-optimisation — it makes protocol correctness structural.
Lexicographic ordering, absence of duplicate OIDs, absence of GETNEXT loops and a
correct `endOfMibView` all follow from the array being sorted, so none of them can
regress through a collector change. A test suite asserts the claim rather than
trusting it.

```
Collectors (Win32 API via ctypes)
        │
        ▼
Snapshot builder ──► sorted array + pre-encoded BER bytes
        │
        ▼ atomic handover (reference assignment)
Custom MibInstrumController      bisect
        │
        ▼
pysnmp (message / USM / VACM / transport only)
        ▲
        │
Pre-authentication gate ── source ACL → size cap → rate limit → TLV sanity
        ▲
        │
   UDP/161
```

Two consequences worth calling out:

- **Response bytes are pre-encoded when the snapshot is built.** Assembling a
  response is slicing plus concatenation, which took response encoding from
  164 µs to 0.35 µs per varbind.
- **Nothing reaches the BER decoder before the gate.** The agent runs as
  LocalSystem, so any parser bug is a SYSTEM-level bug. Source ACL, packet size
  cap, per-source token bucket and a coarse TLV check all run before pysnmp sees
  a single byte.

## Host impact

The requirement is that polling must not make the machine feel slow. That is
measured, not assumed. Under stress at roughly **7,000× the real polling rate**
(1,406 complete walks in 60 seconds) on a Windows 11 host:

| Metric | Normal priority | **Below-normal (shipped)** |
|---|---|---|
| Fixed workload degradation | 4.19% ❌ | **0.41% ✅** |
| Agent CPU | 23.4% of one core / 3.9% of machine | — |
| RSS growth over 1,406 walks | +0.12 MB | no leak |
| Threads / handles | flat | flat |

A single complete walk costs 12.5 ms of CPU. LibreNMS polls every five minutes,
so real-world usage is around **0.004% CPU**.

## Security

| Area | Approach |
|---|---|
| Threat model | The primary adversary is an attacker already inside the network, not an external scanner. The agent runs as LocalSystem, so any RCE is a SYSTEM compromise. |
| Pre-authentication | Source IP allow-list, 4096-byte packet cap, per-source token bucket, outer-TLV sanity — all before the BER decoder |
| Access control | Deny by default. The installer requires a management network; `Any/Any` is refused |
| Firewall | Inbound UDP/161 restricted to the management network, created by the installer, removed on uninstall |
| Privileges | `sc privs` drops everything but `SeChangeNotify` / `SeSystemProfile` / `SeIncreaseQuota` |
| Filesystem | Program files in `%ProgramFiles%`, state in `%ProgramData%` with ACLs reset to SYSTEM + Administrators (the default `ProgramData` ACL lets any user create subdirectories) |
| Packaging | PyInstaller **one-folder** only. One-file extracts to `%TEMP%` before executing, which is a known DLL-hijack path |
| Response size | Capped at 1400 bytes so responses never fragment |
| Information disclosure | Installed software, running processes, ARP tables and listening ports are all off by default |
| Scanning | Bandit / Semgrep / Ruff-S / pip-audit / CycloneDX SBOM, plus protocol fuzzing and Windows-specific checks — see [`docs/security-scanning.md`](docs/security-scanning.md) |

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| SNMP | pysnmp 7.1 — message / USM / VACM / transport layers only; the MIB layer is replaced |
| Data sources | Win32 API via ctypes — iphlpapi, psapi, ntdll, kernel32 IOCTLs, SMBIOS, registry |
| Runtime dependencies | `pysnmp` → `pyasn1`. That is the whole list |
| Packaging | PyInstaller one-folder → self-contained `jt-snmpd.exe`, no Python on the target |
| Service | pywin32 service framework, LocalSystem, automatic start |
| Deployment | ZIP + PowerShell installer today; MSI (WiX) for GPO / Intune / SCCM |

## Install

Requires an elevated PowerShell session. The management network is mandatory —
the agent refuses to listen to `Any/Any`.

```powershell
# Extract the release archive, then:
.\install.ps1 -ManagementNetworks 192.168.1.0/24
```

The installer will:

1. Check OS version, architecture, disk space and who owns UDP/161
2. Read the existing Microsoft SNMP Service configuration and carry over the
   community string, permitted managers, sysContact and sysLocation
3. Stop any previous version and wait for its file handles to be released
4. Install to `%ProgramFiles%\JT SNMP Agent\` and create `%ProgramData%\JT-SNMP\`
   with hardened ACLs
5. Disable the built-in SNMP Service — **disable, not remove**, and record enough
   state to restore it
6. Register the service with failure-recovery actions and reduced privileges
7. Create the firewall rule scoped to the management network
8. Start the service and **verify it answers a loopback SNMP query** — a service
   in the *Running* state is not the same as a service that responds
9. Print a migration report and every path an administrator might need

If UDP/161 is held by something that is not the Microsoft SNMP Service, the
installer stops and leaves it alone rather than disabling a third-party agent.

```powershell
# Uninstall (restores the built-in SNMP Service, keeps configuration and state)
.\install.ps1 -Uninstall

# Uninstall and remove everything
.\install.ps1 -Uninstall -Purge
```

Configuration and state are kept by default on purpose: administrators often
reinstall to troubleshoot, and discarding the interface index map would make
LibreNMS re-discover every port and orphan the historical RRDs.

## Paths

| Purpose | Location |
|---|---|
| Program files | `%ProgramFiles%\JT SNMP Agent\` |
| Configuration | `%ProgramData%\JT-SNMP\config.json` |
| Group Policy (overrides configuration) | `HKLM\SOFTWARE\Policies\JasonTools\JTSNMPD` |
| Interface index map | `%ProgramData%\JT-SNMP\state\index-map.json` |
| Restore information | `%ProgramData%\JT-SNMP\state\ms-snmp-restore.json` |
| Logs | `%ProgramData%\JT-SNMP\logs\` |
| Service name | `jt-snmpd` |

The same paths are reported over SNMP (`jtAgentConfigPath`, `jtAgentLogPath`,
`jtAgentInstallPath`), so "where is the configuration file" can be answered from
LibreNMS without logging into the host.

## Project layout

```
jt-snmpd/
├── deploy/          # agent source: jt_agent.py, preauth.py, smbios.py, diskhealth.py
├── packaging/       # build-exe.ps1, install.ps1, make-release.ps1
├── build/           # PyInstaller one-folder output (not in git)
├── dist/            # release artefacts (not in git)
├── tests/           # cross-platform tests — static analysis based, run on Linux CI
├── bench/gate_c/    # architecture prototype and performance measurement
└── docs/            # findings, naming decisions, security scanning, fixtures
```

## Status

| Area | State |
|---|---|
| SNMPv2c, IF-MIB, HOST-RESOURCES, UCD-DISKIO, ENTITY-MIB, ENTITY-SENSOR, IP / TCP / UDP / ICMP, self-health OIDs | ✅ verified against production LibreNMS |
| Windows service, boot start, migration from the built-in service | ✅ verified on Windows 10 and 11 |
| PowerShell installer with health-check gate | ✅ verified, including the upgrade path |
| Disk temperature and SMART health | ✅ verified on physical hardware |
| SNMPv3 (SHA-256 + AES-128) | ⛔ not implemented |
| VACM view presets | ⛔ not implemented |
| MSI for GPO / Intune / SCCM | ⛔ not implemented |
| Authenticode signing | ⛔ pending SignPath |
| Windows Server, Server Core, domain controllers | ⛔ not yet verified |
| Multi-homed source address selection | ⛔ not yet verified |

Not planned for v1.0: SNMP traps and informs, SNMP SET, ARM64, IPv6-only
deployments, cluster awareness.

## License

GPL-3.0-or-later. Commercial support: contact Jason Tools.
