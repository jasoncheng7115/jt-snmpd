"""What a failing collector means, and how its health is tracked.

**Why these semantics need a test holding them in place**

- **Startup does not fail hard.** One broken collector must not take the agent
  down with it. An agent that refuses to answer is invisible: LibreNMS shows the
  device as down, and whoever walks over to the machine finds the service
  Running.
- **What cannot be measured is not reported.** When a collector fails its rows
  disappear from the snapshot rather than being filled with 0 or the previous
  value. Returning the default (an empty list) makes the table absent, which is
  the honest outcome.
- **jtAgentCollectorTable exists because the agent's own failures are silent**,
  so LibreNMS has to be able to monitor the agent itself. The error count
  accumulates and is not reset on recovery: reset it and an intermittent fault,
  which is the hardest kind to find, leaves no trace on the graph at all.

The test lifts `_collector` and `_health` out with ast and runs them on their
own, so it needs no Windows and runs in CI.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"


def _load_health_module():
    """Lift _health and _collector out of the agent source into a namespace of
    their own.

    Importing the agent fails: it needs winreg, ctypes.windll and iphlpapi, none
    of which exist on Linux. Extracting keeps the core logic testable anywhere.
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
    assert not missing, f"not found in the agent: {missing}"
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
    assert st["status"] == 1, "success should be status=1 (ok)"
    assert st["errors"] == 0
    assert st["last_error"] == ""
    assert st["last_ok"] > 0


def test_failing_collector_returns_default_instead_of_raising(health_ns):
    """A failing collector must not bring the agent down."""
    collector, health = health_ns["_collector"], health_ns["_health"]

    def boom():
        raise OSError("simulated GetIfTable2 failure")

    result = collector("bad", boom, [])
    assert result == [], "a failure has to return the default rather than raise"
    st = health["collectors"]["bad"]
    assert st["status"] == 3, "a failure has to be marked status=3 (failed)"
    assert st["errors"] == 1
    assert "GetIfTable2" in st["last_error"]


def test_default_is_returned_verbatim_not_fabricated(health_ns):
    """What cannot be measured is not reported: the default comes back
    untouched, so the table disappears from the snapshot."""
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
    """The error count is **not** reset on recovery.

    An intermittent fault is the hardest kind to find. Clearing the count on
    recovery leaves nothing on the LibreNMS graph to show that this collector
    has been dropping in and out.
    """
    collector, health = health_ns["_collector"], health_ns["_health"]

    def boom():
        raise OSError("fail")

    collector("flaky", boom, [])
    collector("flaky", boom, [])
    result = collector("flaky", lambda: ["ok"], [])

    st = health["collectors"]["flaky"]
    assert result == ["ok"]
    assert st["status"] == 1, "status has to return to ok after recovery"
    assert st["errors"] == 2, "the error count accumulates and must survive recovery"
    assert st["last_error"] == "", "last_error has to be cleared on recovery"


# Windows' default timer granularity is about 15.6 ms, so a 20 ms sleep can be
# measured as 14 ms. The floor here is deliberately loose: what these tests care
# about is that a duration is recorded at all and that it reflects the sleep
# rather than being zero — not that the clock is precise. Asserting >= 15 made
# the Windows CI job fail on `assert 14 >= 15`, which says nothing about the
# behaviour under test.
SLEEP_MS = 20
MIN_MEASURED_MS = 8


def test_duration_is_recorded(health_ns):
    collector, health = health_ns["_collector"], health_ns["_health"]
    collector("slow", lambda: time.sleep(SLEEP_MS / 1000) or "x", None)
    assert health["collectors"]["slow"]["duration_ms"] >= MIN_MEASURED_MS


def test_duration_recorded_even_on_failure(health_ns):
    """A failure is timed too: a collector that hangs is found by this and
    nothing else."""
    collector, health = health_ns["_collector"], health_ns["_health"]

    def slow_boom():
        time.sleep(SLEEP_MS / 1000)
        raise OSError("fail")

    collector("slowbad", slow_boom, None)
    st = health["collectors"]["slowbad"]
    assert st["duration_ms"] >= MIN_MEASURED_MS
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
