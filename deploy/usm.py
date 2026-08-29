"""SNMPv3 user security model: which algorithms are allowed, and where the keys
live.

**Why the keys on disk are localized keys and not passphrases**

A localized key is derived from the passphrase *and* this engine's engineID, so
it authenticates to exactly one agent. Storing passphrases instead would mean
that reading one machine's secrets file, which a stolen backup or a misconfigured
share is enough for, hands over an account on every machine that shares the
credential. Customers run these by the hundred from one GPO, so that is the
realistic shape of the loss. The passphrase is used once, at provisioning time,
and is not written down.

The trade this makes is deliberate and has a cost: **a localized key cannot be
re-localized**. If the engineID changes, and a cloned or reimaged machine
regenerates it by design, the stored keys are dead and cannot be recovered here,
because the passphrase they came from is gone. The agent detects that case and
says so rather than failing to authenticate for reasons nobody can see. Estates
built from templates should provision through Group Policy after first boot
rather than baking accounts into the image; docs/snmpv3.md says so.

**Why DPAPI machine scope**

The blob is decryptable only on the machine that wrote it, which is what stops a
copied file from being useful elsewhere. It does not, and cannot, stop an
administrator on that machine: the agent runs as LocalSystem and has to be able
to read the keys unattended, so any protection it can undo unattended is
protection an administrator can undo too. The honest claim is that it defends
the file at rest and in transit, not the machine's own administrators. The
secrets directory is already restricted to SYSTEM and Administrators.
"""

from __future__ import annotations

import ctypes
import json
import os
from dataclasses import dataclass

from pysnmp.entity import config as _pyconfig
from pysnmp.proto import rfc1902

# --- Algorithms -------------------------------------------------------------
#
# The defaults are SHA-256 and AES-128, which is what LibreNMS's net-snmp can
# talk to everywhere. AES-192 and AES-256 were never standardised for USM: two
# incompatible key-extension schemes exist (the Blumenthal draft and Reeder's),
# and Debian and Ubuntu build net-snmp without --enable-blumenthal-aes, so an
# agent configured that way can be unreachable from the very manager it was set
# up for. They are selectable, and saying so at startup is part of selecting
# them.
AUTH_PROTOCOLS = {
    "SHA-224": _pyconfig.USM_AUTH_HMAC128_SHA224,
    "SHA-256": _pyconfig.USM_AUTH_HMAC192_SHA256,
    "SHA-384": _pyconfig.USM_AUTH_HMAC256_SHA384,
    "SHA-512": _pyconfig.USM_AUTH_HMAC384_SHA512,
}

PRIV_PROTOCOLS = {
    "AES-128": _pyconfig.USM_PRIV_CFB128_AES,
    "AES-192": _pyconfig.USM_PRIV_CFB192_AES,
    "AES-256": _pyconfig.USM_PRIV_CFB256_AES,
}

# Interoperability risk rather than a weakness in the cipher. Warned about at
# startup, not refused.
PRIV_NEEDS_WARNING = {"AES-192", "AES-256"}

# Refused even though pysnmp implements them. MD5 and SHA-1 are broken for this
# purpose, DES is 56-bit, and 3DES in USM never left draft. A configuration
# naming one of these is an error the operator has to see, not something to
# quietly downgrade or quietly accept.
REFUSED_AUTH = {"MD5": "HMAC-MD5 is broken; use SHA-256",
                "SHA": "HMAC-SHA-1 is deprecated for this use; use SHA-256",
                "SHA-1": "HMAC-SHA-1 is deprecated for this use; use SHA-256"}
REFUSED_PRIV = {"DES": "56-bit DES is not a defence; use AES-128",
                "3DES": "3DES in USM never left draft; use AES-128"}

DEFAULT_AUTH = "SHA-256"
DEFAULT_PRIV = "AES-128"

# Long enough that guessing is not the cheapest attack. RFC 3414 §11.2 asks for
# at least 8; monitoring credentials are typed once and stored, so there is no
# reason to sit at the floor.
MIN_PASSPHRASE = 12


class UsmError(ValueError):
    """A configuration problem the operator has to fix."""


