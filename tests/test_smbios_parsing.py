"""SMBIOS parsing against hostile bytes.

Every length and count in an SMBIOS table comes from the BIOS. Hard rule 17 says
to treat those as hostile input, and the reason is not memory safety — Python
will not read out of bounds — but work: a nonsense length has the agent allocate
or loop on the strength of it, and the first requirement of this project is not
to slow the host it monitors.

`tests/test_sensors_parsing.py` is the template. This is the same treatment for
the other parser that reads firmware, which had the separation into a pure
function but never had the test.
"""

from __future__ import annotations

import random
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import smbios  # noqa: E402


def _structure(stype: int, formatted: bytes, strings: list[bytes]) -> bytes:
    """One well-formed SMBIOS structure: header, formatted area, string area."""
    body = struct.pack("<BBH", stype, 4 + len(formatted), 0x1000) + formatted
    if strings:
        tail = b"\x00".join(strings) + b"\x00\x00"
    else:
        tail = b"\x00\x00"
    return body + tail


def _table(structures: bytes) -> bytes:
    """The 8-byte RSMB header GetSystemFirmwareTable returns, then the table."""
    return struct.pack("<BBBBI", 0, 3, 3, 0, len(structures)) + structures


def test_a_well_formed_table_parses():
    blob = _table(_structure(1, b"\x01\x02" + b"\x00" * 20, [b"ACME", b"Server X"])
                  + _structure(127, b"", []))
    out = smbios.parse_smbios(blob)
    assert [s["type"] for s in out] == [1, 127]
    assert out[0]["strings"][:2] == ["ACME", "Server X"]


def test_end_of_table_stops_the_walk():
    blob = _table(_structure(127, b"", []) + _structure(1, b"\x00" * 8, [b"never"]))
    assert [s["type"] for s in smbios.parse_smbios(blob)] == [127]


@pytest.mark.parametrize("blob", [
    b"", b"\x00", b"\x00" * 7,                        # shorter than the header
    _table(b""),                                      # header, no structures
    _table(b"\x01"), _table(b"\x01\x02"),             # truncated header
    _table(struct.pack("<BBH", 1, 0, 0)),             # length 0
    _table(struct.pack("<BBH", 1, 3, 0)),             # length below the header
    _table(struct.pack("<BBH", 1, 255, 0)),           # length past the buffer
    _table(struct.pack("<BBH", 1, 4, 0)),             # no string terminator
    struct.pack("<BBBBI", 0, 3, 3, 0, 0xFFFFFFFF),    # header claims 4 GB
])
def test_malformed_tables_return_rather_than_hang(blob):
    out = smbios.parse_smbios(blob)
    assert isinstance(out, list)


def test_a_length_of_zero_cannot_spin_forever():
    """The loop advances by the structure's own length. A zero would leave `pos`
    where it was, and the guard is what turns that into a stop rather than a
    hang."""
    blob = _table(struct.pack("<BBH", 1, 0, 0) * 100)
    assert smbios.parse_smbios(blob) == []


def test_the_structure_count_is_capped():
    """Thousands of minimal structures are still a legal table. The cap is what
    keeps the work bounded by our constant rather than by what firmware claims.
    """
    one = _structure(1, b"", [])
    blob = _table(one * (smbios.MAX_STRUCTURES + 500))
    assert len(smbios.parse_smbios(blob)) <= smbios.MAX_STRUCTURES


def test_random_bytes_never_raise():
    rnd = random.Random(20260827)
    for _ in range(3000):
        n = rnd.randrange(0, 400)
        blob = bytes(rnd.randrange(256) for _ in range(n))
        smbios.parse_smbios(blob)


def test_random_mutations_of_a_valid_table_never_raise():
    """Closer to what a half-broken BIOS produces than random noise: a real
    table with bytes flipped in it."""
    rnd = random.Random(7115)
    good = bytearray(_table(
        _structure(1, b"\x01\x02" + b"\x00" * 20, [b"ACME", b"Server X"])
        + _structure(17, b"\x03" + b"\x00" * 30, [b"DIMM0"])
        + _structure(127, b"", [])))
    for _ in range(3000):
        blob = bytearray(good)
        for _ in range(rnd.randrange(1, 6)):
            blob[rnd.randrange(len(blob))] = rnd.randrange(256)
        smbios.parse_smbios(bytes(blob))


def test_the_buffer_ceiling_is_enforced_not_only_declared():
    """A constant nobody consults is decoration. This is the same mistake as a
    .bak that is written and never read."""
    import ast

    src = (Path(__file__).resolve().parents[1] / "deploy" / "smbios.py").read_text(
        encoding="utf-8")
    fn = next(ast.unparse(n) for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "get_raw_smbios")
    assert "MAX_TABLE_BYTES" in fn, (
        "the size comes from firmware; asking for it is right, believing it "
        "without a ceiling is not")
    walk = next(ast.unparse(n) for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name == "parse_smbios")
    assert "MAX_STRUCTURES" in walk
