"""Deduplicating ipRouteTable, which RFC 1213 indexes by destination alone.

**How this was found**

On a physical laptop with seven addresses and several virtual adapters, the MSI
failed to install with 1603. The agent log said:

    AssertionError: duplicate OID: (1,3,6,1,2,1,4,21,1,1, 224,0,0,0)

RFC 1213's `ipRouteTable` is indexed by **destination address alone**, so one
destination can have exactly one row. A real host has a 224.0.0.0 multicast route
and a 255.255.255.255 broadcast route per adapter, and equal-cost multipath
produces several routes to the same destination as well.

On a machine with one adapter this never happens, which makes it the textbook
case of "it worked on the machine I tested on".

**Why a guard caught it**

The correctness of snapshot + bisect rests on there being no duplicate OIDs. The
duplicate check in `build_snapshot()` made the agent refuse to start, and the
installer's loopback health check then rolled the whole MSI transaction back.
Both gates did their job, and no installation was left looking healthy while
serving wrong data.

The newer `ipForwardTable` and `inetCidrRouteTable` include the interface in the
index and have no such limit. This project serves RFC 1213's `ipRouteTable`
because that is what LibreNMS reads, so the deduplication has to happen at the
source: one row per destination, keeping the lowest metric, which is the route
that would actually be used.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"
SRC = AGENT.read_text(encoding="utf-8")


def _dedup_by_lowest_metric(routes: list[dict]) -> dict[tuple, dict]:
    """A copy of the agent's deduplication, so it can be exercised on its own.

    It matches the agent: for one destination, keep the lowest metric.
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
    """Every adapter has a 224.0.0.0 route. This is the case that actually broke."""
    routes = [_r("224.0.0.0", 256, 5), _r("224.0.0.0", 256, 8),
              _r("224.0.0.0", 256, 17), _r("224.0.0.0", 256, 20)]
    out = _dedup_by_lowest_metric(routes)
    assert len(out) == 1, f"224.0.0.0 should keep exactly one row, got {len(out)}"


def test_broadcast_route_on_every_nic_collapses_to_one():
    routes = [_r("255.255.255.255", 256, i) for i in (5, 8, 17, 19, 20)]
    assert len(_dedup_by_lowest_metric(routes)) == 1


def test_lowest_metric_wins():
    """The row kept has to be the route that would be used: the lowest metric."""
    routes = [_r("0.0.0.0", 300, 8), _r("0.0.0.0", 25, 5), _r("0.0.0.0", 100, 20)]
    out = _dedup_by_lowest_metric(routes)
    assert len(out) == 1
    kept = next(iter(out.values()))
    assert kept["metric"] == 25, "the lowest-metric route should be the one kept"
    assert kept["if_index"] == 5


def test_distinct_destinations_are_all_kept():
    """Deduplication applies within one destination and must not drop others."""
    routes = [_r("0.0.0.0", 0, 5), _r("192.168.1.0", 256, 5),
              _r("127.0.0.0", 256, 1), _r("224.0.0.0", 256, 5)]
    assert len(_dedup_by_lowest_metric(routes)) == 4


def test_equal_metric_keeps_first_deterministically():
    """On a tie the first wins. What matters is that the result is **stable**:
    a walk that answers differently each time makes LibreNMS's graphs jump."""
    routes = [_r("10.0.0.0", 256, 5), _r("10.0.0.0", 256, 9)]
    out = _dedup_by_lowest_metric(routes)
    assert len(out) == 1
    assert next(iter(out.values()))["if_index"] == 5


def test_empty_route_list():
    assert _dedup_by_lowest_metric([]) == {}


# --- confirm the agent really has this logic --------------------------------

def test_agent_deduplicates_routes():
    """The deduplication has to live in the agent, not only in this test."""
    assert "_seen_routes" in SRC, "the agent has no route deduplication"
    assert 'rt["metric"] < prev["metric"]' in SRC, "deduplication has to keep the lowest metric"


def test_agent_still_has_duplicate_oid_guard():
    """Fixing the cause must not remove the guard.

    It is the last line of defence for snapshot + bisect's correctness, and it is
    what caught this in the first place.
    """
    assert "duplicate OID" in SRC, "build_snapshot's duplicate-OID guard is gone"
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_snapshot":
            body = ast.unparse(node)
            assert "AssertionError" in body or "raise" in body, (
                "the guard has to raise, not merely log")
            return
    pytest.fail("build_snapshot not found")
