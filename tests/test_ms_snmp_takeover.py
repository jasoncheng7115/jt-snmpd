"""接管與歸還 Windows 內建 SNMP Service。

**這個 bug 怎麼被發現的**

使用者問「安裝/更新程式會偵測到 windows 內建有啟 snmp 的話，將它停止並設為停用
對吧」。答案是對的，但去核對程式碼時發現**升級路徑會把還原記錄毀掉**：

`msi-configure.ps1` 每次執行都重讀當下的內建 SNMP 狀態，然後無條件覆寫
`state\\ms-snmp-restore.json`。第一次安裝時讀到的是真實原狀（例如 Automatic /
Running），沒問題；但**升級時內建 SNMP 早就被上一次安裝停用了**，重讀只會得到
`Disabled / Stopped`，然後把這個值寫回還原記錄。

解除安裝那段的判斷是：

    if ($orig -and $orig -ne 'Disabled') { Set-Service -Name SNMP -StartupType $orig }

`$orig` 已經變成 `Disabled` → 條件不成立 → **內建 SNMP 再也回不來**。

所以「安裝 → 移除」會正確還原，「安裝 → 升級 → 移除」不會。差別只在中間多了
一次升級，而升級正是這個產品的常態操作。第一次的生命週期測試沒抓到，因為它
在移除階段只檢查了我們自己的服務、目錄與防火牆規則，沒有檢查歸還了什麼。

要記的是「**我們第一次動手之前**的樣子」，因此既有記錄一律優先。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "packaging"
SRC = (PKG / "msi-configure.ps1").read_text(encoding="utf-8-sig")


# --- 偵測與停用 -------------------------------------------------------------

def test_detects_builtin_snmp_and_records_original_state():
    assert "Get-Service -Name SNMP" in SRC, "未偵測內建 SNMP Service"
    for field in ("original_start_type", "original_status", "service_existed"):
        assert field in SRC, f"還原記錄缺少 {field}"


def test_disables_rather_than_removes():
    """停用，不移除。移除是不可逆的，而我們只是借用 UDP/161。"""
    assert "Set-Service -Name SNMP -StartupType Disabled" in SRC
    assert "Stop-Service -Name SNMP -Force" in SRC
    assert not re.search(r"Remove-WindowsCapability|Uninstall-WindowsFeature|"
                         r"sc\.exe delete SNMP\b", SRC), (
        "不得移除內建 SNMP，停用即可，且必須可還原")


def test_disable_result_is_verified():
    """群組原則或第三方管控可能擋下停用。沒真的停掉，內建 SNMP 仍佔著 UDP/161，
    我們會綁定失敗，那時只會看到一個查不出原因的健康檢查逾時。"""
    i = SRC.find("if ($msCfg.service_exists -and $KeepMsSnmp -ne '1')")
    assert i != -1, "找不到停用區塊"
    block = SRC[i:i + 1400]
    assert "$after = Get-Service -Name SNMP" in block, "停用後未驗證實際狀態"
    assert "exit 1" in block, "停用失敗必須讓安裝失敗，而不是繼續往下走"


def test_keepmssnmp_escape_hatch_exists():
    """有些環境的內建 SNMP 掛著 ExtensionAgents 不能停。要留退路，
    但退路必須是明示的（傳屬性），不能是預設行為。"""
    assert "$KeepMsSnmp -ne '1'" in SRC
    wxs = (PKG / "wix" / "jt-snmpd.wxs").read_text(encoding="utf-8-sig")
    assert "KEEPMSSNMP" in wxs, "wxs 未定義 KEEPMSSNMP 屬性，該退路實際上不存在"


# --- 升級不得毀掉還原記錄（本檔的核心）-------------------------------------

def test_existing_restore_record_takes_precedence():
    """核心斷言：既有記錄優先，只有第一次安裝才寫入當下狀態。"""
    assert "$RESTORE_FILE" in SRC, "還原記錄路徑應集中為變數，避免兩處不一致"
    assert "if (Test-Path $RESTORE_FILE)" in SRC, (
        "未檢查既有還原記錄，升級會用已停用的狀態覆寫掉真正的原狀")
    i = SRC.find("if (Test-Path $RESTORE_FILE)")
    j = SRC.find("$restore = [ordered]@{", i)
    assert j != -1
    block = SRC[i:j]
    assert "ConvertFrom-Json" in block, "既有記錄必須被解析後沿用"
    assert "if (-not $msSnmpBlock)" in block, (
        "沒有既有記錄時才可以用當下狀態建立")


def test_restore_block_is_not_rebuilt_unconditionally():
    """反向斷言：$restore 裡不可再直接讀 $msCfg 的狀態欄位。"""
    i = SRC.find("$restore = [ordered]@{")
    j = SRC.find("}\n", SRC.find("not_imported", i))
    block = SRC[i:j]
    assert "$msCfg.start_type" not in block, (
        "$restore 直接取用當下狀態 = 升級時會覆寫掉真正的原狀")
    assert "ms_snmp = $msSnmpBlock" in block


def test_uninstall_restores_original_start_type():
    assert "Set-Service -Name SNMP -StartupType $orig" in SRC
    assert "$r.ms_snmp.disabled_by_us" in SRC, (
        "只還原我們動過的機器，KEEPMSSNMP 安裝的不該被我們改回去")
    assert "original_status -eq 'Running'" in SRC, (
        "原本在執行中的才需要重新啟動")


def test_restore_is_skipped_when_original_was_already_disabled():
    """原本就是 Disabled 的機器，移除後不該被我們「好心」啟用。"""
    assert "$orig -ne 'Disabled'" in SRC


# --- 用真實 JSON 驗證還原判斷的語意 ----------------------------------------

def _would_restore(record: dict) -> bool:
    """複製解除安裝端的判斷條件，讓語意可以被獨立驗證。"""
    ms = record.get("ms_snmp", {})
    if not (ms.get("disabled_by_us") and ms.get("service_existed")):
        return False
    orig = ms.get("original_start_type")
    return bool(orig) and orig != "Disabled"


def test_first_install_record_restores():
    assert _would_restore({"ms_snmp": {
        "service_existed": True, "original_start_type": "Automatic",
        "original_status": "Running", "disabled_by_us": True}})


def test_record_polluted_by_upgrade_does_not_restore():
    """這就是 bug 的形狀：升級把 Automatic 覆寫成 Disabled 之後，
    還原判斷直接失效。留著這個測試是為了記住它長什麼樣子。"""
    assert not _would_restore({"ms_snmp": {
        "service_existed": True, "original_start_type": "Disabled",
        "original_status": "Stopped", "disabled_by_us": True}})


def test_machine_without_builtin_snmp_restores_nothing():
    assert not _would_restore({"ms_snmp": {
        "service_existed": False, "original_start_type": None,
        "original_status": None, "disabled_by_us": False}})


def test_keepmssnmp_install_restores_nothing():
    assert not _would_restore({"ms_snmp": {
        "service_existed": True, "original_start_type": "Automatic",
        "original_status": "Running", "disabled_by_us": False}})


def test_restore_record_shape_is_json_serialisable():
    """PowerShell 端寫的欄位名與這裡的判斷必須對得起來。"""
    sample = {"schema_version": 1, "ms_snmp": {
        "service_existed": True, "original_start_type": "Automatic",
        "original_status": "Running", "disabled_by_us": True}}
    assert json.loads(json.dumps(sample)) == sample
    for k in sample["ms_snmp"]:
        assert k in SRC, f"PowerShell 端未寫出欄位 {k}"


# --- 安全規則優先於忠實移轉----------------------------------

def test_writable_communities_are_downgraded_not_copied():
    assert "ValidCommunities" in SRC
    assert "trap_destinations" in SRC and "not_imported" in SRC, (
        "trap 與 ExtensionAgents 必須列出但不匯入")
