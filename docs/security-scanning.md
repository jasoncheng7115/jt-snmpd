---
layout: default
title: Security Scanning
description: Security scanning toolchain and the report a review will accept
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/security-scanning_zh-TW.html)

# Security scanning toolchain and reports

| Item | Value |
|---|---|
| Purpose | Security reviews for government and hospital tenders need **a report that can be submitted**, not a verbal assurance |

## Why ZAP does not apply

OWASP ZAP is a dynamic scanner (DAST) for **web applications** — it crawls HTTP
endpoints, injects payloads and inspects the responses. jt-snmpd **has no HTTP
interface**; its attack surface is BER-encoded packets on UDP/161. ZAP has
nothing to work with.

The right combination is three layers: **static analysis of the source (SAST) +
dependency vulnerability scanning (SCA) + protocol-level fuzzing (DAST, but the
SNMP-specific kind)**.

---

## 1. SAST — static source analysis

| Tool | Purpose | Why it is needed |
|---|---|---|
| **Bandit** | Python security anti-patterns | Catches `eval`, `subprocess(shell=True)`, hardcoded passwords, weak hashes, unsafe tempfiles, `assert` used for validation |
| **Semgrep** | Rule-based SAST (`p/security-audit`, `p/secrets`) | Deeper data flow than Bandit, and custom rules can target this project's specific hazards |
| **Ruff** (`S` rules) | A very fast flake8-bandit port | Fast enough to run on every commit; overlaps Bandit's rules but far quicker |
| **mypy** | Static type checking | Type errors are especially dangerous at the ctypes boundary — the wrong type reads or writes the wrong memory |
| **CodeQL** | GitHub's data-flow analysis | Traces "unauthenticated input → dangerous operation" across functions; the closest thing to a human review |

### Patterns specific to this project

These need custom Semgrep rules; the standard rule sets do not catch them:

```yaml
# Every ctypes foreign function must declare argtypes/restype
# Without them a 64-bit return value is truncated -- measured: drive C: showing 0 GB
- id: ctypes-missing-argtypes
  pattern: $LIB.$FUNC(...)
  pattern-not-inside: |
      $LIB.$FUNC.argtypes = ...
      ...

# OCTET STRINGs must go through octet()
# A bare rfc1902.OctetString(str) raises PyAsn1UnicodeEncodeError on non-ASCII
- id: bare-octetstring
  pattern: rfc1902.OctetString($X)
  pattern-not: rfc1902.OctetString($X.encode(...))

# Internal timing must not use the wall clock
- id: wall-clock-timing
  patterns:
    - pattern-either:
        - pattern: datetime.now()
        - pattern: time.time()
```

---

## 2. SCA — dependency scanning and SBOM

| Tool | Purpose |
|---|---|
| **pip-audit** | Python dependency CVEs against the PyPI Advisory DB and OSV |
| **OSV-Scanner** | Google's cross-ecosystem scanner; wider coverage than pip-audit |
| **CycloneDX-python** | Produces the **SBOM** a security review will ask for |
| **Trivy** | Also scans the filesystem and the SBOM, and checks for secrets in passing |

jt-snmpd has very few dependencies (`pysnmp` → `pyasn1`, plus `pywin32` and
`pyinstaller` at packaging time). That is deliberate: **fewer dependencies means
a cleaner SCA report and an easier review.**

---

## 3. Secret scanning

| Tool | Purpose |
|---|---|
| **gitleaks** | Scans the whole git history, not only the working tree |
| **detect-secrets** | Supports a baseline, which keeps false-positive fatigue down |

A project rule: keys must not appear in clear text in the configuration, the
logs, the Event Log or MSI properties. Secret scanning is the automated
enforcement of that rule.

---

## 4. DAST — protocol-level fuzzing (what replaces ZAP here)

Two are mandatory:

| Item | Tool | Pass condition |
|---|---|---|
| 24-hour fuzzing | **boofuzz**, sending malformed BER to UDP/161 | Zero crashes, zero hangs, no RSS growth |
| PROTOS c06-snmpv1 | The University of Oulu SNMP corpus | As above |

Added by this document:

| Item | Method |
|---|---|
| Pre-parse gate effectiveness | Sources outside the allow-list must get zero responses; `tests/test_preauth_gate.py` has 27 adversarial cases |
| Unauthenticated packet flood | CPU stays within budget, RSS does not grow, a legitimate manager stays inside its SLA |
| Response size | Every response under 1400 bytes, with no IP fragmentation |

