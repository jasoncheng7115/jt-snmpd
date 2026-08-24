---
layout: default
title: Code Signing
description: The installer is unsigned - what you will see, and how to handle it
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/code-signing_zh-TW.html)

# The installer is not code signed

The MSI, and the executable it installs, carry **no Authenticode signature**.
There is no plan to obtain a certificate at present, so this is the state you
should expect from every release rather than a temporary gap.

An Authenticode signature does two things: it proves the file came from a named
publisher, and it proves the bytes have not been altered since. This project
answers the second with a **published SHA-256** for every release asset, and
leaves the first unanswered. The sections below cover what that looks like at
install time and what to do about it.

---

## 1. What you will actually see

| Where | What appears | What to do |
|---|---|---|
| Browser download | Edge or Chrome may warn that the file "isn't commonly downloaded" | Keep the file, then verify the SHA-256 (§2) |
| Double-clicking the MSI | Microsoft Defender SmartScreen: *"Windows protected your PC"* | **More info → Run anyway**, once the hash matches |
| The UAC elevation prompt | Publisher shown as **Unknown**, on the yellow banner rather than the blue verified one | Expected. Confirm you are elevating for the file you just verified |
| `msiexec /qn` from an elevated console | Nothing — SmartScreen only intercepts interactive launches | Nothing |
| GPO software deployment | Nothing — the installation runs as SYSTEM with no interactive session | Nothing. Host the MSI on an internal share (§3) |
| WDAC or AppLocker enforced | **Blocked.** No publisher rule can match an unsigned file | Add a hash rule (§4), or sign it yourself (§5) |
| Microsoft Defender | PyInstaller output has a history of heuristic false positives | If it is quarantined, submit the sample and add an exclusion (§6) |

None of these are errors in the installer. They are Windows correctly reporting
that it cannot identify who produced the file.

---

## 2. Verify the download — this is the important step

The hash is the integrity check that a signature would otherwise have given you
at install time. Every release attaches `<msi-name>.sha256` alongside the MSI.

```powershell
# CLI - run in the folder containing both files
Get-FileHash .\jt-snmpd-0.9.2-x64.msi -Algorithm SHA256
Get-Content  .\jt-snmpd-0.9.2-x64.msi.sha256
```

The two values must match, ignoring case. **If they do not, stop** — do not
install, and re-download from the release page.

Fetch the `.sha256` file from the GitHub release itself, not from a mirror or a
copy someone forwarded you. A hash that travelled with the file it is supposed
to protect proves nothing.

---

## 3. Clear the Mark of the Web

Files downloaded through a browser carry a zone marker in an alternate data
stream, and that marker is what triggers SmartScreen. Once you have verified the
hash, remove it:

```powershell
# CLI
Unblock-File .\jt-snmpd-0.9.2-x64.msi
```

The graphical equivalent is right-click → **Properties** → tick **Unblock** at
the bottom of the General tab.

Copying the MSI to an internal file share and installing from there avoids the
marker entirely, which is why GPO deployment never encounters it.

---

## 4. WDAC and AppLocker: add a hash rule

In an environment with Windows Defender Application Control or AppLocker in
enforcement, an unsigned file cannot be allowed by publisher — there is no
publisher. A **file hash rule** is the supported alternative, and it is
genuinely strict: it permits exactly the bytes you approved and nothing else.

Two files need covering: the MSI itself, and the service executable it installs.

```powershell
# CLI - the paths a rule has to cover
.\jt-snmpd-0.9.2-x64.msi
C:\Program Files\jt-snmpd\jt-snmpd.exe
```

For WDAC, generate a policy fragment from the installed folder and merge it into
your existing policy:

```powershell
# CLI
New-CIPolicy -Level Hash -FilePath .\jt-snmpd.xml `
  -ScanPath 'C:\Program Files\jt-snmpd' -UserPEs
Merge-CIPolicy -PolicyPaths .\existing.xml,.\jt-snmpd.xml -OutputFilePath .\merged.xml
```

Because the rule is bound to the hash, **it has to be regenerated on every
upgrade**. Treat that as part of the upgrade procedure, not as an afterthought:
a merged policy that still names the previous version will block the new
service from starting.

---

## 5. Sign it yourself

If your organisation runs an internal PKI with a code-signing template — common
in government agencies and hospitals — signing the MSI with your own certificate
is more useful than a public signature would be. It makes the file match the
WDAC and AppLocker publisher rules you already maintain, and it puts your own
name in the UAC prompt, which is a more meaningful statement to your operators
than a third party's name.

```powershell
# CLI
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
  /f your-code-signing.pfx /p <password> `
  .\jt-snmpd-0.9.2-x64.msi

signtool verify /pa /v .\jt-snmpd-0.9.2-x64.msi
```

Verify the SHA-256 against the published value **before** signing. Signing
overwrites the file, so the published hash no longer applies afterwards —
record your own hash of the signed artefact for your internal records.

---

## 6. If Defender quarantines the file

PyInstaller-produced executables are periodically flagged by heuristics, not by
signature matches. If that happens:

1. Verify the SHA-256 first, so you know you are defending the file you meant to.
2. Submit it at
   [Microsoft Security Intelligence](https://www.microsoft.com/en-us/wdsi/filesubmission)
   as a suspected false positive. Submissions are usually resolved within a few
   days and the fix reaches every Defender installation.
3. As an interim measure, add a path exclusion for
   `C:\Program Files\jt-snmpd\` — and remove it once the submission is resolved,
   since a permanent exclusion on a directory is itself a weakness.

Do not disable real-time protection as a workaround.

---

## 7. Deciding whether this is acceptable

It may not be, and that is a legitimate conclusion. Some points to weigh:

- **The source is public and the build is reproducible from it.** Releases are
  built by GitHub Actions from a tagged commit, and the workflow that produced
  each artefact is visible in the Actions log.
- **The hash chain is complete** from the release page to the installed file, as
  long as you fetch the hash from the release page.
- **What is missing is publisher identity**, and no amount of hashing supplies
  it. If your controls require a named, certificate-backed publisher, §5 is the
  route that satisfies them.
- **Building from source is supported.** If you would rather not trust a binary
  at all, `packaging/build-msi.ps1` produces the same MSI locally.

---

## Related documentation

- [Documentation home](https://jasoncheng7115.github.io/jt-snmpd/)
- [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
- [Security scanning toolchain](https://jasoncheng7115.github.io/jt-snmpd/security-scanning.html)
