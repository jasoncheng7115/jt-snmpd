"""walk 正確性與回應大小測試（spec §36、§4.4）。

spec §36 把下列每一種都列為「不可接受」，而它們全部發生在 MIB 層：
GETNEXT loop、ordering 錯亂、duplicate OID、endOfMibView 不正確、
bulk response 太大。§4.3 的 snapshot + bisect 架構聲稱這些是「結構保證」——
這個檔案就是在驗證那個聲稱，不是相信它。
"""

from __future__ import annotations

import socket
import shutil
import subprocess
import sys
import time

import pytest
from pyasn1.codec.ber import decoder as ber_decoder
from pyasn1.codec.ber import encoder as ber_encoder
from pysnmp.proto import api

from bench.gate_c.snapshot import build_synthetic_snapshot

BASE = ".1.3.6.1.4.1.99999"
BASE_TUPLE = (1, 3, 6, 1, 4, 1, 99999)
# These tests drive a real agent with net-snmp's CLI, so they need net-snmp
# installed. Skip explicitly when it is missing rather than letting subprocess
# raise FileNotFoundError — the Windows CI runner has no net-snmp, and an
# unexplained WinError 2 buried in a traceback took a round trip to diagnose.
#
# Skipping does create a coverage hole, so the Linux workflow installs net-snmp
# and asserts it is present before running: these are the tests that validate
# GETNEXT/GETBULK against a real implementation, and quietly not running them
# would be worse than not having them.
_NETSNMP = shutil.which("snmpbulkwalk")
pytestmark = pytest.mark.skipif(
    _NETSNMP is None,
    reason="needs net-snmp (snmpbulkwalk); install the 'snmp' package")

N_VARBINDS = 2000
COMMUNITY = "bench"
PORT = 11191
MAX_RESPONSE_BYTES = 1400


