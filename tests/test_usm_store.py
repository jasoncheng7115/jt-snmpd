"""The SNMPv3 algorithm allow-list and the key store.

Two things are being defended here.

**A refused algorithm has to be refused loudly.** pysnmp implements MD5, DES and
3DES, so a configuration naming one of them would otherwise work, and work is
exactly the wrong outcome: the operator would believe the traffic is protected.
Nothing here silently downgrades or silently upgrades.

**A store from another machine has to be rejected with the reason.** Localized
keys are bound to the engineID they were made for. Handing them to pysnmp anyway
produces authentication failures with no explanation attached, on an estate
where the machines were cloned from one template and the operator has no reason
to suspect the image.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import usm  # noqa: E402

ENGINE_A = bytes.fromhex("80001f8804" + "aa" * 16)
ENGINE_B = bytes.fromhex("80001f8804" + "bb" * 16)


# --- The allow-list ---------------------------------------------------------

@pytest.mark.parametrize("auth", ["MD5", "SHA", "SHA-1"])
def test_broken_authentication_protocols_are_refused(auth):
    with pytest.raises(usm.UsmError) as exc:
        usm.check_algorithms(auth, "AES-128")
    assert "SHA-256" in str(exc.value), "the message has to name the replacement"


@pytest.mark.parametrize("priv", ["DES", "3DES"])
def test_broken_privacy_protocols_are_refused(priv):
    with pytest.raises(usm.UsmError) as exc:
        usm.check_algorithms("SHA-256", priv)
    assert "AES-128" in str(exc.value)


def test_an_unknown_protocol_lists_the_valid_ones():
    with pytest.raises(usm.UsmError) as exc:
        usm.check_algorithms("SHA-999", "AES-128")
    assert "SHA-256" in str(exc.value) and "SHA-512" in str(exc.value)


def test_the_default_pair_is_accepted_without_warnings():
    assert usm.check_algorithms(usm.DEFAULT_AUTH, usm.DEFAULT_PRIV) == []


@pytest.mark.parametrize("priv", ["AES-192", "AES-256"])
def test_the_unstandardised_aes_sizes_warn_but_are_allowed(priv):
    """Not a weakness in the cipher: two incompatible key-extension schemes
    exist, and Debian and Ubuntu build net-snmp without the one pysnmp uses, so
    the agent can end up unreachable from the manager it was set up for."""
    warnings = usm.check_algorithms("SHA-256", priv)
    assert warnings and "net-snmp" in warnings[0]


def test_a_short_passphrase_is_refused():
    with pytest.raises(usm.UsmError):
        usm.check_passphrase("authentication", "short")


# --- Localization -----------------------------------------------------------

def test_a_key_is_bound_to_its_engine():
    """The property the whole storage design rests on: the same passphrase on
    two machines does not produce the same key, so a stolen file is worth one
    machine rather than the estate."""
    a = usm.localize("SHA-256", "AES-128", "auth-passphrase", "priv-passphrase",
                     ENGINE_A)
    b = usm.localize("SHA-256", "AES-128", "auth-passphrase", "priv-passphrase",
                     ENGINE_B)
    assert a != b


def test_key_sizes_match_the_algorithms():
    auth, priv = usm.localize("SHA-256", "AES-128", "auth-passphrase",
                              "priv-passphrase", ENGINE_A)
    assert len(auth) == 32, "SHA-256 localizes to 32 bytes"
    assert len(priv) == 16, "AES-128 takes a 128-bit key"


def test_localizing_a_refused_algorithm_raises_rather_than_computing():
    with pytest.raises(usm.UsmError):
        usm.localize("MD5", "DES", "auth-passphrase", "priv-passphrase", ENGINE_A)


# --- The store --------------------------------------------------------------

def _one_user() -> usm.UsmUser:
    auth, priv = usm.localize("SHA-256", "AES-128", "auth-passphrase",
                              "priv-passphrase", ENGINE_A)
    return usm.UsmUser("librenms", "SHA-256", "AES-128", auth, priv)


def test_a_store_round_trips():
    user = _one_user()
    users, problems = usm.store_from_json(
        usm.store_to_json(ENGINE_A, [user]), ENGINE_A)
    assert problems == []
    assert users == [user]


def test_the_passphrase_is_nowhere_in_the_stored_bytes():
    raw = usm.store_to_json(ENGINE_A, [_one_user()])
    assert b"auth-passphrase" not in raw
    assert b"priv-passphrase" not in raw


def test_a_store_from_another_machine_is_rejected_with_the_reason():
    """The cloned-VM case, which is the one that actually happens."""
    users, problems = usm.store_from_json(
        usm.store_to_json(ENGINE_A, [_one_user()]), ENGINE_B)
    assert users == []
    assert len(problems) == 1
    assert "cloned" in problems[0]
    assert "provisioned again" in problems[0], (
        "the operator needs to be told what to do, not only what went wrong")


def test_a_user_naming_a_refused_algorithm_is_dropped_not_downgraded():
    raw = usm.store_to_json(ENGINE_A, [_one_user()]).replace(b"SHA-256", b"MD5\x20\x20\x20\x20")
    users, problems = usm.store_from_json(raw, ENGINE_A)
    assert users == []
    assert problems


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"null", b"{}"])
def test_damaged_stores_yield_a_problem_rather_than_an_exception(raw):
    users, problems = usm.store_from_json(raw, ENGINE_A)
    assert users == []
    assert problems or raw == b"{}"


def test_one_bad_entry_does_not_discard_the_good_ones():
    good = _one_user()
    payload = usm.store_to_json(ENGINE_A, [good]).decode()
    payload = payload.replace('"users": [', '"users": [ {"name": ""}, ', 1)
    users, problems = usm.store_from_json(payload.encode(), ENGINE_A)
    assert [u.name for u in users] == ["librenms"]
    assert problems


# --- The provisioning CLI ---------------------------------------------------

def test_the_cli_never_takes_a_passphrase_as_an_argument():
    """An argument is visible in the process list to every user on the machine
    while the command runs, and stays in console history. This is the same
    reasoning that keeps keys out of MSI properties, which land in the msiexec
    log and in Event IDs 1033 and 11707."""
    import ast
    agent = (Path(__file__).resolve().parents[1] / "deploy" / "jt_agent.py")
    fn = next(n for n in ast.walk(ast.parse(agent.read_text(encoding="utf-8")))
              if isinstance(n, ast.FunctionDef) and n.name == "_usm_cli")
    body = ast.unparse(fn)
    assert "getpass" in body, "a console prompt has to be the interactive path"
    assert "sys.stdin" in body, (
        "an unattended install has no console to prompt at; without a stdin "
        "path the next person reaches for an argument instead")
    for forbidden in ("--auth-pass", "--authkey", "--password", "--passphrase"):
        assert forbidden not in body, f"{forbidden} would put a secret in argv"


def test_the_two_passphrases_must_differ():
    agent = (Path(__file__).resolve().parents[1] / "deploy" / "jt_agent.py")
    src = agent.read_text(encoding="utf-8")
    assert "one compromise should not be two" in src, (
        "reusing one passphrase for authentication and privacy turns a single "
        "disclosure into both")


def test_refusing_to_start_says_why_in_the_log():
    """`v3_only` with no usable account is a deliberate refusal to start, and a
    refusal the operator cannot explain is barely better than a crash. Without
    its own log line the last thing written is "agent thread ended
    unexpectedly", which is what every other startup failure writes too."""
    agent = (Path(__file__).resolve().parents[1] / "deploy" / "jt_agent.py")
    src = agent.read_text(encoding="utf-8")
    # Anchor on the raise, not on the message: the log line now contains the
    # same words, and searching for those found the log line itself.
    i = src.index('raise SystemExit("v3_only is set')
    window = src[max(0, i - 900):i]
    assert "refusing to start" in window, (
        "the refusal has to reach the log, not only the exception")
    assert "user add" in window, "say how to fix it, not only what went wrong"


