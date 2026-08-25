"""jt-snmpd version — the single source of truth.

Why this is its own file: the version used to be hardcoded in jt_agent.py while
the MSI version came from a build-script argument, and nothing kept the two in
step. The observed result was an MSI at 0.1.6 whose agent still reported
`jtAgentVersion = 0.1.0-dev` over SNMP — and the only reason that OID exists is
to answer "we upgraded several hundred machines, which ones did not take?".
A version that does not match makes the feature worthless.

The build reads VERSION from here for all of:
  - jtAgentVersion, embedded in the PyInstaller output
  - the MSI's ProductVersion and file name
  - the release archive file name
"""

VERSION = "0.9.8"
BUILD_DATE = "2026-08-25"
