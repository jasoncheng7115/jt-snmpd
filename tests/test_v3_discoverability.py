"""SNMPv3 has to be findable by someone who only ever double-clicks the MSI.

**Why this file exists**

1.1.2 supported SNMPv3 and told nobody. The wizard's settings page asks for the
management networks and a community and stops there, so it reads as "this
product speaks v2c"; the completion page said "Click the Finish button" and
nothing else; the README's installation section never mentioned it; and the
project page's install instructions said only that the two values could be
changed later in config.json. The string `user add` did not appear in either
README or on the project page at all.

Every one of those is defensible on its own. Together they meant a feature that
was built, tested on four machines and verified against a production LibreNMS
could not be reached by the people it was for.

This is the same defect 1.1.1 fixed one layer down, where `rate_pps`,
`rate_burst` and `v3_only` were live settings absent from the file that is
supposed to list the settings. A capability nobody can discover is close to a
capability that is not there, and the fix in both cases is to say so at the
place where the reader is already looking.

The reason SNMPv3 is not *asked for* by the installer is separate and stays:
an MSI property is written to the msiexec log and to Event IDs 1033 and 11707,
so a passphrase handed to an installer ends up in plain text on every machine it
reaches. These assertions pin the signposting, not a change to that rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wxs() -> str:
    return (ROOT / "packaging" / "wix" / "jt-snmpd.wxs").read_text(encoding="utf-8")


def test_the_completion_page_points_at_snmpv3(wxs: str):
    """The last screen of a graphical install, and the only one everybody
    reaches."""
    assert 'Id="WIXUI_EXITDIALOGOPTIONALTEXT"' in wxs
    block = wxs[wxs.index('Id="WIXUI_EXITDIALOGOPTIONALTEXT"'):]
    block = block[:block.index("/>")]
    assert "SNMPv3" in block
    assert "user add" in block, "naming the command is the point; a reader who is "\
        "told a feature exists but not how to reach it is no better off"


def test_the_settings_page_says_v3_exists(wxs: str):
    """The page that asks who may query the host is where its absence is most
    likely to be read as 'not supported'."""
    dlg = wxs[wxs.index('<Dialog Id="JtSettingsDlg"'):]
    dlg = dlg[:dlg.index("</Dialog>")]
    assert "SNMPv3" in dlg


def test_the_installer_still_does_not_collect_a_passphrase(wxs: str):
    """The counterweight. Signposting must not turn into a v3 input field: the
    reason there is no field is that MSI properties reach the installer log and
    Event IDs 1033 and 11707."""
    dlg = wxs[wxs.index('<Dialog Id="JtSettingsDlg"'):]
    dlg = dlg[:dlg.index("</Dialog>")]
    props = {line.split('Property="')[1].split('"')[0]
             for line in dlg.splitlines() if 'Property="' in line}
    assert props == {"MANAGEMENTNETWORKS", "COMMUNITY", "KEEPMSSNMP"}, (
        f"the settings page collects {props}; a passphrase must never be an "
        "MSI property")


@pytest.mark.parametrize("name", ["README.md", "README_zh-TW.md"])
def test_the_readme_installation_section_explains_provisioning(name: str):
    text = (ROOT / name).read_text(encoding="utf-8")
    install = text[text.index("## Install") if "## Install" in text
                   else text.index("## 安裝"):]
    install = install[:install.index("## Paths") if "## Paths" in install
                      else install.index("## 路徑")]
    assert "user add" in install, (
        f"{name}: someone reading the installation section has to be able to "
        "find out how SNMPv3 is turned on")
    assert "1033" in install, (
        f"{name}: say why the installer does not ask, or the omission reads "
        "as an oversight and somebody will file it as one")


def test_the_project_page_explains_provisioning():
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    assert "user add" in html
    assert "1033" in html
