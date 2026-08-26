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
