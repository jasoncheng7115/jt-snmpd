"""感測器解析對惡意/損壞緩衝區的韌性。

**為什麼需要這一整檔測試**

WMI 資料區塊的每一個偏移量與長度都**取自緩衝區自身**，而緩衝區的內容來自
韌體與驅動程式。Python 是記憶體安全的，所以不會有典型的堆疊破壞；真正的
風險換了一種形式：

1. 一個亂寫的 `InstanceCount`（例如 0xFFFFFFFF）會讓迴圈跑四十億次——
   在「絕不能拖慢 host」這條硬性要求下，這就是一次自我 DoS。
2. 一個亂寫的 `BufferSize` 會讓我們配置任意大的記憶體。
3. 韌體提供的字串直接進 SNMP OCTET STRING，控制字元與超長字串會讓
   回應變形或撐破 1400 位元組上限。
4. `0` 與 `0xFFFFFFFF` 是 ACPI 表示「未知」的方式，換算成攝氏是 -273°C
   與 4 億度。送進 LibreNMS 就是一串假告警。

因此解析與採集完全分離：`parse_wnode_all_data()` 是純函式，可以在 Linux 上
餵它任意位元組。這些測試就是在做那件事。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
import sensors  # noqa: E402


def _wnode(*, instances: int, flags: int = 0x0200, data_off: int = 64,
           inst_size: int = 76, name_off: int = 0, payload: bytes = b"",
           buffer_size: int | None = None, total: int | None = None) -> bytes:
    """組出一個可控的 WNODE_ALL_DATA 緩衝區，供刻意破壞。"""
    body = bytearray(total if total is not None else max(64 + instances * inst_size,
                                                         64 + len(payload)))
    struct.pack_into("<I", body, 0, buffer_size if buffer_size is not None else len(body))
    struct.pack_into("<I", body, 44, flags)
    struct.pack_into("<III", body, 48, data_off, instances, name_off)
    struct.pack_into("<I", body, 60, inst_size)
    if payload:
        body[data_off:data_off + len(payload)] = payload
    return bytes(body)


def _zone_payload(current_tenths_k: int, critical: int = 3800,
                  passive: int = 3600) -> bytes:
    """MSAcpi_ThermalZoneTemperature 的 9 個 ULONG。"""
    return struct.pack("<9I", 0, 0, 0, 0, 4, current_tenths_k, passive, critical, 0)


# --- 溫度換算：未知值必須消失，不得變成假數字 -------------------------------

@pytest.mark.parametrize("tenths,expected", [
    (2981, 25.0),       # 25.0 °C
    (3081, 35.0),
    (2732, 0.0),        # 冰點
    (3448, 71.7),       # 使用者實測看過的磁碟高溫區間
])
def test_valid_temperatures_convert(tenths, expected):
    assert sensors.tenths_kelvin_to_celsius(tenths) == pytest.approx(expected, abs=0.1)


@pytest.mark.parametrize("bad", [0, 1, 0xFFFFFFFF, 0x80000000, -1, 2331, 4733, 10**9])
def test_unknown_or_absurd_temperatures_are_rejected(bad):
    """ACPI 以 0 / 0xFFFFFFFF 表示未知。換算後是 -273°C 與 4 億度，
    兩者若送進 LibreNMS 都會產生假告警。"""
    assert sensors.tenths_kelvin_to_celsius(bad) is None


def test_non_integer_temperature_rejected():
    assert sensors.tenths_kelvin_to_celsius("2981") is None
    assert sensors.tenths_kelvin_to_celsius(None) is None


# --- 緩衝區解析：不可信任任何自稱的數字 -------------------------------------

def test_empty_and_short_buffers():
    for raw in (b"", b"\x00", b"\x00" * 63):
        assert sensors.parse_wnode_all_data(raw) == []


def test_random_garbage_never_raises():
    """任何位元組序列都不得讓解析拋例外——拋了就是一次快照建置失敗。"""
    for pattern in (b"\xff", b"\x00", b"\xaa", b"\x7f"):
        for length in (64, 100, 512, 4096):
            sensors.parse_wnode_all_data(pattern * length)


def test_absurd_instance_count_is_capped():
    """核心斷言：四十億筆執行個體不得變成四十億次迴圈。"""
    raw = _wnode(instances=0xFFFFFFFF, total=4096)
    out = sensors.parse_wnode_all_data(raw)
    assert len(out) <= sensors.MAX_INSTANCES


def test_instance_count_cap_is_configurable_and_enforced():
    raw = _wnode(instances=1000, inst_size=76, total=64 + 1000 * 76)
    assert len(sensors.parse_wnode_all_data(raw, max_instances=3)) == 3


def test_data_offset_beyond_buffer_yields_nothing():
    raw = _wnode(instances=4, data_off=100000, total=1024)
    assert sensors.parse_wnode_all_data(raw) == []


def test_instance_size_zero_is_rejected():
    """inst_size=0 會讓每筆都落在同一個偏移，且迴圈永遠取到空資料。"""
    assert sensors.parse_wnode_all_data(_wnode(instances=5, inst_size=0)) == []


def test_instance_size_larger_than_buffer_is_rejected():
    assert sensors.parse_wnode_all_data(
        _wnode(instances=2, inst_size=1 << 30, total=1024)) == []


def test_buffer_size_field_larger_than_actual_bytes():
    """緩衝區自稱 1 MB 但實際只有 1 KB —— 必須以實際持有的位元組為準。"""
    raw = _wnode(instances=2, inst_size=76, buffer_size=1 << 20,
                 total=64 + 2 * 76, payload=_zone_payload(2981) * 2)
    out = sensors.parse_wnode_all_data(raw)
    for inst in out:
        assert len(inst.data) <= len(raw)


def test_partial_last_instance_is_dropped_not_truncated():
    """最後一筆被截斷時應丟棄該筆，而非送出半套資料。"""
    raw = _wnode(instances=3, inst_size=76, total=64 + 76 * 2 + 10,
                 payload=_zone_payload(2981) * 2)
    out = sensors.parse_wnode_all_data(raw)
    assert len(out) == 2
    assert all(len(i.data) == 76 for i in out)


def test_variable_length_instances_with_bad_entries_skip_only_those():
    """壞掉的一筆不該讓整批資料作廢。"""
    total = 4096
    body = bytearray(total)
    struct.pack_into("<I", body, 0, total)
    struct.pack_into("<I", body, 44, 0)          # 非固定長度
    struct.pack_into("<III", body, 48, 0, 3, 0)
    good = _zone_payload(2981)
    body[200:200 + len(good)] = good
    body[400:400 + len(good)] = good
    struct.pack_into("<II", body, 60, 200, len(good))          # 好
    struct.pack_into("<II", body, 68, 999999, len(good))       # 偏移越界
    struct.pack_into("<II", body, 76, 400, len(good))          # 好
    out = sensors.parse_wnode_all_data(bytes(body))
    assert len(out) == 2


# --- 執行個體名稱：韌體字串必須清理 -----------------------------------------

def test_control_characters_are_stripped():
    assert sensors.sanitise_name("THM_\x00\x07\x1b[31m0") == "THM_[31m0"
    assert "\n" not in sensors.sanitise_name("a\nb")


def test_name_length_is_capped():
    assert len(sensors.sanitise_name("A" * 10000)) == sensors.MAX_NAME_CHARS


def test_bad_name_offsets_do_not_raise():
    for name_off in (1, 63, 100000, 0xFFFFFFFF):
        raw = _wnode(instances=2, name_off=name_off, total=1024,
                     payload=_zone_payload(2981) * 2)
        sensors.parse_wnode_all_data(raw)


def test_absurd_name_length_is_rejected():
    """名稱長度欄位若是垃圾，不可據以切出巨大的字串。"""
    total = 2048
    body = bytearray(total)
    struct.pack_into("<I", body, 0, total)
    struct.pack_into("<I", body, 44, 0x0200)
    struct.pack_into("<III", body, 48, 200, 1, 400)
    struct.pack_into("<I", body, 60, 76)
    body[200:276] = _zone_payload(2981)
    struct.pack_into("<I", body, 400, 500)       # 名稱位於 500
    struct.pack_into("<H", body, 500, 0xFFFF)    # 宣稱長度 65535
    out = sensors.parse_wnode_all_data(bytes(body))
    assert len(out) == 1
    assert out[0].name == ""                     # 長度不可信 → 不給名稱


# --- 熱區欄位解析 -----------------------------------------------------------

def test_thermal_zone_round_trip():
    raw = _wnode(instances=1, payload=_zone_payload(2981, critical=3800),
                 total=1024)
    inst = sensors.parse_wnode_all_data(raw)[0]
    z = sensors.parse_thermal_zone(inst)
    assert z is not None
    assert z.celsius == pytest.approx(25.0, abs=0.1)
    assert z.critical_c == pytest.approx(106.85, abs=0.1)


def test_thermal_zone_with_unknown_temperature_is_dropped():
    """溫度未知時整筆消失（§6.9），而不是回報 0°C 或 -273°C。"""
    raw = _wnode(instances=1, payload=_zone_payload(0), total=1024)
    inst = sensors.parse_wnode_all_data(raw)[0]
    assert sensors.parse_thermal_zone(inst) is None


def test_thermal_zone_too_short_is_dropped():
    assert sensors.parse_thermal_zone(
        sensors.WnodeInstance(0, b"\x00" * 20, "x")) is None


def test_critical_trip_point_may_be_absent_without_dropping_reading():
    """跳脫點不可信時仍應保留溫度本身——那才是主要資料。"""
    raw = _wnode(instances=1, payload=_zone_payload(2981, critical=0, passive=0),
                 total=1024)
    z = sensors.parse_thermal_zone(sensors.parse_wnode_all_data(raw)[0])
    assert z is not None and z.celsius == pytest.approx(25.0, abs=0.1)
    assert z.critical_c is None and z.passive_c is None


# --- 緩衝區配置上限（避免驅動回報巨大尺寸）---------------------------------

def test_buffer_allocation_is_bounded():
    assert 0 < sensors.MAX_WMI_BUFFER <= 16 << 20
    assert 0 < sensors.MAX_INSTANCES <= 4096
    assert 0 < sensors.MAX_PROCESSORS <= 4096


def test_module_imports_on_non_windows():
    """agent 的測試在 Linux 上跑；本模組必須能被匯入且回空值。"""
    assert sensors.read_thermal_zones() == []
    assert sensors.read_battery() is None
    assert sensors.read_cpu_frequencies() == []


def test_processor_buffer_uses_group_aware_count():
    """os.cpu_count() 只反映呼叫端所屬的處理器群組，在 64 核以上會少報，
    而核心是照**實際處理器數**寫回緩衝區的——配小了就是真正的堆積毀損。
    這是原型階段實際存在的缺陷，修正必須留在程式碼裡。"""
    src = (Path(__file__).resolve().parent.parent / "deploy"
           / "sensors.py").read_text(encoding="utf-8")
    assert "GetActiveProcessorCount" in src, "必須使用群組感知的處理器計數"
    assert "_ALL_PROCESSOR_GROUPS" in src
    # 只看可執行的敘述——說明文字裡本來就會提到 os.cpu_count()，
    # 那是在解釋為什麼**不能**用它。
    import ast
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "read_cpu_frequencies")
    stmts = [x for x in fn.body
             if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant))]
    body = "\n".join(ast.unparse(x) for x in stmts)
    assert "os.cpu_count" not in body, "不可用 os.cpu_count() 決定緩衝區大小"
    assert "MAX_PROCESSORS" in body, "缺少上限，API 回傳異常值會配置過大緩衝區"
