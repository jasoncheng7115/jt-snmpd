---
layout: default
title: Deploying to Windows Server
description: What differs between Server 2016, 2019, 2022 and 2025, and which of those differences reach this agent
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/windows-server-notes_zh-TW.html)

# Deploying to Windows Server

Measured on **Windows Server 2016 Standard, build 14393, a domain controller**:
the forty-check installation lifecycle passes, the migration from the built-in
SNMP Service works, and the agent reports the domain-controller `sysObjectID`
branch. Everything below marked *measured* comes from that machine. Everything
marked *documented* comes from Microsoft and has **not** been reproduced here —
2019, 2022 and 2025 have not been tested.

---

## 1. The short version

| Release | Expected to work | Watch |
|---|---|---|
| 2016 | **Measured.** 40/40 lifecycle, migration, DC branch | — |
| 2019 | Yes | Installing the built-in service, if you want its settings migrated |
| 2022 | Yes | The same, plus a known Microsoft issue where only SNMP Trap appears in the service list |
| 2025 | Yes | Credential Guard and VBS on by default; SMB signing required by default |

Nothing in 2019, 2022 or 2025 removes an interface this agent depends on. The
differences are in the environment around it.

---

## 2. The built-in SNMP Service is installed differently per release

This only matters if you want the installer to **carry the existing settings
over**. jt-snmpd does not need the built-in service to be present.

| Release | How the built-in service is added |
|---|---|
| Server 2016 | `Install-WindowsFeature SNMP-Service` — a Windows Feature. *Measured: `Get-WindowsFeature SNMP-Service` reported `Installed`.* |
| Server 2019 / 2022 / 2025 | A Feature on Demand: `Add-WindowsCapability -Online -Name "SNMP.Client~~~~0.0.1.0"` |
| Windows 10 / 11 | The same capability. `dism /online /enable-feature /featureName:SNMP` fails with `0x800f080c`, **because the feature is deprecated** |

Two consequences for a deployment:

- **A Feature on Demand needs a source.** On a machine with no internet — which
  is the environment this project is built for — `Add-WindowsCapability` fails
  unless you point it at a FoD ISO or a WSUS/local source. If you were planning
  to install the built-in service purely so that jt-snmpd could migrate its
  community, do not: **supply `COMMUNITY=` to the installer instead**. It is one
  property and it needs nothing from the network.
- **On Server 2022 the service list can look wrong.** Microsoft records an issue
  where, after adding SNMP and the WMI SNMP Provider, only *SNMP Trap* appears
  in `services.msc`. If the installer reports that it found no built-in service
  to migrate, check `Get-Service SNMP` rather than the console.

If there is no built-in service and no `COMMUNITY=`, the installer **stops and
says so** rather than inventing one — verified on a Server 2016 domain
controller whose built-in service was running with no community configured:
msiexec returns 1603, the transaction rolls back, nothing is left behind, and
the built-in service is not touched.

---

## 3. Windows Server 2025: the defaults changed

**Credential Guard is on by default**, on domain-joined machines that meet the
hardware requirements, and turning it on turns on virtualisation-based security
with it. Microsoft states this applies to **non-domain-controller** systems;
upgrades to 2025 keep it enabled unless it is explicitly turned off.

For most monitoring agents this is where trouble starts, because reading CPU
temperature or voltages means loading a kernel driver, and the drivers commonly
used for it are on Microsoft's vulnerable-driver blocklist. **This agent has no
kernel-mode component at all** — every collector calls Win32 through ctypes —
so VBS, HVCI and Credential Guard have nothing of ours to block. That was a
design rule from the start rather than a reaction, and 2025 is where it pays.

It is also why the agent reports no CPU core temperature. The honest position is
in [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html):
a value that cannot be read without a driver is not reported at all.

**SMB signing is required by default for all outbound connections.** The agent
never touches SMB. This reaches you only if you deploy the MSI from a file
share — Group Policy software installation, or a manual copy — and every
supported Windows release can sign, so a modern share is unaffected. A NAS or
appliance hosting the MSI that cannot sign is the case to check.

**Removed in 2025, and none of it is used here:** Windows PowerShell 2.0 (the
installer uses 5.1, which stays), the SMTP Server feature, WordPad. **WMIC** is
a Feature on Demand in 2025 and Microsoft says it will be removed. This agent
has never called `wmic.exe` and starts no subprocess on the data path, so that
removal is not a migration for anyone using it.

---

## 4. Server Core

The installer has no interactive prompt: every value it needs arrives as an MSI
property, and it fails closed when one is missing rather than waiting for
someone to type. `msiexec /qn` is the whole procedure.

Not yet verified on an actual Server Core installation. The graphical wizard
obviously does not apply there; the silent path is the one to use, and it is the
path the forty lifecycle checks exercise.

---

## 5. Domain controllers

**Measured on a live DC.** The agent detects the role through
`DsRoleGetPrimaryDomainInformation` and reports the domain-controller branch of
`sysObjectID`:

```
sysObjectID = 1.3.6.1.4.1.311.1.1.3.1.3
```

That matters at the LibreNMS end: `Windows.php` uses this branch to choose the
datacenter version string, so a DC reported as an ordinary server gets a
different — wrong — version. Client, server and domain controller are three
separate branches for that reason.

A read-only domain controller has not been tested.

---

## 6. `sysServices` will not tell you which agent answered

Both report **76** on a Server 2016 domain controller: the built-in service's
value comes from `RFC1156Agent\sysServices` in the registry, which an
administrator sets from the Agent tab of the service properties. It describes
the machine, not the software. To tell them apart, read `sysDescr`, or ask for
`jtAgentVersion` under the private subtree — the built-in service has no way to
produce that.

This note exists because this project used the 76-versus-79 rule internally for
months before a Server measurement showed it was not one.

---

## Related documentation

- [Documentation home](https://jasoncheng7115.github.io/jt-snmpd/)
- [Comparison against the built-in service](https://jasoncheng7115.github.io/jt-snmpd/comparison-vs-builtin-snmp.html)
- [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
- [Building and signing it yourself](https://jasoncheng7115.github.io/jt-snmpd/build-and-sign.html)

**Microsoft sources:**
[Features removed or no longer developed in Windows Server](https://learn.microsoft.com/en-us/windows-server/get-started/removed-deprecated-features-windows-server) ·
[Can't install the SNMP and WMI SNMP Provider features](https://learn.microsoft.com/en-us/troubleshoot/windows-client/networking/cannot-install-snmp-wmisnmpprovider) ·
[Credential Guard overview](https://learn.microsoft.com/en-us/windows/security/identity-protection/credential-guard/) ·
[SMB security hardening in Windows Server 2025](https://learn.microsoft.com/en-us/windows-server/storage/file-server/smb-security-hardening)
