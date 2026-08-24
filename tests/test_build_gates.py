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
    assert "LastWriteTime" in EXE, "缺少產物新鮮度檢查"
    assert re.search(r"exit 1", EXE), "檢查失敗必須以非零碼結束"


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
    assert re.search(r"if \(\$code -ne 0\)", MSI), "wix build 的結束碼必須被檢查"
    i = MSI.index("$code = $LASTEXITCODE")
    block = MSI[i:i + 700]
    assert "exit 1" in block


def test_msi_build_rejects_a_stale_msi():
    assert "不是本次建置的產物" in MSI, "MSI 必須確認是本次建出來的"


def test_msi_build_rejects_an_exe_older_than_the_source():
    """build-msi packages what is already in build/. Without this gate a forgotten
    build-exe ships old code under a new version number."""
    assert "$exeTime" in MSI and "$newestSrc" in MSI, "缺少執行檔新鮮度閘門"
    i = MSI.index("$newestSrc")
    block = MSI[i:i + 800]
    assert "exit 1" in block, "執行檔過舊必須中止"
    assert "build-exe.ps1" in block, "錯誤訊息應告訴使用者下一步"


def test_msi_build_records_source_fingerprints():
    """"Which version of the configure script is inside the package the customer
    is holding?" — one machine once had two copies of msi-configure.ps1 and the
    build used the one that had not been edited."""
    assert "SrcHash" in MSI
    for what in ("configure", "wxs", "agent"):
        assert f'"{what}' in MSI or f"{what} " in MSI, f"BUILDINFO 缺少 {what} 指紋"


def test_msi_uses_the_real_icon_and_says_so_when_it_cannot():
    """A blank placeholder icon made the Add/Remove Programs entry look like a
    half-finished install."""
    assert "brand\\jt-snmpd.ico" in MSI
    assert "改用空白佔位圖示" in MSI, "找不到圖示時必須講出來，不可無聲退回"


# --- 兩者共通 ----------------------------------------------------------------

@pytest.mark.parametrize("script,name", [(EXE, "build-exe.ps1"), (MSI, "build-msi.ps1")])
def test_scripts_are_utf8_with_bom(script, name):
    """PowerShell 5.1 reads a BOM-less .ps1 as the system ANSI code page, which
    turns Chinese comments into a syntax error."""
    raw = (PKG / name).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), f"{name} 必須存成 UTF-8 with BOM"
