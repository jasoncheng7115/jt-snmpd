"""Diagnosability: log rotation, the Event Log, and a service that is alive but dead.

**Why these exist**

The question was a practical one: if something goes wrong, or the service will
not start, how does anyone find out why. The agent did have a log at
`%ProgramData%\\jt-snmpd\\logs\\jt-snmpd.log`, but it was no help in the very case
of the service not starting, and it had two worse problems.

1. **The log grew without limit.** A failing snapshot rebuild writes a line every
   five seconds, seventeen thousand a day. Across hundreds of machines over
   years, the monitoring agent fills the system drive of the host it monitors,
   which is the least acceptable failure available to it.
2. **The service was alive but dead.** `SvcDoRun` started the agent thread and
   then waited on `WaitForSingleObject(hstop, INFINITE)`. If the agent thread
   died during startup -- a failed bind, a failed MIB load, a failed snapshot
   build -- `run_agent` logged and returned, and the service stayed **Running for
   ever**. The SCM says Running, LibreNMS says timeout, and two authorities
   disagreeing is the hardest thing to diagnose in the field. Worse, the
   three-stage recovery configured with `sc failure` never fires, because the
   process never exits.
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
    pytest.fail(f"{name} not found")


# --- log rotation -----------------------------------------------------------

def test_log_has_a_size_cap():
    assert "LOG_MAX_BYTES" in SRC, "the log has no size ceiling and grows without limit"
    for node in TREE.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "LOG_MAX_BYTES":
            cap = eval(ast.unparse(node.value))  # noqa: S307 - the source is a constant in this repo
            assert 0 < cap <= 64 * 1024 * 1024, f"a ceiling of {cap} is not plausible"
            return
    pytest.fail("LOG_MAX_BYTES is not a module-level constant")


def test_rotation_keeps_a_bounded_number_of_generations():
    assert "LOG_KEEP" in SRC
    body = _func("_rotate_log")
    assert "os.remove" in body, "the oldest generation has to be deleted, or the total is still unbounded"
    assert "os.replace" in body, "rotation should use os.replace, which renames atomically on one volume"


def test_log_actually_calls_rotation():
    """A constant and a function that both exist and are never called is the
    easiest silent failure there is."""
    body = _func("log")
    assert "_rotate_log" in body, "log() never triggers rotation"
    assert "LOG_MAX_BYTES" in body, "log() never compares against the ceiling"


def test_size_is_taken_from_the_open_handle():
    """fh.tell() rather than a separate os.stat: stat-ing on every write is disk
    I/O for nothing, against a hard requirement not to slow the host down."""
    body = _func("log")
    assert "fh.tell()" in body, "use fh.tell() for the size rather than stat-ing on every write"


# --- the Event Log ----------------------------------------------------------

def test_errors_reach_the_windows_event_log():
    """Field staff open Event Viewer first, and diagnosing hundreds of machines
    remotely, Get-WinEvent can collect centrally."""
    body = _func("_event_log_error")
    assert "LogErrorMsg" in body, "errors never reach the Event Log"
    # ast.unparse normalises quotes to single ones, so the quotes are not matched
    assert re.search(r"globals\(\)\.get\(.servicemanager.\)", body), (
        "servicemanager is imported further down the module, so it has to be "
        "fetched lazily rather than referenced at module level")


def test_event_log_failure_cannot_kill_the_agent():
    body = _func("_event_log_error")
    assert "except Exception" in body and "pass" in body, (
        "failing to write to the Event Log, on permissions or an unregistered "
        "source, must not bring the agent down with it")


def test_log_supports_an_error_channel():
    body = _func("log")
    assert "error: bool" in body or "error=False" in body, "log() has no error channel"
    assert "_event_log_error" in body


def test_agent_abort_is_reported_as_error():
    """An agent terminating unexpectedly is something the operator needs to know,
    so it goes to the Event Log."""
    body = _func("run_agent")
    assert re.search(r"log\([^)]*terminated abnormally.*error=True", body, re.S), (
        "an unexpected exit from run_agent is not marked as an error")


def test_routine_collector_failures_stay_out_of_the_event_log():
    """The converse. Writing an event for every small collector failure floods
    the Event Log, and the one entry that mattered is then unfindable."""
    body = _func("_collector")
    assert "error=True" not in body, (
        "routine collector failures should not reach the Event Log; they dilute "
        "the entries that matter")


# --- alive but dead ---------------------------------------------------------

def _svc_do_run() -> str:
    i = SRC.find("def SvcDoRun(self):")
    assert i != -1, "SvcDoRun not found"
    j = SRC.find("\n    _HAVE_SERVICE", i)
    return SRC[i:j if j != -1 else len(SRC)]


def test_service_does_not_wait_on_stop_event_alone():
    """The central assertion: waiting on hstop alone is what "alive but dead" is."""
    body = _svc_do_run()
    assert "WaitForSingleObject(self.hstop, win32event.INFINITE)" not in body, (
        "waiting on hstop alone leaves the service Running after the agent "
        "thread has died")
    assert "WaitForMultipleObjects" in body, "both 'stop' and 'the agent died' have to be waited on"


def test_worker_death_is_signalled():
    body = _svc_do_run()
    assert "self.hdead" in body, "there is no agent-died event"
    assert "finally:" in body, "the died event has to fire in a finally, or the exception path misses it"
    i = SRC.find("def __init__(self, args):")
    assert "self.hdead = win32event.CreateEvent" in SRC[i:i + 600], "hdead is never created"


def test_unexpected_death_exits_nonzero_to_trigger_recovery():
    """The three-stage recovery from `sc failure` only applies when the process
    exits. Not exiting makes that configuration decorative."""
    body = _svc_do_run()
    assert "1064" in body, "the SCM should be told ERROR_EXCEPTION_IN_SERVICE (1064)"
    assert "os._exit" in body, "the process has to actually exit, or recovery never fires"
    assert "error=True" in body, "an unexpected exit has to reach the Event Log"


def test_normal_stop_is_not_treated_as_a_crash():
    """A normal stop must not trigger recovery, or the service climbs back up
    after `sc stop`."""
    body = _svc_do_run()
    assert "not self.stop_event.is_set()" in body, (
        "the normal stop path has to be excluded, or SvcStop is undone by recovery")


def test_recovery_is_configured_by_the_installer():
    """The program exiting is only half of it; the installer has to have
    configured the recovery actions for the path to exist at all."""
    cfg = (Path(__file__).resolve().parent.parent / "packaging"
           / "msi-configure.ps1").read_text(encoding="utf-8-sig")
    assert "sc.exe failure" in cfg, "no service recovery actions are configured"
    assert "failureflag" in cfg, "without failureflag, a non-zero exit does not trigger recovery"


# --- where the log lives has to be answerable -------------------------------

def test_log_path_is_exposed_over_snmp():
    """A project rule: "where is the configuration" has to be answerable in four
    places. The same goes for the log, so that diagnosing remotely means a walk
    rather than a guess about which directory on which machine."""
    assert "jtAgentLogPath" in SRC, "the log path is not exposed over SNMP"
    assert "octet(LOG_DIR)" in SRC
