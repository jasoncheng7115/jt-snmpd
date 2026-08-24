"""The behaviour of the privacy scanner itself.

**Why the tool needs tests**

A scanner has an unusual failure mode: it does not raise, it says "nothing
found".

That has already happened here. Run in a directory freshly produced by
`prepare-public-repo.py`, before `git init`, both `git ls-files` calls fail with
exit status 128, the exception was swallowed, the file list was empty, and the
output was "scope: 0 files" followed by "nothing found".

**A scanner that always reports safe is more dangerous than no scanner**,
because it convinces people the check happened. This file pins down the rule
that a missing file list has to abort.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
SCANNER = TOOLS / "check-privacy.py"
SRC = SCANNER.read_text(encoding="utf-8")


def _func(name: str) -> str:
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    pytest.fail(f"{name} not found")


def test_scanner_exists_and_is_executable():
    assert SCANNER.exists()
    if sys.platform != "win32":
        # Windows has no POSIX execute bit and git does not preserve one, so
        # checking it there only produces a failure unrelated to the code.
        assert SCANNER.stat().st_mode & 0o111, "the scanner should be directly executable"


def test_empty_file_list_aborts_rather_than_passing():
    """The central assertion: no file list must abort, never report "nothing found"."""
    body = _func("tracked_files")
    assert "if not files:" in body, "the empty-list check is missing"
    assert "SystemExit" in body, "an empty list has to abort"


def test_abort_message_explains_the_fix():
    """The message has to say what to do next, or it gets treated as noise."""
    body = _func("tracked_files")
    assert "git init" in body, "the message should say how to fix it"


def test_scanner_aborts_in_a_non_git_directory(tmp_path):
    """Run it for real: outside a git repository it must exit non-zero."""
    (tmp_path / "tools").mkdir()
    for f in ("check-privacy.py", "privacy-allowlist.txt"):
        src = TOOLS / f
        if src.exists():
            (tmp_path / "tools" / f).write_text(src.read_text(encoding="utf-8"),
                                                encoding="utf-8")
    # Force UTF-8 both ways. Windows consoles default to a legacy code page, so
    # without this the child's non-ASCII output comes back backslash-escaped and
    # the assertion below compares against something that can never match.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    r = subprocess.run([sys.executable, str(tmp_path / "tools" / "check-privacy.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=tmp_path, env=env)
    assert r.returncode != 0, "it must not report success outside a git repository"
    assert "scan aborted" in (r.stderr + r.stdout), \
        f"stdout={r.stdout[:200]!r} stderr={r.stderr[:200]!r}"


def test_high_severity_blocks_the_push():
    body = _func("main")
    assert "if n_high:" in body and "return 1" in body, \
        "any HIGH finding has to produce a non-zero exit"


def test_images_require_review_not_pattern_matching():
    """A regular expression cannot read pixels, so images go through review-and-hash."""
    body = _func("check_binaries")
    assert "image-unreviewed" in body and "image-changed" in body
    assert "sha256" in body, "a hash is what detects a changed image"


def test_allowlist_entries_require_a_reason():
    """The allow-list is deliberately built to require a reason. Exceptions with
    none turn, over time, into the place where all the warnings get switched
    off."""
    al = TOOLS / "privacy-allowlist.txt"
    assert al.exists()
    text = al.read_text(encoding="utf-8")
    comment_lines = [l for l in text.splitlines() if l.strip().startswith("#")]
    rule_lines = [l for l in text.splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    assert len(comment_lines) >= len(rule_lines), (
        "there should be at least as many comment lines as rules: "
        "every exception needs its reason")


def test_ip_rule_ignores_oids():
    """An OID's dotted-decimal form is indistinguishable from IPv4 (the first
    four arcs of `1.3.6.1.2.1` are a valid address). Failing to tell them apart
    buries the output under thousands of OIDs, and the results become noise
    nobody reads."""
    sys.path.insert(0, str(TOOLS))
    import importlib.util
    spec = importlib.util.spec_from_file_location("cp", SCANNER)
    cp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cp)
    assert not cp._looks_like_real_ip("1.3.6.1")
    assert not cp._looks_like_real_ip("0.0.0.0")
    assert not cp._looks_like_real_ip("192.0.2.10"), \
        "the documentation ranges are not a leak"
    assert cp._looks_like_real_ip("192.168.1.68")
    assert cp._looks_like_real_ip("172.16.5.4")
