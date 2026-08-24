# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

繁體中文版：[CHANGELOG_zh-TW.md](CHANGELOG_zh-TW.md)

---

## [0.9.3] - 2026-08-24

### Fixed

- **The graphical installation could not install anything.** Double-clicking the
  MSI raised "the management networks must be specified" on the Welcome page,
  with no way forward, while the page that asks for the management networks sat
  two clicks further on. The cause is an ordering property of Windows Installer:
  `LaunchConditions` runs at the very start of the InstallUISequence, before any
  dialog is shown, so a launch condition that depends on a property the wizard is
  meant to collect can never be satisfied in a wizard install. The condition now
  exempts `UILevel > 4`, which is the full wizard and the only level where a page
  exists to ask; every quieter level (`/qn`, `/qb`, `/qr`) still stops, and the
  settings page enforces the same requirement itself by refusing to advance.

  Nothing in the suite could have caught this: the WiX source was valid, the
  build succeeded, `/qn` installs worked, and the lifecycle test drives `msiexec
  /qn` throughout. Every gate was green while the path an operator uses first was
  completely broken. `tests/test_msi_ui_gating.py` now pins the shape of the fix,
  including that the dialog still refuses to advance without a value and that the
  configure script still fails closed.
- **`docs/naming-and-paths.md` named three files that do not exist**:
  `config.yaml` (it is `config.json`), `engine-state.json` (`engine.json`) and
  `ms-snmp-migration.json` (`ms-snmp-restore.json`). The layout is now what is
  actually on disk, with the planned-but-absent entries marked as such.

---

## [Unreleased]

### Added

- **A manual removal document** (`docs/manual-removal.md`), for when the
  installer cannot finish: a rolled-back installation, an uninstall that reports
  success while the service is still running, or a machine where the product is
  gone from Apps & Features but UDP/161 still answers. Each step is the manual
  equivalent of something the installer does, in the order that keeps the
  built-in SNMP Service restorable — the record saying what it used to be lives
  in the data directory, so deleting that first throws away the only copy.

- **A "manual trust" section in the code-signing document**, covering both the
  single-machine route (SmartScreen "Run anyway", `Unblock-File`, allowing a
  quarantined file in Defender) and the fleet route (deploy from an internal
  share, or sign with your own certificate and push it to Trusted Publishers by
  Group Policy, which settles every other prompt at once).
- **What the withheld OIDs would actually buy you**, checked against the
  LibreNMS source rather than assumed. Of the four categories held back, three —
  installed software, running processes, and the connection tables — have **no
  consumer in LibreNMS at all**; publishing 2,727 OIDs of vulnerability and
  connection data would produce no page and no graph. Only ARP is consumed
  (`LibreNMS/Modules/ArpTable.php` → `ipv4_mac` → ARP and FDB search), and it is
  already implemented behind `enable_arp_table`.
- **System graphs and volume-label encoding** added to the comparison. Windows
  shows three System graphs where Linux shows eight, because the other five come
  from UCD-SNMP-MIB, which the built-in service does not implement. Non-ASCII
  volume labels are a real failure mode rather than a nicety: pysnmp raises
  `PyAsn1UnicodeEncodeError` on them, and the whole snapshot fails to build.

### Changed

- **The README's install section was still the pre-MSI one.** It led with
  `install.ps1` and a ZIP archive that releases no longer ship, and the status
  table said the MSI for GPO / Intune / SCCM was "not implemented" when it has
  been released and lifecycle-verified since 0.9.0. Both READMEs now lead with
  downloading the MSI, and cover the graphical and command-line paths, GPO
  deployment, and uninstall.
- **VACM is explained** rather than left as an acronym in a status table: it is
  RFC 3415's View-based Access Control Model, restricting which parts of the OID
  tree a set of credentials can reach.

- **Code signing is planned, not abandoned.** A certificate through an
  open-source code-signing programme is intended; until it arrives the documents
  say what you will see and how to get past it safely.
- **Published files no longer cite internal documents.** Every reference to the
  internal specification and to the internal working notes has been replaced by
  the substance it was pointing at. A citation a reader cannot follow is worse
  than no citation. The Phase 0 gate report is no longer published: it is
  structured entirely around the internal specification's section numbering.
