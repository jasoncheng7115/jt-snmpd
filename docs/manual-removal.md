---
layout: default
title: Manual Removal
description: What to do when the installer cannot install, upgrade or uninstall
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/manual-removal_zh-TW.html)

# When the installer cannot finish

The MSI is meant to handle install, upgrade and uninstall on its own, and on the
lifecycle tests it does. This page is for the times it does not: a rolled-back
installation, an uninstall that reports success while the service is still
running, a machine where the product no longer appears in Apps & Features but
UDP/161 is still answering.

Everything below is a manual equivalent of a step the installer performs. Work
from an **elevated PowerShell** prompt.

> Read [§5](#5-restore-the-built-in-snmp-service) before you delete anything.
> The record that says how to put the built-in Windows SNMP Service back lives
> in the data directory, so deleting that directory first throws away the only
> copy.

---

## 1. Find out what is actually there

```powershell
# CLI
Get-Service jt-snmpd -ErrorAction SilentlyContinue
Get-CimInstance Win32_Service -Filter "Name='jt-snmpd'" | Select-Object Name, State, StartMode, PathName
Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' |
  Where-Object DisplayName -like '*JT SNMP*' |
  Select-Object DisplayName, DisplayVersion, PSChildName
Get-NetFirewallRule -DisplayName 'jt-snmpd*' -ErrorAction SilentlyContinue |
  Select-Object DisplayName, Enabled
Test-Path 'C:\Program Files\jt-snmpd'
Test-Path 'C:\ProgramData\jt-snmpd'
Get-NetUDPEndpoint -LocalPort 161 -ErrorAction SilentlyContinue
```

`PSChildName` in the third command is the **ProductCode** — the GUID in braces
that `msiexec` needs. It changes with every version.

## 2. Try the supported uninstall first

```powershell
# CLI
msiexec /x '{PUT-THE-PRODUCTCODE-HERE}' /qn /l*v "$env:TEMP\jt-uninstall.log"
```

If it fails, `$env:TEMP\jt-uninstall.log` names the action that failed. Search it
for `Return value 3`; the lines just above are the cause. Read
`C:\ProgramData\jt-snmpd\logs\` as well — the configure step writes its own log
there, in English, and it usually says plainly what it could not do.

Only continue past this point if the supported uninstall will not complete.

## 3. Stop and remove the service

```powershell
# CLI
Stop-Service jt-snmpd -Force -ErrorAction SilentlyContinue

# If it will not stop, end the process directly
$svc = Get-CimInstance Win32_Service -Filter "Name='jt-snmpd'"
if ($svc.ProcessId) { Stop-Process -Id $svc.ProcessId -Force }

sc.exe delete jt-snmpd
```

`sc.exe delete` marks the service for deletion. If the entry survives, something
still holds a handle to it — usually an open Services console or Event Viewer.
Close them, or reboot; the deletion completes on the next start.

## 4. Remove the firewall rules

```powershell
# CLI
Get-NetFirewallRule -DisplayName 'jt-snmpd*' | Remove-NetFirewallRule
```

Two rules are created: `jt-snmpd (UDP 161)` and `jt-snmpd (ICMPv4)`.
Leaving them behind is not a security problem once the service is gone, but the
next installation will replace them, so it is tidier to remove them here.

## 5. Restore the built-in SNMP Service

**This is the step most worth getting right.** The installer disables the
built-in Windows SNMP Service rather than removing it, and records what it was
so that uninstalling can put it back. That record is:

```
C:\ProgramData\jt-snmpd\state\ms-snmp-restore.json
```

Read it before deleting the data directory:

```powershell
# CLI
Get-Content 'C:\ProgramData\jt-snmpd\state\ms-snmp-restore.json' -Raw | ConvertFrom-Json
```

It names the start type and the state the built-in service had before we touched
it. Put those back:

```powershell
# CLI - substitute the values from the record
Set-Service -Name SNMP -StartupType Automatic
Start-Service SNMP
```

If the record is missing or unreadable, you have to decide from your own records
whether that machine was running the built-in SNMP Service before jt-snmpd was
installed. **Do not guess.** Leaving it disabled is the safer error: the machine
stops being monitored, which is visible. Enabling it when it was not previously
enabled reopens a service the site may have deliberately turned off.

## 6. Remove the files

```powershell
# CLI
Remove-Item 'C:\Program Files\jt-snmpd' -Recurse -Force
Remove-Item 'C:\ProgramData\jt-snmpd' -Recurse -Force
# On a machine upgraded from 0.9.5 or earlier, the pre-rename directory may
# still be present if a migration was interrupted:
Remove-Item 'C:\ProgramData\JT-SNMP' -Recurse -Force -ErrorAction SilentlyContinue
```

`C:\ProgramData\jt-snmpd` holds the configuration, the logs, the SNMP engine
identity and the interface index map. Keeping the directory is what a normal
uninstall does, deliberately: administrators often uninstall and reinstall to
troubleshoot, and discarding the index map makes LibreNMS rediscover every port,
leaving the historical RRDs with nothing pointing at them. Delete it only when
you mean to remove the agent for good.

If a file is locked, something is still running. Recheck §3.

## 7. Clear a stuck Windows Installer registration

Occasionally the files and the service are gone but Windows still believes the
product is installed, and a fresh MSI refuses with "another version of this
product is already installed".

```powershell
# CLI - ask Windows Installer what it thinks is registered
Get-Package -ProviderName msi | Where-Object Name -like '*JT SNMP*'
Get-Package -ProviderName msi -Name 'jt-snmpd' | Uninstall-Package
```

If that also fails, the registration can be removed from the registry directly.
**This is a last resort**, it bypasses Windows Installer's own bookkeeping, and
it should be done only after §3 to §6 are complete:

```powershell
# CLI - inspect first, delete second
Remove-Item "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{PRODUCTCODE}" -Recurse
```

Back up the key with `reg export` before removing it. After this, verify a fresh
installation works before considering the machine fixed.

## 8. Verify the machine is clean

```powershell
# CLI - every one of these should come back empty or false
Get-Service jt-snmpd -ErrorAction SilentlyContinue
Get-NetFirewallRule -DisplayName 'jt-snmpd*' -ErrorAction SilentlyContinue
Test-Path 'C:\Program Files\jt-snmpd'
Test-Path 'C:\ProgramData\jt-snmpd'
Get-NetUDPEndpoint -LocalPort 161 -ErrorAction SilentlyContinue
Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' |
  Where-Object DisplayName -like '*JT SNMP*'
```

If `Get-NetUDPEndpoint -LocalPort 161` still returns something, check which
process owns it before assuming it is ours:

```powershell
# CLI
Get-NetUDPEndpoint -LocalPort 161 | ForEach-Object {
  Get-Process -Id $_.OwningProcess | Select-Object Id, ProcessName, Path
}
```

It may be the built-in SNMP Service you restored in §5, which is the correct
outcome, or a third-party agent that was there all along.

---

## When installation is what fails

| Symptom | Likely cause | What to do |
|---|---|---|
| MSI ends with 1603 and rolls back | The configure step failed. It is a rollback, so the machine is left as it was | Read `C:\ProgramData\jt-snmpd\logs\`, then the verbose MSI log |
| "no community could be determined" | Installed silently with no `COMMUNITY`, and the built-in service had none to carry over | Supply `COMMUNITY=` on the command line, or use the graphical installation |
| Installation aborts, saying UDP/161 is in use | A third-party agent holds the port | Deliberate: we never disable somebody else's agent. Decide which one should own the port |
| Service starts, then stops | The health check found the agent not answering on loopback | `C:\ProgramData\jt-snmpd\logs\` records why; a bad configuration file is the usual cause |
| Windows says the product is already installed | A previous registration is stuck | §7 |

Collect a verbose log to diagnose an installation:

```powershell
# CLI
msiexec /i jt-snmpd-0.9.7-x64.msi /qn /l*v "$env:TEMP\jt-install.log" `
  MANAGEMENTNETWORKS=10.0.0.0/24 COMMUNITY=your-community
```

---

## Reporting it

If the installer could not do something this page had to do by hand, that is a
defect worth reporting, not just a machine to fix. Please open an issue with the
verbose MSI log and the contents of `C:\ProgramData\jt-snmpd\logs\`, with the
community string removed.

<https://github.com/jasoncheng7115/jt-snmpd/issues>

---

## Related documentation

- [Documentation home](https://jasoncheng7115.github.io/jt-snmpd/)
- [Code signing](https://jasoncheng7115.github.io/jt-snmpd/code-signing.html)
- [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
