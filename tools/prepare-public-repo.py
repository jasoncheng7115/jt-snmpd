#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Produce a directory holding only publishable content, ready to push.

**Why not simply push the existing repository**

The development history contains internal documents. Git history is
content-addressed, so **pushing the existing history carries their contents
along** even if the files are removed in a commit today, and GitHub keeps forks
and caches: deleting afterwards only removes it from the display.

Rewriting history, with filter-repo or similar, can be made to work, but one
imperfect pass leaks, and "nothing was left behind" cannot be demonstrated.
Starting from a fresh history can be: **what is in this directory is what gets
pushed, and nothing else.**

The local development repository keeps its full history and is unaffected.

Usage::

    python3 tools/prepare-public-repo.py /tmp/jt-snmpd-public
    cd /tmp/jt-snmpd-public && python3 tools/check-privacy.py   # and scan again
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Never published, stated explicitly. .gitignore covers most of it; this is the
# second line, because .gitignore does nothing for files that are already
# **tracked**, and these are.
NEVER_PUBLISH = {
    "spec.md",              # the internal specification
    "CLAUDE.md",            # internal notes, with production addresses and operating discipline
    "upt.b64",
    # The gate 0 report: written throughout against the internal specification's
    # section numbering, so an outside reader has nothing to follow, and Chinese
    # only. Rewriting thirty-odd unresolvable references into standalone prose
    # would be a bigger job than the document is worth publishing for.
    "phase0-findings.md",
    # Real credentials, read by check-privacy.py and by the config-loading guard
    # so that both can look for them. .gitignore already covers it; naming it
    # here is the second line, and this is the one file where a gap would hand
    # over the thing everything else exists to protect.
    ".privacy-secrets",
}
NEVER_PUBLISH_DIRS = {"reports", "state", "logs", ".venv", "build", "dist",
                      "__pycache__", ".pytest_cache", ".git"}
NEVER_PUBLISH_SUFFIX = {".log", ".msi", ".exe", ".pem", ".key", ".pfx",
                        ".walk", ".snmpwalk", ".rrd"}

# These directories are kept, but only their READMEs: they explain what belongs
# there, and the artefacts themselves are not version controlled.
# The match is on the name *starting* with README rather than equalling
# README.md. It used to test for README.md exactly, and dist/README_zh-TW.md was
# therefore never published at all.
KEEP_README_ONLY = {"build", "dist"}


def should_skip(rel: Path) -> str | None:
    if rel.name in NEVER_PUBLISH:
        return f"excluded by name: {rel.name}"
    if rel.suffix.lower() in NEVER_PUBLISH_SUFFIX:
        return f"excluded by suffix: {rel.suffix}"
    for part in rel.parts:
        if part in NEVER_PUBLISH_DIRS:
            if part in KEEP_README_ONLY and rel.name.startswith("README"):
                return None
            return f"excluded directory: {part}/"
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    dest = Path(sys.argv[1]).resolve()
    existing_git = (dest / ".git").is_dir()
    if dest.exists() and any(dest.iterdir()) and not existing_git:
        print(f"{dest} is not empty and is not a git repository.\n"
              "Empty it first, so nothing old is mixed in.",
              file=sys.stderr)
        return 1
    dest.mkdir(parents=True, exist_ok=True)

    # An existing public repository is synced in place: .git is kept, since the
    # remote and the history live there, but the old files are cleared first.
    # Otherwise a file deleted at the source stays in the public repository for
    # ever, which is exactly the "thought it was removed, it was not" leak.
    if existing_git:
        for item in dest.iterdir():
            if item.name == ".git":
                continue
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        print("kept .git, cleared the old files, resyncing")

    # List files the way git sees them: tracked, plus untracked and not ignored.
    # Walking the filesystem directly would sweep in .venv and the rest.
    files: set[str] = set()
    for cmd in (["git", "ls-files"],
                ["git", "ls-files", "--others", "--exclude-standard"]):
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout
        files.update(f for f in out.splitlines() if f.strip())

    copied, skipped = 0, []
    for rel_str in sorted(files):
        rel = Path(rel_str)
        src = ROOT / rel
        if not src.is_file():
            continue
        reason = should_skip(rel)
        if reason:
            skipped.append((rel_str, reason))
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1

    print(f"copied {copied} files to {dest}")
    if skipped:
        print(f"\nexcluded {len(skipped)}:")
        for rel_str, reason in skipped:
            print(f"  {rel_str:52} {reason}")

    print("\nnext:")
    print(f"  cd {dest}")
    print("  python3 tools/check-privacy.py      # scan the publishable content again")
    print("  git init -b main && git add -A")
    print("  git commit -m 'jt-snmpd v<version>'")
    print("  git remote add origin git@github.com:jasoncheng7115/jt-snmpd.git")
    print("  git push -u origin main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
