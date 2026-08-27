"""The graphical installation must survive its own launch conditions.

**Why this file exists**

0.9.2 shipped a graphical installer that could not install anything. Double-click
the MSI and the Welcome page immediately raised "the management networks must be
specified", with no way forward -- while the page that asks for the management
networks sat two clicks further on.

The cause is an ordering property of Windows Installer that is easy to miss:
`LaunchConditions` runs at the very start of the **InstallUISequence**, before
any dialog is shown. A launch condition that depends on a property the wizard is
supposed to collect can therefore never be satisfied in a wizard install. The
condition was correct for `/qn` and fatal for everything else.

Nothing in the existing suite could catch it. The WiX source was valid, the build
succeeded, the MSI installed cleanly under `/qn`, and the lifecycle test drives
`msiexec /qn` throughout -- so every gate was green while the double-click path,
the one an operator actually uses first, was completely broken.

These assertions pin the shape of the fix rather than the wording:

  1. the mandatory-property launch condition exempts the full-UI case
  2. the dialog that collects the property still refuses to advance without it,
     so the exemption gives nothing away
  3. the configure script remains a backstop and fails closed
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WXS = ROOT / "packaging" / "wix" / "jt-snmpd.wxs"
CONFIGURE = ROOT / "packaging" / "msi-configure.ps1"

NS = {"w": "http://wixtoolset.org/schemas/v4/wxs"}

SRC = WXS.read_text(encoding="utf-8")
TREE = ET.fromstring(SRC)


def _launch_conditions() -> list[str]:
    return [e.get("Condition", "") for e in TREE.iter(f"{{{NS['w']}}}Launch")]


def _condition_mentioning(prop: str) -> str:
    for c in _launch_conditions():
        if prop in c:
            return c
    pytest.fail(f"no <Launch> condition mentions {prop}")


# --------------------------------------------------------------- the actual bug
def test_mandatory_property_condition_exempts_the_full_ui_case():
    """A launch condition on a wizard-collected property must exempt full UI.

    Without this, LaunchConditions fires on the Welcome page and the wizard can
    never reach the page that would satisfy it.
    """
    cond = _condition_mentioning("MANAGEMENTNETWORKS")
    assert "UILevel" in cond, (
        "the MANAGEMENTNETWORKS launch condition has no UILevel exemption, so it "
        "fires before any dialog can collect the value and the graphical "
        "installation aborts on its Welcome page (this shipped in 0.9.2)")
    m = re.search(r"UILevel\s*>\s*(\d+)", cond)
    assert m, f"expected a `UILevel > N` comparison, got: {cond}"
    assert int(m.group(1)) == 4, (
        "the exemption must be `UILevel > 4`. Only level 5 is the full wizard; "
        "2 (/qn), 3 (/qb) and 4 (/qr) show no page that could ask, so the "
        "condition still has to stop those")


def test_condition_still_blocks_silent_installs():
    """The exemption must not turn into a blanket pass."""
    cond = _condition_mentioning("MANAGEMENTNETWORKS")
    assert re.search(r"\bMANAGEMENTNETWORKS\b", cond), \
        "the property itself must still be part of the condition"
    assert "REMOVE" in cond, \
        "uninstall has no management networks to supply and must not be blocked"


def test_uninstall_is_exempt():
    cond = _condition_mentioning("MANAGEMENTNETWORKS")
    assert "REMOVE" in cond


# ------------------------------------------- the dialog still enforces the rule
def _jt_settings_dialog():
    for d in TREE.iter(f"{{{NS['w']}}}Dialog"):
        if d.get("Id") == "JtSettingsDlg":
            return d
    pytest.fail("JtSettingsDlg not found; the graphical settings page is gone")


def test_settings_dialog_refuses_to_advance_without_networks():
    """Exempting full UI is only safe because the dialog enforces it instead.

    If this ever stops being true, the exemption becomes a hole: a wizard install
    could complete with no ACL, and an agent that answers everyone is worse than
    one that fails to install.
    """
    dlg = _jt_settings_dialog()
    nxt = [c for c in dlg.iter(f"{{{NS['w']}}}Control") if c.get("Id") == "Next"]
    assert nxt, "JtSettingsDlg has no Next control"
    publishes = [(p.get("Value"), p.get("Condition") or "")
                 for p in nxt[0].iter(f"{{{NS['w']}}}Publish")]
    forward = [v for v, c in publishes if c.strip() == "MANAGEMENTNETWORKS"]
    blocked = [v for v, c in publishes if c.strip().upper() == "NOT MANAGEMENTNETWORKS"]
    assert forward, (
        "Next must move on only when MANAGEMENTNETWORKS is set; "
        f"conditions found: {publishes}")
    assert blocked, (
        "Next must do something visible when MANAGEMENTNETWORKS is empty, "
        "rather than silently staying put")


def test_settings_dialog_binds_an_editable_field_to_the_property():
    """The page has to actually collect the value it is trusted to collect."""
    dlg = _jt_settings_dialog()
    edits = [c.get("Property") for c in dlg.iter(f"{{{NS['w']}}}Control")
             if c.get("Type") == "Edit"]
    assert "MANAGEMENTNETWORKS" in edits, \
        "no Edit control is bound to MANAGEMENTNETWORKS"
    assert "COMMUNITY" in edits, "no Edit control is bound to COMMUNITY"


def test_settings_dialog_is_reachable_from_the_standard_flow():
    """A page nobody routes to is the same as no page at all."""
    targets = [p.get("Value") for p in TREE.iter(f"{{{NS['w']}}}Publish")
               if p.get("Event") == "NewDialog"]
    assert "JtSettingsDlg" in targets, \
        "nothing publishes NewDialog=JtSettingsDlg, so the wizard never shows it"


def test_settings_page_outranks_the_builtin_route_to_verifyready():
    """The route to the settings page must be published *after* WixUI's own.

    WixUI_InstallDir publishes NewDialog=VerifyReadyDlg on InstallDirDlg's Next
    at Order 4. When several NewDialog events are true, the last one processed
    decides where the wizard goes. Published at Order 3, our row was overruled
    every time and the settings page never appeared -- which is exactly what
    0.9.3 did: straight from Destination Folder to Ready to install, and then a
    failed install because nothing had supplied the management networks.
    """
    rows = [p for p in TREE.iter(f"{{{NS['w']}}}Publish")
            if p.get("Dialog") == "InstallDirDlg" and p.get("Control") == "Next"
            and p.get("Value") == "JtSettingsDlg"]
    assert rows, "no route from InstallDirDlg to JtSettingsDlg"
    order = rows[0].get("Order")
    assert order is not None and int(order) > 4, (
        f"the route to JtSettingsDlg is at Order {order}; WixUI_InstallDir "
        "publishes NewDialog=VerifyReadyDlg at Order 4 on the same control and "
        "would win, skipping the settings page entirely")


def test_settings_page_respects_path_validation():
    """Ordering last must not mean overruling the invalid-path check."""
    rows = [p for p in TREE.iter(f"{{{NS['w']}}}Publish")
            if p.get("Dialog") == "InstallDirDlg" and p.get("Value") == "JtSettingsDlg"]
    cond = rows[0].get("Condition") or ""
    assert "WIXUI_INSTALLDIR_VALID" in cond, (
        "coming after WixUI's own row means this condition has to repeat the "
        "path-validity check, or a rejected path walks straight past InvalidDirDlg")


def test_newdialog_is_the_last_event_on_the_settings_page():
    """Windows Installer ignores every event published after a NewDialog.

    With NewDialog first, the "you must enter the management networks" prompt
    would only ever run because its own condition happened to be mutually
    exclusive. Ordering should not be load-bearing for correctness.
    """
    dlg = _jt_settings_dialog()
    nxt = [c for c in dlg.iter(f"{{{NS['w']}}}Control") if c.get("Id") == "Next"][0]
    events = [(int(p.get("Order") or 0), p.get("Event"))
              for p in nxt.iter(f"{{{NS['w']}}}Publish")]
    assert events, "Next publishes no events"
    last_order = max(o for o, _ in events)
    news = [o for o, e in events if e == "NewDialog"]
    assert news, "Next never moves the wizard on"
    assert min(news) >= last_order, (
        f"NewDialog is not the last event on Next: {sorted(events)}. Everything "
        "published after a NewDialog is discarded by Windows Installer")


# ---------------------------------------------------- the checkbox told the truth
def test_optional_checkbox_defaults_to_unticked():
    """A checkbox is drawn ticked whenever its property is non-empty.

    `Value="0"` is a non-empty string, so the "keep the built-in SNMP service"
    box appeared **ticked** while the installer went on to disable the service:
    the label promised the opposite of what happened. Confirmed by reading the
    0.9.4 Property table, which held KEEPMSSNMP = "0".

    The second-order problem was worse. Unticking clears the property to "",
    and only CheckBoxValue ever writes "1", so the states were:

        "0"  initial, drawn ticked   -> service disabled
        ""   after unticking         -> service disabled
        "1"  after re-ticking        -> service kept

    Using the box as labelled could not keep the service. You had to untick it
    and tick it again. The property must therefore start empty.
    """
    props = {e.get("Id"): e.get("Value")
             for e in TREE.iter(f"{{{NS['w']}}}Property")}
    checkboxes = {c.get("Property")
                  for d in TREE.iter(f"{{{NS['w']}}}Dialog")
                  for c in d.iter(f"{{{NS['w']}}}Control")
                  if c.get("Type") == "CheckBox"}
    assert checkboxes, "no checkbox controls found"
    for prop in checkboxes:
        assert props.get(prop) in (None, ""), (
            f"{prop} backs a checkbox and defaults to {props[prop]!r}. Any "
            "non-empty value, including \"0\", draws the box ticked, and "
            "unticking then writes \"\" rather than the original value")


def test_optional_checkbox_property_stays_settable_from_the_command_line():
    """Clearing the default must not cost the silent-install path.

    A property with no value is absent from the Property table, so it has to be
    declared Secure for `msiexec ... KEEPMSSNMP=1` to reach the execute
    sequence.
    """
    for e in TREE.iter(f"{{{NS['w']}}}Property"):
        if e.get("Id") == "KEEPMSSNMP":
            assert e.get("Secure") == "yes", (
                "KEEPMSSNMP has no default value, so without Secure=\"yes\" it "
                "cannot be set on the command line either")
            return
    pytest.fail("KEEPMSSNMP is not declared")


def test_configure_script_treats_anything_but_one_as_disable():
    """The script side of the same contract: only "1" keeps the service."""
    body = CONFIGURE.read_text(encoding="utf-8")
    assert "$KeepMsSnmp -ne '1'" in body, (
        "the configure script no longer keys off an exact \"1\"; with the "
        "property now defaulting to empty, a truthiness test would read \"\" "
        "and \"0\" differently from each other")


def test_dialog_titles_match_the_rest_of_the_wizard():
    """Our pages must not announce a different product from WixUI's pages."""
    titles = {d.get("Id"): d.get("Title") for d in TREE.iter(f"{{{NS['w']}}}Dialog")}
    assert titles, "no dialogs found"
    for dlg, title in titles.items():
        assert title == "jt-snmpd Setup", (
            f"{dlg} has title {title!r}; WixUI's own pages use "
            "\"jt-snmpd Setup\" and the title bar changing mid-wizard "
            "reads as a different program")