def check_algorithms(auth: str, priv: str) -> list[str]:
    """Validate a pair of algorithm names, returning any warnings.

    Raises UsmError with a message naming the replacement, because "unknown
    protocol" tells an operator nothing they can act on.
    """
    if auth in REFUSED_AUTH:
        raise UsmError(f"authentication protocol {auth} is refused: {REFUSED_AUTH[auth]}")
    if priv in REFUSED_PRIV:
        raise UsmError(f"privacy protocol {priv} is refused: {REFUSED_PRIV[priv]}")
    if auth not in AUTH_PROTOCOLS:
        raise UsmError(f"unknown authentication protocol {auth!r}; "
                       f"choose one of {', '.join(sorted(AUTH_PROTOCOLS))}")
    if priv not in PRIV_PROTOCOLS:
        raise UsmError(f"unknown privacy protocol {priv!r}; "
                       f"choose one of {', '.join(sorted(PRIV_PROTOCOLS))}")
    warnings = []
    if priv in PRIV_NEEDS_WARNING:
        warnings.append(
            f"{priv} was never standardised for SNMPv3 and has two incompatible "
            "key-extension schemes; Debian and Ubuntu build net-snmp without "
            "the one pysnmp uses, so LibreNMS may be unable to reach this "
            "agent. AES-128 is the interoperable choice")
    return warnings


def check_passphrase(label: str, value: str) -> None:
    if len(value) < MIN_PASSPHRASE:
        raise UsmError(f"the {label} passphrase must be at least "
                       f"{MIN_PASSPHRASE} characters")


# --- Key localization -------------------------------------------------------

def localize(auth: str, priv: str, auth_pass: str, priv_pass: str,
             engine_id: bytes) -> tuple[bytes, bytes]:
    """Derive the localized auth and privacy keys for one engine.

    The derivation deliberately mirrors what pysnmp's add_v3_user does with a
    passphrase, so a key localized here and a key localized there are the same
    bytes. Doing it here is what lets the passphrase be discarded.
    """
    check_algorithms(auth, priv)
    auth_oid, priv_oid = AUTH_PROTOCOLS[auth], PRIV_PROTOCOLS[priv]
    auth_svc = _pyconfig.AUTH_SERVICES[auth_oid]
    priv_svc = _pyconfig.PRIV_SERVICES[priv_oid]
    # pysnmp's localization reaches for asOctets(), so the engineID travels as
    # an OctetString rather than as plain bytes
    eid = rfc1902.OctetString(engine_id)
    auth_key = auth_svc.localize_key(
        auth_svc.hash_passphrase(auth_pass.encode("utf-8")), eid)
    priv_key = priv_svc.localize_key(
        auth_oid, priv_svc.hash_passphrase(auth_oid, priv_pass.encode("utf-8")),
        eid)
    return bytes(auth_key), bytes(priv_key)


# --- DPAPI ------------------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


