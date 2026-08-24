"""Base OID 常數對照 RFC 標準值。

為什麼需要這個測試：OID 打錯是**無聲**的。實測踩過一次，ifXTable 寫成
`1.3.6.1.31.1.1.1`（少了 `2.1`），agent 照樣啟動、walk 照樣有回應，
只是那張表整個掛在一個不存在的分支底下。LibreNMS 端的症狀是
「Ports 頁沒有名稱、沒有 64-bit counters」，而 agent 這邊完全看不出異常。

LibreNMS 的 windows.yaml 設了 `ifname: true`，port 標籤直接取自 ifXTable 的
ifName，所以這張表錯了等於 Ports 功能廢掉一半。

這個檔案把每個 base OID 釘死在 RFC 定義上。改動 OID 常數必須同時改這裡，
而改這裡會強迫你去查 RFC，而不是憑印象。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"

# 來源：RFC 1213 / RFC 2863 (IF-MIB) / RFC 2790 (HOST-RESOURCES-MIB) / UCD-SNMP-MIB
EXPECTED = {
    "SYS":    ((1, 3, 6, 1, 2, 1, 1),                  "SNMPv2-MIB::system"),
    "IFT":    ((1, 3, 6, 1, 2, 1, 2, 2, 1),            "IF-MIB::ifEntry"),
    "IFX":    ((1, 3, 6, 1, 2, 1, 31, 1, 1, 1),        "IF-MIB::ifXEntry"),
    "HR":     ((1, 3, 6, 1, 2, 1, 25),                 "HOST-RESOURCES-MIB::host"),
    "HRSTOR": ((1, 3, 6, 1, 2, 1, 25, 2, 3, 1),        "HOST-RESOURCES-MIB::hrStorageEntry"),
    "HRDEV":  ((1, 3, 6, 1, 2, 1, 25, 3, 2, 1),        "HOST-RESOURCES-MIB::hrDeviceEntry"),
    "HRPROC": ((1, 3, 6, 1, 2, 1, 25, 3, 3, 1),        "HOST-RESOURCES-MIB::hrProcessorEntry"),
    "DIO":    ((1, 3, 6, 1, 4, 1, 2021, 13, 15, 1, 1), "UCD-SNMP-MIB::diskIOEntry"),
}


def _module_constants() -> dict[str, tuple[int, ...]]:
    """靜態解析 agent 原始碼取出 base OID 常數。

    用 ast 而不是 import：agent 依賴 winreg / ctypes.windll，在 Linux CI 上
    無法 import。靜態解析讓這個測試在任何平台都能跑。
    """
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    env: dict[str, tuple[int, ...]] = {}

    def resolve(node) -> tuple[int, ...] | None:
        if isinstance(node, ast.Tuple):
            vals = []
            for el in node.elts:
                if isinstance(el, ast.Constant) and isinstance(el.value, int):
                    vals.append(el.value)
                else:
                    return None
            return tuple(vals)
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = resolve(node.left), resolve(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name):
                got = resolve(stmt.value)
                if got is not None:
                    env[target.id] = got
    return env


@pytest.mark.parametrize("name,expected,rfc_name", [
    (k, v[0], v[1]) for k, v in EXPECTED.items()
])
def test_base_oid_matches_rfc(name: str, expected: tuple[int, ...], rfc_name: str):
    consts = _module_constants()
    assert name in consts, f"{name} 未在 agent 中定義"
    got = consts[name]
    assert got == expected, (
        f"{name} 應為 {rfc_name} = {'.'.join(map(str, expected))}，"
        f"實際為 {'.'.join(map(str, got))}"
)


def test_all_base_oids_are_under_iso_org_dod_internet():
    """所有 base OID 必須在 .1.3.6.1 之下。打錯前置碼會讓整張表消失在無效分支。"""
    consts = _module_constants()
    for name in EXPECTED:
        got = consts[name]
        assert got[:4] == (1, 3, 6, 1), f"{name} 前置碼錯誤: {got[:4]}"


def test_enterprise_oids_use_registered_pen():
    """私有分支必須用已註冊的 PEN。2021 = UCD-SNMP（net-snmp），這是 LibreNMS
    讀 diskIO 的標準位置；不可換成自訂 PEN，否則 LibreNMS 抓不到。"""
    consts = _module_constants()
    assert consts["DIO"][:6] == (1, 3, 6, 1, 4, 1), "DIO 必須在 enterprises 之下"
    assert consts["DIO"][6] == 2021, "diskIO 必須用 UCD-SNMP 的 PEN 2021"
