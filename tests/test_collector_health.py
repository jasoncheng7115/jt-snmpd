"""collector 失敗語意與健康狀態追蹤（spec §6.7 / §6.9 / §7.1）。

為什麼這些語意必須被測試守住：

- **§6.7 啟動絕不硬失敗**：一個 collector 掛掉不能讓整個 agent 垮掉。
  一個拒絕回應的 agent 是隱形的——LibreNMS 只看到裝置 down，
  到現場一看服務是 Running。
- **§6.9 絕不捏造數值**：collector 失敗時該列必須從 snapshot 消失，
  不得回傳 0 或前一次的值。回傳 default（空 list）讓該表不出現，是正確行為。
- **§7.1 jtAgentCollectorTable**：agent 的失效是靜默的，必須能用 LibreNMS
  監控 agent 自己。錯誤計數是累計值，恢復後不歸零——否則間歇性故障
  （最難查的那種）在圖表上會完全看不出來。

這個測試以 ast 抽出 `_collector` 與 `_health` 獨立執行，
因此不需要 Windows，可在 CI 上跑。
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"


def _load_health_module():
    """從 agent 原始碼抽出 _health 與 _collector，建立獨立可測的命名空間。

    直接 import agent 會失敗——它相依 winreg、ctypes.windll、iphlpapi，
    在 Linux 上不存在。抽取法讓核心邏輯可以跨平台測試。
    """
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    ns: dict = {"time": time, "log": lambda _m: None}
    wanted_fn = {"_collector"}
    wanted_assign = {"_health"}
    found = set()

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in wanted_assign:
                    exec(compile(ast.Module([node], []), "<agent>", "exec"), ns)  # noqa: S102
                    found.add(tgt.id)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_fn:
            exec(compile(ast.Module([node], []), "<agent>", "exec"), ns)  # noqa: S102
            found.add(node.name)

    missing = (wanted_fn | wanted_assign) - found
    assert not missing, f"agent 中找不到：{missing}"
    return ns


@pytest.fixture
def health_ns():
    ns = _load_health_module()
    ns["_health"]["collectors"].clear()
    return ns


def test_successful_collector_reports_ok(health_ns):
    collector, health = health_ns["_collector"], health_ns["_health"]
    result = collector("good", lambda: [1, 2, 3], [])
    st = health["collectors"]["good"]
    assert result == [1, 2, 3]
    assert st["status"] == 1, "成功應為 status=1 (ok)"
    assert st["errors"] == 0
    assert st["last_error"] == ""
    assert st["last_ok"] > 0


def test_failing_collector_returns_default_instead_of_raising(health_ns):
    """§6.7：collector 失敗絕不能讓 agent 垮掉。"""
    collector, health = health_ns["_collector"], health_ns["_health"]

    def boom():
        raise OSError("模擬 GetIfTable2 失敗")

    result = collector("bad", boom, [])
    assert result == [], "失敗時必須回傳 default，不可拋出"
    st = health["collectors"]["bad"]
    assert st["status"] == 3, "失敗必須標記 status=3 (failed)"
    assert st["errors"] == 1
    assert "GetIfTable2" in st["last_error"]


def test_default_is_returned_verbatim_not_fabricated(health_ns):
    """§6.9：絕不捏造數值。default 原封不動回傳，讓該表從 snapshot 消失。"""
    collector = health_ns["_collector"]

    def boom():
        raise RuntimeError("x")

    assert collector("a", boom, []) == []
    assert collector("b", boom, None) is None
    sentinel = object()
    assert collector("c", boom, sentinel) is sentinel


def test_error_count_accumulates_across_failures(health_ns):
    collector, health = health_ns["_collector"], health_ns["_health"]

    def boom():
        raise OSError("fail")

    for _ in range(3):
        collector("bad", boom, [])
    assert health["collectors"]["bad"]["errors"] == 3


def test_recovery_clears_status_but_keeps_error_count(health_ns):
    """恢復後錯誤計數**不歸零**。

    間歇性故障是最難查的一種。若恢復時把計數清掉，LibreNMS 的圖表上
    就完全看不出這個 collector 一直在閃斷。
    """
    collector, health = health_ns["_collector"], health_ns["_health"]

    def boom():
        raise OSError("fail")

    collector("flaky", boom, [])
    collector("flaky", boom, [])
    result = collector("flaky", lambda: ["ok"], [])

    st = health["collectors"]["flaky"]
    assert result == ["ok"]
    assert st["status"] == 1, "恢復後 status 必須回到 ok"
    assert st["errors"] == 2, "錯誤計數是累計值，恢復不該歸零"
    assert st["last_error"] == "", "恢復後 last_error 必須清空"


def test_duration_is_recorded(health_ns):
    collector, health = health_ns["_collector"], health_ns["_health"]
    collector("slow", lambda: time.sleep(0.02) or "x", None)
    assert health["collectors"]["slow"]["duration_ms"] >= 15


def test_duration_recorded_even_on_failure(health_ns):
    """失敗也要記錄耗時——卡住的 collector 正是要靠這個發現。"""
    collector, health = health_ns["_collector"], health_ns["_health"]

    def slow_boom():
        time.sleep(0.02)
        raise OSError("fail")

    collector("slowbad", slow_boom, None)
    st = health["collectors"]["slowbad"]
    assert st["duration_ms"] >= 15
    assert st["status"] == 3


def test_each_collector_tracked_independently(health_ns):
    collector, health = health_ns["_collector"], health_ns["_health"]

    def boom():
        raise OSError("fail")

    collector("good", lambda: 1, None)
    collector("bad", boom, None)
    assert health["collectors"]["good"]["status"] == 1
    assert health["collectors"]["bad"]["status"] == 3
    assert health["collectors"]["good"]["errors"] == 0
    assert health["collectors"]["bad"]["errors"] == 1
