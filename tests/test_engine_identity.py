"""The SNMPv3 engine identity: engineID stability and snmpEngineBoots.

Three failures this guards against, none of which can be produced on demand on
a real machine, which is why the decision is a pure function and tested here.

**A cloned VM.** Customer estates are built from Proxmox and Hyper-V templates.
If a template is captured after the agent has run once, every clone answers with
the same engineID. The manager then keeps a single boots/time pair for what it
believes is one engine, and SNMPv3 authentication fails intermittently across
the whole estate with nothing in the logs to explain it.

**A corrupted state file.** Treating an unreadable engine.json as "no state"
would restart snmpEngineBoots at 1 and reopen the replay window the counter
exists to close.

**A saturated counter.** RFC 3414 §2.2 requires a new engineID at 2^31-1 rather
than a wrap.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1] / "deploy" / "jt_agent.py"

GUID_A = "11111111-2222-3333-4444-555555555555"
GUID_B = "99999999-8888-7777-6666-555555555555"


def _extract() -> dict:
    """Pull the pure parts of the engine identity out of the agent source.

    Importing the agent fails on Linux: it needs winreg, ctypes.windll and
    iphlpapi. Extracting keeps the logic testable anywhere.
    """
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    ns: dict = {"hashlib": hashlib}
    wanted_fn = {"_plan_engine_state", "_new_engine_id"}
    wanted_assign = {"ENGINE_BOOTS_MAX", "ENGINE_PEN"}
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


NS = _extract()
plan = NS["_plan_engine_state"]
new_id = NS["_new_engine_id"]
BOOTS_MAX = NS["ENGINE_BOOTS_MAX"]


def _first(guid: str = GUID_A, boot_key: int = 100) -> dict:
    state, _ = plan({}, guid, boot_key, new_id(guid))
    return state


# --- engineID ---------------------------------------------------------------

def test_engine_id_is_rfc3411_shaped():
    raw = bytes.fromhex(new_id(GUID_A))
    assert raw[0] & 0x80, "the top bit marks the RFC 3411 format and must be set"
    pen = ((raw[0] & 0x7F) << 24) | (raw[1] << 16) | (raw[2] << 8) | raw[3]
    assert pen == NS["ENGINE_PEN"]
    assert raw[4] == 4, "format 4, administratively assigned text"
    assert 5 <= len(raw) <= 32, "RFC 3411 §5 caps the identifier at 32 bytes"


def test_engine_id_differs_per_machine():
    assert new_id(GUID_A) != new_id(GUID_B)


def test_engine_id_is_stable_for_one_machine():
    assert new_id(GUID_A) == new_id(GUID_A)


def test_engine_id_survives_a_restart_within_the_same_boot():
    first = _first()
    again, reasons = plan(first, GUID_A, first["boot_key"], new_id(GUID_A))
    assert again["engine_id"] == first["engine_id"]
    assert again["boots"] == first["boots"], "a service restart is not a boot"
    assert reasons == []


# --- the cloned VM ----------------------------------------------------------

def test_a_cloned_machine_gets_a_new_engine_id_and_resets_boots():
    """The template was captured after the agent had already run."""
    cloned_from = {"schema_version": 2, "machine_guid": GUID_A,
                   "engine_id": new_id(GUID_A), "boot_key": 100, "boots": 57}
    state, reasons = plan(cloned_from, GUID_B, 900, new_id(GUID_B))
    assert state["engine_id"] == new_id(GUID_B)
    assert state["engine_id"] != cloned_from["engine_id"]
    assert state["machine_guid"] == GUID_B
    assert state["boots"] == 1, "a brand new identity starts its own count"
    assert reasons, "a silent reidentification is unsupportable"
    assert "cloned" in reasons[0]
    assert "provisioned again" in reasons[0], (
        "the operator has to be told their v3 users need re-provisioning, "
        "because a localised key is bound to the engineID it was made for")


def test_the_original_machine_is_untouched_by_the_clone_rule():
    state, reasons = plan(_first(), GUID_A, 101, new_id(GUID_A))
    assert state["engine_id"] == new_id(GUID_A)
    assert reasons == []


# --- snmpEngineBoots --------------------------------------------------------

def test_boots_increments_once_per_boot():
    state = _first()
    for expected, boot_key in enumerate([200, 300, 400], start=2):
        state, _ = plan(state, GUID_A, boot_key, new_id(GUID_A))
        assert state["boots"] == expected


def test_boots_never_starts_below_one():
    assert _first()["boots"] == 1


@pytest.mark.parametrize("damaged", [
    {}, None, [], "", {"boots": 5},                       # no identity at all
    {"engine_id": "", "machine_guid": GUID_A, "boots": 5},
    {"engine_id": None, "machine_guid": GUID_A, "boots": 5},
])
def test_a_damaged_file_still_yields_a_usable_identity(damaged):
    state, reasons = plan(damaged, GUID_A, 100, new_id(GUID_A))
    assert state["engine_id"] == new_id(GUID_A)
    assert state["boots"] >= 1
    assert reasons


def test_a_json_true_is_not_counted_as_one_boot():
    """bool is a subclass of int, so a hand-edited `true` would slip through."""
    state, _ = plan({"engine_id": new_id(GUID_A), "machine_guid": GUID_A,
                     "boot_key": 100, "boots": True}, GUID_A, 100, new_id(GUID_A))
    assert state["boots"] == 1


def test_a_negative_count_cannot_drag_boots_backwards():
    state, _ = plan({"engine_id": new_id(GUID_A), "machine_guid": GUID_A,
                     "boot_key": 100, "boots": -9000}, GUID_A, 555, new_id(GUID_A))
    assert state["boots"] >= 1


def test_boots_is_monotonic_across_a_long_run_of_boots():
    state, seen = _first(), []
    for boot_key in range(101, 141):
        state, _ = plan(state, GUID_A, boot_key, new_id(GUID_A))
        seen.append(state["boots"])
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen), "a repeated pair breaks replay protection"


def test_the_ceiling_forces_a_new_engine_id():
    """RFC 3414 §2.2: regenerate rather than wrap."""
    saturated = {"schema_version": 2, "machine_guid": GUID_A,
                 "engine_id": new_id(GUID_A), "boot_key": 100,
                 "boots": BOOTS_MAX}
    state, reasons = plan(saturated, GUID_A, 101, new_id(GUID_A))
    assert state["boots"] == 1
    assert any("ceiling" in r for r in reasons)


def test_boots_never_exceeds_the_ceiling():
    state, _ = plan({"engine_id": new_id(GUID_A), "machine_guid": GUID_A,
                     "boot_key": 100, "boots": BOOTS_MAX + 5000},
                    GUID_A, 100, new_id(GUID_A))
    assert 1 <= state["boots"] <= BOOTS_MAX


# --- the file the agent writes ----------------------------------------------

def test_the_recorded_state_carries_what_the_next_start_needs():
    state = _first()
    assert set(state) == {"schema_version", "machine_guid", "engine_id",
                          "boot_key", "boots"}
    assert state["schema_version"] == 2, (
        "0.9.x through 1.0.0 wrote schema 1, which had no machine_guid and no "
        "engine_id; the bump is what tells them apart")


def test_a_schema_1_file_is_read_without_losing_the_boot_count():
    """Upgrading from 1.0.0 must not restart the counter."""
    old = {"schema_version": 1, "boot_key": 100, "boots": 44}
    state, reasons = plan(old, GUID_A, 100, new_id(GUID_A))
    assert state["boots"] == 44, "the count carries across the schema change"
    assert state["engine_id"] == new_id(GUID_A)
    assert reasons, "generating the first engine identity is worth a log line"


# --- The hostname fallback --------------------------------------------------

def test_a_rename_is_not_reported_as_a_clone():
    """MachineGuid is read from the registry, and when that fails the identity
    falls back to the hostname. A hostname is not stable — renaming a machine is
    routine — so a rename then changes the engineID and kills every SNMPv3
    account on the host.

    Reporting that as "the machine was cloned or reimaged" would be untrue and
    would send whoever read it looking for a template that does not exist. The
    two cases are told apart by the source recorded alongside the identity.
    """
    before = {"schema_version": 2, "machine_guid": "OLD-HOSTNAME",
              "engine_id": new_id("OLD-HOSTNAME"), "boot_key": 100, "boots": 9,
              "identity_source": "hostname"}
    _, reasons = plan(before, "NEW-HOSTNAME", 101, new_id("NEW-HOSTNAME"))
    joined = " ".join(reasons)
    assert "rename, not a clone" in joined
    assert "cloned or reimaged" not in joined
    assert "MachineGuid" in joined, "say what to fix so it stops happening"


def test_a_real_clone_still_reads_as_a_clone():
    before = {"schema_version": 2, "machine_guid": GUID_A,
              "engine_id": new_id(GUID_A), "boot_key": 100, "boots": 9,
              "identity_source": "machine-guid"}
    _, reasons = plan(before, GUID_B, 101, new_id(GUID_B))
    assert "cloned" in " ".join(reasons)


def test_an_older_file_without_the_source_reads_as_a_clone():
    """1.1.0 added identity_source. A file written before it has no such key,
    and the previous behaviour is the right default: those machines did read
    MachineGuid successfully in the overwhelming majority of cases."""
    before = {"schema_version": 2, "machine_guid": GUID_A,
              "engine_id": new_id(GUID_A), "boot_key": 100, "boots": 9}
    _, reasons = plan(before, GUID_B, 101, new_id(GUID_B))
    assert "cloned" in " ".join(reasons)


def test_the_fallback_is_logged_and_says_what_it_costs():
    """It used to be silent. Everything else in this file depends on the
    identity being stable, and the fallback quietly changes what "stable"
    means."""
    src = AGENT.read_text(encoding="utf-8")
    i = src.index("def _machine_guid()")
    body = src[i:src.index("\ndef ", i + 1)]
    assert "gethostname" in body
    assert "log(" in body, "a silent change of basis is the problem"
    assert "invalidate every SNMPv3 account" in body, (
        "say the consequence, not just that it happened")
    assert 'return host, "hostname"' in body, (
        "the source has to travel with the value or the clone check cannot "
        "tell a rename from a clone")