---

## 5. Windows-specific checks

These are invisible to SAST tools and are certain to come up in a security
audit:

| Item | Tool or method | Why |
|---|---|---|
| **Authenticode signature** | `signtool verify /pa /v`, Sysinternals `sigcheck` | Until a certificate is in place, this verifies the result after you sign it yourself — see [Code signing](https://jasoncheng7115.github.io/jt-snmpd/code-signing.html) |
| **Unquoted service path** | `sc qc jt-snmpd`, confirming binPath is quoted | The single most commonly raised audit finding. The default install path contains a space |
| **Service account and privileges** | `sc qprivs jt-snmpd` | Confirms the privilege stripping took effect  |
| **File and directory ACLs** | Sysinternals `accesschk -d` | `C:\ProgramData` lets Users create subdirectories by default, so an attacker can get there first |
| **Weak-ACL escalation paths** | **PowerUp** / **PrivescCheck** | Purpose-built for Windows service escalation paths; covers the three items above as well |
| **DLL hijacking** | Confirm one-folder rather than one-file; watch load paths in Process Monitor | one-file extracts to `%TEMP%` and runs from there |
| **Defender / EDR false positives** | Install on a machine with Defender and HVCI enabled, and watch for quarantine | PyInstaller output has a history of false positives |
| **Memory integrity compatibility** | Enable HVCI, reboot, confirm the service still runs | Customer endpoints commonly have it on |

---

## 6. Suggested CI arrangement

```
Every commit (fast)
  ruff check --select S       # security rules
  mypy deploy/
  pytest tests/ -q

Every PR (medium)
  bandit -r deploy/ -f json -o reports/bandit.json
  semgrep --config p/security-audit --config p/secrets --json -o reports/semgrep.json
  pip-audit --format json -o reports/pip-audit.json
  gitleaks detect --report-format json --report-path reports/gitleaks.json
  cyclonedx-py env -o reports/sbom.json

Every release (slow; required before shipping)
  all of the above, plus
  boofuzz for 24 hours against UDP/161
  PROTOS c06-snmpv1
  Windows platform checks (signtool / accesschk / PrivescCheck)
  the installation test matrix
  30-day stability (major releases)
```

## 7. Report output

Every tool here can emit JSON or SARIF. Collapse them into one submittable
summary:

```
reports/
├── sbom.json                 CycloneDX SBOM (dependency list; reviews require it)
├── bandit.json               SAST
├── semgrep.json              SAST, including the custom rules
├── pip-audit.json            Dependency CVEs
├── gitleaks.json             Secret scan
├── fuzzing-summary.txt       boofuzz 24-hour result
├── windows-checks.txt        Signature / ACLs / privileges / unquoted path
└── SECURITY-REPORT.md        A human-readable summary of the above, with a pass or fail verdict
```

**The verdict rule**: SAST High and Critical findings must be zero or carry a
written exception; dependency CVEs at High or above must be zero; fuzzing must
produce zero crashes. Anything short of that does not ship (`TEST_PLAN.md` §10,
Release Gate).

## 8. Current state, and where the results are

**[Security scan results](https://jasoncheng7115.github.io/jt-snmpd/security-report.html)**
carries the current baseline with a verdict on every finding. In short: Bandit
HIGH 0, pip-audit clean across 62 packages, and a runtime dependency surface of
two packages.

Running on every push, in GitHub Actions:

| Check | Where |
|---|---|
| The full test suite | `tests.yml`, Linux |
| Personal data and secret scan | `tests.yml`, Linux |
| Executable and MSI build | `tests.yml`, Windows |
| Installer artefact checks, read from the MSI's own tables | `tests.yml`, Windows |

Run by hand, not yet in CI: Bandit and pip-audit. Not yet run at all: Semgrep,
gitleaks, the fuzzing tiers, and the Windows platform batch. The results page
lists those explicitly rather than leaving this document to imply they happened.

---

## Related documentation

- [Documentation home](https://jasoncheng7115.github.io/jt-snmpd/)
- [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
- [Code signing](https://jasoncheng7115.github.io/jt-snmpd/code-signing.html)
- [Release checklist](https://jasoncheng7115.github.io/jt-snmpd/release-checklist.html)
