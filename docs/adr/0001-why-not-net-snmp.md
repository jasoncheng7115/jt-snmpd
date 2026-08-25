---
layout: default
title: ADR-0001 Why Not Net-SNMP
description: Why the agent was written from scratch in Python rather than rebuilding Net-SNMP
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/adr/0001-why-not-net-snmp_zh-TW.html)

# ADR-0001: why this was written in Python rather than rebuilding Net-SNMP for Windows

| | |
|---|---|
| Status | Accepted |
| Date | 2026-08-24 |

## Context

The original plan rested on "there is no modern official Net-SNMP binary for
Windows". That is true, and it does not mean one could not be built: Net-SNMP's
win32 mibgroup already implements a good deal of HOST-RESOURCES and IF-MIB. So
the decision was written down and compared rather than assumed, to save having
the argument again later.

## The options

| Option | Up-front cost | Long-term maintenance | Extending with private MIBs | Fit with existing skills | Self-contained deployment |
|---|---|---|---|---|---|
| **A. Written from scratch in Python (chosen)** | High | Medium | Easy | High | Easy, through PyInstaller |
| B. Rebuild Net-SNMP with vcpkg and MSVC, filling in the mibgroup | Medium | High, tracking upstream | Requires C | Low | The C runtime has to be handled |
| C. Telegraf or windows_exporter | Very low | Low | — | — | — |

## Decision: A

### Why not C, Telegraf or windows_exporter

**LibreNMS consumes SNMP, not Prometheus or InfluxDB.** Telegraf emits line
protocol and windows_exporter emits Prometheus metrics; neither is SNMP, and
LibreNMS cannot poll either with its standard SNMP poller.

There is a second reason. Customer environments generally require that no agent
makes outbound connections of its own, and the usual deployment of both tools is
either the agent pushing or Prometheus scraping. Neither matches the model
LibreNMS uses, which is a poller pulling over SNMP.

### Why not B, rebuilding Net-SNMP

1. **Maintenance.** It means tracking upstream Net-SNMP releases indefinitely,
   and the win32 mibgroup's build chain — vcpkg plus MSVC — is fragile in CI.
2. **Extending it means writing C.** The self-health OIDs and the small
   adjustments needed for LibreNMS compatibility would all be C changes and a
   recompile, which makes every iteration slow.
3. **It does not match the skills available.** The other jt-* projects are
   Python. Rebuilding Net-SNMP calls for C, autotools and win32 build expertise.
4. **Self-contained deployment is harder.** A C binary brings MSVC runtime
   dependencies with it, where PyInstaller's one-folder output is self-contained
   by construction. That was verified on hardware at gate D.

### Why A, and what already demonstrates it

The up-front cost is real, and against it:

- **The snapshot + bisect architecture turns protocol correctness into a
  structural guarantee.** GETNEXT ordering, the absence of duplicate OIDs and a
  correct endOfMibView all follow from the array being sorted, so none of them is
  maintained by hand. Verified at gate C, with 20 tests passing.
- **Extending the private MIB is adding entries to a sorted array**, with nothing
  to compile.
- **It matches the skills the other jt-* projects are built on.**
- **PyInstaller one-folder is self-contained**, which is what the customer
  requirement of no downloads and no external dependencies actually needs.
  Verified at gate D.
- **It was demonstrated on real hardware before being committed to.** The Python
  agent was deployed to a Windows 11 machine and the production LibreNMS detected
  the OS through it and collected ports, storage, processors and disk I/O.

## Consequences

- The performance of BER in pure Python is our problem now. It was known going
  in, and the answer is in gate C: pre-encoded wire bytes, and a purpose-built
  parser to follow.
- Compatibility with LibreNMS is ours to maintain, and the rule is to fix the
  agent rather than change LibreNMS.
- In exchange: fast iteration, a self-contained artefact, a match with the skills
  available, and correctness that comes from the architecture rather than from
  vigilance.

This record exists so that neither a future version of us nor an outside reviewer
has to hold the discussion again.

---

## Related documentation

- [Documentation home](https://jasoncheng7115.github.io/jt-snmpd/)
- [Compared with the built-in SNMP Service](https://jasoncheng7115.github.io/jt-snmpd/comparison-vs-builtin-snmp.html)
- [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
