---
layout: default
title: Security Scan Results
description: The current scan baseline, with a verdict on every finding
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/security-report_zh-TW.html)

# Security scan results

[The toolchain document](https://jasoncheng7115.github.io/jt-snmpd/security-scanning.html)
describes what should be run. This is what **was** run, and what it found.

A report of counts alone is not worth submitting. Every finding below carries a
verdict and the reasoning for it, because the question a reviewer actually has is
not "how many" but "why is each one acceptable".

| | |
|---|---|
| Date | 2026-08-27 |
| Version | jt-snmpd 1.1.3 |
| Scope | `deploy/`, `tools/`, `packaging/` — 14 files, 4,967 lines |

---

## Summary

| Check | Tool | Result |
|---|---|---|
| Static analysis (SAST) | Bandit 1.9.4 | **HIGH 0**, MEDIUM 3, LOW 12 — all accounted for below |
| Dependency vulnerabilities (SCA) | pip-audit 2.10.1 | **0 across 70 packages** |
| Personal data and secrets | `tools/check-privacy.py` | **HIGH 0** — runs on every push |
| Test suite | pytest | 963 passed, 1 skipped — runs on every push |
| Installer artefact checks | Windows Installer tables | 5 checks — run on every push |

**The runtime dependency surface is two packages.** `pysnmp 7.1.29`, which
requires `pyasn1 0.6.4`, which requires nothing. Everything else in the 70 is
build or test tooling that never reaches a customer machine. That is deliberate:
the fewer dependencies, the shorter this section stays.

---

## Static analysis: every finding

Bandit reports no HIGH findings. The fifteen below are MEDIUM and LOW, and each
one is either a false positive or a documented decision.

**The SNMPv3 code added none of them.** `deploy/usm.py` — key localization, the
DPAPI-protected store, the algorithm allowlist — is the newest file and the one
handling secrets, and it is clean. That is worth stating rather than leaving to
be inferred from a total.

### B104 — "possible binding to all interfaces" (MEDIUM ×3)

| Location | Verdict |
|---|---|
| `deploy/jt_agent.py:3441`, `:3716` | **Accepted, by design.** The agent binds `0.0.0.0` deliberately. A bind address does not filter senders; it only chooses which local addresses receive. Source restriction is enforced twice over and in the right places: the Windows Firewall rule is scoped to the management networks, and the pre-parse gate checks the source address before pysnmp sees a byte. Binding a single address would break multi-homed hosts and would add no security. See [Security assessment §1](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html). |
| `tools/check-privacy.py:163` | **False positive.** `"0.0.0.0"` appears in a list of addresses the IP rule *excludes*, so that wildcards are not reported as leaks. |

### B105 — "possible hardcoded password" (LOW ×2)

`deploy/diskhealth.py:314` and `:317`. **False positive.** The values are the
booleans in `{"health_pass": True}`. Bandit's heuristic matches any name
containing `pass`, and `health_pass` is a SMART result, not a credential.

### B110 / B112 — try/except pass, try/except continue (LOW ×3)

| Location | Verdict |
|---|---|
| `deploy/jt_agent.py:220` | **Accepted.** Writing to the Windows Event Log can fail on permissions or an unregistered source. A monitoring agent that dies because it could not log an error is worse than one that carries on; the same message is already written to the log file. |
| `deploy/jt_agent.py:1613` | **Accepted.** The last resort inside an already-failed path: reading the engine identity threw, the outer handler has logged why, and this is the second attempt at the machine GUID. Failing here leaves `"unknown"`, which produces a volatile engine ID — worse than a stable one, and far better than no agent. |
| `deploy/diskhealth.py:413` | **Accepted.** A disk that does not answer a SMART command is skipped rather than fabricated. One unresponsive USB bridge must not remove every other disk from the snapshot. |

All three are narrow, each catches a specific expected failure, and each carries
a comment saying why.

### B404 / B603 / B607 — subprocess use (LOW ×7)

`tools/check-privacy.py`, `tools/prepare-public-repo.py` and
`tools/check-terminology.py` call `git` to list the tracked files.
**Accepted.** All three pass a fixed argument vector with no shell and no
user-supplied input, and none of them ships to a customer machine: they are
repository tooling. B607 is the same call flagged again for naming `git`
rather than an absolute path. The agent itself starts no subprocess at all,
which is a project rule.

---

## Dependency vulnerabilities

pip-audit found no known vulnerabilities across 70 installed packages, checked
against the PyPI Advisory Database and OSV.

Worth separating, because 70 overstates the exposure:

| | Packages | Reaches a customer machine |
|---|---|---|
| Runtime | `pysnmp`, `pyasn1` | Yes, inside the MSI |
| Packaging | `pyinstaller`, `pywin32` | Their output does; they do not |
| Testing and tooling | The rest | No |

---

## What is not yet run

Stating this plainly is part of the report. The toolchain document lists more
than has actually been executed:

| Item | State |
|---|---|
| Semgrep, including the project-specific rules | Not run |
| gitleaks over the full history | Not run |
| CycloneDX SBOM | Generated once; not refreshed on a schedule |
| 24-hour boofuzz against UDP/161 | Not run |
| PROTOS c06-snmpv1 | Not run |
| Windows platform checks (`signtool`, `accesschk`, PrivescCheck) | Not run as a batch; `sc qprivs` verified by hand |
| HVCI / WDAC endpoint survival | Blocked; no such endpoint available |

None of these are blocked on effort except the last. They are listed so nobody
reads the toolchain document and assumes all of it has happened.

---

## Reproducing this

```bash
pip install bandit pip-audit
bandit -r deploy/ tools/ packaging/ -f json -o reports/bandit.json
pip-audit --format json -o reports/pip-audit.json
python3 tools/check-privacy.py
python3 -m pytest tests/ -q
```

`reports/` is not published: the raw JSON carries local filesystem paths. This
page is the submittable form.

---

## Related documentation

- [Documentation home](https://jasoncheng7115.github.io/jt-snmpd/)
- [Security scanning toolchain](https://jasoncheng7115.github.io/jt-snmpd/security-scanning.html)
- [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
