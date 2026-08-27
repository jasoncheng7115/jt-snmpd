---
layout: default
title: SNMPv3
description: Authenticated, encrypted polling - how to provision it and what it costs
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/snmpv3_zh-TW.html)

# SNMPv3

> **Status: verified.** Checked on the wire against net-snmp, which is what
> LibreNMS polls with, and then on four real machines: Windows 10, Windows 11,
> Server 2016 (a domain controller) and Server 2022. The three of them that a
> production LibreNMS monitors were switched from v2c to v3, and none was
> rediscovered — ports, storage and sensors all kept their existing entries.

SNMPv2c sends its community string in clear text and authenticates nothing.
Anyone who can see the traffic can read it, and anyone who can guess the
community can poll the host. SNMPv3 fixes both: every request is authenticated
with an HMAC, and with `authPriv` the contents are encrypted as well.

jt-snmpd serves v3 **beside** v2c rather than in place of it, so upgrading does
not take an existing deployment off the map.

---

## 1. Provisioning an account

Passphrases are prompted for. They are never accepted as command-line
arguments, because an argument is visible in the process list to every user on
the machine while the command runs, and it lands in console history. For the
same reason the installer does not take keys as MSI properties: those end up in
the msiexec log and in Event IDs 1033 and 11707.

From an elevated command prompt on the monitored host:

```
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user add librenms
```

It asks for an authentication passphrase and a privacy passphrase, twice each.
Both must be at least 12 characters, and they must differ from one another: one
compromise should not be two.

```
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user list
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user remove librenms
```

Restart the service for a change to take effect:

```
sc stop jt-snmpd && sc start jt-snmpd
```

### Algorithms

The default is **SHA-256 for authentication and AES-128 for privacy**, which is
what net-snmp can talk to everywhere. Override with `--auth` and `--priv`:

| Setting | Accepted |
|---|---|
| `--auth` | `SHA-224`, `SHA-256` (default), `SHA-384`, `SHA-512` |
| `--priv` | `AES-128` (default), `AES-192`, `AES-256` |

**MD5, SHA-1, DES and 3DES are refused.** pysnmp implements all four, so a
configuration naming one of them would otherwise work, and working is the wrong
outcome: the operator would believe the traffic was protected when it was not.

**AES-192 and AES-256 warn.** That is an interoperability risk rather than a
weakness in the cipher. Neither was standardised for USM, two incompatible
key-extension schemes exist (the Blumenthal draft and Reeder's), and Debian and
Ubuntu build net-snmp without the one pysnmp uses. An agent configured that way
can be unreachable from the very LibreNMS installation it was set up for.
AES-128 is the interoperable choice.

---

## 2. Adding the device in LibreNMS

Devices → Add Device, then:

| Field | Value |
|---|---|
| SNMP Version | v3 |
| Auth Level | authPriv |
| Auth User Name | the name given to `user add` |
| Auth Password | the authentication passphrase |
| Auth Algorithm | SHA-256 |
| Crypto Password | the privacy passphrase |
| Crypto Algorithm | AES |

To check from the LibreNMS server before adding it:

```
snmpwalk -v3 -l authPriv -u librenms \
  -a SHA-256 -A '<auth passphrase>' \
  -x AES     -X '<privacy passphrase>' \
  <host> 1.3.6.1.2.1.1
```

---

## 3. Turning v2c off

Once every manager is on v3, set `v3_only` in
`C:\ProgramData\jt-snmpd\config.json` and restart the service:

```json
{ "v3_only": true }
```

The agent then does not register v2c at all. If `v3_only` is set and no v3 user
can be loaded, **the service refuses to start**. That is deliberate: listening
with no way in would look healthy from Windows while answering nobody, and the
operator would go looking at the network for a fault that is in a configuration
file.

---

## 4. Where the keys live, and what that protects

`%ProgramData%\jt-snmpd\secrets\usm.dat`, encrypted with **DPAPI machine
scope**. The directory is restricted to SYSTEM and Administrators.

What is stored is the **localized key**, not the passphrase. A localized key is
derived from the passphrase *and this machine's engineID*, so it authenticates
to exactly one agent. The passphrase is used once, at provisioning time, and is
not written down.

The point of that is the estate. If passphrases were stored, reading one
machine's secrets file — a stolen backup or a misconfigured share is enough —
would hand over an account on every machine that shares the credential. Hundreds
provisioned from one policy is the normal deployment, so that is the realistic
shape of the loss. With localized keys, the same theft is worth one machine.

**What DPAPI does not do.** The blob is decryptable only on the machine that
wrote it, which is what stops a copied file being useful elsewhere. It does not,
and cannot, stop an administrator on that machine: the service runs unattended
as LocalSystem and has to be able to read the keys without anyone present, so
any protection it can undo unattended is protection an administrator can undo
too. The honest claim is that it defends the file at rest and in transit, not
the machine's own administrators.

---

## 5. Cloned VMs, and the one thing that will bite you

An engineID must be unique. jt-snmpd derives it from the Windows MachineGuid and
records which MachineGuid it was derived from.

If the MachineGuid changes — the machine was cloned from a template, or
reimaged — the agent generates a new engineID, resets snmpEngineBoots and says
so in its log. It has to: fifty clones answering with the same engineID make a
manager keep one boots/time pair for what it believes is a single engine, and
authentication then fails intermittently across the whole estate for reasons
nothing in the logs explains.

**A localized key cannot survive that.** It is bound to the engineID it was made
for and cannot be converted, and the passphrase it came from was deliberately
not kept. So a clone's stored users are dead. The agent detects this and says
exactly that, rather than failing to authenticate for reasons nobody can see:

```
[!] SNMPv3: the SNMPv3 keys were localized against engineID 8001869f04... but
    this engine is 8001869f04... A localized key is bound to the engineID it was
    made for and cannot be converted, so every SNMPv3 user has to be provisioned
    again. This normally means the machine was cloned from a template or
    reimaged
```

**So do not bake accounts into an image.** Provision after first boot, from
Group Policy or from the CLI. A template captured before any v3 user is added
has nothing to lose.

---

## 6. What SNMPv3 does not change

- **The agent is still read-only.** v3 brings SET with it in the protocol; this
  agent does not implement SET at all, at any version.
- **The pre-auth gate still runs first.** Source address allow-list, packet size
  cap and rate limit all happen before any cryptography. That ordering is
  deliberate: v3 makes denial of service cheaper, because every packet costs an
  HMAC.
- **It is not a substitute for the firewall rule.** Management networks are
  still mandatory at install time and still deny by default.
