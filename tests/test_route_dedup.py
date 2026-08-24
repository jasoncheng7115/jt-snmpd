"""ipRouteTable 的單索引去重（RFC1213 的固有限制）。

**這個 bug 怎麼被發現的**

在一台有 7 個 IP、多張虛擬網卡的實體筆電上，MSI 安裝失敗（EXIT=1603）。
agent 的記錄檔顯示：

    AssertionError: 重複 OID: (1,3,6,1,2,1,4,21,1,1, 224,0,0,0)

RFC1213 的 `ipRouteTable` 以**目的位址單獨**當索引，一個目的位址只能有一筆。
但真實主機每張網卡都會有自己的多播路由 224.0.0.0、廣播路由 255.255.255.255，
等價多路徑也會產生同一目的位址的多筆路由。

在只有一張網卡的機器上完全不會發生——這是「在 A 機器測過就以為沒問題」
的典型陷阱。

**為什麼是護欄抓到的**

snapshot + bisect 架構的正確性建立在「無重複 OID」之上。
`build_snapshot()` 的重複檢查護欄直接讓 agent 拒絕啟動，
而安裝程式的 loopback 健康檢查又讓整個 MSI 交易回滾。
兩道關卡都發揮了作用——沒有留下一個「看起來裝好了但資料是錯的」安裝。

較新的 `ipForwardTable` / `inetCidrRouteTable` 把介面納入索引，沒有這個限制。
本專案目前只提供 RFC1213 的 `ipRouteTable`（LibreNMS 讀它），
因此必須在來源端去重：同一目的位址只保留 metric 最小的那筆，
也就是實際會被選用的路由。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"
SRC = AGENT.read_text(encoding="utf-8")


def _dedup_by_lowest_metric(routes: list[dict]) -> dict[tuple, dict]:
    """複製 agent 中的去重邏輯，供測試獨立驗證。

    與 agent 的實作保持一致：同一目的位址保留 metric 最小者。
    """
    seen: dict[tuple, dict] = {}
    for rt in routes:
        key = tuple(rt["dest_raw"])
        prev = seen.get(key)
        if prev is None or rt["metric"] < prev["metric"]:
            seen[key] = rt
    return seen


def _r(dest: str, metric: int, if_index: int) -> dict:
    return {"dest": dest, "dest_raw": bytes(int(p) for p in dest.split(".")),
            "metric": metric, "if_index": if_index}


def test_multicast_route_on_every_nic_collapses_to_one():
    """每張網卡都有 224.0.0.0 —— 這是實測觸發 bug 的實際情境。"""
    routes = [_r("224.0.0.0", 256, 5), _r("224.0.0.0", 256, 8),
              _r("224.0.0.0", 256, 17), _r("224.0.0.0", 256, 20)]
    out = _dedup_by_lowest_metric(routes)
    assert len(out) == 1, f"224.0.0.0 應只保留一筆，實得 {len(out)}"


def test_broadcast_route_on_every_nic_collapses_to_one():
    routes = [_r("255.255.255.255", 256, i) for i in (5, 8, 17, 19, 20)]
    assert len(_dedup_by_lowest_metric(routes)) == 1


def test_lowest_metric_wins():
    """保留的必須是實際會被選用的路由，也就是 metric 最小者。"""
    routes = [_r("0.0.0.0", 300, 8), _r("0.0.0.0", 25, 5), _r("0.0.0.0", 100, 20)]
    out = _dedup_by_lowest_metric(routes)
    assert len(out) == 1
    kept = next(iter(out.values()))
    assert kept["metric"] == 25, "應保留 metric 最小的路由"
    assert kept["if_index"] == 5


def test_distinct_destinations_are_all_kept():
    """去重只能作用在相同目的位址上，不可誤刪不同路由。"""
    routes = [_r("0.0.0.0", 0, 5), _r("192.168.1.0", 256, 5),
              _r("127.0.0.0", 256, 1), _r("224.0.0.0", 256, 5)]
    assert len(_dedup_by_lowest_metric(routes)) == 4


def test_equal_metric_keeps_first_deterministically():
    """metric 相同時保留先出現者。重點是**結果必須穩定**——
    每次 walk 給不同答案會讓 LibreNMS 的圖表跳動。"""
    routes = [_r("10.0.0.0", 256, 5), _r("10.0.0.0", 256, 9)]
    out = _dedup_by_lowest_metric(routes)
    assert len(out) == 1
    assert next(iter(out.values()))["if_index"] == 5


def test_empty_route_list():
    assert _dedup_by_lowest_metric([]) == {}


# --- 確認 agent 真的有這段邏輯 ---------------------------------------------

def test_agent_deduplicates_routes():
    """去重必須存在於 agent 中，而不只是測試裡。"""
    assert "_seen_routes" in SRC, "agent 缺少路由去重邏輯"
    assert 'rt["metric"] < prev["metric"]' in SRC, "去重必須以 metric 最小者為準"


def test_agent_still_has_duplicate_oid_guard():
    """重複 OID 護欄不可因為這次修正而被移除。

    它是 snapshot + bisect 正確性的最後一道防線，
    而且正是它抓到了這個 bug。
    """
    assert "重複 OID" in SRC, "build_snapshot 的重複 OID 護欄不可移除"
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_snapshot":
            body = ast.unparse(node)
            assert "AssertionError" in body or "raise" in body, (
                "護欄必須實際拋出，不能只是記錄")
            return
    pytest.fail("找不到 build_snapshot")