def test_the_lifecycle_script_looks_for_the_right_product_name():
    """The lifecycle test finds the product by its display name, which is the
    wxs's Package Name.

    They drifted apart once: the rename to jt-snmpd left the script matching
    "JT SNMP Agent", so every check that depends on finding the product would
    have reported it absent, on a machine where it was installed correctly.
    """
    lifecycle = (ROOT / "tests" / "lifecycle.ps1").read_text(encoding="utf-8-sig")
    pkg = TREE.find(f"{{{NS['w']}}}Package")
    assert pkg is not None, "the wxs has no Package element"
    name = pkg.get("Name")
    assert name, "the Package element has no Name"
    assert f'DisplayName -eq "{name}"' in lifecycle or f'DisplayName -like "*{name}*"' in lifecycle, (
        f"lifecycle.ps1 does not look for DisplayName {name!r}, which is the "
        "product name in the wxs; it would find nothing on a correct install")


def test_the_lifecycle_script_does_not_pin_a_version():
    """A hardcoded MSI name tests whatever was built when it was written."""
    lifecycle = (ROOT / "tests" / "lifecycle.ps1").read_text(encoding="utf-8-sig")
    assert not re.search(r"jt-snmpd-\d+\.\d+\.\d+-x64\.msi", lifecycle), (
        "lifecycle.ps1 names a specific MSI version, so it stops testing the "
        "current build the moment the version changes")