- **The `msiexec` line is one line.** It is short enough that the caret
  continuations were noise.
- **The Proxmox breadcrumb is gone from the SMART screenshot.** It was an
  unrelated application LibreNMS had discovered on the control host, and next to
  a SMART comparison it read as part of the comparison.

### Fixed

- **The code-signing document named the wrong install directory.** It said
  `C:\Program Files\jt-snmpd\`; the MSI installs to `C:\Program Files\JT SNMP
  Agent\`, so the WDAC scan path and the Defender exclusion path were both
  wrong.

- **`prepare-public-repo.py` kept only `README.md` from `dist/` and `build/`**,
  so the Traditional Chinese READMEs in both had never been published.
- **More terminology**: privilege stripping is 縮減 rather than 剝除, blocked is
  受阻, process is 處理程序, installer is 安裝檔, thermal zone is 溫度區 (with an
  explanation of what one is, since the literal translation explains nothing),
  and Mark of the Web is 網頁標記. Absolute phrasing (絕不, 永不) has been
  replaced with plain description, and the em dash is gone from Chinese text.

### Added

- **Bilingual documentation.** Every published document now exists in both
  English (`docs/<name>.md`) and Traditional Chinese (`docs/<name>_zh-TW.md`),
  with a language switch and a link back to the documentation home at the top of
  each page, and a related-documents list at the bottom. Until now the English
  README and the English project site linked to documents that only existed in
  Chinese, which for most readers is a dead end.
- **`docs/code-signing.md`** (and the Chinese version), covering what an
  unsigned installer actually looks like at install time — the SmartScreen
  prompt, the unknown publisher in the UAC dialog, what GPO deployment sees
  (nothing), and what WDAC and AppLocker do — together with the ways to handle
  it: verifying the published SHA-256, clearing the Mark of the Web, adding a
  WDAC hash rule, and signing with your own certificate.
- **Download links and a GPO note in the install section** of the project site.
  The page showed an `msiexec` command with nowhere to get the MSI from, and did
  not say that the same command line is what Group Policy software deployment
  uses.

### Changed

- **No code-signing certificate will be sought.** Everywhere that previously said
  a SignPath Foundation application was pending now states plainly that the
  installer is unsigned, that this is the permanent state, and where to read
  about handling it. An indefinite "coming soon" is worse than a clear no: it
  leads deployers to wait for something that is not coming.
- **Release notes are English first, Chinese second**, and every line is a whole
  sentence rather than a hard-wrapped fragment — GitHub renders the notes as
  Markdown, so a break mid-sentence became a break in the rendered page.
- **"Attack surface analysis" renamed to "Security assessment"**, which is what
  the document is: measured exposure, the mitigations, and an honest list of what
  is not mitigated.
- **Measurement metadata moved from blockquotes into tables.** Consecutive lines
  in a blockquote are joined into one paragraph when rendered, so three separate
  facts ran together into an unreadable line.
- `.github/workflows/release.yml` converted to English, finishing the conversion
  of comments and identifiers begun in `deploy/` and `packaging/`.

### Fixed

- **Terminology that is not Taiwanese usage**, corrected against Microsoft's
  Traditional Chinese terminology: filter driver is 篩選器驅動程式 rather than
  過濾驅動, tunnel is 通道, instance is 執行個體. Full-width slashes between
  words were replaced with a spaced half-width slash, which is what Taiwanese
  technical writing uses.

---

## [0.9.2] - 2026-08-24

### Added

- **Graphical installation.** Double-clicking the MSI used to install silently
  with no opportunity to supply the management networks or the community string,
  which meant it failed with "no community could be determined". There is now a
  settings page between the install directory and the confirmation, and it will
  not continue without a management network — an empty list means the agent
  answers only loopback, which is installed but not monitoring. Silent
  installation is unchanged: the UI sequence does not run under `/qn`, and both
  paths read the same properties.

### Fixed

- **The Add/Remove Programs entry had no icon.** `ARPPRODUCTICON` pointing at the
  Icon table is the documented approach and it did not work here — the property
  was present, the Icon table entry was present, and the registry value was still
  empty. Re-encoding the .ico without PNG-compressed entries made no difference
  either. `DisplayIcon` is now written directly, pointing at the installed
  executable, which carries the icon anyway.

- **`build-msi.ps1` could ship stale code.** It packaged whatever was already in
  `build/` without checking its age, so a fix that was never rebuilt shipped
  inside an MSI carrying the new version number, a fresh SHA-256 and its own
  archive directory. It also never checked WiX's exit code, so a failed build
  fell through to the previous MSI and reported success with the old version.
  Both are now gates, and `tests/test_build_gates.py` keeps them there — this is
  the third time an artefact has been shipped under a label it did not earn.

### Changed

- Enabling SMART in LibreNMS is documented from the web interface first — gear
  icon, Settings, Discovery, Discovery Modules, `applications` — with the `lnms`
  command kept as the alternative.

## [0.9.1] - 2026-08-24

### Fixed

- **The agent never read its configuration file.** The installer collected the
  community string and the management networks, validated them, and wrote them
  to `config.json`. The agent declared `CFG_PATH` pointing at `config.yaml` — a
  different file — and never opened either one. Every installation ran on the
  defaults compiled into the source.

  Those defaults were `community="mon2"` and
  `allowed_networks=("192.168.1.0/24",)`: exactly the values the development lab
  used, which is why months of testing never noticed. Install with anything else
  and the loopback health check queries with the operator's community, the agent
  answers on a different one, the check times out, and MSI rolls the whole
  transaction back with error 1603. The failure was total, and still invisible,
  because the one configuration that worked was the only one ever tried.

  The agent now loads `config.json` at startup, before its own entry point reads
  any setting — loading it later would be the same bug, since the values are
  passed as arguments and were already bound. Both defaults are empty: a missing
  community refuses to serve rather than inventing one, and the file is read with
  `utf-8-sig` because PowerShell and Notepad both write a BOM.

  Settings can now be changed the way the documentation always implied: edit
  `C:\ProgramData\JT-SNMP\config.json`, restart the service.

- **An unconfigured source ACL allowed every source.** The pre-auth gate treated
  an empty network list as "no filtering". While the installer was the config's
  only author that state was unreachable, but editing the file by hand is now a
  supported workflow, and an emptied list would have silently exposed the agent
  to the whole network. It now denies everything except loopback, which stops
  monitoring visibly instead of over-sharing quietly. To serve every source
  deliberately, list `0.0.0.0/0`.

### Added

- **Project icon** — an OID tree, since a hierarchy of object identifiers is what
  SNMP fundamentally is. Drawn at a single stroke weight so it survives 16 px in
  a browser tab and a Windows service list. It replaces the blank placeholder
  that made the Add/Remove Programs entry look like a half-finished install.

- **CI** — tests run on Linux and Windows; a tagged push builds the MSI and
  publishes the release. Failures are surfaced as workflow annotations because
  GitHub's run logs need authentication to read, and "exit code 1" is not a
  diagnosis. The Linux job installs net-snmp and then asserts the protocol
  correctness tests actually ran, so they cannot quietly skip.

## [0.9.0] - 2026-08-24

### Added

- **Disk health status in LibreNMS** — the SMART application now shows
  `PhysicalDrive0 (OK)` / `(FAIL)` / `(Overheating)` next to each drive. The
  verdict comes from ATA `SMART RETURN STATUS` (0xDA) — the same thing
  `smartctl -H` reports — or from the NVMe critical-warning bitmap, never from
  guessing at attributes: zero reallocated sectors does not mean healthy, since
  the firmware may already be predicting failure on some other attribute, and a
  handful of reallocated sectors is normal on some models. When the drive does
  not answer at all (USB bridges commonly do not pass SMART commands through)
  the key is omitted, so LibreNMS shows nothing rather than a fabricated `(OK)`.

- **`jtDiskHealthTable`** in the private OID subtree — one state value per disk
  (ok / warning / critical / unknown) for anyone who wants to alert on it
  directly. Note that a green/red indicator on the LibreNMS *device overview*
  page is not achievable without adding a discovery definition to the LibreNMS
  server, which this project deliberately does not require.

- **Privacy tooling for publishing** — `tools/check-privacy.py` scans exactly the
  files git would push for keys, passwords, community strings, MAC addresses,
  addresses and serial numbers, and `docs/release-checklist.md` documents the
  process. Images are handled by review-and-hash rather than pattern matching,
  because a regular expression cannot read pixels: the first set of README
  screenshots carried four MAC addresses and six neighbouring device names out
  with them, which is an internal network map.

- **Disk SMART over SNMP (`NET-SNMP-EXTEND-MIB`)** — LibreNMS reads SMART through
  its `smart` application, and that application is fetched **entirely over SNMP**
  (`snmp_get nsExtendOutputFull."smart"`). No LibreNMS agent, no `smartctl`, no
  script on the monitored host. jt-snmpd already read the SMART attributes
  natively through IOCTLs; it now serialises them into the JSON that application
  expects. Verified on a Dell Latitude E5270: reallocated sectors 0, wear
  levelling 4, UDMA CRC errors 0, temperature 33 °C, power-on hours 491, written
  into `app-smart-*.rrd`.

  The payload is `base64(gzip(json))` — a form `json_app_get()` explicitly
  supports, and a necessary one: responses are capped at 1400 bytes and never
  fragmented, so uncompressed JSON would overflow at two disks. Attributes that
  were not measured are `null`, never `0`; a fabricated zero in "reallocated
  sectors" reads as "this disk is healthy".

  **This requires `discovery_modules.applications` to be enabled in LibreNMS** —
  it is `false` by default. Without it the extend data is served but never
  collected.

- **Disk maximum temperature** (`max_temp`) — LibreNMS's SMART application renders a
  "Max Temp(C)" panel whether or not the data exists, so without this key every
  installation showed a broken graph. Windows' storage APIs expose thresholds
  (warning, critical) but not a lifetime maximum, and using a threshold there
  would mislabel the line, so jt-snmpd records the highest temperature it has
  actually observed, persisted across restarts and written only when the maximum
  rises — a snapshot rebuilds every five seconds, and writing every time would be
  seventeen thousand needless disk writes a day.

- **Comparison screenshots** in `docs/images/`, captured from a production
  LibreNMS in both English and Traditional Chinese, in the light theme: sensors,
  SMART, ports and memory, each showing the same page for a Windows 10 host
  running the built-in SNMP Service and one running jt-snmpd.

- **ACPI thermal zone temperature** — a system/mainboard temperature that needs no
  kernel driver, read through `advapi32!WmiOpenBlock` + `WmiQueryAllDataW`
  (the WMI data-block API, not WMI COM, and no subprocess). Measured 25 °C with a
  107 °C critical trip point on physical hardware; virtual machines report
  `ERROR_WMI_GUID_NOT_FOUND` and the sensor simply does not appear.

  CPU package temperature remains out of reach and will stay that way: it
  requires MSR access, which requires a kernel driver. The driver everyone uses
  for this (WinRing0) is on Microsoft's vulnerable-driver blocklist and will not
  load under HVCI/WDAC — precisely the configuration our customers run.

- **CPU frequency sensor** (`entPhySensorType = hertz`, `mega` scale) — one sensor
  rather than one per logical processor, because `CallNtPowerInformation` reports
  a package-level P-state and every core returns the same value. Note that
  LibreNMS currently drops these: `entity-sensor.inc.php` maps `hertz` to the
  class `freq`, but the valid class in `LibreNMS/Enum/Sensor.php` is `frequency`.
  The same defect affects `cisco-entity-sensor.inc.php` and `openbsd.inc.php`.
  The OID is correct per RFC 3433 and readable by `snmpwalk`; the graph will
  appear once LibreNMS fixes the mapping.

- **Battery state** in the private OID subtree (charge percent, AC line status,
  estimated runtime) via `GetSystemPowerStatus`. Kept private deliberately:
  LibreNMS's entity-sensor table has no mapping for charge or percent, so
  publishing it as a standard sensor would produce nothing.

- **`SNMP-FRAMEWORK-MIB` engine group** (`snmpEngineID`, `snmpEngineBoots`,
  `snmpEngineTime`, `snmpEngineMaxMessageSize`) — this fixes a false "Device
  rebooted" alert that would otherwise fire on every host after 497 days of
  uptime. `sysUpTime` is `TimeTicks`, so it wraps at 2^32 hundredths of a second
  ≈ 497.1 days; that is mandated by RFC 3418 and the built-in Windows SNMP
  Service wraps too. What is fixable is the consequence: LibreNMS takes
  `max(sysUpTime/100, snmpEngineTime, hrSystemUptime/100)`, and `windows.yaml`
  disables only `hrSystemUptime`. `snmpEngineTime` counts in seconds up to
  2147483647 (≈ 68 years), so once the wrap happens the maximum keeps rising and
  the reboot test never trips.

- **Log rotation and Windows Event Log integration.** The agent log had no size
  limit; a repeated snapshot failure writes a line every five seconds, which is
  seventeen thousand lines a day. Across hundreds of hosts over several years a
  monitoring agent filling the system drive of the host it monitors is the least
  acceptable failure there is. Errors now also reach the Event Viewer, where
  field staff look first and where `Get-WinEvent` can collect them centrally.

- **Full lifecycle test** (`tests/lifecycle.ps1`) — install, upgrade, uninstall,
  reinstall and PURGE uninstall, 40 assertions, run against the packaged MSI on
  real hardware.

### Fixed

- **The service reported `Running` after its worker thread had died.** `SvcDoRun`
  waited on the stop event alone, so a failure during start-up — a bind failure, a
  MIB load failure, a snapshot build failure — left the Service Control Manager
  reporting a healthy service with nothing listening. The Service Control Manager
  saying `Running` while the monitoring system reports a timeout is the hardest
  state to diagnose in the field, and it also meant the configured three-stage
  automatic recovery never triggered, because the process never exited.

- **Uninstalling after an upgrade left the built-in SNMP Service disabled
  forever.** The configuration script re-read the current state of the built-in
  service on every run and overwrote its restore record. On first install that
  state is genuine; on upgrade the service has already been disabled by the
  previous install, so `Disabled` was written back as though it were the original
  setting, and the uninstall guard `$orig -ne 'Disabled'` then never fired.
  Install → uninstall restored correctly; install → upgrade → uninstall did not,
  and upgrading is the normal operation for this product.

- **`PURGE=1` left the data directory behind.** The custom action's own log file
  lives inside the directory it was deleting, so the two closing log lines
  recreated `logs\`. Deletion failures were also swallowed by
  `-ErrorAction SilentlyContinue` and reported as success.

- **Built-in SNMP shutdown was assumed rather than verified.** Group Policy or
  third-party management can block it; the install now confirms the service is
  actually stopped and disabled and fails with an explanation if it is not,
  instead of continuing to a health-check timeout with no visible cause.

- **`CallNtPowerInformation` buffer sizing.** The prototype used `os.cpu_count()`,
  which only reflects the caller's processor group; on machines with more than 64
  logical processors the kernel would write past the end of the allocation.
  Buffers are now sized with `GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)`.
  ctypes is exactly where Python's memory safety stops applying.

### Changed

- Disk sensor labels keep the native Windows name (`PhysicalDrive0 Temp` rather
  than `Drive0 Temp`).
- SMART attribute IDs are preserved as read, not only the ones we had names for.
  LibreNMS wants IDs 10, 183, 184, 188, 196 and 199, none of which were named.
- All firmware-supplied buffers are now parsed defensively, with the parsers
  separated from acquisition as pure functions so they can be tested against
  hostile input on Linux. Lengths and offsets in a WMI data block come from the
  block itself; an implausible instance count is a self-inflicted denial of
  service on a host we promised not to slow down.


### Added

- **UCD-SNMP-MIB `systemStats`** — this is what fills the LibreNMS System graph
  group. Windows hosts previously showed only three graphs (Processes, Users,
  Uptime) because those come from HOST-RESOURCES; everything else on a Linux
  device — Detailed Processor Usage, Context Switches, Interrupts, I/O, Swap I/O —
  comes from UCD-SNMP-MIB. Now sourced from `NtQuerySystemInformation`
  (`SystemPerformanceInformation` and per-CPU times). Five new graphs appear

- **`hrFSTable`, `hrPartitionTable` and `ipRouteTable`** — found genuinely missing
  while building the side-by-side comparison against the built-in SNMP Service.
  File systems and partitions come from `GetVolumeInformationW`, routes from
  `GetIpForwardTable2`. None of these carry the information-disclosure concerns
  that keep the software and connection tables off by default
- **Comparison document** (`docs/comparison-vs-builtin-snmp.md`) measuring
  jt-snmpd against a Windows 10 host still running the built-in SNMP Service,
  table by table, with an explanation for every place jt-snmpd reports less

- **MSI installer (WiX v5)** — this is what makes Group Policy deployment possible;
  GPO software installation only accepts MSI. Verified end to end on Windows 11:
  silent install (`msiexec /qn`), **upgrade by simply installing the newer MSI**
  (0.1.0 → 0.1.1, one entry in Add/Remove Programs, `index-map.json` byte-identical
  so LibreNMS does not re-discover ports), uninstall restoring the built-in SNMP
  Service and keeping configuration and state, and reinstall. A failed loopback
  health check rolls the whole transaction back

- **README** in English and Traditional Chinese, following the jt-ipam layout
- **Security scanning toolchain** documented in `docs/security-scanning.md`, with a
  first baseline: Bandit HIGH=0, pip-audit clean across 59 dependencies, CycloneDX
  SBOM generated. ZAP is not applicable — it is a web DAST and this agent has no
  HTTP surface; the correct combination is SAST + SCA/SBOM + protocol fuzzing plus
  Windows-specific checks (Authenticode, unquoted service path, `sc qprivs`,
  `accesschk`, PrivescCheck)
- **Three-branch `sysObjectID`** with domain-controller detection via
  `DsRoleGetPrimaryDomainInformation`. LibreNMS uses the third branch to call
  `getDatacenterVersion()`, so a DC previously reported the wrong Windows version
- **Windows Server scenarios** enumerated in `TEST_PLAN.md` §5.5 — 22 items across
  version/install type, Server-specific data sources and deployment differences

- **IP address tables**: `ipAddrTable` (RFC 1213) and `ipAddressTable` (IP-MIB,
  IPv4 + IPv6) via `GetUnicastIpAddressTable`, feeding the LibreNMS
  ipv4-addresses and ipv6-addresses modules
- **Neighbour cache** (`ipNetToPhysicalTable`, ARP and IPv6 ND) via `GetIpNetTable2`.
  **Disabled by default**: an internal ARP table is a
  ready-made lateral-movement target list
- **Disk temperature and health** (ENTITY-SENSOR-MIB `entPhySensorTable`) via
  `IOCTL_STORAGE_QUERY_PROPERTY` with `StorageDeviceTemperatureProperty` and the
  NVMe SMART health log. This deliberately avoids
  LibreHardwareMonitor, whose WinRing0 driver is on the Microsoft vulnerable
  driver blocklist and triggers Defender on HVCI endpoints

- **Complete memory reporting via `GetPerformanceInfo`**: in addition to Physical
  and Virtual Memory, the agent now reports **Cached Memory**, **Swap Space**
  (the page-file portion of the commit limit, which is a different concept from
  commit charge) and the kernel paged / non-paged pools.
  LibreNMS now shows four memory pools instead of two
- **Real volume labels and serial numbers** in `hrStorageDescr` via
  `GetVolumeInformationW`, replacing a hard-coded placeholder. Non-ASCII labels
  (for example a Traditional Chinese volume name) are encoded as UTF-8 and
  verified end to end through LibreNMS

- **`sysContact` / `sysLocation` configuration sources**: values are resolved with
  ADMX policy taking precedence over the existing Windows SNMP Service registry
  settings. Customers already running the built-in SNMP
  service do not have to re-enter these when switching over — the settings are
  picked up automatically, even after the built-in service has been disabled,
  because its registry keys remain. `jtAgentConfigSource` reports which source won
- **`build/` and `dist/` directories**: `build/` holds the PyInstaller one-folder
  output (executables), `dist/` holds release artefacts (MSI and friends).
  Both keep only their README under version control

- **Complete `hrSystem`**: added `hrSystemProcesses` (the source for the LibreNMS
  System → Processes graph), `hrSystemDate` (RFC 2579 DateAndTime binary format
  including time zone), and `hrSystemInitialLoadDevice` / `hrSystemInitialLoadParameters`
- **Network protocol statistics** (the LibreNMS Netstats graph set): the `ip`,
  `icmp`, `tcp` and `udp` groups, all sourced from iphlpapi via
  `GetIpStatisticsEx` / `GetIcmpStatistics` / `GetTcpStatisticsEx` /
  `GetUdpStatisticsEx`, each returning a whole counter set in one call
- **SNMPv2-MIB `snmp` group**: the agent's own packet statistics, which also serve
  as the external view of pre-authentication gate drop counts

- **Complete inventory**:
  - **ENTITY-MIB `entPhysicalTable`** (LibreNMS Inventory page), sourced by parsing
    SMBIOS via `GetSystemFirmwareTable('RSMB')` — no WMI and no special privileges
    required. Covers Type 0 BIOS, Type 1 System, Type 2 Baseboard,
    Type 4 Processor and Type 17 Memory Device, using the segmented index layout
    (1000 system / 1100 mainboard / 2000+ CPU / 3000+ DIMM / 4000+ disks)
  - **Full `hrDeviceTable` family** (LibreNMS Devices page): processors, network
    interfaces and physical disks, with `hrProcessorTable`, `hrNetworkTable` and
    `hrDiskStorageTable`. All derived tables share one `hrDeviceIndex` space
  - **Physical disk inventory**: model, serial and bus type via
    `IOCTL_STORAGE_QUERY_PROPERTY`; capacity via `IOCTL_DISK_GET_DRIVE_GEOMETRY_EX`
  - Hardware inventory is cached permanently  — SMBIOS does not change
    after boot

- **Pre-authentication gate** : four checks that run before pysnmp sees any bytes — source IP allow-list,
  packet size limit, per-source token bucket rate limiting, and a coarse outer-TLV
  sanity check. Dropped packets **never reach the BER decoder**, so deeply nested
  structures, oversized length fields and OID amplification cannot touch pyasn1

- **Self-health OIDs** : this agent fails
  silently, so these OIDs let LibreNMS monitor the agent itself. They cover
  version, service uptime, RSS, thread and handle counts, snapshot age and build
  time, configuration paths, and a security warning summary
- **`jtAgentCollectorTable`**: per-collector status, time since last success,
  duration, cumulative error count and last error message
- **Collector health tracking**: every collector is wrapped by `_collector()`,
  returning its default instead of raising, so a single failing collector cannot
  bring the agent down

- **Project named `jt-snmpd`**; service name, executable name and installation
  paths finalised (`docs/naming-and-paths.md`)
- **Snapshot + bisect architecture**: the entire MIB is a single OID-sorted array;
  GET uses `bisect_left`, GETNEXT uses `bisect_right`. SNMP protocol correctness
  becomes a structural guarantee rather than something maintained by hand
- **Wire pre-encoding**: BER bytes are produced when the snapshot is built, so
  assembling a response degenerates to byte concatenation
- **IF-MIB** (ifTable + ifXTable with 64-bit counters), **HOST-RESOURCES**
  (hrStorage / hrProcessor / hrDevice), and **UCD-DISKIO**
- **Interface filtering**: only physical adapters are exported; WFP filter drivers,
  VPN virtual adapters, tunnels and loopback are excluded
- **Persistent ifIndex** keyed by NET_LUID, so LibreNMS does not rebuild ports and
  orphan their RRDs after a reboot
- **Windows service**: packaged with PyInstaller one-folder as `jt-snmpd.exe`,
  acting as its own service host. Starts at boot as LocalSystem with no Python
  dependency on the target machine
- **`--selftest` build gate**: after building, the executable initialises a real
  SNMP engine and builds a snapshot, catching "executable produced but data files
  missing" situations
- **Reduced process priority**: the service runs at `BELOW_NORMAL_PRIORITY_CLASS`
- **Build script** `packaging/build-exe.ps1`: single source of truth for build
  parameters, with handle-release verification and artefact freshness checks
- **Tests**: BER size cross-check (540 cases), walk correctness (20 cases),
  base OID values against their RFCs (10 cases)

### Fixed

- **UCD `systemStats` field numbers were assigned from memory and were wrong.**
  The real order is IOSent(57) / IOReceived(58) / Interrupts(59) / Contexts(60) /
  SwapIn(62) / SwapOut(63); I had guessed SwapIn/SwapOut first. Context switches
  were therefore plotted as I/O. Nothing about this is visible from the agent side —
  the walk succeeds, the graphs draw, the numbers move. It only shows up when the
  output is resolved through the MIB (`snmpwalk -m UCD-SNMP-MIB -O QUs`).
  `tests/test_ucd_field_numbers.py` now pins every field to its MIB name

- **`ipRouteTable` produced duplicate OIDs on multi-homed hosts.** RFC 1213 indexes
  that table by destination address alone, but every NIC contributes its own
  224.0.0.0 multicast and 255.255.255.255 broadcast route. On a laptop with seven
  addresses this tripped the duplicate-OID guard and the agent refused to start —
  which in turn made the MSI health check fail and roll the install back. Routes are
  now deduplicated by destination, keeping the lowest-metric entry (the one actually
  selected). This never reproduces on a single-NIC machine

- **`hrSystemNumUsers` returned a hard-coded 1.** On a Remote Desktop Session Host
  that is simply wrong — one machine may have dozens of users. Now enumerates real
  sessions via `WTSEnumerateSessions`, counting Active and Disconnected states
  (a disconnected user is still logged in and still holding resources)
- **NIC team members were exported alongside the team interface**, so LibreNMS
  counted the same traffic twice. Team members report
  `ConnectionType = Passive` and are now excluded

All of the following were found and fixed during deployment on real hardware:

- **Service reported Running but no socket was bound**: pysnmp's
  `open_server_mode()` must be called from within a running event loop, otherwise
  the socket is never actually bound
- **64-bit return values truncated**: without `argtypes`/`restype` declarations,
  ctypes treats Win32 return values as `c_int`, which reported the C: drive as
  0 GB and overflowed uptime beyond 24.8 days
- **pywin32 service class must live at module level**: defining it inside a
  function yields `AttributeError: module has no attribute`, and the service
  fails to start without writing any log entry
- **Incorrect ifXTable OID**: `1.3.6.1.31.1.1.1` was missing `2.1`, placing the
  whole table on an invalid branch. LibreNMS relies on this table when
  `ifname: true`, so the Ports page lost both names and 64-bit counters
- **Non-ASCII OCTET STRING encoding failure**: pyasn1 encodes strings as latin-1
  by default, so a Traditional Chinese adapter name raises
  `PyAsn1UnicodeEncodeError`
- **Unquoted paths containing spaces are truncated**: the default installation
  path `%ProgramFiles%\JT SNMP Agent\` contains a space, and without quoting the
  process fails to start with no log output
- **PowerShell scripts require a UTF-8 BOM**: Windows PowerShell 5.1 reads `.ps1`
  files using the system ANSI code page when no BOM is present, which corrupts
  non-ASCII comments and breaks parsing
- **Build artefact freshness misjudged**: treating "the executable exists" as
  build success picks up a stale binary when the build actually failed
- **Loaded images cannot be deleted**: Windows returns access-denied for `.pyd`
  and `.dll` files already loaded as images, even after the service is stopped
  and unregistered. Renaming is used instead

### Performance

- MIB lookup: **8 µs per varbind**
- Response assembly: **164 → 0.35 µs per varbind** using wire pre-encoding
- Full request path: **18.3 µs per varbind**
- **Host impact** under stress at roughly 7,000x the real polling rate:
  degradation of a fixed workload dropped from **4.19% to 0.41%** after lowering
  process priority
- Memory: RSS grew 0.12 MB across 1,406 complete walks; thread and handle counts
  remained flat

### Known limitations

- Not yet verified: multi-homed source address selection (the test machine has a
  single NIC), HVCI/WDAC endpoints, Authenticode signing
- Not yet implemented: SNMPv3, pre-authentication gate, VACM presets,
  self-health OIDs, MSI installer
