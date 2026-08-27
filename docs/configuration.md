---
layout: default
title: Changing settings after installation
description: Changing the community, networks and SNMPv3 accounts after installation
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/configuration_zh-TW.html)

# Changing settings after installation

Every value the installer asked for can be changed afterwards without
reinstalling.

**The pattern is always: edit the file, restart the service.** Settings are read
once, at service start. That is deliberate — re-reading on every snapshot would
mean picking up a file an operator is halfway through editing.

```
sc stop jt-snmpd && sc start jt-snmpd
```

---

## 1. SNMPv2c: the community and the management networks

```
C:\ProgramData\jt-snmpd\config.json
```

The directory is restricted to SYSTEM and Administrators, so open it from an
elevated editor.

```json
{
  "schema_version": 1,
  "community": "your-community",
  "allowed_networks": ["192.168.1.0/24"],
  "port": 161,
  "enable_arp_table": false,
  "rate_pps": 50,
  "rate_burst": 300,
  "v3_only": false
}
```

| Key | Type | Default | What it does |
|---|---|---|---|
| `community` | string | as installed | The v2c community. An empty string is ignored, which leaves the built-in default — also empty — and the agent then answers no v2c at all |
| `allowed_networks` | array of strings | as installed | **The pre-auth gate's source allow-list.** A packet from anywhere else is dropped before anything is parsed. An empty array means loopback only |
| `port` | integer | 161 | 1 to 65535 |
| `enable_arp_table` | boolean | `false` | Serves `ipNetToPhysicalTable`. **That is a ready-made target list for lateral movement**, which is why it is off |
| `rate_pps` | integer | 50 | Packets per second allowed from one source address |
| `rate_burst` | integer | 300 | The burst allowance. One full walk is roughly one request per 25 varbinds, and the burst has to exceed that or ordinary polling trips the agent's own rate limit |
| `v3_only` | boolean | `false` | Refuses SNMPv2c outright. See section 3 |

**How to confirm a change took effect.** Restart the service and read the first
lines of the log:

```
C:\ProgramData\jt-snmpd\logs\jt-snmpd.log

2026-08-27 14:18:43 config loaded from C:\ProgramData\jt-snmpd\config.json:
                    community, allowed_networks(1), port, enable_arp_table, v3_only
```

**That line names the keys that were actually applied.** A key missing from it
did not take: the wrong type, a value out of range, or a misspelled name are all
skipped quietly. This line is the only place that tells you.

---

## 2. SNMPv3 accounts

**Not in `config.json`.** The keys are stored encrypted, and managed with the
CLI:

```
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user list
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user add librenms
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user remove librenms
```

Passphrases are prompted for and never accepted as arguments — an argument is
visible in the process list to every user on the machine while the command runs,
and stays in console history. For unattended use they can come from standard
input:

```
(echo auth-passphrase& echo priv-passphrase) | "C:\Program Files\jt-snmpd\jt-snmpd.exe" user add librenms
```