# ------------------------------------------------------------- wizard presentation
def test_wizard_supplies_its_own_artwork_and_licence():
    """WiX substitutes placeholders for any of these that is left unset.

    0.9.3 shipped all three defaults: a stock banner, a stock side panel, and a
    licence page reading "Lorem ipsum dolor sit amet". The last one is the
    serious one -- a document shown as terms of use, saying nothing, for software
    licensed GPL-3.0-or-later.
    """
    declared = {v.get("Id"): v.get("Value")
                for v in TREE.iter(f"{{{NS['w']}}}WixVariable")}
    for var in ("WixUIBannerBmp", "WixUIDialogBmp", "WixUILicenseRtf"):
        assert var in declared, (
            f"{var} is not set, so WiX will substitute its own placeholder")
        value = declared[var]
        # A WixVariable path is resolved against the working directory, not
        # against the .wxs, so a bare file name fails the build with WIX0103.
        # The build supplies UiDir; what is checked here is that the file the
        # value names really is beside the source.
        assert value.startswith("$(var."), (
            f"{var} is {value!r}: a bare relative path is resolved against the "
            "working directory and will not be found at build time")
        target = WXS.parent / value.rsplit("\\", 1)[-1]
        assert target.exists(), f"{var} names {target.name}, which is missing"


