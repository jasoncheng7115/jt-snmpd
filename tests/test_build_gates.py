"""Build scripts must refuse to ship a stale artefact.

**Why this file exists**

The same failure has now happened three times, in three different places:

1. `build-exe.ps1` checked only that the exe existed. PyInstaller failed, the
   old exe was still on disk, and the script reported success.
2. `build-msi.ps1` checked only that the MSI existed. A missing WiX extension
   failed the build, the previous MSI was still on disk, and the script printed
   `[OK] jt-snmpd-0.9.0-x64.msi` while the current version was 0.9.1.
3. `build-msi.ps1` packaged whatever was in `build/` without asking how old it
   was. A fix was made, the exe was not rebuilt, and the MSI shipped with the
   new version number, a fresh SHA-256, its own archive directory — and the old
   code inside.

Every one of these produced a *green* build. That is what makes the class
dangerous: the failure is not that something breaks, it is that something old
gets shipped wearing a new label. These assertions keep the gates in place.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "packaging"
EXE = (PKG / "build-exe.ps1").read_text(encoding="utf-8-sig")
MSI = (PKG / "build-msi.ps1").read_text(encoding="utf-8-sig")


# --- build-exe.ps1 -----------------------------------------------------------

def test_exe_build_checks_the_artefact_is_newer_than_the_source():
    assert "LastWriteTime" in EXE, "there is no freshness check on the output"
    assert re.search(r"exit 1", EXE), "a failed check has to exit non-zero"


def test_exe_build_runs_a_selftest():
    """"The exe exists" is not "the exe works". pysnmp's MIB data files were
    once left out: the exe built, and the service reported Running while raising
    MibNotFoundError on every request."""
    assert "--selftest" in EXE


def test_exe_build_waits_for_file_handles_to_be_released():
    """`Stop-Service` returning does not mean the handles are gone."""
    assert "Wait-ForProcessGone" in EXE


# --- build-msi.ps1 -----------------------------------------------------------

def test_msi_build_fails_when_wix_fails():
    """This checked only `Test-Path $msi`, so a failed build picked up the
    previous MSI and reported success with the old version number."""
    assert re.search(r"if \(\$code -ne 0\)", MSI), "wix build's exit code is not checked"
    i = MSI.index("$code = $LASTEXITCODE")
    block = MSI[i:i + 700]
    assert "exit 1" in block


def test_msi_build_rejects_a_stale_msi():
    assert "is not from this build" in MSI, "the MSI is not confirmed to be from this build"


def test_msi_build_rejects_an_exe_older_than_the_source():
    """build-msi packages what is already in build/. Without this gate a forgotten
    build-exe ships old code under a new version number."""
    assert "$exeTime" in MSI and "$newestSrc" in MSI, "the executable freshness gate is missing"
    i = MSI.index("$newestSrc")
    block = MSI[i:i + 800]
    assert "exit 1" in block, "an executable older than the source has to abort the build"
    assert "build-exe.ps1" in block, "the message should say what to do next"


def test_msi_build_records_source_fingerprints():
    """"Which version of the configure script is inside the package the customer
    is holding?" — one machine once had two copies of msi-configure.ps1 and the
    build used the one that had not been edited."""
    assert "SrcHash" in MSI
    for what in ("configure", "wxs", "agent"):
        assert f'"{what}' in MSI or f"{what} " in MSI, f"BUILDINFO has no {what} fingerprint"


def test_msi_uses_the_real_icon_and_says_so_when_it_cannot():
    """A blank placeholder icon made the Add/Remove Programs entry look like a
    half-finished install."""
    assert "brand\\jt-snmpd.ico" in MSI
    assert "using a blank placeholder icon" in MSI, \
        "a missing icon has to be reported, not silently substituted"


# --- common to both ---------------------------------------------------------

@pytest.mark.parametrize("script,name", [(EXE, "build-exe.ps1"), (MSI, "build-msi.ps1")])
def test_scripts_are_utf8_with_bom(script, name):
    """PowerShell 5.1 reads a BOM-less .ps1 as the system ANSI code page, which
    turns Chinese comments into a syntax error."""
    raw = (PKG / name).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), f"{name} has to be saved as UTF-8 with BOM"


def test_the_shipped_program_is_english_only():
    """Documentation is bilingual; the program is not.

    A Taiwanese-built agent that emits a fullwidth full stop into an English
    sentence on an English Windows looks broken, and one had reached a
    user-visible line: the installer's "could not disable the built-in SNMP
    Service" error carried a `。` in the middle of it, which went to the console
    and into the installation log on every machine that hit it.

    The rest were fullwidth punctuation left in comments and docstrings by
    translation. Nobody sees those, but they are the same mistake one step
    further from the user, and they are how the first one got there.
    """
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[1]
    files = (sorted((root / "deploy").glob("*.py"))
             + sorted((root / "packaging").glob("*.ps1"))
             + sorted((root / "packaging" / "wix").glob("*.wxs")))

    def is_cjk(ch: str) -> bool:
        o = ord(ch)
        return (0x3000 <= o <= 0x303F      # CJK punctuation
                or 0x3400 <= o <= 0x9FFF   # Han
                or 0xF900 <= o <= 0xFAFF
                or 0xFF00 <= o <= 0xFFEF)  # fullwidth forms

    # One deliberate exception, and it is data rather than prose: the docstring
    # on octet() quotes a Traditional Chinese adapter name to say why encoding
    # is stated explicitly. Removing it would remove the reason.
    allowed = {"乙太網路"}

    offenders = []
    for f in files:
        for i, line in enumerate(f.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not any(is_cjk(c) for c in line):
                continue
            if any(a in line for a in allowed):
                continue
            offenders.append(f"{f.relative_to(root)}:{i}: {line.strip()[:70]}")
    assert not offenders, (
        "the program ships in English only; these lines carry CJK text:\n  "
        + "\n  ".join(offenders))
