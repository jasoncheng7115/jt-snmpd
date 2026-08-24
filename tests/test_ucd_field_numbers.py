"""UCD-SNMP-MIB systemStats 欄位編號對照（釘死在 MIB 定義上）。

**這個測試為什麼存在**

實作 UCD systemStats 時，我憑直覺把 57~63 排成
SwapIn / SwapOut / IOSent / IOReceived / Contexts / Interrupts，
但 UCD-SNMP-MIB 的實際順序是
IOSent(57) / IOReceived(58) / Interrupts(59) / Contexts(60) / SwapIn(62) / SwapOut(63)。

**錯位完全不會被察覺**：agent 正常啟動、SNMP walk 有回應、LibreNMS 圖表有線、
數字持續變動，只是 context switches 被畫在 I/O 圖上。既有的
「無重複 OID」「排序正確」「回應大小」測試一個都抓不到，因為結構完全合法，
只是語意錯了。

要發現它，唯一的方法是拿 MIB 名稱去解析我們的輸出：

    snmpwalk -m UCD-SNMP-MIB -O QUs <host> systemStats

這個測試把編號釘死，改動時必須同步改這裡，而改這裡會強迫去查 MIB。
權威來源：

    snmptranslate -m UCD-SNMP-MIB -On UCD-SNMP-MIB::<name>
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"
SRC = AGENT.read_text(encoding="utf-8")

# 來源：snmptranslate -m UCD-SNMP-MIB -On UCD-SNMP-MIB::<name>
# 於 LibreNMS 26.8.1 隨附的 UCD-SNMP-MIB 實測取得
UCD_SYSTEMSTATS = {
    1: "ssIndex",
    2: "ssErrorName",
    50: "ssCpuRawUser",
    51: "ssCpuRawNice",
    52: "ssCpuRawSystem",
    53: "ssCpuRawIdle",
    54: "ssCpuRawWait",
    55: "ssCpuRawKernel",
    56: "ssCpuRawInterrupt",
    57: "ssIORawSent",
    58: "ssIORawReceived",
    59: "ssRawInterrupts",
    60: "ssRawContexts",
    61: "ssCpuRawSoftIRQ",
    62: "ssRawSwapIn",
    63: "ssRawSwapOut",
    64: "ssCpuRawSteal",
    65: "ssCpuRawGuest",
}

# agent 中每個 UCDSS 欄位旁的註解必須是正確的 MIB 名稱。
# 呼叫可能跨多行（參數換行），因此從 add(UCDSS + (N, 0) 起往後找最近的
# `# ss<Name>` 註解，而不是只看同一行。
_EMIT = re.compile(r"add\(UCDSS \+ \((\d+), 0\)(.{0,200}?)#\s*(ss\w+)", re.S)


def _emitted() -> dict[int, str]:
    """從原始碼取出「欄位編號 → 註解中的名稱」。"""
    out: dict[int, str] = {}
    for m in _EMIT.finditer(SRC):
        num = int(m.group(1))
        # 中間不可跨到下一個 add(UCDSS，那代表這個欄位自己沒有註解
        if "add(UCDSS" in m.group(2):
            continue
        out.setdefault(num, m.group(3))
    return out


def test_ucd_base_oid_is_correct():
    """UCDSS 必須是 .1.3.6.1.4.1.2021.11（UCD-SNMP-MIB::systemStats）。"""
    tree = ast.parse(SRC)
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "UCDSS":
            got = tuple(e.value for e in node.value.elts)
            assert got == (1, 3, 6, 1, 4, 1, 2021, 11), (
                f"UCDSS 應為 .1.3.6.1.4.1.2021.11，實際 {got}")
            return
    pytest.fail("找不到 UCDSS 定義")


def test_every_emitted_field_has_correct_mib_name():
    """每個輸出欄位旁註解的名稱，必須與 MIB 中該編號的名稱一致。

    這是防止「憑直覺編號」的核心斷言。
    """
    emitted = _emitted()
    assert emitted, "找不到任何 UCDSS 欄位輸出"
    wrong = {n: (name, UCD_SYSTEMSTATS.get(n))
             for n, name in emitted.items()
             if UCD_SYSTEMSTATS.get(n) != name}
    assert not wrong, (
        "欄位編號與 MIB 名稱不符（格式：編號: (程式碼中的名稱, MIB 正確名稱)）：\n"
        + "\n".join(f"  {n}: 程式碼寫 {a}，MIB 實際是 {b}" for n, (a, b) in wrong.items()))


@pytest.mark.parametrize("field,expected_num", [
    ("ssCpuRawUser", 50), ("ssCpuRawNice", 51), ("ssCpuRawSystem", 52),
    ("ssCpuRawIdle", 53), ("ssCpuRawInterrupt", 56),
    ("ssIORawSent", 57), ("ssIORawReceived", 58),
    ("ssRawInterrupts", 59), ("ssRawContexts", 60),
    ("ssRawSwapIn", 62), ("ssRawSwapOut", 63),
])
def test_required_field_emitted_at_correct_number(field: str, expected_num: int):
    """LibreNMS 實際會讀的欄位，必須以正確編號輸出。"""
    emitted = _emitted()
    assert expected_num in emitted, f"{field} (欄位 {expected_num}) 未輸出"
    assert emitted[expected_num] == field


def test_cpu_four_fields_all_present_for_librenms():
    """LibreNMS 的 ucd-mib poller 要求 user/nice/system/idle **四個都存在**
    才建立 Detailed Processor Usage 圖表：

        if (isset($ss['ssCpuRawUser']) && isset($ss['ssCpuRawNice'])
            && isset($ss['ssCpuRawSystem']) && isset($ss['ssCpuRawIdle']))

    Windows 沒有 nice，但輸出 0 是「Windows 上永遠沒有 nice 時間」的正確陳述，
    與「無法量測」的 iowait / steal 不同。少了它整張圖表就不會出現。
    """
    emitted = _emitted()
    for num, name in ((50, "ssCpuRawUser"), (51, "ssCpuRawNice"),
                      (52, "ssCpuRawSystem"), (53, "ssCpuRawIdle")):
        assert emitted.get(num) == name, (
            f"{name} 未輸出，LibreNMS 會因此不建立 Detailed Processor Usage 圖表")


def test_unmeasurable_fields_are_not_emitted():
    """無法在 Windows 上量測的欄位必須**不輸出**，而不是填 0。

    填 0 會讓 LibreNMS 建立圖表並畫出一條零線，看起來像「量測過且為零」，
    實際上是「根本無法量測」。量不到就不回報。
    """
    emitted = _emitted()
    for num, name in ((54, "ssCpuRawWait"), (64, "ssCpuRawSteal"),
                      (61, "ssCpuRawSoftIRQ"), (65, "ssCpuRawGuest")):
        assert num not in emitted, (
            f"{name} (欄位 {num}) 在 Windows 上無法量測，不應輸出")


def test_userhz_conversion_documented():
    """UCD 的 ssCpuRaw* 單位是 USER_HZ（1/100 秒），Windows 是 100ns，
    相差 10^5。搞錯係數會讓百分比完全失真。"""
    assert "100_000" in SRC or "100000" in SRC, "缺少 USER_HZ 換算"
    assert "USER_HZ" in SRC, "換算係數應有註解說明來源"
