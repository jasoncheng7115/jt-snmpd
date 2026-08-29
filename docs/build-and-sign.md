---
layout: default
title: Building and Signing It Yourself
description: Build the MSI from source on your own machine and sign it with your own certificate
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/build-and-sign_zh-TW.html)

# Building and signing it yourself

The released MSI is unsigned, and in an environment with WDAC or AppLocker in
enforcement that is a wall rather than an inconvenience: no publisher rule can
match a file that has no publisher. [Code signing](https://jasoncheng7115.github.io/jt-snmpd/code-signing.html)
covers the hash-rule route, which works but has to be redone on every upgrade.

This page covers the other route, and the better one if you already run an
internal PKI: build the installer yourself from the published source, sign it
with your own code-signing certificate, and deploy it as a file you issued. Two
things follow from that. Your existing publisher rules apply with no per-version
work, and you are no longer trusting a binary someone else compiled — you are
trusting source you can read and a build you ran.

---

## 1. What the build machine needs

The **installer** is self-contained and installs with no network access. The
**build** is not: it downloads Python packages and the WiX toolset. Do the build
on a machine that can reach PyPI and NuGet, or on one pointed at your internal
mirrors, and move the finished MSI to where it is deployed.

| Requirement | Version | Notes |
|---|---|---|
| Windows x64 | 10 / 11 / Server 2019+ | Matches what the released MSI is built on (`windows-latest`) |
| Python | **3.12** | The version used for releases. A different 3.x will build, but the PyInstaller runtime it embeds is then a different one from the tested combination |
| Python packages | `pysnmp==7.1.29`, `pywin32`, `pyinstaller` | The pysnmp version is pinned in `pyproject.toml` and is not incidental: the BER encoding this project pre-computes is matched against pyasn1's actual output |
| .NET SDK | 8 or newer | WiX v5 runs as a dotnet global tool |
| WiX | **5.x**, with the Util and UI extensions at **5.0.2** | Unpinned, `wix extension add` resolves to 7.x and fails with `WIX6101` |
| `signtool.exe` | Windows SDK | Ships with the Windows SDK signing components; also present in the Visual Studio build tools |
| Git | any | Optional. Only used to record the commit in `BUILDINFO.txt` |

```powershell
# CLI - one-time setup on the build machine
python -m pip install --upgrade pip
python -m pip install pysnmp==7.1.29 pywin32 pyinstaller pytest

dotnet tool install --global wix --version 5.*
$env:PATH = "$env:USERPROFILE\.dotnet\tools;$env:PATH"
wix extension add -g WixToolset.Util.wixext/5.0.2
wix extension add -g WixToolset.UI.wixext/5.0.2
wix extension list -g
```

---

## 2. Get the source, at a tag

Build from a tag rather than from the branch tip. A tag is what a release was
built from, and it is what the version number in the file name refers to.

```powershell
# CLI
git clone https://github.com/jasoncheng7115/jt-snmpd.git
cd jt-snmpd
git checkout v1.0.0
```

Do not edit `deploy/version.py`. The version is read from it by the executable,
by the MSI's `ProductVersion`, by the file name and by the `jtAgentVersion` OID,
and the point of that OID is to answer "we upgraded several hundred machines,
which ones did not take?". A build whose reported version does not match the
file it came from makes that unanswerable.

### Run the tests first

They take about twenty seconds and they are the same suite the release gate
runs. If something in your environment is wrong, this is where it shows up,
rather than on a customer's machine.

```powershell
# CLI
python -m pytest tests\ -q
```

---

## 3. Build the executable

```powershell
# CLI - from the repository root
$py = (Get-Command python).Source
.\packaging\build-exe.ps1 -Python $py -Source deploy\jt_agent.py
```

This produces `build\jt-snmpd\jt-snmpd.exe` together with `_internal\`, the
PyInstaller one-folder runtime. The script does not simply trust that PyInstaller
exited zero. It checks that the executable is **newer than its source**, and it
runs `jt-snmpd.exe --selftest` and requires it to pass. Both gates exist because
this project has three times produced a green build that shipped the previous
version's code under a new version number.

---

## 4. Sign the executables — before packaging, not after

**Order matters.** The MSI packages whatever is in `build\jt-snmpd\` at the
moment it is built. Sign after packaging and you have a signed MSI containing an
unsigned service executable, which is exactly the file WDAC checks when the
service starts.

```powershell
# CLI - certificate from the machine store, selected by subject name
signtool sign /n "Your Organisation" /fd SHA256 `
  /tr http://timestamp.digicert.com /td SHA256 `
  .\build\jt-snmpd\jt-snmpd.exe
```

With a PFX file instead, replace `/n "Your Organisation"` with
`/f your-code-signing.pfx /p <password>`. Prefer a certificate held in the
machine store, or on an HSM or smart card, over a PFX on disk with a password on
a command line that lands in the PowerShell history.

### If WDAC enforces DLL rules

The default WDAC configuration checks DLLs as well as executables, and
`_internal\` holds the CPython runtime along with the pysnmp and pywin32
extension modules, as `.dll` and `.pyd` files. Some arrive already signed by
their publishers — the CPython runtime is signed by the Python
Software Foundation — and others, notably the pywin32 extension modules, are not.
Sign the whole tree so the answer does not depend on which:

```powershell
# CLI
Get-ChildItem .\build\jt-snmpd -Recurse -Include *.exe,*.dll,*.pyd |
  ForEach-Object { signtool sign /n "Your Organisation" /fd SHA256 `
      /tr http://timestamp.digicert.com /td SHA256 $_.FullName }
```

Re-signing a file that already carries a valid signature replaces it, which is
harmless here.

### If Group Policy sets the PowerShell execution policy

The installer runs its configuration script as
`powershell.exe -ExecutionPolicy Bypass -File msi-configure.ps1`. That argument
is enough on an ordinary machine, but **an execution policy set by Group Policy
takes precedence over the command line**. On a domain that enforces `AllSigned`,
the script is refused and the installation fails at the custom action. Sign it
as well:

```powershell
# CLI
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Select-Object -First 1
Set-AuthenticodeSignature -FilePath .\packaging\msi-configure.ps1 `
  -Certificate $cert -TimestampServer http://timestamp.digicert.com
```

This appends a signature block to the file, so its SHA-256 no longer matches the
one in the repository. That is expected, and §7 explains how to check the part
that still should match.

### About the timestamp

`/tr` countersigns the signature with a trusted time, which is what keeps it
valid after the certificate expires. Without it, every file you signed stops
verifying the day the certificate does — on machines that may be years into a
maintenance contract by then. If the build machine has no route to a public
timestamp service, use your own RFC 3161 server rather than dropping the
countersignature.

---

## 5. Build the MSI

```powershell
# CLI
$env:PATH = "$env:USERPROFILE\.dotnet\tools;$env:PATH"
.\packaging\build-msi.ps1
```

The version comes from `deploy\version.py`; there is no argument to pass. The
result is `dist\jt-snmpd-1.1.3-x64.msi`, its `.sha256`, and a per-version copy
under `dist\releases\1.0.0\` alongside `BUILDINFO.txt`:

```
product   jt-snmpd
version   1.0.0
built     <date and time of this build>
builder   <machine> / <account>
commit    <short commit of the tag you built>
sha256    <the MSI's hash>
files     <file count>
size      <size>

-- source fingerprints (first 16 hex of SHA-256) --
configure <first 16 hex of the SHA-256 of packaging\msi-configure.ps1>
wxs       <first 16 hex of the SHA-256 of packaging\wix\jt-snmpd.wxs>
agent     <first 16 hex of the SHA-256 of deploy\jt_agent.py>
```

Keep that file. It is what answers, eighteen months later, which version of the
configuration script is inside the MSI a particular site is holding. That
question has already been asked once here, when two copies of
`msi-configure.ps1` existed on one machine and the build silently used the one
nobody was editing.

---

## 6. Sign the MSI

```powershell
# CLI
signtool sign /n "Your Organisation" /fd SHA256 `
  /tr http://timestamp.digicert.com /td SHA256 `
  .\dist\jt-snmpd-1.1.3-x64.msi
```

Signing rewrites the file, so the `.sha256` produced in §5 no longer describes
it. Regenerate it, and distribute that value internally the way the release page
distributes its own:

```powershell
# CLI
$msi = ".\dist\jt-snmpd-1.1.3-x64.msi"
"$((Get-FileHash $msi -Algorithm SHA256).Hash.ToLower())  $(Split-Path $msi -Leaf)" |
  Set-Content "$msi.sha256" -Encoding ascii
```

---

## 7. Verify what you built

```powershell
# CLI
signtool verify /pa /v .\dist\jt-snmpd-1.1.3-x64.msi
Get-AuthenticodeSignature .\build\jt-snmpd\jt-snmpd.exe | Format-List Status, SignerCertificate
```

`Status` must be `Valid`. `NotSigned` on the executable after §4 usually means
the MSI was built before the signing step and packaged the unsigned copy.

To confirm that what you built came from the source you read rather than from
something that arrived in between, compare the fingerprints in `BUILDINFO.txt`
against the checked-out tree:

```powershell
# CLI
(Get-FileHash .\deploy\jt_agent.py            -Algorithm SHA256).Hash.Substring(0,16)
(Get-FileHash .\packaging\wix\jt-snmpd.wxs    -Algorithm SHA256).Hash.Substring(0,16)
```

The `agent` and `wxs` lines must match. The `configure` line will not if you
signed `msi-configure.ps1` in §4 — compare that one before signing, or verify
its signature instead.

---

## 8. Deploy your certificate, then use publisher rules

A signature only helps once the clients trust the certificate. Push it with
Group Policy:

```
Computer Configuration → Windows Settings → Security Settings → Public Key Policies
  → Trusted Publishers                        <- your code-signing certificate
  → Trusted Root Certification Authorities    <- your internal CA, if it issued it
  → Intermediate Certification Authorities    <- any issuing CA in the chain
```

With that in place:

- The UAC prompt names your organisation on the verified banner, instead of
  showing an unknown publisher on the yellow one.
- SmartScreen does not intervene.
- **WDAC and AppLocker publisher rules apply**, which is the part that matters.
  A publisher rule keeps working across upgrades; the hash rule it replaces has
  to be regenerated for every new version, and a merged policy that still names
  the previous version will block the new service from starting.

```powershell
# CLI - a WDAC rule by publisher rather than by hash
New-CIPolicy -Level Publisher -FilePath .\jt-snmpd.xml `
  -ScanPath 'C:\Program Files\jt-snmpd' -UserPEs
Merge-CIPolicy -PolicyPaths .\existing.xml,.\jt-snmpd.xml -OutputFilePath .\merged.xml
```

---

## 9. What is not reproducible, stated plainly

**Your MSI will not be byte-identical to the released one, and its SHA-256 will
not match the published value.** That is a property of the toolchain, not a sign
that anything is wrong:

- PyInstaller writes a build timestamp into the PE headers it produces, so two
  builds of identical source differ.
- Windows Installer stamps a fresh package code into every build, and the
  product code is generated by WiX rather than written down.
- Signing rewrites both the executable and the MSI by definition.

So a hash comparison against the release is not available to you, and nobody
should imply otherwise. What you can verify is the input rather than the output:
the source is public and at a tag, the tests are the release gate, and
`BUILDINFO.txt` fingerprints the three files that decide what the package
actually does. If byte-for-byte reproducibility is a requirement in your
environment, treat that as an open item rather than a solved one.

---

## 10. Living alongside upstream releases

Your build and the official one share an **UpgradeCode**, which is deliberate:
it means either can upgrade the other in place, carrying
`%ProgramData%\jt-snmpd\` across with it — including `state\index-map.json`,
whose loss makes LibreNMS delete every port and rediscover, taking the
historical graphs with it.

The consequence is worth stating: if someone installs the official unsigned MSI
on a machine running your signed build, it upgrades cleanly and quietly replaces
a signed installation with an unsigned one. Two habits prevent that:

1. **Rebuild and re-sign for every version you adopt.** Track the releases, build
   the tag, sign it, and publish it to your own share. The moment you skip one,
   the pressure to "just use the official MSI this once" is what undoes the work.
2. **Keep the upstream version number.** Do not renumber your build. It is what
   `jtAgentVersion` reports, and one SNMP walk across the estate telling you
   which machines are on which version is worth more than a private numbering
   scheme.

---

## Related documentation

- [Documentation home](https://jasoncheng7115.github.io/jt-snmpd/)
- [Code signing](https://jasoncheng7115.github.io/jt-snmpd/code-signing.html)
- [Naming and paths](https://jasoncheng7115.github.io/jt-snmpd/naming-and-paths.html)
- [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
- [Release checklist](https://jasoncheng7115.github.io/jt-snmpd/release-checklist.html)