def test_aes_cfb_privacy_still_works_in_this_cryptography():
    """SNMPv3 privacy is AES in CFB mode, and `cryptography` is moving CFB out of
    `primitives.ciphers.modes` into `decrepit`, warning on every use. pysnmp
    still imports the deprecated path, so the day that removal completes, every
    authPriv packet stops working — not degrades, stops.

    This is why cryptography is pinned. If you are here because this test failed
    after raising the pin, that is the reason, and the fix is upstream in pysnmp
    rather than in this file.
    """
    from pysnmp.entity import config as _cfg  # import first; circular otherwise
    from pysnmp.proto.secmod.rfc3826.priv import aes

    svc = _cfg.PRIV_SERVICES[_cfg.USM_PRIV_CFB128_AES]
    assert isinstance(svc, aes.Aes)
    assert svc.KEY_SIZE == 16

    # The real check: a round trip through the cipher this depends on
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key, iv, plain = b"k" * 16, b"i" * 16, b"varbind payload"
    enc = Cipher(algorithms.AES(key), modes.CFB(iv)).encryptor()
    ct = enc.update(plain) + enc.finalize()
    dec = Cipher(algorithms.AES(key), modes.CFB(iv)).decryptor()
    assert dec.update(ct) + dec.finalize() == plain


# --- Losing the store -------------------------------------------------------

