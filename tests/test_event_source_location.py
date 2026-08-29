"""Nothing the Event Log service loads may live in the installation folder.

**Why this file exists**

Driving the graphical upgrade on a real machine on 2026-08-29 produced *two*
"Files in use" pages. The first listed `jt-snmpd`, which is correct and is the
specified behaviour. The second listed `nxlog` and **Windows Event Log**, and the
default button on that page has Windows Installer stop a system service that
other services depend on.

The cause was ours. pywin32's `servicemanager` registers our event source with
`EventMessageFile` pointing at its own `servicemanager.pyd`, which PyInstaller
puts inside the installation folder. The Event Log service loads that DLL to
format our messages and then holds a handle on it, so an upgrade that has to
replace the folder finds the file in use.

Two things had to change together, and this file pins both:

  1. the agent writes events through a source whose message file is in System32
  2. the installer writes that registration too, because a machine upgrading
     from 1.1.2 arrives with the key already pointing into the old folder and it
     has to be corrected *before* the files are replaced

No table read would have found this. The MSI was correct, the WiX source was
correct, and forty lifecycle assertions were green: `/qn` never asks anyone, so
the page only exists for someone double-clicking. It is the same lesson as the
three broken graphical installers before it, which is why the project rule is to
drive the wizard rather than read the artefact.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "deploy" / "jt_agent.py"
CONFIGURE = ROOT / "packaging" / "msi-configure.ps1"


@pytest.fixture(scope="module")
def agent_src() -> str:
    return AGENT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def configure_src() -> str:
    return CONFIGURE.read_bytes().decode("utf-8-sig")


def test_the_agent_no_longer_logs_through_servicemanager(agent_src: str):
    """LogMsg and LogErrorMsg are what register the in-folder message file."""
    calls = [n for n in ast.walk(ast.parse(agent_src))
             if isinstance(n, ast.Attribute)
             and n.attr in {"LogMsg", "LogErrorMsg", "LogInfoMsg", "LogWarningMsg"}]
    assert not calls, (
        "servicemanager's logging helpers register EventMessageFile against "
        "servicemanager.pyd inside the installation folder, which is what put "
        "Windows Event Log on the Files in use page")


def test_initialize_is_given_an_explicit_message_file(agent_src: str):
    """`servicemanager.Initialize()` with no arguments does the same
    registration, so the call has to name a file we do not ship."""
    assert "servicemanager.Initialize(EVENT_SOURCE, _event_message_file())" in agent_src


def test_the_message_file_is_outside_anything_we_install(agent_src: str):
    fn = agent_src[agent_src.index("def _event_message_file"):]
    fn = fn[:fn.index("\n\n\n")]
    assert "SystemRoot" in fn and "System32" in fn
    for bad in ("_internal", "INSTALLFOLDER", "Program Files", "sys.executable",
                "_MEIPASS"):
        assert bad not in fn, (
            f"the message file resolves through {bad}; the whole point is that "
            "an upgrade must be able to replace every file we install while "
            "the Event Log service is running")


def test_the_installer_registers_the_source_itself(configure_src: str):
    """A machine upgrading from 1.1.2 already has the key pointing into the old
    folder. Waiting for the agent to fix it on next start is too late: the files
    are replaced first."""
    assert "EventLog\\\\Application" in configure_src or \
           "EventLog\\Application" in configure_src
    assert re.search(r"EventMessageFile", configure_src)
    assert "EventCreate.exe" in configure_src
    assert "System32" in configure_src


def test_the_installer_removes_the_source_on_uninstall(configure_src: str):
    uninstall = configure_src[:configure_src.index('Log "=== uninstall complete ==="')]
    assert "event source removed" in uninstall


def test_the_lazily_imported_modules_are_bundled():
    """_write_event imports win32evtlog inside the function, so PyInstaller's
    static analysis cannot see it. Missing, every event write fails: harmless by
    design, and it would quietly remove the copy of our errors that Get-WinEvent
    collects."""
    build = (ROOT / "packaging" / "build-exe.ps1").read_bytes().decode("utf-8-sig")
    assert '"win32evtlog"' in build
    assert '"win32evtlogutil"' in build
