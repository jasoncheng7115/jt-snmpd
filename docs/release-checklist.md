---
layout: default
title: Release Checklist
description: What must be true before anything is pushed or published
---

[← All documentation](https://jasoncheng7115.github.io/jt-snmpd/) ·
**English** | [繁體中文](https://jasoncheng7115.github.io/jt-snmpd/release-checklist_zh-TW.html)

# Release and publication checklist

Public repository: <https://github.com/jasoncheng7115/jt-snmpd>

> **Once it is on GitHub it cannot be taken back.** GitHub retains forks, caches
> and Git history; deleting something afterwards removes it from the display, not
> from reach. Everything here therefore has to be done **before** the push.

---

## 0. One-off: where the public repository's history starts

This project's development history contains internal documents that are not
published. **Removing them now would not help: pushing the existing history
would carry their contents along.** The public repository therefore starts from a
**fresh history**:

```bash
# Build a repository containing only publishable content, in a clean temp dir
python3 tools/prepare-public-repo.py /tmp/jt-snmpd-public
cd /tmp/jt-snmpd-public
git init -b main
git add -A
git commit -m "jt-snmpd v<version>"
git remote add origin git@github.com:jasoncheng7115/jt-snmpd.git
git push -u origin main
```

The local development repository keeps its full history and is unaffected.

---

## Pinned inputs

Three things are pinned, and each was unpinned until an outside review pointed
at it:

- **`requirements-build.txt`** — every Python package a build installs. The
  agent pre-computes BER on the wire, so the pysnmp and pyasn1 versions decide
  what bytes go out; an unnoticed upgrade that changes an encoded length pushes
  responses past the 1400-byte cap, and the symptom at the LibreNMS end is
  intermittent missing data rather than an error.
- **Pillow, to the version that generated the wizard artwork.** CI regenerates
  the BMPs and compares hashes, so a different encoder fails that check for a
  reason that has nothing to do with the release.
- **Every GitHub Action, by commit SHA.** A tag is a moving pointer that the
  action's owner can repoint; a commit cannot be changed under you. The tag is
  kept in a trailing comment so the pin stays readable.

Updating any of them is a deliberate commit with CI to back it, not something
that happens because a build ran on a different morning.

## 1. Before every push

### 1.1 Privacy and secret scan

```bash
python3 tools/check-privacy.py
python3 tools/check-terminology.py
```

The second checks Taiwanese wording. A dozen terms in this project were caught
only by a person reading the finished page, which is the most expensive place to
catch them. A word enters the list once it has actually been wrong here, so a
finding means something. Read the list with
`python3 tools/check-terminology.py --list` rather than copying it here: a
document that enumerates the forbidden words is one the tool then flags.

**Do not push with any `HIGH`.** The scan covers the files git would actually
push (tracked, plus untracked files that are not ignored), not the whole working
directory.

**Run it in the development repository, not only in the public one.** The real
credentials live in `tools/.privacy-secrets`, which is untracked and never
synced to the public repository, and the scanner matches that list literally.
The public repository has no such file, so running the scan there prints
`known secrets: NONE` to say that this check is not running at all — read that
line, because it looks very much like having found nothing and means something
entirely different.

| Level | Meaning | Handling |
|---|---|---|
| `HIGH` | Private keys, passwords, community strings, MAC addresses, API credentials, unreviewed images | **Must be fixed.** Do not push |
| `MED` | IP addresses, serial numbers, email addresses, UNC paths | Confirm each one is a documentation example rather than field data |
| `LOW` | Host names, internal domains, user paths | The project owner has decided host names may be published; still worth knowing what went out |

Items confirmed safe can go in `tools/privacy-allowlist.txt`, and **every entry
needs its reason written down**. Exceptions with no stated reason turn, over
time, into the place where all the warnings get switched off.

### 1.2 Image review by a person

A regular expression cannot read pixels. **Every new or modified image has to be
looked at by a human.**

This has already gone wrong once: the ports comparison screenshot taken for the
README carried the SNMP neighbours LibreNMS had drawn — `host-101-ipmi`, `vas1`,
`dc2`, `router-003`, `ap-112`, `nas4`, plus four MAC addresses. That is a map of
the internal network.

Check every image for:

- [ ] MAC addresses (the screenshot script rewrites them to `xx:xx:xx:xx:xx:xx`, but confirm it actually took effect)
- [ ] Internal IP addresses (your own ranges, as opposed to documentation ranges such as `192.0.2.0/24`)
- [ ] Hardware serial numbers, licence keys, community strings
- [ ] Neighbour device names (LibreNMS's ports page shows SNMP and LLDP neighbours)
- [ ] User names, account names, email addresses
- [ ] The browser tab bar and bookmarks bar (use a private window or headless capture)

Then record the review:

```bash
python3 tools/check-privacy.py --update-images
```

Any change to an image breaks its hash, the scan blocks the push, and the review
has to happen again.

### 1.3 Tests and versions

- [ ] `.venv/bin/python -m pytest tests/ -q` fully green
- [ ] `deploy/version.py` updated
- [ ] **Both** `CHANGELOG.md` (English) and `CHANGELOG_zh-TW.md` (Traditional Chinese) updated
- [ ] Version numbers consistent across both READMEs
- [ ] `tests/lifecycle.ps1` run on real hardware with `LIFECYCLE_RESULT=PASS`

### 1.4 Installer

- [ ] MSI built and archived to `dist/releases/<version>/`
- [ ] Source fingerprints in `BUILDINFO.txt` (configure / wxs / agent) match the repository
- [ ] **The MSI itself does not go into git** (excluded in `.gitignore`); it is attached to the GitHub Release
- [ ] SHA-256 published alongside it

---

## 2. What is never published

`.gitignore` excludes these, but confirm each time that nothing bypassed it with
`git add -f` or similar:

| Item | Reason |
|---|---|
| The internal specification | Not published |
| Internal working notes | They carry production addresses and operating discipline |
| `reports/` | Scan reports, containing local paths |
| `*.log`, `logs/` | Agent logs record interface names, disk models and serial numbers |
| `state/`, `index-map.json`, `engine.json` | Runtime state, including adapter LUIDs and the engineID |
| `*.walk`, `*.snmpwalk` | Raw walk output from real machines |
| `dist/`, `build/`, `*.msi` | Build artefacts |
| `.env`, `*.pem`, `*.key`, `*.pfx` | Certificates and keys |

---

## 3. "What the agent reports" and "what is in the repository" are different questions

Keep them apart:

- **The agent does report real serial numbers, real interface names and real
  addresses.** That is its job — when someone has to decide which disk or which
  memory module to replace, the serial is what makes it findable. That data stays
  inside **the customer's own monitoring system**.
- **Nothing about any real host belongs in the public repository.** Documentation
  uses the RFC 5737 reserved ranges (`192.0.2.0/24`, `198.51.100.0/24`), and
  serial numbers are replaced with `****`.

In other words, the masking exists because *this document is public*, not because
the data should not have been collected.

---

## 4. Quick commands

```bash
# Full check
.venv/bin/python -m pytest tests/ -q && python3 tools/check-privacy.py

# Exactly which files would be pushed
git ls-files; git ls-files --others --exclude-standard

# Whether a path is ignored (note: this says nothing about *tracked* files;
# they need git rm --cached first)
git check-ignore -v <path>
```

---

## Related documentation

- [Documentation home](https://jasoncheng7115.github.io/jt-snmpd/)
- [Security assessment](https://jasoncheng7115.github.io/jt-snmpd/attack-surface.html)
- [Code signing](https://jasoncheng7115.github.io/jt-snmpd/code-signing.html)
- [Security scanning toolchain](https://jasoncheng7115.github.io/jt-snmpd/security-scanning.html)
