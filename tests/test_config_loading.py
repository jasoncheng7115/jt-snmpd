"""The agent must actually read the configuration file.

**How this bug was found**

A user asked a simple question: can the settings be changed after installation
by editing the config file? Checking the answer revealed that they could not —
and neither could they be set *during* installation.

The installer collected `COMMUNITY` and `MANAGEMENTNETWORKS`, validated them,
and wrote them to `C:\\ProgramData\\JT-SNMP\\config.json`. The agent declared
`CFG_PATH = ...\\config.yaml` — a different file — and never opened either one.
`CFG` was a module-level dict with `community="mon2"` and
`allowed_networks=("192.168.1.0/24",)` baked in, and those were the values every
installation actually ran with.

Proven on a real machine:

    config.json:  "community": "zzz-proof-only", "allowed_networks": ["172.31.0.0/16"]
    agent log:    community=mon2   networks=['192.168.1.0/24']

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
    pytest.fail(f"找不到 {name}")


def _func(name: str) -> str:
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    pytest.fail(f"找不到 {name}")


# --- 設定檔路徑必須與安裝程式一致 -------------------------------------------

def test_agent_and_installer_agree_on_the_config_path():
    """The exact mismatch that caused the bug: the agent pointed at config.yaml
    while the installer wrote config.json."""
    cfg_path = ast.unparse(_assign("CFG_PATH").value)
    assert "config.json" in cfg_path, f"CFG_PATH 應指向 config.json，實際為 {cfg_path}"
    installer = (DEPLOY.parent / "packaging" / "msi-configure.ps1").read_text(
        encoding="utf-8-sig")
    assert "config.json" in installer, "安裝程式寫的檔名與 agent 讀的不一致"
    assert "config.yaml" not in CODE, "不應再出現 config.yaml"


def test_config_file_is_actually_opened():
    """A path constant that nothing opens is decorative. `CFG_PATH` was only
    ever published as an OID value."""
    body = _func("load_config")
    assert "open(CFG_PATH" in body, "load_config 必須實際開啟設定檔"
    assert "json.load" in body


def test_settings_are_validated_before_the_engine_is_configured():
    """The unusable-configuration checks must run before anything binds a
    socket or registers a community, so a misconfigured agent fails at startup
    rather than serving with whatever happened to be in memory."""
    run = _func("run_agent")
    i_check = run.index("if not CFG['community']")
    for later in ("add_v1_system", "PreAuthGate(", "open_server_mode"):
        assert i_check < run.index(later), f"設定檢查必須在 {later} 之前"


# --- 預設值不得掩蓋「沒讀到設定」-------------------------------------------

def test_no_usable_community_default():
    """A default that matches the test lab is what let the bug survive."""
    cfg = ast.literal_eval(ast.unparse(_assign("CFG").value))
    assert cfg["community"] == "", "community 不得有預設值"
    assert "mon2" not in CODE, "測試環境的 community 不可留在可執行的程式碼中"


def test_no_usable_network_default():
    cfg = ast.literal_eval(ast.unparse(_assign("CFG").value))
    assert cfg["allowed_networks"] == (), "allowed_networks 不得有預設值"
    assert "192.168.1.0/24" not in CODE, "測試環境的網段不可留在可執行的程式碼中"


def test_missing_community_refuses_to_serve():
    """An SNMP agent whose community nobody knows is not useful; it should say
    so rather than invent one."""
    run = _func("run_agent")
    assert re.search(r"if not CFG\['community'\]", run), "缺少 community 檢查"
    assert "SystemExit(1)" in run, "沒有 community 時必須以失敗結束"


def test_missing_networks_is_reported_loudly():
    run = _func("run_agent")
    m = re.search(r"if not CFG\['allowed_networks'\]:(.{0,400})", run, re.S)
    assert m, "缺少 allowed_networks 檢查"
    assert "error=True" in m.group(1), "未設定網段必須寫入事件檢視器"


# --- 值的驗證：設定檔是使用者手動編輯的，不可信任 ---------------------------

def test_loader_validates_types_and_ranges():
    """Operators edit this file by hand. A port of "161" as a string, or 99999,
    must not become the running configuration."""
    body = _func("load_config")
    assert "1 <= port <= 65535" in body, "port 必須做範圍檢查"
    assert "isinstance(data.get('community'), str)" in body or \
           'isinstance(data.get("community"), str)' in body, "community 必須做型別檢查"
    assert "isinstance(data, dict)" in body, "頂層必須是物件"


def test_broken_config_does_not_crash_the_loader():
    body = _func("load_config")
    for exc in ("FileNotFoundError", "OSError", "ValueError", "UnicodeError"):
        assert exc in body, f"未處理 {exc}"


def test_loader_reports_where_the_settings_came_from():
    """"Which config is this agent actually running?" must be answerable — that
    is the question this whole bug was hiding."""
    body = _func("load_config")
    assert "log(" in body and "CFG_PATH" in body
    assert "CFG_SOURCE" in AGENT


# --- 安裝程式與 agent 的欄位必須對得起來 -----------------------------------

def test_installer_writes_the_keys_the_agent_reads():
    installer = (DEPLOY.parent / "packaging" / "msi-configure.ps1").read_text(
        encoding="utf-8-sig")
    body = _func("load_config")
    for key in ("community", "allowed_networks", "port", "enable_arp_table"):
        assert key in installer, f"安裝程式未寫出 {key}"
        assert key in body, f"agent 未讀取 {key}"


def test_documented_config_shape_round_trips():
    """文件承諾「改設定檔、重啟服務」，欄位形狀必須與 agent 的讀取一致。"""
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
    assert "utf-8-sig" in body, "設定檔必須以 utf-8-sig 讀取以容忍 BOM"
    assert 'encoding="utf-8")' not in body and "encoding='utf-8')" not in body, \
        "不可用不容忍 BOM 的 utf-8 讀設定檔"


def test_installer_writes_json_without_a_bom():
    """兩端都修：agent 容忍 BOM，安裝程式仍寫乾淨的檔。"""
    installer = (DEPLOY.parent / "packaging" / "msi-configure.ps1").read_text(
        encoding="utf-8-sig")
    i = installer.index("config.json")
    block = installer[max(0, i - 400):i + 200]
    assert "UTF8Encoding $false" in block, \
        "config.json 應以不含 BOM 的 UTF-8 寫入（Set-Content -Encoding UTF8 會加 BOM）"


def test_config_is_loaded_before_the_entry_point_reads_cfg():
    """`run_agent(host, port, community, ...)` takes its settings as arguments,
    so the caller reads CFG to build the call. Loading the config inside
    `run_agent` therefore runs *after* those arguments were bound: the file is
    read, the log says so, and the agent still listens with the pre-load value.

    That is precisely what happened — `config loaded from ...: community` in one
    line, `LISTENING ... community=` (empty) in the next. The load has to happen
    at the entry point, before CFG is read for anything.
    """
    src = AGENT
    i_svc = src.index("def SvcDoRun(self):")
    body = src[i_svc:i_svc + 900]
    i_load = body.index("load_config()")
    i_use = body.index("run_agent(")
    assert i_load < i_use, "SvcDoRun 必須先載入設定再呼叫 run_agent"
    # 記錄行也在載入之後，否則它會印出載入前的值
    assert i_load < body.index("SvcDoRun port="), "記錄行必須反映載入後的設定"

    # main_co 內不應再載入（那是太晚的位置）
    run = _func("run_agent")
    assert "load_config()" not in run, "run_agent 內載入設定為時已晚"


def test_command_line_overrides_the_file_not_the_other_way_round():
    i = AGENT.index('if __name__ == "__main__":')
    tail = AGENT[i:]
    assert tail.index("load_config()") < tail.index('_arg("--community"'), \
        "命令列是覆寫，必須在讀檔之後套用"


def test_loading_twice_is_harmless():
    """Both entry points call it — `__main__` for the command-line overrides and
    `SvcDoRun` before it reads CFG. Re-reading the file each time would log the
    same line twice and, worse, could pick up a different file if an operator
    happened to be saving it at that moment."""
    body = _func("load_config")
    assert 'CFG_SOURCE != "defaults"' in body or "CFG_SOURCE != 'defaults'" in body, \
        "load_config 必須冪等"
