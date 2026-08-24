"""前置解析閘門的對抗式測試（spec §3.2 / §3.9）。

這個檔案的立場是**攻擊者**，不是使用者。每個測試都在問「這樣能不能繞過」，
而不是「正常請求會不會通過」。

spec §3.1 的威脅模型指出：agent 以 LocalSystem 常駐，任何 RCE 直接等同 SYSTEM，
而 UDP/161 上每一個位元組都會先經過純 Python 的 BER decoder 才輪到認證。
主要對手是**已在內網的攻擊者**——所以「來源 IP 在內網」不能當成信任依據，
速率限制與畸形封包檢查一樣要對內網來源生效。

位址一律使用 RFC 5737 的文件用保留範圍（192.0.2.0/24、198.51.100.0/24）。
測試的是網段包含關係，用哪一段都一樣；用保留範圍可以讓
個資掃描不必為測試夾具開例外。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
from preauth import DropReason, PreAuthGate  # noqa: E402


def _seq(payload: bytes) -> bytes:
    """組一個長度正確的外層 SEQUENCE。"""
    n = len(payload)
    if n < 0x80:
        return bytes([0x30, n]) + payload
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x30, 0x80 | len(b)]) + b + payload


GOOD = _seq(b"\x02\x01\x01\x04\x06public")


@pytest.fixture
def gate():
    return PreAuthGate(
        allowed_networks=PreAuthGate.parse_networks(["192.0.2.0/24"]),
        rate_pps=50, burst=100,
    )


# --- ① 來源 IP 白名單 -------------------------------------------------------

def test_allowed_source_passes(gate):
    ok, reason = gate.check(GOOD, "192.0.2.68", now=0.0)
    assert ok and reason is None


def test_source_outside_acl_is_dropped_with_zero_parsing(gate):
    ok, reason = gate.check(GOOD, "10.0.0.1", now=0.0)
    assert not ok and reason == DropReason.ACL


def test_acl_is_checked_before_everything_else(gate):
    """ACL 必須是第一道。畸形又超大的封包若來自被拒 IP，
    應以 ACL 記數，代表它根本沒被檢查內容——零解析。"""
    evil = b"\xff" * 100_000
    ok, reason = gate.check(evil, "10.0.0.1", now=0.0)
    assert not ok
    assert reason == DropReason.ACL, "被拒來源不應進入大小或格式檢查"
    assert gate.counters[DropReason.OVERSIZE] == 0
    assert gate.counters[DropReason.MALFORMED] == 0


def test_empty_acl_denies_everything_except_loopback():
    """An unconfigured ACL denies, it does not pass everything through.

    This test asserted the opposite until the config file became something
    operators edit by hand. While the installer was the only author of the
    config, "empty list means no filtering" was merely untidy — the installer
    refuses to proceed without a management network, so the state was
    unreachable in practice.

    Once editing the file by hand is a documented workflow, the same code path
    becomes fail-open: emptying the list, or mistyping the key, silently exposes
    the agent to every source on the network. Nothing warns, and the agent keeps
    answering, so the mistake is invisible.

    Denying instead makes it obvious — monitoring stops. Loopback stays allowed
    so the installer's health check and local diagnosis still work. To serve
    every source deliberately, list 0.0.0.0/0 and ::/0 explicitly.
    """
    g = PreAuthGate()
    ok, reason = g.check(GOOD, "203.0.113.1", now=0.0)
    assert not ok
    assert reason == "acl"
    # loopback 仍必須放行，否則安裝的健康檢查會失敗
    assert g.check(GOOD, "127.0.0.1", now=0.0)[0]


def test_explicit_any_still_works():
    """明確寫出 0.0.0.0/0 才是「刻意開放給所有來源」的表達方式。"""
    g = PreAuthGate(allowed_networks=PreAuthGate.parse_networks(["0.0.0.0/0"]))
    assert g.check(GOOD, "203.0.113.1", now=0.0)[0]


def test_malformed_source_ip_is_rejected(gate):
    ok, reason = gate.check(GOOD, "not-an-ip", now=0.0)
    assert not ok and reason == DropReason.ACL


def test_ipv4_address_does_not_match_ipv6_network():
    """version 不同必須直接不匹配，不可讓 ipaddress 拋出而變成例外路徑。"""
    g = PreAuthGate(allowed_networks=PreAuthGate.parse_networks(["2001:db8::/32"]))
    ok, reason = g.check(GOOD, "192.0.2.68", now=0.0)
    assert not ok and reason == DropReason.ACL


def test_single_ip_without_mask_is_host_route():
    g = PreAuthGate(allowed_networks=PreAuthGate.parse_networks(["192.0.2.68"]))
    assert g.check(GOOD, "192.0.2.68", now=0.0)[0]
    assert not g.check(GOOD, "192.0.2.69", now=0.0)[0]


# --- ② 封包大小上限 ---------------------------------------------------------

def test_oversized_packet_dropped(gate):
    ok, reason = gate.check(b"\x30" + b"\x00" * 5000, "192.0.2.68", now=0.0)
    assert not ok and reason == DropReason.OVERSIZE


def test_size_limit_is_checked_before_tlv_parsing(gate):
    """超大封包不應進入 TLV 解析——那正是記憶體/CPU 放大的入口。"""
    gate.check(b"\xff" * 9000, "192.0.2.68", now=0.0)
    assert gate.counters[DropReason.OVERSIZE] == 1
    assert gate.counters[DropReason.MALFORMED] == 0


# --- ③ 速率限制 -------------------------------------------------------------

def test_burst_is_allowed_then_rate_limited(gate):
    for i in range(100):
        ok, _ = gate.check(GOOD, "192.0.2.68", now=0.0)
        assert ok, f"burst 內第 {i} 個封包不應被擋"
    ok, reason = gate.check(GOOD, "192.0.2.68", now=0.0)
    assert not ok and reason == DropReason.RATE_LIMIT


def test_tokens_refill_over_time(gate):
    for _ in range(100):
        gate.check(GOOD, "192.0.2.68", now=0.0)
    assert not gate.check(GOOD, "192.0.2.68", now=0.0)[0]
    # 1 秒後應補回 rate_pps 個 token
    ok, _ = gate.check(GOOD, "192.0.2.68", now=1.0)
    assert ok


def test_rate_limit_is_per_source_not_global(gate):
    """單一來源不得耗盡全域配額——否則一台被入侵的機器就能讓
    正常管理主機取不到資料。"""
    for _ in range(100):
        gate.check(GOOD, "192.0.2.10", now=0.0)
    assert not gate.check(GOOD, "192.0.2.10", now=0.0)[0]
    ok, _ = gate.check(GOOD, "192.0.2.68", now=0.0)
    assert ok, "其他來源不該受影響"


def test_rate_limit_applies_to_allowed_sources_too(gate):
    """主要對手是**已在內網的攻擊者**。來源 IP 在白名單內
    不能成為免除速率限制的理由。"""
    for _ in range(100):
        gate.check(GOOD, "192.0.2.68", now=0.0)
    ok, reason = gate.check(GOOD, "192.0.2.68", now=0.0)
    assert not ok and reason == DropReason.RATE_LIMIT


# --- ④ 外層 TLV 合法性 ------------------------------------------------------

@pytest.mark.parametrize("data,desc", [
    (b"", "空封包"),
    (b"\x30", "只有 tag"),
    (b"\x02\x01\x01", "第一個 byte 不是 0x30"),
    (b"\x30\x05abc", "宣告長度大於實際"),
    (b"\x30\x02abcdef", "宣告長度小於實際（尾端夾帶）"),
    (b"\x30\x80\x02\x01\x01", "不定長度編碼"),
    (b"\x30\x85\x01\x01\x01\x01\x01x", "長度欄位過長"),
    (b"\x30\x84\xff\xff\xff\xffx", "宣告 4GB 長度"),
])
def test_malformed_outer_tlv_rejected(gate, data, desc):
    ok, reason = gate.check(data, "192.0.2.68", now=0.0)
    assert not ok, f"應拒絕：{desc}"
    assert reason == DropReason.MALFORMED, desc


def test_deeply_nested_sequence_is_size_or_tlv_bounded(gate):
    """深度巢狀 SEQUENCE 是 pyasn1 RecursionError 的入口（spec §3.2）。

    閘門不做深度解析（那正是要避免的），但巢狀攻擊要嘛超過大小上限、
    要嘛外層長度對不上。這裡驗證一個「外層長度正確」的深巢狀封包
    仍會被大小上限攔下。
    """
    payload = b"\x05\x00"
    for _ in range(3000):
        payload = _seq(payload)
    ok, reason = gate.check(payload, "192.0.2.68", now=0.0)
    assert not ok
    assert reason == DropReason.OVERSIZE


def test_long_form_length_accepted_when_correct(gate):
    """合法的長格式長度不能被誤殺——正常的大型 GETBULK 回應請求會用到。"""
    body = b"\x04" + bytes([0x82]) + (200).to_bytes(2, "big") + b"A" * 200
    ok, reason = gate.check(_seq(body), "192.0.2.68", now=0.0)
    assert ok, f"合法長格式被誤殺: {reason}"


# --- 計數器與記憶體 ---------------------------------------------------------

def test_counters_track_each_drop_reason(gate):
    gate.check(GOOD, "10.0.0.1", now=0.0)                    # ACL
    gate.check(b"\x30" + b"\x00" * 9000, "192.0.2.68", now=0.0)  # oversize
    gate.check(b"\x02\x01\x01", "192.0.2.68", now=0.0)     # malformed
    gate.check(GOOD, "192.0.2.68", now=0.0)                # passed
    assert gate.counters[DropReason.ACL] == 1
    assert gate.counters[DropReason.OVERSIZE] == 1
    assert gate.counters[DropReason.MALFORMED] == 1
    assert gate.counters["passed"] == 1


def test_bucket_table_does_not_grow_unbounded(gate):
    """偽造來源 IP 洗一輪就能讓 bucket dict 無限成長——這本身是攻擊面。

    注意：這些來源都不在 ACL 內，所以連 bucket 都不該建立。
    """
    for i in range(500):
        gate.check(GOOD, f"10.1.{i // 256}.{i % 256}", now=0.0)
    assert len(gate._buckets) == 0, "被 ACL 拒絕的來源不該建立 bucket"


def test_prune_removes_idle_buckets(gate):
    for i in range(50):
        gate.check(GOOD, f"192.0.2.{i}", now=0.0)
    assert len(gate._buckets) == 50
    removed = gate.prune(now=1000.0, idle_seconds=300.0)
    assert removed == 50
    assert len(gate._buckets) == 0


def test_prune_keeps_active_buckets(gate):
    gate.check(GOOD, "192.0.2.68", now=0.0)
    gate.check(GOOD, "192.0.2.99", now=900.0)
    gate.prune(now=1000.0, idle_seconds=300.0)
    assert "192.0.2.99" in gate._buckets
    assert "192.0.2.68" not in gate._buckets


# --- loopback 永遠放行（spec §6.5 / §5.7 第 7 步）---------------------------

@pytest.mark.parametrize("addr", ["127.0.0.1", "127.0.0.53", "::1"])
def test_loopback_always_allowed_regardless_of_acl(gate, addr):
    """loopback 自我測試是唯一能偵測「服務 Running 但事件迴圈卡死」的機制，
    安裝程式的健康檢查也靠它。若被 ACL 擋住，每個站台的安裝都會在最後一步失敗。

    這個 bug 實測發生過：安裝程式在實體機上跑到最後一步，服務其實正常
    （670 varbinds、正在聽 161），但 loopback 查詢被閘門丟棄，安裝判定失敗。
    """
    ok, reason = gate.check(GOOD, addr, now=0.0)
    assert ok, f"loopback {addr} 必須永遠放行，實際被擋: {reason}"


def test_loopback_still_subject_to_rate_limit(gate):
    """放行不等於免疫。loopback 仍受速率限制，避免本機行程灌爆 agent。"""
    for _ in range(100):
        gate.check(GOOD, "127.0.0.1", now=0.0)
    ok, reason = gate.check(GOOD, "127.0.0.1", now=0.0)
    assert not ok and reason == DropReason.RATE_LIMIT


def test_loopback_still_subject_to_malformed_check(gate):
    """畸形封包檢查對 loopback 一樣生效。"""
    ok, reason = gate.check(b"\x02\x01\x01", "127.0.0.1", now=0.0)
    assert not ok and reason == DropReason.MALFORMED
