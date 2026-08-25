---
layout: default
title: Naming and Paths
description: Naming and paths, and the encoding rules that came out of real bugs
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/naming-and-paths_zh-TW.html)

# Naming and paths

## Naming

**One name, everywhere: `jt-snmpd`.**

It used to be split three ways: `jt-snmpd` as the technical identifier, "JT SNMP
Agent" as the human-readable display name, and `JT-SNMP` for the data directory.
Separating a display name from an identifier is ordinary Windows convention —
the firewall shows as "Windows Defender Firewall" while its service is `mpssvc` —
but here it produced confusion rather than clarity: you find jt-snmpd on GitHub,
meet a different name in Apps & Features, and find a third spelling on disk.
Since 0.9.6 they all agree.

| Item | Name | Reason |
|---|---|---|
| Project and repository | **jt-snmpd** | The trailing `d` follows daemon convention, as in `snmpd` and `sshd`: it reads as a resident service |
| Windows service name | **jt-snmpd** | `sc query jt-snmpd` is the obvious thing to type |
| Service display name | **jt-snmpd** | Matches the service name, so services.msc shows one name rather than two |
| Product name (MSI, Apps & Features) | **jt-snmpd** | What you find on GitHub is what appears in the control panel |
| Installer title | **jt-snmpd Setup** | |
| Service description | A read-only SNMP agent serving Windows host metrics through standard MIBs | |
| Executable | **jt-snmpd.exe** | The service itself |
| Management CLI | **jt-snmpdctl.exe** | Separate from the service binary, so the two cannot be confused, as with systemctl |
| Install directory | **jt-snmpd** | Which also takes the space out of the path, and with it a whole class of unquoted-service-path findings |
| Data directory | **jt-snmpd** | `JT-SNMP` before 0.9.5; the installer moves it, see below |
| Firewall rules | **jt-snmpd (UDP 161)**, **jt-snmpd (ICMPv4)** | |
| Group policy key | `HKLM\SOFTWARE\Policies\JasonTools\JTSNMPD` | The registry key is deliberately unchanged: renaming it would break every existing GPO |

### Upgrading from 0.9.5 or earlier

The installer **moves** `%ProgramData%\JT-SNMP` to `%ProgramData%\jt-snmpd`.
That step is not optional. `state\index-map.json` holds the ifIndex assignments,
and losing it makes LibreNMS delete every port, rediscover, and orphan the
historical RRDs. `state\ms-snmp-restore.json` is the only record of what the
built-in SNMP service looked like before it was disabled.

If the move fails it copies instead and says so in the log, because a duplicated
directory is recoverable and a lost one is not.
`tests/test_data_dir_migration.py` holds this in place.

## Install paths, with program and data strictly separated

What follows is what is actually on disk, not a plan. Entries marked *(planned)*
do not exist yet.

```
%ProgramFiles%\jt-snmpd\            <- the program, read-only. Never ProgramData
    jt-snmpd.exe
    msi-configure.ps1                   called by the MSI on install and uninstall
    _internal\                          the PyInstaller one-folder runtime
    jt-snmpdctl.exe                     (planned) management CLI
    mibs\                               (planned) MIB files, for LibreNMS and snmpwalk

%ProgramData%\jt-snmpd\             <- configuration and state, writable
    config.json                         written by the installer, read by the agent at its entry point
    state\index-map.json                the ifIndex assignments (lose it and LibreNMS rebuilds every port)
    state\engine.json                   engineID and engineBoots
    state\ms-snmp-restore.json          the built-in service's original start type and state
    state\disk-maxtemp.json             the highest disk temperature actually observed, kept across restarts
    logs\jt-snmpd.log                   rotated, 5 MB across 5 generations
    logs\msi-configure.log              the installer's own log
    secrets\                            created with a tightened ACL; SNMPv3 keys (planned)
```

> This listing once named `config.yaml`, `engine-state.json` and
> `ms-snmp-migration.json`. All three were wrong. Paths in documentation have to
> be checked against a running machine before they are written down.

## Rules about paths

1. **ImagePath must be quoted.** Measured: `C:\程式集測試\JT SNMP 代理程式\jt_snmpd.py`
   unquoted was truncated at the first space to `C:\程式集測試\JT`, and the
   process died with nothing in any log. This is the *unquoted service path*
   finding that security audits raise most often.

2. **The ProgramData ACL has to be verified and reset, not merely created.**
   `C:\ProgramData`'s default ACL lets Users create subdirectories, so an
   attacker can create ours first and keep write access to it.
   Create-if-not-exists is not enough. The target ACL is `SYSTEM: Full`,
   `Administrators: Full`, and nothing else.

3. **Non-ASCII paths have to work.** A customer may install under a path in their
   own language. Verified below.

## Verified on hardware, 2026-08-24, Windows 11 build 26200, Traditional Chinese

Running the agent from `C:\程式集測試\JT SNMP 代理程式\` — non-ASCII **and** a
space:

```
LISTENING 0.0.0.0:16162 varbinds=131
fs_encoding=utf-8
sysDescr     = Hardware: AMD64 Family 25 Model 80 Stepping 0 AT/AT COMPATIBLE
               - Software: Windows Version 6.3 (Build 26200 Multiprocessor Free)
sysServices  = 76                        <- see the note below
ifName       = 乙太網路                   <- non-ASCII, correct UTF-8
ifDescr      = Red Hat VirtIO Ethernet Adapter
hrProcLoad   = 8                         <- a real CPU sample
diskIODevice = PhysicalDrive0            <- UCD-DISKIO
```

> `sysServices` is not a way to tell the two agents apart, although it was used
> as one here for a while. It comes from `RFC1156Agent\sysServices` in the
> registry, which an administrator sets from the Agent tab of the service's
> properties, so it describes the machine rather than the software. Measured on
> a Windows Server 2016 domain controller, an untouched built-in service reports
> **76** — the same value this agent reports. Use `sysDescr`, or the presence of
> `jtAgentVersion` under the private subtree, which the built-in service has no
> way to produce.

### The encoding rule, which came out of a real failure

**An SNMP OCTET STRING is bytes, not text.** pyasn1 encodes a `str` as latin-1 by
default and raises `PyAsn1UnicodeEncodeError` on anything outside it. On a
Traditional Chinese Windows installation the network adapter's alias *is*
non-ASCII — 乙太網路 is simply what "Ethernet" is called — so in the target
environment this is not an edge case, it is guaranteed.

Everything goes through `octet()`, which encodes to UTF-8 explicitly. A bare
`rfc1902.OctetString(str)` is not allowed.

The same applies to files: every open states `encoding="utf-8"`. Windows' `open()`
defaults to the system ANSI code page, cp950 on a Traditional Chinese
installation, and writing anything outside it raises `UnicodeEncodeError`.

---

## Related documentation

- [Documentation home](https://jasoncheng7115.github.io/jt-snmpd/)
- [Manual removal](https://jasoncheng7115.github.io/jt-snmpd/manual-removal.html)
- [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
