"""Adversarial tests for the pre-parse gate.

This file takes the **attacker's** side, not the user's. Every test asks whether
something can get through, not whether a normal request works.

The threat model: the agent runs continuously as LocalSystem, so any remote code
execution is immediately SYSTEM, and on UDP/161 every byte reaches a pure-Python
BER decoder before authentication happens at all. The primary adversary is
**already inside the network**, so a source address being internal is not grounds
for trust: the rate limit and the malformed-packet check apply to internal
sources too.

Addresses use the RFC 5737 documentation ranges (192.0.2.0/24, 198.51.100.0/24).
What is being tested is network containment, so any range would do, and using the
reserved ones means the privacy scan needs no exception for test fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
from preauth import DropReason, PreAuthGate  # noqa: E402


def _seq(payload: bytes) -> bytes:
    """An outer SEQUENCE with a correct length."""
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


# --- 1. the source allow-list -----------------------------------------------

def test_allowed_source_passes(gate):
    ok, reason = gate.check(GOOD, "192.0.2.68", now=0.0)
    assert ok and reason is None


def test_source_outside_acl_is_dropped_with_zero_parsing(gate):
    ok, reason = gate.check(GOOD, "10.0.0.1", now=0.0)
    assert not ok and reason == DropReason.ACL


def test_acl_is_checked_before_everything_else(gate):
    """The allow-list comes first. A malformed, oversized packet from a rejected
    address must be counted against the allow-list, which is how we know its
    contents were never examined at all."""
    evil = b"\xff" * 100_000
    ok, reason = gate.check(evil, "10.0.0.1", now=0.0)
    assert not ok
    assert reason == DropReason.ACL, "a rejected source must not reach the size or format checks"
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
    # Loopback still has to pass, or the installer's health check fails
    assert g.check(GOOD, "127.0.0.1", now=0.0)[0]


def test_explicit_any_still_works():
    """Writing 0.0.0.0/0 explicitly is how "open to every source" is said aloud."""
    g = PreAuthGate(allowed_networks=PreAuthGate.parse_networks(["0.0.0.0/0"]))
    assert g.check(GOOD, "203.0.113.1", now=0.0)[0]


def test_malformed_source_ip_is_rejected(gate):
    ok, reason = gate.check(GOOD, "not-an-ip", now=0.0)
    assert not ok and reason == DropReason.ACL


def test_ipv4_address_does_not_match_ipv6_network():
    """A version mismatch is simply not a match; it must not raise and turn into
    an exception path."""
    g = PreAuthGate(allowed_networks=PreAuthGate.parse_networks(["2001:db8::/32"]))
    ok, reason = g.check(GOOD, "192.0.2.68", now=0.0)
    assert not ok and reason == DropReason.ACL


def test_single_ip_without_mask_is_host_route():
    g = PreAuthGate(allowed_networks=PreAuthGate.parse_networks(["192.0.2.68"]))
    assert g.check(GOOD, "192.0.2.68", now=0.0)[0]
    assert not g.check(GOOD, "192.0.2.69", now=0.0)[0]


# --- 2. the packet size cap -------------------------------------------------

def test_oversized_packet_dropped(gate):
    ok, reason = gate.check(b"\x30" + b"\x00" * 5000, "192.0.2.68", now=0.0)
    assert not ok and reason == DropReason.OVERSIZE


def test_size_limit_is_checked_before_tlv_parsing(gate):
    """An oversized packet must not reach TLV parsing, which is the way in for
    memory and CPU amplification."""
    gate.check(b"\xff" * 9000, "192.0.2.68", now=0.0)
    assert gate.counters[DropReason.OVERSIZE] == 1
    assert gate.counters[DropReason.MALFORMED] == 0


# --- 3. the rate limit ------------------------------------------------------

def test_burst_is_allowed_then_rate_limited(gate):
    for i in range(100):
        ok, _ = gate.check(GOOD, "192.0.2.68", now=0.0)
        assert ok, f"packet {i} is inside the burst and should not be dropped"
    ok, reason = gate.check(GOOD, "192.0.2.68", now=0.0)
    assert not ok and reason == DropReason.RATE_LIMIT


def test_tokens_refill_over_time(gate):
    for _ in range(100):
        gate.check(GOOD, "192.0.2.68", now=0.0)
    assert not gate.check(GOOD, "192.0.2.68", now=0.0)[0]
    # After a second, rate_pps tokens should be back
    ok, _ = gate.check(GOOD, "192.0.2.68", now=1.0)
    assert ok


def test_rate_limit_is_per_source_not_global(gate):
    """One source must not exhaust a shared allowance, or a single compromised
    machine can starve the real management host."""
    for _ in range(100):
        gate.check(GOOD, "192.0.2.10", now=0.0)
    assert not gate.check(GOOD, "192.0.2.10", now=0.0)[0]
    ok, _ = gate.check(GOOD, "192.0.2.68", now=0.0)
    assert ok, "other sources should be unaffected"


def test_rate_limit_applies_to_allowed_sources_too(gate):
    """The primary adversary is **already inside the network**. Being on the
    allow-list is not grounds for exemption from the rate limit."""
    for _ in range(100):
        gate.check(GOOD, "192.0.2.68", now=0.0)
    ok, reason = gate.check(GOOD, "192.0.2.68", now=0.0)
    assert not ok and reason == DropReason.RATE_LIMIT


# --- 4. outer TLV sanity ----------------------------------------------------

@pytest.mark.parametrize("data,desc", [
    (b"", "empty packet"),
    (b"\x30", "tag only"),
    (b"\x02\x01\x01", "first byte is not 0x30"),
    (b"\x30\x05abc", "declared length exceeds what arrived"),
    (b"\x30\x02abcdef", "declared length is short, with trailing bytes"),
    (b"\x30\x80\x02\x01\x01", "indefinite length"),
    (b"\x30\x85\x01\x01\x01\x01\x01x", "over-long length field"),
    (b"\x30\x84\xff\xff\xff\xffx", "declares a 4 GB length"),
])
def test_malformed_outer_tlv_rejected(gate, data, desc):
    ok, reason = gate.check(data, "192.0.2.68", now=0.0)
    assert not ok, f"should have been rejected: {desc}"
    assert reason == DropReason.MALFORMED, desc


def test_deeply_nested_sequence_is_size_or_tlv_bounded(gate):
    """Deeply nested SEQUENCEs are the way to a RecursionError inside pyasn1.

    The gate does no deep parsing, since parsing is the thing being avoided. A
    nesting attack either exceeds the size cap or fails the outer length check.
    This confirms that one with a correct outer length is still stopped by the
    size cap.
    """
    payload = b"\x05\x00"
    for _ in range(3000):
        payload = _seq(payload)
    ok, reason = gate.check(payload, "192.0.2.68", now=0.0)
    assert not ok
    assert reason == DropReason.OVERSIZE


def test_long_form_length_accepted_when_correct(gate):
    """A valid long-form length must not be rejected; ordinary large GETBULK
    requests use one."""
    body = b"\x04" + bytes([0x82]) + (200).to_bytes(2, "big") + b"A" * 200
    ok, reason = gate.check(_seq(body), "192.0.2.68", now=0.0)
    assert ok, f"a valid long-form length was rejected: {reason}"


# --- counters and memory ----------------------------------------------------

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
    """A flood from spoofed sources would grow the bucket dictionary without
    bound, which is an attack surface in itself.

    Note that none of these sources is on the allow-list, so no bucket should be
    created for them at all.
    """
    for i in range(500):
        gate.check(GOOD, f"10.1.{i // 256}.{i % 256}", now=0.0)
    assert len(gate._buckets) == 0, "a source rejected by the allow-list must not get a bucket"


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


# --- loopback is always allowed ---------------------------------------------

@pytest.mark.parametrize("addr", ["127.0.0.1", "127.0.0.53", "::1"])
def test_loopback_always_allowed_regardless_of_acl(gate, addr):
    """The loopback self-test is the only thing that detects "the service reports
    Running but the event loop is wedged", and the installer's health check relies
    on it. Behind the allow-list, every site's installation fails at its last
    step.

    That happened: the installer reached its final step on real hardware with the
    service working perfectly -- 670 varbinds, listening on 161 -- and the gate
    dropped the loopback query, so the installation was judged a failure.
    """
    ok, reason = gate.check(GOOD, addr, now=0.0)
    assert ok, f"loopback {addr} has to be allowed, but was dropped: {reason}"


def test_loopback_still_subject_to_rate_limit(gate):
    """Allowed is not exempt. Loopback is still rate limited, so a local process
    cannot flood the agent."""
    for _ in range(100):
        gate.check(GOOD, "127.0.0.1", now=0.0)
    ok, reason = gate.check(GOOD, "127.0.0.1", now=0.0)
    assert not ok and reason == DropReason.RATE_LIMIT


def test_loopback_still_subject_to_malformed_check(gate):
    """The malformed-packet check applies to loopback as well."""
    ok, reason = gate.check(b"\x02\x01\x01", "127.0.0.1", now=0.0)
    assert not ok and reason == DropReason.MALFORMED
