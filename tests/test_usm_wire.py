"""SNMPv3 on the wire, against net-snmp.

The one thing that has to be true and cannot be established by reading the code:
**a key this agent localizes itself has to be the same key the manager derives
from the passphrase.** The agent stores localized keys so a stolen secrets file
cannot be replayed against the rest of the estate, which means the derivation
happens here rather than inside pysnmp. Get it subtly wrong and every
authentication fails as "wrong password", pointing the operator at the one thing
that is not the problem.

net-snmp is the other end on purpose: it is what LibreNMS polls with, so an
agreement between pysnmp and pysnmp would prove less than it appears to.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

import pytest

_NETSNMP = shutil.which("snmpget")
pytestmark = pytest.mark.skipif(
    _NETSNMP is None,
    reason="needs net-snmp (snmpget); install the 'snmp' package")

PORT = 11194
USER = "librenms"
AUTH_PASS = "auth-passphrase-1"
PRIV_PASS = "priv-passphrase-1"
ENGINE_ID = "8000270104626e6368"
# The synthetic snapshot lives under the enterprise subtree
PROBE_OID = "1.3.6.1.4.1.99999"


@pytest.fixture(scope="module")
def agent():
    proc = subprocess.Popen(
        [sys.executable, "-m", "bench.gate_c.agent", "--varbinds", "50",
         "--port", str(PORT), "--v3-user", USER,
         "--v3-auth-pass", AUTH_PASS, "--v3-priv-pass", PRIV_PASS,
         "--v3-engine-id", ENGINE_ID],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(600):
            line = proc.stdout.readline()
            if line.startswith("READY"):
                break
            if not line:
                raise RuntimeError("the agent exited early")
            time.sleep(0.01)
        else:
            raise RuntimeError("the agent did not become ready")
        yield PORT
    finally:
        proc.kill()
        proc.wait()


def _run(tool: str, *args: str, level: str = "authPriv", user: str = USER,
         auth: str = AUTH_PASS, priv: str = PRIV_PASS,
         a: str = "SHA-256", x: str = "AES") -> str:
    cmd = [tool, "-v3", "-l", level, "-u", user]
    if level != "noAuthNoPriv":
        cmd += ["-a", a, "-A", auth]
    if level == "authPriv":
        cmd += ["-x", x, "-X", priv]
    cmd += ["-e", ENGINE_ID, "-t", "3", "-r", "0", f"127.0.0.1:{PORT}", *args]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return done.stdout + done.stderr


def test_authpriv_returns_data(agent):
    """The whole point: a locally localized key authenticates a real manager."""
    out = _run("snmpwalk", PROBE_OID)
    assert "99999" in out, out
    assert "Authentication failure" not in out


def test_the_walk_is_not_truncated_by_encryption(agent):
    """authPriv adds a header and padding to every response. If the response
    budget were computed on the plaintext, walks would end early under
    encryption, and the loss would look like missing data rather than an error.
    """
    lines = [ln for ln in _run("snmpwalk", PROBE_OID).splitlines() if "99999" in ln]
    assert len(lines) >= 30, f"only {len(lines)} varbinds came back"


def test_a_wrong_authentication_passphrase_is_refused(agent):
    assert "Authentication failure" in _run("snmpget", "1.3.6.1.2.1.1.1.0",
                                            auth="WRONG-passphrase-xx")


def test_a_wrong_privacy_passphrase_is_refused(agent):
    out = _run("snmpget", PROBE_OID + ".1.1.1.1.1", priv="WRONG-passphrase-xx")
    assert "Counter32" not in out and "INTEGER" not in out, out


def test_an_unknown_user_is_refused(agent):
    assert "Unknown user name" in _run("snmpget", "1.3.6.1.2.1.1.1.0",
                                       user="attacker")


def test_noauth_is_refused_for_an_authpriv_user(agent):
    """VACM places the user at authPriv. Asking under the same name without
    authentication must not quietly drop to a lower level."""
    out = _run("snmpget", PROBE_OID + ".1.1.1.1.1", level="noAuthNoPriv")
    assert "Counter32" not in out and "INTEGER" not in out, out
    assert out.strip(), "a silent empty success would be the worst outcome"


def test_v2c_is_still_answered_alongside_v3(agent):
    """v3 is added beside v2c, not in place of it. An upgrade that stopped
    answering v2c would take every existing deployment off the map at the moment
    it was installed."""
    done = subprocess.run(
        ["snmpwalk", "-v2c", "-c", "bench", "-t", "3", "-r", "0",
         f"127.0.0.1:{PORT}", PROBE_OID],
        capture_output=True, text=True, timeout=30)
    assert "99999" in done.stdout, done.stdout + done.stderr
