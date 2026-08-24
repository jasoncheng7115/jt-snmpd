#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Personal-data and secret scan, to be run before pushing to the public repo.

**Why this exists**

This project is developed against a real internal network, and that is where its
value comes from: every measurement is taken on real hardware. The same fact
means the measurements, logs, screenshots and scan reports are full of host
names, addresses, MAC addresses, hardware serial numbers and community strings.
Once any of that is on GitHub it **cannot be taken back**: GitHub retains forks,
caches and Git history, and deleting something afterwards only removes it from
the display.

This has already gone wrong once: the ports comparison screenshot taken for the
README had LibreNMS's SNMP neighbours drawn into it -- `host-101-ipmi`, `vas1`,
`dc2`, `router-003.<internal domain>`, `ap-112`, `nas4`. That is a map of the
internal network, and grep cannot find it, because it is pixels.

**What is checked**

1. **Text files**: regular expressions for addresses, MAC addresses, internal
   domains, serial numbers, credentials and community strings.
2. **Binaries (screenshots, mostly)**: a regular expression cannot inspect an
   image, so these go through review-and-hash instead. Every image has to be
   listed in `docs/images/REVIEWED.md` with its SHA-256. Change the image and the
   hash no longer matches, the scan blocks the push, and the review has to happen
   again.

**Scope** is the files git would actually push (tracked, plus untracked files
that are not ignored), rather than the whole working directory -- otherwise
`.venv` drowns out every real finding.

Usage::

    python3 tools/check-privacy.py            # scan; exits 1 on any HIGH
    python3 tools/check-privacy.py --update-images   # regenerate the image review list
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / "tools" / "privacy-allowlist.txt"
IMAGE_MANIFEST = ROOT / "docs" / "images" / "REVIEWED.md"

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
                   ".pdf", ".msi", ".exe", ".dll", ".zip", ".7z", ".ttf", ".otf"}

HIGH, MED, LOW = "HIGH", "MED", "LOW"