def test_the_store_keeps_a_previous_copy():
    """temp-flush-replace already rules out a torn write. The .bak is for what
    comes from outside: a filesystem error, antivirus quarantining the file, a
    backup agent restoring something odd.

    It is worth keeping because of the cost. Every SNMPv3 account on the machine
    goes with the file and cannot be recovered from anywhere else — the keys are
    localized to this engineID and the passphrases were deliberately never
    stored. Someone has to visit the machine and provision each account again,
    and on an estate provisioned from one policy that is every machine that lost
    it.
    """
    import ast as _ast
    src = (Path(__file__).resolve().parents[1] / "deploy" / "usm.py").read_text(
        encoding="utf-8")
    save = next(_ast.unparse(n) for n in _ast.walk(_ast.parse(src))
                if isinstance(n, _ast.FunctionDef) and n.name == "save_store")
    assert ".bak" in save, "the previous copy has to be kept"
    assert "fsync" in save, "and the new one flushed before it replaces anything"
    load = next(_ast.unparse(n) for n in _ast.walk(_ast.parse(src))
                if isinstance(n, _ast.FunctionDef) and n.name == "load_store")
    assert ".bak" in load, (
        "keeping a copy nobody reads is what index-map did for four releases")


def test_a_damaged_store_falls_back_and_says_what_may_be_missing(tmp_path, monkeypatch):
    good = usm.store_to_json(ENGINE_A, [_one_user()])
    # Stand in for DPAPI, which only exists on Windows
    monkeypatch.setattr(usm, "protect", lambda b: b)
    monkeypatch.setattr(usm, "unprotect", lambda b: b)

    store = tmp_path / "usm.dat"
    store.write_bytes(b"\x00 not a store")
    (tmp_path / "usm.dat.bak").write_bytes(good)

    users, problems = usm.load_store(str(store), ENGINE_A)
    assert [u.name for u in users] == ["librenms"]
    joined = " ".join(problems)
    assert "previous copy" in joined
    assert "will be missing" in joined, (
        "an account added since the last save is gone; say so rather than "
        "letting it look like a clean recovery")


def test_both_copies_unusable_reports_and_does_not_invent_users(tmp_path, monkeypatch):
    monkeypatch.setattr(usm, "unprotect", lambda b: b)
    store = tmp_path / "usm.dat"
    store.write_bytes(b"rubbish")
    (tmp_path / "usm.dat.bak").write_bytes(b"also rubbish")
    users, problems = usm.load_store(str(store), ENGINE_A)
    assert users == []
    assert problems

@pytest.fixture
def plain_dpapi(monkeypatch):
    """load_store and save_store go through DPAPI, which is Windows only. The
    behaviour under test is the fallback logic around them, so the encryption is
    replaced by identity rather than the test being skipped: this is the layer
    that decided a correctly emptied store was a broken one."""
    monkeypatch.setattr(usm, "protect", lambda b: b)
    monkeypatch.setattr(usm, "unprotect", lambda b: b)


def test_removing_the_last_user_actually_removes_it(tmp_path, plain_dpapi):
    """`user remove` on the only account left the account working.

    The store keeps a .bak, and load_store fell through to it whenever the
    primary file yielded no users. A store with zero users is exactly what
    removing the last one produces, so the loader treated a correct file as a
    broken one and restored the account from the previous copy. `user remove`
    printed "removed", `user list` still listed the user, and the next service
    start registered it again.

    That is the worst shape this bug could take: the operator most likely to
    remove the only account is removing one they believe is compromised, and
    they were told it was gone.

    Measured on a Server 2016 domain controller before the fix.
    """
    store = str(tmp_path / "usm.dat")
    user = _one_user()

    usm.save_store(store, ENGINE_A, [user])
    usm.save_store(store, ENGINE_A, [user])          # now there is a .bak
    assert os.path.exists(store + ".bak")

    usm.save_store(store, ENGINE_A, [])
    users, problems = usm.load_store(store, ENGINE_A)
    assert users == [], (
        f"the removed account came back from the .bak: {[u.name for u in users]}")
    assert not any("previous copy" in p for p in problems), (
        "an empty store parsed cleanly; reporting it as unusable sends the "
        "operator looking for a fault that is not there")


def test_a_store_that_cannot_be_parsed_still_falls_back(tmp_path, plain_dpapi):
    """The counterweight. Telling empty apart from broken must not remove the
    fallback a genuinely corrupt file needs: losing the store costs every
    SNMPv3 account on the machine, and none of them can be recovered from
    anywhere else."""
    store = str(tmp_path / "usm.dat")
    user = _one_user()
    usm.save_store(store, ENGINE_A, [user])
    usm.save_store(store, ENGINE_A, [user])
    with open(store, "wb") as fh:
        fh.write(b"not a store at all")
    users, problems = usm.load_store(store, ENGINE_A)
    assert [u.name for u in users] == ["librenms"], "the .bak was not used"
    assert any("previous copy" in p for p in problems)