Algorithms are chosen with `--auth` and `--priv`; the full list and the
trade-offs are in
[SNMPv3](https://jasoncheng7115.github.io/jt-snmpd/snmpv3.html).

**A change here also needs a service restart.**

---

## 3. Refusing v2c entirely

```json
{ "v3_only": true }
```

After a restart the agent does not register v2c at all, and says so:

```
v3_only is set: SNMPv2c is not registered on this agent
```

**With `v3_only` set and no usable SNMPv3 account, the service refuses to
start**, and the log gives the reason and both ways out. That is deliberate:
listening with no way in looks healthy from Windows while answering nobody, and
the operator would go looking at the network for a fault that is in a
configuration file.

Switch in this order: **provision the v3 account and confirm the manager can
reach it, then set `v3_only`.** The other way round leaves a window where
neither works.

---

## 4. What Group Policy can and cannot override

```
HKLM\SOFTWARE\Policies\JasonTools\JTSNMPD
```

**This key controls two values only: `SysContact` and `SysLocation`.** They
override whatever was migrated from the built-in SNMP service.

| | Decided by |
|---|---|
| `SysContact` / `SysLocation` | Group Policy, then the built-in SNMP registry values |
| Community, networks, port, rate limits, `v3_only` | **`config.json` only.** Group Policy does not override these |
| SNMPv3 accounts | `secrets\usm.dat` only, managed with the CLI |

To change the community or the management networks across hundreds of machines,
redeploy the MSI with `/qn` and the new properties — the installer rewrites
`config.json`. An upgrade does not lose `index-map.json`, so LibreNMS keeps its
ports and their history.

---

## 5. What happens when it is wrong

**Malformed JSON.** The log says `config file at ... could not be read` and the
agent **starts anyway** on the built-in defaults — where the community is empty,
so in practice it stops answering v2c. **That is deliberate**: carrying on
quietly with the previous settings would let an operator believe an edit had
taken effect.

**File missing.** `no config file at ...; using built-in defaults`, as above.

**A value of the wrong type.** That one key is skipped and the rest are applied.
The `config loaded from ...` line lists what went in, so it names what did not.

**To get back to the settings the installer wrote**, run the same MSI again with
`/qn` and the original properties.

---

## 6. Worked examples

### SNMPv2c only, the common case

`C:\ProgramData\jt-snmpd\config.json`:

```json
{
  "schema_version": 1,
  "community": "mon-readonly-2026",
  "allowed_networks": ["192.168.1.0/24", "10.20.0.0/16"],
  "port": 161,
  "enable_arp_table": false,
  "rate_pps": 50,
  "rate_burst": 300,
  "v3_only": false
}
```

In LibreNMS: Devices → Add Device, SNMP Version **v2c**, community
`mon-readonly-2026`.

Check it from the LibreNMS host first:

```
snmpwalk -v2c -c mon-readonly-2026 <host> 1.3.6.1.2.1.1
```

---

### v2c and v3 together, while migrating

The configuration file is as above with `v3_only` left `false`. Add an account
on the monitored host:

```
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user add librenms
  authentication passphrase: ****************
  confirm:                   ****************
  privacy passphrase:        ****************
  confirm:                   ****************

added librenms (SHA-256 + AES-128).
Only the localized keys were stored; the passphrases were not.

sc stop jt-snmpd && sc start jt-snmpd
```

Both now answer. **This is where to sit during a migration**: the existing
monitoring keeps working while the new path is verified host by host.

Confirm v3 reaches it before changing anything in LibreNMS:

```
snmpwalk -v3 -l authPriv -u librenms \
  -a SHA-256 -A '<auth passphrase>' \
  -x AES     -X '<privacy passphrase>' \
  <host> 1.3.6.1.2.1.1
```

Then switch the device: its page → gear → Edit → SNMP

| Field | Value |
|---|---|
| SNMP Version | v3 |
| Auth Level | authPriv |
| Auth User Name | `librenms` |
| Auth Password | the authentication passphrase |
| Auth Algorithm | SHA-256 |
| Crypto Password | the privacy passphrase |
| Crypto Algorithm | AES |

**Switching does not make LibreNMS rediscover the device.** Ports, storage and
sensors keep their existing entries, and their history with them.

---

### SNMPv3 only, refusing v2c

**The order matters: confirm v3 works, and set `v3_only` last.**

```json
{
  "schema_version": 1,
  "community": "",
  "allowed_networks": ["192.168.1.0/24"],
  "port": 161,
  "enable_arp_table": false,
  "rate_pps": 50,
  "rate_burst": 300,
  "v3_only": true
}
```

After a restart the log reads:

```
v3_only is set: SNMPv2c is not registered on this agent
SNMPv3 user 'librenms' registered (SHA-256 + AES-128)
LISTENING 0.0.0.0:161 varbinds=652
```

Verify that v2c is refused and v3 is not:

```
snmpget -v2c -c anything <host> 1.3.6.1.2.1.1.5.0
  → Timeout: No Response

snmpget -v3 -l authPriv -u librenms -a SHA-256 -A '...' -x AES -X '...' <host> 1.3.6.1.2.1.1.5.0
  → STRING: "WIN11-PRO-1"
```

**With `v3_only` set and no account, the service refuses to start** and says so:

```
ERROR refusing to start: v3_only is set but no SNMPv3 user could be loaded,
so nothing could authenticate. Provision one with `jt-snmpd.exe user add <name>`,
or clear v3_only in config.json to serve v2c again
```

---

## Related

- [SNMPv3](https://jasoncheng7115.github.io/jt-snmpd/snmpv3.html)
- [Naming and paths](https://jasoncheng7115.github.io/jt-snmpd/naming-and-paths.html)
- [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
- [Manual removal](https://jasoncheng7115.github.io/jt-snmpd/manual-removal.html)
