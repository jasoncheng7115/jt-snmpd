# Security policy

## Reporting a vulnerability

Report privately, not as a public issue.

- **GitHub private advisory** — [Report a vulnerability](https://github.com/jasoncheng7115/jt-snmpd/security/advisories/new).
  This is the preferred route: it creates a private thread and, if the report is
  valid, becomes the published advisory.
- **Email** — jason@jason.tools. Say "jt-snmpd security" in the subject.

A first reply within **5 working days**, and an assessment within **15**. This
is a small project maintained by one person; those figures are what can be kept,
not what sounds good.

Please include enough to reproduce: the version (`jtAgentVersion`, or the entry
in Apps & Features), the Windows build, and what you sent. A `snmpwalk` or a
packet capture is worth more than a description.

## Scope

**In scope**

- The agent (`deploy/`), including anything reachable by an unauthenticated
  UDP 161 packet: the pre-parse gate, the BER decoding path, snapshot building,
  every collector.
- The installer (`packaging/`): privilege handling, the ACL it sets on the data
  directory, the service registration and its `ImagePath`, the state it records
  in order to restore the built-in SNMP Service.
- Anything that would let a query yield data the agent is documented not to
  serve.

**Out of scope**

- **The installer is not code signed.** SmartScreen warnings and an unknown
  publisher in the UAC prompt are the documented consequence, not a
  vulnerability. See
  [Code signing](https://jasoncheng7115.github.io/jt-snmpd/code-signing.html).
- **SNMPv2c has no cryptography.** A community string travels in clear text and
  anyone who can reach UDP 161 from a permitted source can read what the agent
  serves. That is the protocol. The mitigations are the source ACL and the
  read-only design; SNMPv3 is not implemented yet.
- Reports produced by a scanner with no analysis behind them.

## What the agent is built not to do

These are design commitments, so a report that one of them is false is a
vulnerability rather than a feature request:

- **Read only.** `write_variables` is never implemented. There is no SET.
- **No outbound connection, ever.** No update check, no telemetry, nothing
  fetched at runtime or at install time.
- **No subprocess on the data path.** Collectors call Win32 through ctypes.
- **Source control before parsing.** The ACL, the size cap and the rate limit
  all run before the BER decoder sees a byte.
- **Nothing measured, nothing reported.** A failed collector removes its rows
  from the snapshot rather than reporting a zero or a stale value.

## Supported versions

The latest release. This is pre-1.0 software; fixes go into a new release
rather than being backported.

## Current state, stated plainly

The measured exposure, what is deliberately withheld and what is **not** yet
mitigated are in
[Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
and the current scan results are in
[Scan results](https://jasoncheng7115.github.io/jt-snmpd/security-report.html).
Read the "not yet mitigated" section before deploying: it is there because an
honest list is more useful than a short one.