CRYPTPROTECT_LOCAL_MACHINE = 0x4
CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _call(fn, data: bytes, flags: int) -> bytes:
    out = _Blob()
    if not fn(ctypes.byref(_blob(data)), None, None, None, None, flags,
              ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    """Encrypt to this machine. UI_FORBIDDEN because there is no desktop to
    prompt on: the agent runs as a service."""
    return _call(ctypes.windll.crypt32.CryptProtectData, data,
                 CRYPTPROTECT_LOCAL_MACHINE | CRYPTPROTECT_UI_FORBIDDEN)


def unprotect(data: bytes) -> bytes:
    return _call(ctypes.windll.crypt32.CryptUnprotectData, data,
                 CRYPTPROTECT_LOCAL_MACHINE | CRYPTPROTECT_UI_FORBIDDEN)


# --- The store --------------------------------------------------------------

@dataclass(frozen=True)
class UsmUser:
    name: str
    auth: str
    priv: str
    auth_key: bytes
    priv_key: bytes


def store_to_json(engine_id: bytes, users: list[UsmUser]) -> bytes:
    return json.dumps({
        "schema_version": 1,
        "engine_id": engine_id.hex(),
        "users": [{"name": u.name, "auth": u.auth, "priv": u.priv,
                   "auth_key": u.auth_key.hex(), "priv_key": u.priv_key.hex()}
                  for u in users],
    }, indent=1).encode("utf-8")


def store_from_json(raw: bytes, engine_id: bytes) -> tuple[list[UsmUser], list[str]]:
    """Parse a decrypted store, returning the usable users and any problems.

    An engineID that no longer matches is the cloned-machine case. Those keys
    are unusable and cannot be repaired from here, so they are dropped with an
    explanation rather than handed to pysnmp to fail authentication silently.
    """
    problems: list[str] = []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        return [], [f"the SNMPv3 store could not be parsed: {exc}"]
    if not isinstance(data, dict):
        return [], ["the SNMPv3 store is not an object"]

    stored_id = data.get("engine_id")
    if stored_id != engine_id.hex():
        return [], [
            "the SNMPv3 keys were localized against engineID "
            f"{stored_id} but this engine is {engine_id.hex()}. A localized key "
            "is bound to the engineID it was made for and cannot be converted, "
            "so every SNMPv3 user has to be provisioned again. This normally "
            "means the machine was cloned from a template or reimaged"]

    users: list[UsmUser] = []
    for entry in data.get("users") or []:
        if not isinstance(entry, dict):
            problems.append("a user entry was not an object and was skipped")
            continue
        name = entry.get("name")
        try:
            if not isinstance(name, str) or not name:
                raise UsmError("a user entry has no name")
            check_algorithms(entry.get("auth"), entry.get("priv"))
            users.append(UsmUser(name, entry["auth"], entry["priv"],
                                 bytes.fromhex(entry["auth_key"]),
                                 bytes.fromhex(entry["priv_key"])))
        except (UsmError, KeyError, TypeError, ValueError) as exc:
            problems.append(f"SNMPv3 user {name!r} was skipped: {exc}")
    return users, problems


def save_store(path: str, engine_id: bytes, users: list[UsmUser]) -> None:
    """Encrypt and write, keeping the previous copy.

    temp-flush-replace already makes a torn write impossible: an interrupted
    save leaves either the old file or the new one, never half of each. The
    .bak is for the failures that come from outside — a filesystem error,
    antivirus quarantining the file, a backup agent restoring something odd.

    It is worth keeping here because of what losing it costs. Every SNMPv3
    account on the machine goes with it, and they cannot be recovered from
    anywhere else: the keys are localized to this engineID and the passphrases
    they came from were deliberately never stored. The operator has to visit the
    machine and provision each account again, and on an estate provisioned from
    one policy that is every machine that lost the file.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    blob = protect(store_to_json(engine_id, users))
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    if os.path.exists(path):
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass          # a missing .bak only costs the fallback, not the save
    os.replace(tmp, path)


def load_store(path: str, engine_id: bytes) -> tuple[list[UsmUser], list[str]]:
    problems: list[str] = []
    for candidate in (path, path + ".bak"):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "rb") as fh:
                blob = fh.read()
            raw = unprotect(blob)
        except OSError as exc:
            problems.append(
                f"{os.path.basename(candidate)} could not be read or decrypted: "
                f"{exc}. DPAPI machine keys do not travel, so this is also what "
                "a store copied from another machine looks like")
            continue
        users, parse_problems = store_from_json(raw, engine_id)
        # An empty store that parsed cleanly is an answer, not a failure.
        #
        # This used to fall through to the .bak whenever `users` was empty,
        # which made removing the **last** account impossible: the file was
        # written correctly with no users, the loader decided it was unusable,
        # and the previous copy put the account back. An operator removing a
        # compromised credential was told "removed" and the credential kept
        # working. Measured on a domain controller: `user remove librenms`
        # followed by `user list` still listed librenms.
        #
        # `parse_problems` is what separates the two cases. Empty means the file
        # was read and understood and simply holds nothing.
        if users or not parse_problems:
            if candidate.endswith(".bak"):
                problems.append(
                    "the SNMPv3 store was unusable and the previous copy was "
                    "used instead. Check secrets\\usm.dat: an account added "
                    "since the last save will be missing")
            return users, problems + parse_problems
        problems.extend(parse_problems)
    return [], problems
