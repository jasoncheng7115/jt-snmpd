"""解除安裝與 PURGE 完整清除的行為（靜態守則）。

**這個 bug 怎麼被發現的**

完整生命週期測試（安裝 → 升級 → 移除 → 重裝 → PURGE 移除）跑到最後一項失敗：
`PURGE=1` 之後 `C:\\ProgramData\\JT-SNMP` 仍然存在，裡面剩下 `logs\\msi-configure.log`。

原因是自訂動作的記錄檔就放在**它自己要清除的目錄裡**。`Remove-Item` 確實刪掉了整個
目錄，但緊接著的兩行 `Log` 又把 `logs\\` 重新建出來——清除動作被自己的收尾訊息推翻。

**為什麼一般測試抓不到**

`Remove-Item` 成功、結束碼 0、記錄檔也寫著「資料目錄已完整清除」。從 msiexec、
從服務狀態、從程式目錄看全都正常，只有真的去看資料目錄才會發現殘骸。而殘骸的後果
是延遲性的：下次安裝會沿用舊狀態，讓「移除再重裝」這個客戶最常用的排除手段失效。

同一段程式碼還有第二個問題：`Remove-Item ... -ErrorAction SilentlyContinue` 會把
「檔案被鎖住刪不掉」也吞掉，一樣回報成功。服務剛停止時 DPAPI blob 或記錄檔可能還被
短暫持有，這不是假設性情境。

這個測試把兩件事釘住：清除前必須停止檔案記錄，且清除後必須驗證結果、失敗時不得謊報。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "packaging"
CONFIGURE = PKG / "msi-configure.ps1"
SRC = CONFIGURE.read_text(encoding="utf-8-sig")


def _purge_block() -> str:
    """取出 `if ($Purge -eq '1') { ... }` 的 then 區塊。

    用括號配對而非搜尋第一個 `} else {`——then 區塊裡本身就有巢狀的 if/else
    （驗證清除結果），第一次寫的時候正是被這個絆倒。
    """
    i = SRC.find("if ($Purge -eq '1')")
    assert i != -1, "找不到 PURGE 分支"
    start = SRC.index("{", i)
    depth = 0
    for k in range(start, len(SRC)):
        if SRC[k] == "{":
            depth += 1
        elif SRC[k] == "}":
            depth -= 1
            if depth == 0:
                return SRC[start:k + 1]
    pytest.fail("PURGE 分支的大括號未閉合")


# --- 記錄檔重建陷阱 ---------------------------------------------------------

def test_log_writes_are_gated_by_a_flag():
    """Log 必須能被關閉，否則清除後任何一行訊息都會重建目錄。"""
    assert "$script:LogToFile" in SRC, (
        "Log 缺少可關閉的旗標——PURGE 後的收尾訊息會把 logs\\ 重建回來")
    i = SRC.find("function Log {")
    body = SRC[i:SRC.find("\n}", i)]
    assert "if ($script:LogToFile)" in body, "Log 的寫檔動作必須受旗標保護"
    assert "Add-Content" in body


def test_file_logging_is_disabled_before_the_delete():
    """順序很重要：必須先關記錄再刪，反過來沒有意義。"""
    block = _purge_block()
    off = block.find("$script:LogToFile = $false")
    rm = block.find("Remove-Item $DATA_DIR")
    assert off != -1, "PURGE 前未關閉檔案記錄"
    assert rm != -1, "PURGE 分支未刪除資料目錄"
    assert off < rm, "必須在刪除**之前**關閉檔案記錄"


def test_log_dir_lives_inside_data_dir():
    """這個測試的前提：記錄檔確實在要被清除的目錄裡。

    若哪天記錄檔搬到 %TEMP%，上面兩個斷言就不再必要——但那要是個明確的決定，
    而不是無聲的漂移。
    """
    assert "$LOG_DIR     = Join-Path $DATA_DIR 'logs'" in SRC.replace("  ", " ").replace(
        "$LOG_DIR = Join-Path $DATA_DIR 'logs'", "$LOG_DIR     = Join-Path $DATA_DIR 'logs'"
    ) or re.search(r"\$LOG_DIR\s*=\s*Join-Path \$DATA_DIR 'logs'", SRC), (
        "記錄檔位置改變時，請一併檢視 PURGE 的關閉記錄邏輯是否仍需要")


# --- 不得謊報成功 -----------------------------------------------------------

def test_purge_verifies_the_directory_is_actually_gone():
    """spec §6.9 的精神：不得回報未經驗證的結果。"""
    block = _purge_block()
    assert "Test-Path $DATA_DIR" in block, (
        "刪除後必須實際驗證目錄消失，不能只看 Remove-Item 沒拋錯")


def test_purge_retries_because_files_may_still_be_locked():
    """服務剛停止時 DPAPI blob / 記錄檔可能仍被持有。"""
    block = _purge_block()
    assert re.search(r"foreach \(\$attempt in 1\.\.\d+\)", block), "清除必須重試"
    assert "Start-Sleep" in block, "重試之間必須等待"


def test_failed_purge_is_reported_not_swallowed():
    """失敗時必須留下警告——殘骸會讓下次安裝沿用舊狀態。"""
    block = _purge_block()
    assert "WARN" in block, "清除失敗必須以 WARN 回報"
    ok = block.find('Log "data directory completely removed')
    assert ok != -1, "找不到成功訊息"
    # 成功訊息必須在 $purged 為真的分支裡
    assert "if ($purged)" in block, "成功訊息必須以實際驗證結果為條件"
    assert block.find("if ($purged)") < ok, "成功訊息不可無條件輸出"


# --- 預設（非 PURGE）行為 ---------------------------------------------------

def test_default_uninstall_keeps_data_dir():
    """spec §5.7：預設保留是刻意的。

    客戶常以「移除再重裝」排除問題；索引被清掉會讓 LibreNMS 重新 discovery，
    舊 RRD 全數失去對應。
    """
    i = SRC.find("if ($Purge -eq '1')")
    j = SRC.find("Log \"=== 解除安裝完成 ===\"", i)
    tail = SRC[i:j]
    else_i = tail.find("} else {")
    assert else_i != -1
    else_block = tail[else_i:]
    assert "Remove-Item $DATA_DIR" not in else_block, (
        "預設解除安裝不可刪除資料目錄")
    assert "data directory kept" in else_block


def test_uninstall_restores_builtin_snmp():
    """接管內建 SNMP 的機器，移除後必須還原原本的啟動類型與狀態。"""
    assert "Set-Service -Name SNMP -StartupType $orig" in SRC
    assert "original_status" in SRC, "還原時必須考慮原本是否在執行中"


def test_purge_property_is_secured_or_documented():
    """PURGE 是破壞性屬性，必須在 wxs 中有定義才會被自訂動作看見。"""
    wxs = (PKG / "wix" / "jt-snmpd.wxs").read_text(encoding="utf-8-sig")
    assert "PURGE" in wxs, "wxs 未定義 PURGE 屬性，PURGE=1 將無效"