def test_licence_shown_by_the_installer_is_the_repository_licence():
    """Two copies of a licence drift. The one users accept has to be ours."""
    rtf = (WXS.parent / "license.rtf").read_text(encoding="ascii")
    assert "GNU GENERAL PUBLIC LICENSE" in rtf, \
        "the licence page does not show the GPL"
    assert "Lorem ipsum" not in rtf, "the placeholder licence is still in place"
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    marker = "Version 3, 29 June 2007"
    assert marker in licence and marker in rtf, \
        "license.rtf and LICENSE disagree; regenerate with packaging/make-ui-assets.py"


# The banner bitmap is 370 dialog units wide and the icon sits in roughly the
# last 40 of them, so text on the banner may run to about unit 325 before it
# collides. Measured from a rendered dialog: at unit 355 the settings page's
# description came within 4 px of the icon and read as running underneath it.
BANNER_TEXT_LIMIT = 325


def _banner_text_controls():
    """Text controls that sit on the banner strip, which is 44 units tall."""
    out = []
    for d in TREE.iter(f"{{{NS['w']}}}Dialog"):
        for c in d.iter(f"{{{NS['w']}}}Control"):
            if c.get("Type") != "Text":
                continue
            try:
                y = int(c.get("Y", "999")); x = int(c.get("X", "0"))
                w = int(c.get("Width", "0"))
            except ValueError:
                continue
            if y < 44:
                out.append((d.get("Id"), c.get("Id"), x, w))
    return out


def test_banner_text_does_not_reach_the_icon():
    """Text on the banner must stop before the icon, or it reads as overlapping.

    This is not caught by anything that parses the dialog: the control is valid,
    the build succeeds, and the collision only exists once the text is drawn.
    """
    controls = _banner_text_controls()
    assert controls, "no text controls found on any banner"
    for dlg, ctl, x, w in controls:
        assert x + w <= BANNER_TEXT_LIMIT, (
            f"{dlg}/{ctl} spans units {x}..{x + w}, past the banner icon at "
            f"~{BANNER_TEXT_LIMIT}; the text will run under it")


@pytest.mark.parametrize("name,size", [("banner.bmp", (493, 58)),
                                       ("dialog.bmp", (493, 312))])
def test_artwork_is_the_size_wix_requires(name, size):
    """WiX does not scale these; a wrong size is drawn wrong, not rejected."""
    path = WXS.parent / name
    assert path.exists(), f"{name} is missing"
    with path.open("rb") as fh:
        head = fh.read(30)
    assert head[:2] == b"BM", f"{name} is not a BMP"
    w = int.from_bytes(head[18:22], "little")
    h = int.from_bytes(head[22:26], "little", signed=True)
    assert (w, abs(h)) == size, f"{name} is {w}x{abs(h)}, expected {size[0]}x{size[1]}"


