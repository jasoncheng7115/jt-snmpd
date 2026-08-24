"""驗證 pre-auth gate 確實掛在 pysnmp 的封包接收路徑上。

**為什麼這個測試是必要的**

第一次實作時我把覆寫方法命名為 `handle_datagram`，但 pysnmp 7.x 的實際掛點是
`datagram_received`。Python 不會因為「覆寫了一個不存在的方法」而報錯——
子類別只是多了一個沒人呼叫的方法，而 `super()` 的原版照常執行。

結果會是：agent 正常啟動、正常回應、測試全過、log 顯示
「pre-auth gate 啟用」——**而閘門完全沒有生效**。所有 ACL、速率限制、
畸形封包檢查都被繞過，攻擊者的位元組長驅直入 BER decoder。

這是資安控制最危險的失效模式：**看起來有防護，實際上沒有**。
所以掛點本身必須被測試釘死，不能只測閘門邏輯。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from pysnmp.carrier.asyncio.dgram import udp

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"


def _gated_transport_methods() -> set[str]:
    """靜態解析 agent，取出 GatedUdpTransport 覆寫了哪些方法。

    用 ast 而非 import：agent 相依 winreg / ctypes.windll，Linux 上無法 import。
    """
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GatedUdpTransport":
            return {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
    pytest.fail("agent 中找不到 GatedUdpTransport 類別")


def _gated_transport_bases() -> list[str]:
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GatedUdpTransport":
            return [ast.unparse(b) for b in node.bases]
    pytest.fail("agent 中找不到 GatedUdpTransport 類別")


def test_gated_transport_subclasses_pysnmp_udp_transport():
    bases = _gated_transport_bases()
    assert any("UdpTransport" in b for b in bases), (
        f"GatedUdpTransport 必須繼承 pysnmp 的 UdpTransport，實際 bases={bases}")


def test_overridden_methods_actually_exist_on_parent():
    """每個覆寫的方法都必須真的存在於父類別。

    覆寫一個不存在的方法在 Python 中完全合法且無聲——這正是本測試要擋的。
    """
    overridden = _gated_transport_methods() - {"__init__"}
    assert overridden, "GatedUdpTransport 沒有覆寫任何方法"

    parent_attrs = set()
    for cls in udp.UdpTransport.__mro__:
        parent_attrs |= set(vars(cls).keys())

    bogus = {m for m in overridden if m not in parent_attrs}
    assert not bogus, (
        f"這些方法在 pysnmp 的 UdpTransport 繼承鏈上不存在，"
        f"覆寫它們不會有任何效果：{sorted(bogus)}。"
        f"可用的接收掛點："
        f"{sorted(a for a in parent_attrs if 'datagram' in a.lower())}")


def test_datagram_received_is_the_hook_point():
    """釘死掛點名稱。pysnmp 若改名，這個測試會先失敗，
    而不是等到資安控制在正式環境無聲失效。"""
    assert "datagram_received" in _gated_transport_methods(), (
        "必須覆寫 datagram_received —— 這是 pysnmp 7.x 的封包接收掛點")
    assert hasattr(udp.UdpTransport, "datagram_received")


def test_override_signature_matches_parent():
    """簽名不符會在執行期才炸，且只在收到封包時才炸。"""
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    ours = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GatedUdpTransport":
            for n in node.body:
                if isinstance(n, ast.FunctionDef) and n.name == "datagram_received":
                    ours = [a.arg for a in n.args.args]
    assert ours is not None, "找不到 datagram_received 覆寫"

    parent = list(inspect.signature(udp.UdpTransport.datagram_received).parameters)
    assert len(ours) == len(parent), (
        f"參數數量不符：我方 {ours} vs pysnmp {parent}")


def test_override_calls_super():
    """放行的封包必須交回父類別，否則 agent 收得到卻不回應——
    又一個「服務 Running 但沒功能」的失效模式。"""
    src = AGENT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GatedUdpTransport":
            for n in node.body:
                if isinstance(n, ast.FunctionDef) and n.name == "datagram_received":
                    body = ast.unparse(n)
                    assert "super().datagram_received" in body, (
                        "放行路徑必須呼叫 super().datagram_received")
                    return
    pytest.fail("找不到 datagram_received")


def test_gate_is_instantiated_and_assigned_to_module_global():
    """閘門必須真的被建立並指派給模組層級的 _gate，
    否則 datagram_received 裡的 `if gate is not None` 永遠為 None，
    等於整個閘門被短路。"""
    src = AGENT.read_text(encoding="utf-8")
    assert "_gate = PreAuthGate(" in src, "必須建立 PreAuthGate 實例並指派給 _gate"
    assert "global _gate" in src, "指派前需宣告 global，否則只是區域變數"
