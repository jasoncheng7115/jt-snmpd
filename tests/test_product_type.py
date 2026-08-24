"""sysObjectID 三分支與 Windows Server 情境（spec §1.2 / §9.3）。

**為什麼三個分支都必須存在**

LibreNMS 的 `LibreNMS/OS/Windows.php` 依 sysObjectID 走三條不同的版本查表：

    .1.3.6.1.4.1.311.1.1.3.1.1  → getClientVersion()
    .1.3.6.1.4.1.311.1.1.3.1.2  → getServerVersion()
    .1.3.6.1.4.1.311.1.1.3.1.3  → getDatacenterVersion()

同一個 build number 在三張表裡對應不同字串（例如 26100 在 client 表是
`11 (24H2)`、在 server 表是 `Server 2025 (24H2)`）。少了 DC 分支，
網域控制站會被歸到 server，版本字串因此不同——而這是無聲的錯誤，
agent 照常運作、LibreNMS 照常顯示，只是顯示錯的版本。

**Server Core 的陷阱**

`InstallationType` 在不同 Windows 版本的值不一致：`Server`、`Server Core`、
`Windows Server Core` 都出現過。用等值比較會讓 Server Core 被誤判為工作站，
而 spec §9.3 的平台 DoD 明列 Server Core 必須支援。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "deploy" / "jt_agent.py"
SRC = AGENT.read_text(encoding="utf-8")

MS_PREFIX = "(1, 3, 6, 1, 4, 1, 311, 1, 1, 3, 1, "


def _sysobjid_map() -> dict[str, tuple[int, ...]]:
    """從原始碼取出 ptype → sysObjectID 的對照表。

    以 ast 解析而非 import：agent 相依 winreg / ctypes.windll，Linux 上無法 import。
    """
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if {"client", "server", "domain_controller"} <= set(keys):
            out = {}
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Tuple):
                    out[k.value] = tuple(e.value for e in v.elts)
            return out
    pytest.fail("agent 中找不到 client/server/domain_controller 的 sysObjectID 對照表")


@pytest.mark.parametrize("ptype,expected_last", [
    ("client", 1),
    ("server", 2),
    ("domain_controller", 3),
])
def test_sysobjectid_branch_matches_librenms(ptype: str, expected_last: int):
    """三個分支的最後一個 sub-identifier 必須分別是 1 / 2 / 3。

    LibreNMS 以此決定呼叫 getClientVersion / getServerVersion /
    getDatacenterVersion，弄錯會顯示錯的 Windows 版本。
    """
    m = _sysobjid_map()
    assert ptype in m, f"缺少 {ptype} 分支"
    expected = (1, 3, 6, 1, 4, 1, 311, 1, 1, 3, 1, expected_last)
    assert m[ptype] == expected, (
        f"{ptype} 應為 .1.3.6.1.4.1.311.1.1.3.1.{expected_last}，實際 {m[ptype]}")


def test_all_three_branches_are_distinct():
    m = _sysobjid_map()
    assert len(set(m.values())) == 3, f"三個分支必須互異：{m}"


def test_all_branches_use_microsoft_pen():
    """必須沿用 Microsoft 的 PEN 311。

    spec §1.2：取得自有 PEN 之前，sysObjectID 維持 Microsoft 相容值，
    否則 LibreNMS 的三個分支全部落空，Version 欄位會空白。
    """
    for ptype, oid in _sysobjid_map().items():
        assert oid[:6] == (1, 3, 6, 1, 4, 1), f"{ptype} 前綴錯誤"
        assert oid[6] == 311, f"{ptype} 必須用 Microsoft 的 PEN 311，實際 {oid[6]}"


def test_installation_type_uses_prefix_match_not_equality():
    """Server Core 的 InstallationType 是 "Server Core" 而非 "Server"。

    用等值比較會讓 Server Core 被誤判為工作站。spec §9.3 的平台 DoD
    明列 Server Core 必須支援。
    """
    assert 'startswith("server")' in SRC or "startswith('server')" in SRC, (
        "InstallationType 必須用 startswith 比對，否則 Server Core 會被誤判")
    assert '== "Server"' not in SRC, "不可用等值比較 InstallationType"


def test_domain_controller_detection_exists():
    """DC 判定必須實際呼叫 DsRoleGetPrimaryDomainInformation。

    spec §1.2 指定此 API（不使用 WMI）。
    """
    assert "DsRoleGetPrimaryDomainInformation" in SRC
    assert "DsRoleFreeMemory" in SRC, "DsRole 系列 API 配置的記憶體必須釋放"
    # PDC 與 BDC 都算 DC
    assert "DSROLE_PRIMARY_DC" in SRC and "DSROLE_BACKUP_DC" in SRC


def test_product_type_has_fallback_when_installation_type_missing():
    """舊版或精簡安裝可能沒有 InstallationType。

    退路是 ProductOptions\\ProductType：
      WinNT=工作站、LanmanNT=網域控制站、ServerNT=伺服器
    沒有退路的話這些機器會全部被當成工作站。
    """
    assert "ProductOptions" in SRC, "缺少 ProductType 退路"
    assert "LanmanNT" in SRC, "LanmanNT（DC）必須被辨識"
    assert "ServerNT" in SRC, "ServerNT（伺服器）必須被辨識"


def test_dc_detection_failure_does_not_raise():
    """非網域環境或 API 不可用時，DC 判定必須安靜地回傳 False。

    spec §6.7：啟動絕不硬失敗。一個判定不出角色的 agent
    應該當工作站繼續服務，而不是拒絕啟動。
    """
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_is_domain_controller":
            body = ast.unparse(node)
            assert "except" in body, "_is_domain_controller 必須捕捉例外"
            assert "return False" in body, "失敗時必須回傳 False"
            return
    pytest.fail("找不到 _is_domain_controller")
