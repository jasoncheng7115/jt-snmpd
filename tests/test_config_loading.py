"""The agent must actually read the configuration file.

**How this bug was found**

A user asked a simple question: can the settings be changed after installation
by editing the config file? Checking the answer revealed that they could not —
and neither could they be set *during* installation.

The installer collected `COMMUNITY` and `MANAGEMENTNETWORKS`, validated them,
and wrote them to `C:\\ProgramData\\jt-snmpd\\config.json`. The agent declared
`CFG_PATH = ...\\config.yaml` — a different file — and never opened either one.
`CFG` was a module-level dict with the development lab's own community and
`allowed_networks=("192.168.1.0/24",)` baked in, and those were the values every
installation actually ran with.

Proven on a real machine:

    config.json:  "community": "zzz-proof-only", "allowed_networks": ["172.31.0.0/16"]
    agent log:    community=<the lab's own>   networks=['192.168.1.0/24']

**Why it survived every test**

The hardcoded defaults were exactly the values the development lab used. Install
with anything else and the loopback health check queries with the operator's
community, the agent answers on a different one, the check times out and MSI
rolls the whole transaction back with error 1603. So the failure mode was not
subtle — it was total — and it was still invisible, because the one
configuration that worked was the only one ever tried.

The lesson worth keeping: *defaults that match your test environment hide the
code path that reads real input.* Both settings are now empty by default, so a
config that fails to load is loud rather than convenient.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
AGENT = (DEPLOY / "jt_agent.py").read_text(encoding="utf-8")
TREE = ast.parse(AGENT)
# Comment-free view of the source. The comments explaining this bug necessarily
# quote the offending literals, and a plain substring search would flag them —
# the same trap as a style guide that lists the words it forbids.
CODE = ast.unparse(TREE)


def _assign(name: str):
    for node in TREE.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == name:
            return node
    pytest.fail(f"{name} not found")


def _func(name: str) -> str:
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    pytest.fail(f"{name} not found")


# --- the agent and the installer have to agree on the path ------------------

def test_agent_and_installer_agree_on_the_config_path():
    """The exact mismatch that caused the bug: the agent pointed at config.yaml
    while the installer wrote config.json."""
    cfg_path = ast.unparse(_assign("CFG_PATH").value)
    assert "config.json" in cfg_path, f"CFG_PATH should point at config.json, not {cfg_path}"
    installer = (DEPLOY.parent / "packaging" / "msi-configure.ps1").read_text(
        encoding="utf-8-sig")
    assert "config.json" in installer, "the installer writes a different name from the one the agent reads"
    assert "config.yaml" not in CODE, "config.yaml should be gone"


def test_config_file_is_actually_opened():
    """A path constant that nothing opens is decorative. `CFG_PATH` was only
    ever published as an OID value."""
    body = _func("load_config")
    assert "open(CFG_PATH" in body, "load_config never opens the file"
    assert "json.load" in body


def test_settings_are_validated_before_the_engine_is_configured():
    """The unusable-configuration checks must run before anything binds a
    socket or registers a community, so a misconfigured agent fails at startup
    rather than serving with whatever happened to be in memory."""
    run = _func("run_agent")
    i_check = run.index("if not CFG['community']")
    for later in ("add_v1_system", "PreAuthGate(", "open_server_mode"):
        assert i_check < run.index(later), f"the configuration check has to come before {later}"


def _lab_secrets() -> list[str]:
    """Real community strings, read from an untracked file.

    This assertion used to name the lab's community in its own source, which
    published the string in order to check that it had not been published. The
    values now live in `tools/.privacy-secrets`, which is git-ignored and
    excluded from the public repository; when it is absent -- on CI, or for
    anyone who cloned this -- the structural assertions above still run, and
    they are the ones that matter.
    """
    f = DEPLOY.parent / "tools" / ".privacy-secrets"
    if not f.exists():
        return []
    return [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


# --- a default must not hide the configuration having not been read ---------

def test_no_usable_community_default():
    """A default that matches the test lab is what let the bug survive."""
    cfg = ast.literal_eval(ast.unparse(_assign("CFG").value))
    assert cfg["community"] == "", "community must have no default"
    for secret in _lab_secrets():
        assert secret not in CODE, \
            "a real community string from tools/.privacy-secrets is in the agent source"


def test_no_usable_network_default():
    cfg = ast.literal_eval(ast.unparse(_assign("CFG").value))
    assert cfg["allowed_networks"] == (), "allowed_networks must have no default"
    assert "192.168.1.0/24" not in CODE, "the lab network must not survive in executable code"


def test_missing_community_refuses_to_serve():
    """An SNMP agent whose community nobody knows is not useful; it should say
    so rather than invent one."""
    run = _func("run_agent")
    assert re.search(r"if not CFG\['community'\]", run), "the community check is missing"
    assert "SystemExit(1)" in run, "no community has to end in failure"


def test_missing_networks_is_reported_loudly():
    run = _func("run_agent")
    m = re.search(r"if not CFG\['allowed_networks'\]:(.{0,400})", run, re.S)
    assert m, "the allowed_networks check is missing"
    assert "error=True" in m.group(1), "an unset network list has to reach the Event Log"


# --- validation: the file is hand-edited, so it is not trusted --------------

def test_loader_validates_types_and_ranges():
    """Operators edit this file by hand. A port of "161" as a string, or 99999,
    must not become the running configuration."""
    body = _func("load_config")
    assert "1 <= port <= 65535" in body, "port is not range-checked"
    assert "isinstance(data.get('community'), str)" in body or \
           'isinstance(data.get("community"), str)' in body, "community is not type-checked"
    assert "isinstance(data, dict)" in body, "the top level has to be an object"


def test_broken_config_does_not_crash_the_loader():
    body = _func("load_config")
    for exc in ("FileNotFoundError", "OSError", "ValueError", "UnicodeError"):
        assert exc in body, f"{exc} is not handled"


def test_loader_reports_where_the_settings_came_from():
    """"Which config is this agent actually running?" must be answerable — that
    is the question this whole bug was hiding."""
    body = _func("load_config")
    assert "log(" in body and "CFG_PATH" in body
    assert "CFG_SOURCE" in AGENT


# --- the installer and the agent have to use the same field names ----------

def test_installer_writes_the_keys_the_agent_reads():
    installer = (DEPLOY.parent / "packaging" / "msi-configure.ps1").read_text(
        encoding="utf-8-sig")
    body = _func("load_config")
    for key in ("community", "allowed_networks", "port", "enable_arp_table"):
        assert key in installer, f"the installer never writes {key}"
        assert key in body, f"the agent never reads {key}"


def test_documented_config_shape_round_trips():
    """The documentation promises "edit the file and restart the service", so the
    shape of the fields has to match what the agent reads."""
    sample = {"schema_version": 1, "community": "example",
              "allowed_networks": ["192.0.2.0/24"], "port": 161,
              "enable_arp_table": False}
    assert json.loads(json.dumps(sample)) == sample
    for k in sample:
        if k != "schema_version":
            assert k in _func("load_config")


def test_config_is_read_with_bom_tolerance():
    """Windows PowerShell 5.1's `Set-Content -Encoding UTF8` writes a BOM, and so
    does Notepad. Reading with plain utf-8 raises "Unexpected UTF-8 BOM", the
    load fails, and the agent then refuses to serve — which is what happened the
    very first time the installer's config was actually read end to end.

    utf-8-sig accepts a file with or without the marker, so the agent survives
    whichever tool last wrote the file.
    """
    body = _func("load_config")
    assert "utf-8-sig" in body, "the file has to be read as utf-8-sig, to tolerate a BOM"
    assert 'encoding="utf-8")' not in body and "encoding='utf-8')" not in body, \
        "plain utf-8 does not tolerate a BOM and must not be used here"


def test_installer_writes_json_without_a_bom():
    """Both ends fixed: the agent tolerates a BOM, and the installer still writes
    a clean file."""
    installer = (DEPLOY.parent / "packaging" / "msi-configure.ps1").read_text(
        encoding="utf-8-sig")
    # Anchor on the write itself, not on the first mention of the file name.
    # Looking for the name found an earlier reference in the migration block and
    # inspected the wrong region entirely.
    i = installer.index("[IO.File]::WriteAllText")
    block = installer[i:i + 400]
    assert "config.json" in block, \
        "the anchored block is not the config.json write"
    assert "UTF8Encoding $false" in block, (
        "config.json has to be written as UTF-8 without a BOM "
        "(Set-Content -Encoding UTF8 adds one)")


def test_config_is_loaded_before_the_entry_point_reads_cfg():
    """`run_agent(host, port, community, ...)` takes its settings as arguments,
    so the caller reads CFG to build the call. Loading the config inside
    `run_agent` therefore runs *after* those arguments were bound: the file is
    read, the log says so, and the agent still listens with the pre-load value.

    That is precisely what happened — `config loaded from ...: community` in one
    line, `LISTENING ... community=` (empty) in the next. The load has to happen
    at the entry point, before CFG is read for anything.
    """
    # The whole method, taken by parsing rather than by slicing a fixed number
    # of characters. The fixed window broke once when a comment was added above
    # the load, which said nothing about the ordering this test protects, and
    # SvcDoRun is the last method in its class so there is no "next def" to
    # stop at either.
    import ast as _ast
    body = next(_ast.unparse(n) for n in _ast.walk(_ast.parse(AGENT))
                if isinstance(n, _ast.FunctionDef) and n.name == "SvcDoRun")
    i_load = body.index("load_config()")
    i_use = body.index("run_agent(")
    assert i_load < i_use, "SvcDoRun has to load the configuration before calling run_agent"
    # The log line comes after the load too, or it prints the pre-load values
    assert i_load < body.index("SvcDoRun port="), "the log line has to reflect the loaded configuration"

    # Loading inside main_co would be too late
    run = _func("run_agent")
    assert "load_config()" not in run, "loading inside run_agent is too late; the parameters are already bound"


def test_command_line_overrides_the_file_not_the_other_way_round():
    i = AGENT.index('if __name__ == "__main__":')
    tail = AGENT[i:]
    assert tail.index("load_config()") < tail.index('_arg("--community"'), \
        "the command line overrides the file, so it has to be applied after the read"


def test_loading_twice_is_harmless():
    """Both entry points call it — `__main__` for the command-line overrides and
    `SvcDoRun` before it reads CFG. Re-reading the file each time would log the
    same line twice and, worse, could pick up a different file if an operator
    happened to be saving it at that moment."""
    body = _func("load_config")
    assert 'CFG_SOURCE != "defaults"' in body or "CFG_SOURCE != 'defaults'" in body, \
        "load_config has to be idempotent"


def test_every_setting_in_cfg_is_actually_read_from_the_file():
    """A key that exists in CFG but that load_config never reads is silently
    ignored, and the operator has no way to tell: the file accepts the value,
    the agent logs "config loaded", and the setting does nothing.

    That happened to `v3_only`. It was added to the defaults, the installer's
    file carried `"v3_only": true`, the log line listed the four keys it did
    read without mentioning it, and the agent went on answering v2c. A security
    switch that quietly does nothing is worse than not having the switch, because
    someone will certify against it.

    Anything genuinely not settable from the file belongs in the exemption list
    below, with a reason.
    """
    import ast as _ast

    tree = _ast.parse(AGENT)
    cfg = next(n.value for n in _ast.walk(tree)
               if isinstance(n, _ast.Assign)
               and any(getattr(t, "id", "") == "CFG" for t in n.targets))
    keys = {k.value for k in cfg.keys if isinstance(k, _ast.Constant)}

    loader = next(_ast.unparse(n) for n in _ast.walk(tree)
                  if isinstance(n, _ast.FunctionDef) and n.name == "load_config")

    # Set from the command line or derived, never from the file
    exempt = {"contact", "location"}   # read from the built-in SNMP registry

    missing = {k for k in keys - exempt if f"'{k}'" not in loader and f'"{k}"' not in loader}
    assert not missing, (
        f"these settings exist in CFG but load_config never reads them, so a "
        f"value in config.json is silently ignored: {sorted(missing)}")


def test_the_installer_writes_every_setting_the_agent_reads():
    """An operator opening config.json should be able to see what can be
    changed.

    Until 1.1.1 the installer wrote only the keys it had asked about, so
    rate_pps, rate_burst and v3_only existed, worked, and were invisible: the
    file gave no hint they were there, and the only way to know was to have read
    the documentation first. A setting nobody can discover is close to a setting
    that does not exist.
    """
    import ast as _ast
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[1]
    agent = (root / "deploy" / "jt_agent.py").read_text(encoding="utf-8")
    cfg = next(n.value for n in _ast.walk(_ast.parse(agent))
               if isinstance(n, _ast.Assign)
               and any(getattr(t, "id", "") == "CFG" for t in n.targets))
    keys = {k.value for k in cfg.keys if isinstance(k, _ast.Constant)}

    # Read from the machine at runtime rather than set in the file
    not_written = {"contact", "location"}

    script = (root / "packaging" / "msi-configure.ps1").read_text(encoding="utf-8-sig")
    block = script[script.index("$cfg = [ordered]@{"):]
    block = block[:block.index("\n}")]

    missing = sorted(k for k in keys - not_written if k not in block)
    assert not missing, (
        "the installer does not write these, so an operator reading config.json "
        f"cannot tell they exist: {missing}")