@pytest.fixture(scope="module")
def agent():
    proc = subprocess.Popen(
        [sys.executable, "-m", "bench.gate_c.agent", "--varbinds", str(N_VARBINDS),
         "--port", str(PORT), "--community", COMMUNITY],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for _ in range(600):
            line = proc.stdout.readline()
            if line.startswith("READY"):
                break
            if not line:
                raise RuntimeError("agent 提前結束")
            time.sleep(0.01)
        else:
            raise RuntimeError("agent 未就緒")
        yield PORT
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture(scope="module")
def expected():
    return build_synthetic_snapshot(N_VARBINDS)


def _walk(port: int, oid: str = BASE, maxrep: int = 25) -> list[tuple[str, str]]:
    r = subprocess.run(
        ["snmpbulkwalk", "-v2c", "-c", COMMUNITY, f"-Cr{maxrep}", "-r0", "-t", "10",
         "-On", "-Oe", f"127.0.0.1:{port}", oid],
        capture_output=True, text=True, timeout=180,
    )
    out = []
    for ln in r.stdout.splitlines():
        if " = " not in ln or not ln.startswith(".1"):
            continue
        name, val = ln.split(" = ", 1)
        if "No more variables left" in val:
            continue
        out.append((name.strip(), val.strip()))
    return out


def test_walk_returns_every_oid_exactly_once(agent, expected):
    got = _walk(agent)
    got_oids = [tuple(int(p) for p in n.lstrip(".").split(".")) for n, _ in got]
    assert len(got_oids) == len(set(got_oids)), "出現 duplicate OID"
    assert got_oids == list(expected.oids), "walk 結果與 snapshot 內容不一致"


def test_walk_is_strictly_lexicographically_ordered(agent):
    got = _walk(agent)
    oids = [tuple(int(p) for p in n.lstrip(".").split(".")) for n, _ in got]
    bad = [(a, b) for a, b in zip(oids, oids[1:]) if not a < b]
    assert not bad, f"ordering 錯亂或有重複，前 3 處：{bad[:3]}"


def test_walk_terminates_and_does_not_loop(agent, expected):
    """GETNEXT loop 的症狀是 walk 永不結束。用筆數上界當守門。"""
    got = _walk(agent)
    assert len(got) == len(expected), f"預期 {len(expected)} 筆，實得 {len(got)} 筆"


@pytest.mark.parametrize("maxrep", [1, 2, 10, 25, 100, 1000])
def test_walk_result_is_identical_regardless_of_max_repetitions(agent, expected, maxrep):
    """max-repetitions 只該影響封包數，不該影響內容。

    §4.4 要求伺服器端對 max-repetitions 設上限（預設 25）並忽略更大的請求值；
    截斷後 manager 會自動再要一次，因此結果集必須完全相同。
    """
    got = _walk(agent, maxrep=maxrep)
    oids = [tuple(int(p) for p in n.lstrip(".").split(".")) for n, _ in got]
    assert oids == list(expected.oids)


# --- §4.4 回應封包大小上限 --------------------------------------------------

def _raw_getbulk(port: int, oid_tuple: tuple[int, ...], max_reps: int) -> bytes:
    """自己組 GETBULK 並用 raw UDP 送出，量測回應 datagram 的實際位元組數。

    這是唯一能真正驗證 §4.4 的方法：net-snmp 的命令列工具不會告訴你
    封包多大，而超過 MTU 的回應會被防火牆分片丟棄，症狀是
    「LibreNMS 間歇性抓不到資料」。
    """
    pMod = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    req = pMod.GetBulkRequestPDU()
    pMod.apiBulkPDU.set_defaults(req)
    pMod.apiBulkPDU.set_non_repeaters(req, 0)
    pMod.apiBulkPDU.set_max_repetitions(req, max_reps)
    pMod.apiBulkPDU.set_varbinds(req, [(pMod.ObjectIdentifier(oid_tuple), pMod.Null(""))])
    msg = pMod.Message()
    pMod.apiMessage.set_defaults(msg)
    pMod.apiMessage.set_community(msg, COMMUNITY)
    pMod.apiMessage.set_pdu(msg, req)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(10)
    try:
        s.sendto(ber_encoder.encode(msg), ("127.0.0.1", port))
        data, _ = s.recvfrom(65535)
        return data
    finally:
        s.close()


@pytest.mark.parametrize("max_reps", [25, 100, 1000, 10000])
def test_response_never_exceeds_mtu_budget(agent, max_reps):
    data = _raw_getbulk(agent, BASE_TUPLE, max_reps)
    assert len(data) <= MAX_RESPONSE_BYTES, (
        f"max-repetitions={max_reps} 的回應為 {len(data)} bytes，"
        f"超過 {MAX_RESPONSE_BYTES}，會造成 IP 分片"
    )


def test_oversized_max_repetitions_is_capped_not_rejected(agent):
    """要求 10000 筆時必須回傳「較少但有效」的結果，而不是錯誤或空回應。"""
    data = _raw_getbulk(agent, BASE_TUPLE, 10000)
    pMod = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    msg, _ = ber_decoder.decode(data, asn1Spec=pMod.Message())
    pdu = pMod.apiMessage.get_pdu(msg)
    assert int(pMod.apiPDU.get_error_status(pdu)) == 0
    vbs = pMod.apiPDU.get_varbinds(pdu)
    assert 0 < len(vbs) <= 25, f"應被上限截到 25 筆以內，實得 {len(vbs)}"


def test_end_of_mib_view_is_not_padded(agent, expected):
    """走到 MIB 結尾時，回應不該用 endOfMibView 塞滿 max-repetitions 筆。

    pysnmp 原生 BulkCommandResponder 會這麼做（實測 200 筆的樹會回 225 行），
    每個 subtree 的最後一個封包都白費頻寬。§36 列為不可接受。
    """
    last = expected.oids[-1]
    data = _raw_getbulk(agent, last, 25)
    pMod = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    msg, _ = ber_decoder.decode(data, asn1Spec=pMod.Message())
    vbs = pMod.apiPDU.get_varbinds(pMod.apiMessage.get_pdu(msg))
    assert len(vbs) == 1, f"結尾回應應只含 1 筆 endOfMibView，實得 {len(vbs)} 筆"


# --- GET / GETNEXT 邊界 ------------------------------------------------------

def _snmpcmd(tool: str, port: int, oid: str) -> str:
    r = subprocess.run(
        [tool, "-v2c", "-c", COMMUNITY, "-r0", "-t", "10", "-On", f"127.0.0.1:{port}", oid],
        capture_output=True, text=True, timeout=30,
    )
    return (r.stdout + r.stderr).strip()


def test_get_existing_oid(agent, expected):
    oid = "." + ".".join(str(x) for x in expected.oids[0])
    assert "No Such" not in _snmpcmd("snmpget", agent, oid)


def test_get_nonexistent_instance_returns_no_such_instance(agent):
    out = _snmpcmd("snmpget", agent, f"{BASE}.1.1.1.999999")
    assert "No Such Instance" in out, out


def test_get_nonexistent_subtree_returns_no_such_object(agent):
    out = _snmpcmd("snmpget", agent, ".1.3.6.1.4.1.88888.1.0")
    assert "No Such" in out, out


def test_getnext_before_first_oid_returns_first(agent, expected):
    out = _snmpcmd("snmpgetnext", agent, BASE)
    first = "." + ".".join(str(x) for x in expected.oids[0])
    assert out.startswith(first), out


def test_getnext_past_last_oid_returns_end_of_mib(agent, expected):
    last = "." + ".".join(str(x) for x in expected.oids[-1])
    out = _snmpcmd("snmpgetnext", agent, last)
    assert "No more variables left" in out, out