# --- Rules ------------------------------------------------------------------
# Each rule is (severity, name, pattern, why).
#
# IPv4 is the awkward one: an OID looks exactly like an address (the first four
# arcs of `1.3.6.1.2.1` are a valid IPv4 address). Matches therefore go through
# `_looks_like_real_ip()` for a second opinion.
RULES: list[tuple[str, str, re.Pattern, str]] = [
    (HIGH, "private-key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP)?PRIVATE KEY-----"),
     "a private key must never be committed"),
    (HIGH, "password", re.compile(
        r"""(?ix)\b(?:password|passwd|pwd|secret)\s*[=:]\s*["']?[^\s"'{}$<>]{4,}"""),
     "password in clear text"),
    # Match values as they appear on a command line or in a configuration file,
    # not variable assignments in source. Neither
    # `community = v2c.apiMessage.get_community(msg)` nor `COMMUNITY = "bench"`
    # is a secret -- one reads a value, the other is a test constant. The first
    # version reported both as HIGH, and that kind of noise is how people start
    # ignoring the scanner, which is worse than not scanning.
    (HIGH, "community", re.compile(
        r"""(?x)
        \bCOMMUNITY=                     # command line / MSI property form, no spaces
        # Placeholders are not leaks. The match is case-insensitive: the first
        # version only excluded uppercase YOUR, so `your-community` in the
        # documentation was reported as HIGH.
        (?![<$%{]|(?i:public|your|change|example|placeholder|xxx)\b)
        ["']? ([A-Za-z0-9_-]{2,})
        """),
     "SNMP community string (equivalent to a password)"),
    (HIGH, "mac-address", re.compile(
        r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"),
     "a MAC address is globally unique and identifies specific hardware and its vendor"),
    (HIGH, "api-token", re.compile(
        r"""(?ix)\b(?:api[_-]?key|access[_-]?token|bearer)\s*[=:]\s*["']?[A-Za-z0-9_\-]{16,}"""),
     "API credential"),
    (MED, "ipv4", re.compile(
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
     "IP address (an internal one discloses how the network is laid out)"),
    (MED, "ipv6", re.compile(
        r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b"),
     "IPv6 address"),
    # Host names: the project owner has decided these may be published, so they
    # are LOW -- reported for information, and they do not block a push. The rule
    # is kept rather than deleted so that each push still shows which names went
    # out. Deciding something may be published is not the same as not needing to
    # know what was published.
    (LOW, "windows-hostname", re.compile(
        r"\b(?:DESKTOP|LAPTOP|WIN)-[A-Z0-9]{7}\b"),
     "Windows host name (approved for publication)"),
    (LOW, "internal-domain", re.compile(
        r"\b[A-Za-z0-9][A-Za-z0-9-]*\.(?:local|lan|internal|intranet|corp|home\.arpa)\b"),
     "internal domain name (approved for publication)"),
    # Match the serial number's value, not field names in source.
    # `SerialNumberOffset` and `serial_number` are identifiers, not serials; the
    # first version reported all of them as MED, and that noise buries the real
    # findings. Hence the requirement for an explicit separator between the word
    # and the value (a colon, an equals sign, or whitespace and a quote).
    (MED, "hardware-serial", re.compile(
        r"""(?x)
        \b(?:[Ss]erial(?:\s+[Nn](?:umber|o\.?))?|S/N)\b
        \s*[:=#]?\s*
        ["']? (?![A-Za-z]*Offset\b|[A-Za-z]*Length\b)
        ([A-Z0-9]{6,24})\b
        """),
     "a hardware serial number can be traced back to warranty and asset records"),
    (MED, "email", re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
     "email address"),
    (MED, "unc-path", re.compile(r"\\\\[A-Za-z0-9_.-]{2,}\\[A-Za-z0-9_$.-]+"),
     "a UNC path discloses a file server's name"),
    (LOW, "user-profile-path", re.compile(
        r"(?i)[A-Z]:\\Users\\(?!Public\b|<|%)[A-Za-z0-9._-]+"),
     "a user profile path contains an account name"),
]

# Reserved ranges for documentation; entirely normal in an example
# (RFC 5737 / RFC 3849 / RFC 7042)
DOC_IPV4_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.")
DOC_IPV6_PREFIX = "2001:db8"
DOC_MAC_PREFIX = "00:00:5e:00:53"


def _looks_like_real_ip(text: str) -> bool:
    """Rule out OIDs, version numbers and anything else shaped like an address.

    The first four arcs of `1.3.6.1.2.1.25` are a valid IPv4 address, and
    `0.0.0.0` and `255.255.255.255` are wildcards rather than leaks. The test is
    "every octet in 0-255", plus "not part of a longer dotted-numeric string".
    """
    parts = text.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return False
    if text in ("0.0.0.0", "255.255.255.255", "127.0.0.1", "1.1.1.1", "8.8.8.8"):
        return False
    if text.startswith(DOC_IPV4_PREFIXES):
        return False
    # Prefixes an OID commonly starts with
    if parts[0] in ("0", "1", "2") and parts[1] in ("0", "1", "2", "3", "4", "5", "6"):
        return False
    return True


def load_allowlist() -> list[re.Pattern]:
    """The allow-list: one regular expression per line, `#` starts a comment.

    It is **deliberately built to require a reason**. Every entry should say
    plainly beside it why the thing it matches is safe; without that, the file
    turns over time into the place where all the warnings get switched off.
    """
    if not ALLOWLIST.exists():
        return []
    out = []
    for line in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                out.append(re.compile(line))
            except re.error as exc:
                print(f"invalid pattern in the allow-list: {line}  ({exc})", file=sys.stderr)
    return out


def tracked_files() -> list[Path]:
    """The files git would actually push: tracked, plus untracked and not ignored."""
    files: set[str] = set()
    failures = []
    for cmd in (["git", "ls-files"],
                ["git", "ls-files", "--others", "--exclude-standard"]):
        try:
            out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                 check=True).stdout
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            failures.append(f"{' '.join(cmd)}: {exc}")
            continue
        files.update(f for f in out.splitlines() if f.strip())

    # **Scanning zero files must never pass quietly.** In a directory where git
    # init has not been run, both commands fail; swallowing the exception yields
    # "nothing found". A scanner that always reports safe is more dangerous than
    # no scanner, because it convinces people the check happened. This has
    # actually occurred.
    if not files:
        detail = "\n  ".join(failures) if failures else "(git reported 0 files)"
        raise SystemExit(
            f"scan aborted: the file list could not be obtained:\n  {detail}\n"
            f"directory: {ROOT}\n"
            "If this is a freshly generated public repo, run "
            "`git init -b main && git add -A` before scanning.")
    return sorted((ROOT / f) for f in files if (ROOT / f).is_file())


def scan_text(path: Path, allow: list[re.Pattern]) -> list[tuple]:
    """Scan one text file. Returns [(severity, rule, line, excerpt, why)]."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    findings = []
    lines = text.splitlines()
    for sev, name, pattern, why in RULES:
        for m in pattern.finditer(text):
            hit = m.group(0)
            if name == "ipv4" and not _looks_like_real_ip(hit):
                continue
            if name == "ipv6":
                low = hit.lower()
                if low.startswith(DOC_IPV6_PREFIX) or low in ("::1",) or ":" not in hit:
                    continue
                # An OID or a timestamp never has hex letters between the colons,
                # so err on the conservative side here
                if not re.search(r"[a-fA-F]", hit):
                    continue
            if name == "mac-address" and hit.lower().startswith(DOC_MAC_PREFIX):
                continue
            line_no = text.count("\n", 0, m.start()) + 1
            context = lines[line_no - 1].strip() if line_no <= len(lines) else hit
            if any(a.search(context) or a.search(hit) for a in allow):
                continue
            findings.append((sev, name, line_no, hit, context[:110], why))
    return findings


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_reviewed() -> dict[str, str]:
    """Read the list of images a person has reviewed (path -> SHA-256)."""
    if not IMAGE_MANIFEST.exists():
        return {}
    out = {}
    for line in IMAGE_MANIFEST.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def check_binaries(files: list[Path]) -> list[tuple]:
    """Binaries -- screenshots, mostly -- go through review-and-hash.

    A regular expression cannot inspect pixels. The only dependable approach is
    to require that a person has looked at every image and recorded its hash;
    change the image and the hash no longer matches, and the scan blocks.
    """
    reviewed = load_reviewed()
    problems = []
    for f in files:
        if f.suffix.lower() not in BINARY_SUFFIXES:
            continue
        rel = str(f.relative_to(ROOT))
        digest = sha256(f)
        if rel not in reviewed:
            problems.append((HIGH, "image-unreviewed", rel, digest,
                             "not reviewed by a person. An image can carry MAC "
                             "addresses, neighbour host names and serial numbers, "
                             "and a regular expression cannot see pixels"))
        elif reviewed[rel] != digest:
            problems.append((HIGH, "image-changed", rel, digest,
                             f"contents changed (recorded as {reviewed[rel][:12]}…); "
                             "it has to be reviewed again"))
    return problems


def update_manifest(files: list[Path]) -> None:
    rows = []
    for f in sorted(files):
        if f.suffix.lower() in BINARY_SUFFIXES:
            rows.append((str(f.relative_to(ROOT)), sha256(f)))
    body = [
        "# Image review record",
        "",
        "A regular expression cannot read pixels. A README screenshot once carried",
        "the SNMP neighbours LibreNMS had drawn into it: MAC addresses, internal host",
        "names and IPv6 addresses, which together map the internal network.",
        "",
        "**Every image has to be looked at by a person after it is added or changed**,",
        "and confirmed to contain none of:",
        "",
        "- MAC addresses",
        "- Real host names and internal domains",
        "- Internal addresses (your own ranges, as opposed to the documentation ranges)",
        "- Hardware serial numbers, licence keys, community strings",
        "- Neighbour device names (LibreNMS's ports page shows SNMP and LLDP neighbours)",
        "- User names and account names",
        "",
        "Once confirmed, run `python3 tools/check-privacy.py --update-images` to refresh",
        "the table below. When a hash no longer matches, the scan blocks the push and",
        "the review has to happen again.",
        "",
        "| File | SHA-256 |",
        "|---|---|",
    ]
    body += [f"| `{p}` | `{h}` |" for p, h in rows]
    IMAGE_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    IMAGE_MANIFEST.write_text("\n".join(body) + "\n", encoding="utf-8")
    print(f"updated {IMAGE_MANIFEST.relative_to(ROOT)} ({len(rows)} images)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Personal-data and secret scan, to run before pushing to the public repo")
    ap.add_argument("--update-images", action="store_true",
                    help="regenerate the image review list "
                         "(only after actually looking at every image)")
    args = ap.parse_args()

    files = tracked_files()
    if args.update_images:
        update_manifest(files)
        return 0

    allow = load_allowlist()
    text_hits: dict[str, list] = {}
    for f in files:
        if f.suffix.lower() in BINARY_SUFFIXES:
            continue
        hits = scan_text(f, allow)
        if hits:
            text_hits[str(f.relative_to(ROOT))] = hits
    bin_hits = check_binaries(files)

    print(f"scope: {len(files)} files (the ones git would actually push)\n")

    n_high = n_med = n_low = 0
    for rel, hits in sorted(text_hits.items()):
        print(f"── {rel}")
        for sev, name, line_no, hit, context, why in sorted(hits, key=lambda h: h[2]):
            n_high += sev == HIGH
            n_med += sev == MED
            n_low += sev == LOW
            print(f"   [{sev:4}] {name:18} line {line_no}  {hit!r}")
            print(f"          {context}")
        print()

    if bin_hits:
        print("── images / binaries")
        for sev, name, rel, digest, why in bin_hits:
            n_high += 1
            print(f"   [{sev:4}] {name:18} {rel}")
            print(f"          {why}")
            print(f"          SHA-256 {digest}")
        print()

    print(f"result: HIGH={n_high}  MED={n_med}  LOW={n_low}")
    if n_high:
        print("\nThere are HIGH findings. **Do not push.**")
        print("Fix them and run again. Anything confirmed safe can go in "
              "tools/privacy-allowlist.txt, with its reason written down.")
        return 1
    if n_med or n_low:
        print("\nNo HIGH findings, but read every MED and LOW and confirm each is "
              "a documentation example.")
    else:
        print("\nNothing found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
