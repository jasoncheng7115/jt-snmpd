"""可診斷性：記錄檔輪替、事件檢視器、以及「服務假活著」。

**這些測試存在的理由**

使用者問了一個很實際的問題：「萬一出問題或無法啟動服務，才好查問題」。
當時 agent 確實有記錄檔（`%ProgramData%\\JT-SNMP\\logs\\jt-snmpd.log`），
但在「服務起不來」這個情境下它幫不上忙，而且有兩個更嚴重的問題：

1. **記錄檔無上限成長。** 快照重建失敗時每 5 秒一行，一天一萬七千行。
   數百台跑數年，監控代理程式把被監控主機的系統碟寫滿——這是最不能接受的失敗。
2. **服務假活著。** `SvcDoRun` 啟動 agent 執行緒後就 `WaitForSingleObject(hstop,
   INFINITE)`。agent 執行緒若在啟動階段死掉（綁定失敗、MIB 載入失敗、快照建置
   失敗），`run_agent` 記錄完就返回，而服務**永遠停在 Running**。
   SCM 說 Running、LibreNMS 說逾時，兩邊說法不一致是現場最難查的狀況。
   更糟的是 `sc failure` 的三段式自動復原設定完全不會觸發——程序沒結束。

spec §6.5 已經點名過「假活著」，而它在這裡換了個地方重演。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"
SRC = AGENT.read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _func(name: str) -> str:
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
    pytest.fail(f"找不到 {name}")


# --- 記錄檔輪替 -------------------------------------------------------------

def test_log_has_a_size_cap():
    assert "LOG_MAX_BYTES" in SRC, "記錄檔缺少大小上限——會無限成長"
    for node in TREE.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "LOG_MAX_BYTES":
            cap = eval(ast.unparse(node.value))  # noqa: S307 —— 來源是本 repo 的常數
            assert 0 < cap <= 64 * 1024 * 1024, f"上限 {cap} 不合理"
            return
    pytest.fail("LOG_MAX_BYTES 不是模組層級常數")


def test_rotation_keeps_a_bounded_number_of_generations():
    assert "LOG_KEEP" in SRC
    body = _func("_rotate_log")
    assert "os.remove" in body, "最舊的一代必須被刪除，否則總量仍無上限"
    assert "os.replace" in body, "輪替應使用 os.replace（同磁碟區原子改名）"


def test_log_actually_calls_rotation():
    """常數與函式都在、但沒被呼叫，是最容易發生的無聲失效。"""
    body = _func("log")
    assert "_rotate_log" in body, "log() 未觸發輪替"
    assert "LOG_MAX_BYTES" in body, "log() 未比對大小上限"


def test_size_is_taken_from_the_open_handle():
    """用 fh.tell() 而非額外 os.stat：每次寫入都 stat 是不必要的磁碟 I/O，
    而本專案的硬性要求是不得拖慢 host。"""
    body = _func("log")
    assert "fh.tell()" in body, "應以 fh.tell() 取得大小，避免每次寫入都 stat"


# --- 事件檢視器 -------------------------------------------------------------

def test_errors_reach_the_windows_event_log():
    """現場人員先看事件檢視器；遠端診斷數百台時 Get-WinEvent 能集中撈。"""
    body = _func("_event_log_error")
    assert "LogErrorMsg" in body, "錯誤未寫入事件檢視器"
    # ast.unparse 會把引號正規化成單引號，故不比對引號本身
    assert re.search(r"globals\(\)\.get\(.servicemanager.\)", body), (
        "servicemanager 於模組後段才 import，必須延遲取得而非模組層級參考")


def test_event_log_failure_cannot_kill_the_agent():
    body = _func("_event_log_error")
    assert "except Exception" in body and "pass" in body, (
        "寫事件記錄失敗（權限不足、事件來源未註冊）不得讓 agent 跟著倒")


def test_log_supports_an_error_channel():
    body = _func("log")
    assert "error: bool" in body or "error=False" in body, "log() 缺少 error 通道"
    assert "_event_log_error" in body


def test_agent_abort_is_reported_as_error():
    """agent 異常終止是「使用者需要知道」的事件，必須進事件檢視器。"""
    body = _func("run_agent")
    assert re.search(r"log\([^)]*terminated abnormally.*error=True", body, re.S), (
        "run_agent 的異常終止未標記為 error")


def test_routine_collector_failures_stay_out_of_the_event_log():
    """反向約束：每個 collector 的小失敗若都寫事件記錄，會把事件檢視器洗掉，
    真正重要的那筆就再也找不到。"""
    body = _func("_collector")
    assert "error=True" not in body, (
        "collector 的例行失敗不應寫入事件檢視器——會稀釋掉真正重要的事件")


# --- 服務假活著 -------------------------------------------------------------

def _svc_do_run() -> str:
    i = SRC.find("def SvcDoRun(self):")
    assert i != -1, "找不到 SvcDoRun"
    j = SRC.find("\n    _HAVE_SERVICE", i)
    return SRC[i:j if j != -1 else len(SRC)]


def test_service_does_not_wait_on_stop_event_alone():
    """核心斷言：只等 hstop 就是「假活著」。"""
    body = _svc_do_run()
    assert "WaitForSingleObject(self.hstop, win32event.INFINITE)" not in body, (
        "只等 hstop 會讓 agent 執行緒死亡後服務仍顯示 Running（spec §6.5 假活著）")
    assert "WaitForMultipleObjects" in body, "必須同時等待「停止」與「agent 已死」"


def test_worker_death_is_signalled():
    body = _svc_do_run()
    assert "self.hdead" in body, "缺少 agent 死亡事件"
    assert "finally:" in body, "死亡事件必須在 finally 中觸發，異常路徑才不會漏掉"
    i = SRC.find("def __init__(self, args):")
    assert "self.hdead = win32event.CreateEvent" in SRC[i:i + 600], "hdead 未建立"


def test_unexpected_death_exits_nonzero_to_trigger_recovery():
    """`sc failure` 的三段式自動復原只在程序結束時生效。
    不結束 = 那段設定形同虛設。"""
    body = _svc_do_run()
    assert "1064" in body, "應以 ERROR_EXCEPTION_IN_SERVICE (1064) 回報 SCM"
    assert "os._exit" in body, "必須實際結束程序，否則自動復原不會觸發"
    assert "error=True" in body, "非預期結束必須進事件檢視器"


def test_normal_stop_is_not_treated_as_a_crash():
    """正常停止時不可誤觸發復原——否則 `sc stop` 之後服務會自己爬回來。"""
    body = _svc_do_run()
    assert "not self.stop_event.is_set()" in body, (
        "必須排除正常停止路徑，否則 SvcStop 後會被自動復原重新拉起")


def test_recovery_is_configured_by_the_installer():
    """程式端結束了，還要安裝端真的設過復原動作，這條路徑才完整。"""
    cfg = (Path(__file__).resolve().parent.parent / "packaging"
           / "msi-configure.ps1").read_text(encoding="utf-8-sig")
    assert "sc.exe failure" in cfg, "未設定服務失效復原動作"
    assert "failureflag" in cfg, "未設 failureflag，非零結束碼不會觸發復原"


# --- 記錄檔位置必須答得出來 -------------------------------------------------

def test_log_path_is_exposed_over_snmp():
    """CLAUDE.md：「設定檔在哪」必須在四處都答得出來。
    記錄檔同理——遠端診斷時 walk 一下就知道要去哪台的哪個目錄撈。"""
    assert "jtAgentLogPath" in SRC, "記錄檔路徑未透過 SNMP 揭露"
    assert "octet(LOG_DIR)" in SRC