# ------------------------------------------------------- the backstop still holds
def test_configure_script_fails_closed_on_empty_networks():
    """Defence in depth: even if both gates above are wrong, this must stop.

    Never migrate to Any/Any. An agent installed with no source ACL answers every
    host on the network, which is the failure this project exists to avoid.
    """
    body = CONFIGURE.read_text(encoding="utf-8")
    assert re.search(r"\$nets\.Count\s*-eq\s*0", body), \
        "the configure script no longer checks for an empty network list"
    idx = body.index("$nets.Count -eq 0")
    tail = body[idx:idx + 400]
    assert "exit 1" in tail, \
        "an empty network list must abort the install, not continue"


# ------------------------------------------------- the "Files in use" dialog
#
# **This is specified behaviour, not a defect** (decided 2026-08-27):
#
#   graphical install   the operator is shown the Files in use page and decides.
#                       Nothing should shut a monitoring agent down behind
#                       someone's back while they are watching it.
#   silent install      there is nobody to ask, so Windows Installer shuts the
#                       agent down, installs, and starts it again by itself —
#                       and msi-configure.ps1 records that it happened, because
#                       the msiexec log which otherwise records it only exists
#                       if somebody passed /l*v, which GPO deployment does not.
#
# There is deliberately no ServiceControl. Two placements were built and driven
# through the wizard on real hardware before the behaviour above was specified:
#
#   in a component of its own      the dialog still listed jt-snmpd
#   in the executable's component  jt-snmpd was gone from the list, but on a
#                                  machine with other software Windows Installer
#                                  then shut down unrelated services and the
#                                  upgrade rolled back
#
# The second one is the reason this is a test: it removes the dialog by making
# the installer stop other people's services, which is exactly what the
# specification above rules out.
BUILD_MSI = (ROOT / "packaging" / "build-msi.ps1").read_text(encoding="utf-8")


def test_no_service_control_is_emitted():
    """Both placements were tried and measured; neither is acceptable yet.

    This is not "we never thought about it". It is a recorded decision, and the
    reason it is a test is that the second attempt looked like a success in the
    MSI tables while breaking an upgrade on a real machine.
    """
    assert "<ServiceControl" not in BUILD_MSI, (
        "a ServiceControl is being emitted again. Read TEST_PLAN 6.1c.12: the "
        "placement that removes the Files-in-use dialog also made Windows "
        "Installer shut down unrelated services and roll the upgrade back")
    wxs = (ROOT / "packaging" / "wix" / "jt-snmpd.wxs").read_text(encoding="utf-8")
    assert "<ServiceControl" not in wxs, "a ServiceControl is back in the .wxs"


def test_the_reason_is_written_down_next_to_the_decision():
    """A bare absence teaches nobody anything."""
    assert "Files in use" in BUILD_MSI, \
        "the comment explaining why there is no ServiceControl is gone"


def test_the_installer_records_which_mode_it_ran_in():
    """A silent install shuts the agent down and restarts it without asking, and
    that is correct — but it has to leave a trace. Windows Installer records it
    in the msiexec log, which only exists when somebody passes /l*v, and GPO
    deployment does not.
    """
    wxs = (ROOT / "packaging" / "wix" / "jt-snmpd.wxs").read_text(encoding="utf-8")
    assert "[UILevel]" in wxs, (
        "the configure action has to know whether anyone was asked")
    assert "[MsiRestartManagerSessionKey]" in wxs, (
        "and whether Restart Manager was the thing that stopped the agent")

    cfg = (ROOT / "packaging" / "msi-configure.ps1").read_text(encoding="utf-8")
    assert "installation mode:" in cfg, "the mode has to reach our own log"
    assert "Restart Manager session" in cfg, (
        "say whether Restart Manager stopped and restarted the agent; the "
        "operator's only other source for that is a log that was never written")
    for level in ('"2"', '"5"'):
        assert level in cfg, f"UI level {level} has to be distinguished"
