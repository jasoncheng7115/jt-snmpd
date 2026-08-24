"""LibreNMS smart 應用程式（NET-SNMP-EXTEND-MIB）與 497 天 uptime 回捲。

**SMART：為什麼換了一條路**

第一版把 NVMe 耐用度與可用備援空間送成 `entPhySensorType = other(1)`，
在 LibreNMS 上完全看不到。原因在 `includes/discovery/sensors/entity-sensor.inc.php`
的對照表只認 9 種型別：

    voltsDC voltsAC amperes watts hertz percentRH rpm celsius dBm

`other` 不在裡面，整筆被**無聲丟棄**。agent 端一切正常、walk 也查得到值，
只是 LibreNMS 不收，這種「兩邊都沒錯，但接不起來」的落差最難查。

LibreNMS 讀 SMART 的正規路徑是 `json_app_get()`，**完全走 SNMP**：

    snmp_get($device, 'nsExtendOutputFull."smart"', '-Oqv', 'NET-SNMP-EXTEND-MIB')

被監控端不需要 LibreNMS agent、不需要 smartctl。我們本來就用 ctypes 讀到了
SMART 屬性，序列化成同樣的 JSON 即可。

**uptime：497 天的回捲**

`sysUpTime` 是 TimeTicks（Unsigned32，百分之一秒），2^32/100 秒 ≈ 497.1 天
必然回捲。這是 RFC 3418 規定的型別，任何相容 agent 都一樣，Windows 內建
SNMP 也不例外，回捲本身修不掉。

修得掉的是**假重開機告警**。LibreNMS 的 `Core.php::calculateUptime()`：

    $uptime = max(round(sysUpTime/100),
                  bad_snmpEngineTime ? 0 : snmpEngineTime,
                  bad_hrSystemUptime ? 0 : round(hrSystemUptime/100));
    if ($uptime < $device->uptime) { Eventlog::log('Device rebooted after ...'); }

而 `windows.yaml` **只設了 `bad_hrSystemUptime: true`**，沒有設
`bad_snmpEngineTime`。snmpEngineTime 的單位是秒、上限 2147483647（約 68 年），
提供它之後 max() 就有一個不回捲的來源。
"""

from __future__ import annotations

import ast
import base64
import gzip
import json
import re
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
sys.path.insert(0, str(DEPLOY))
import smartjson  # noqa: E402

AGENT_SRC = (DEPLOY / "jt_agent.py").read_text(encoding="utf-8")


# --- 索引編碼必須與 LibreNMS 的 Oid::encodeString 相同 ----------------------

def _extend_index(token: str) -> tuple[int, ...]:
    raw = token.encode("ascii")
    return (len(raw),) + tuple(raw)


def test_smart_token_encodes_as_librenms_expects():
    """LibreNMS 文件明言 'zfs' → nsExtendOutputFull.3.122.102.115。"""
    assert _extend_index("zfs") == (3, 122, 102, 115)
    assert _extend_index("smart") == (5, 115, 109, 97, 114, 116)


def test_agent_uses_the_same_encoding():
    fn = next(n for n in ast.walk(ast.parse(AGENT_SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "_extend_index")
    body = ast.unparse(fn)
    assert "len(raw)" in body and "tuple(raw)" in body


# --- JSON 結構必須對得上 LibreNMS 的取值方式 --------------------------------

def _sample(**over):
    health = {"smart": {"reallocated_sectors": {"value": 100, "worst": 100, "raw": 0},
                        "power_on_hours": {"value": 95, "worst": 95, "raw": 12345},
                        "pending_sectors": {"value": 100, "worst": 100, "raw": 0}},
              "smart_by_id": {199: 2, 196: 0, 187: 0},
              "temp_c": 34}
    health.update(over)
    return [{"name": "PhysicalDrive0", "health": health}]


def test_every_id_librenms_reads_is_present():
    """PHP 端直接 `$disk['5']` 取值，缺鍵會產生警告並洗版記錄檔。"""
    doc = smartjson.build_smart_json(_sample())
    disk = doc["data"]["disks"]["PhysicalDrive0"]
    for sid in smartjson.LIBRENMS_SMART_IDS:
        assert sid in disk, f"缺少 SMART ID {sid}"
    for k in smartjson.SELFTEST_KEYS:
        assert k in disk, f"缺少自我測試欄位 {k}"


def test_top_level_json_app_envelope():
    doc = smartjson.build_smart_json(_sample())
    assert doc["version"] == 1 and doc["error"] == 0
    assert set(doc["data"]) >= {"disks", "exit_nonzero", "unhealthy", "dev_error"}


def test_unmeasured_attributes_are_null_not_zero():
    """填 0 的意思是「這顆磁碟很健康」；真相是「我們沒讀到這個屬性」。
    LibreNMS 的 is_numeric(null) 為 false → RRD 存 U（未知），語意才正確。"""
    disk = smartjson.build_smart_json(_sample())["data"]["disks"]["PhysicalDrive0"]
    assert disk["10"] is None
    assert disk["completed"] is None
    assert disk["5"] == 0, "真的讀到 0 時就該是 0"


def test_ata_attributes_map_to_correct_ids():
    disk = smartjson.build_smart_json(_sample())["data"]["disks"]["PhysicalDrive0"]
    assert disk["5"] == 0          # Reallocated_Sector_Ct
    assert disk["9"] == 12345      # Power_On_Hours
    assert disk["197"] == 0        # Current_Pending_Sector
    assert disk["199"] == 2        # UDMA_CRC_Error_Count（僅存在於 smart_by_id）
    assert disk["194"] == 34       # Temperature_Celsius


def test_nvme_fields_map_conservatively():
    """NVMe 沒有 ATA 屬性表。只映射語意確定等價的欄位，其餘留 null，
    一個對錯的 ID 會讓現場照著錯誤指標做決策。"""
    disks = [{"name": "PhysicalDrive1",
              "health": {"power_on_hours": 4321, "temp_c": 41,
                         "media_errors": 0, "avail_spare_pct": 100,
                         "percentage_used": 3}}]
    d = smartjson.build_smart_json(disks)["data"]["disks"]["PhysicalDrive1"]
    assert d["9"] == 4321 and d["194"] == 41
    assert d["187"] == 0 and d["232"] == 100
    assert d["5"] is None, "NVMe 沒有重新配置磁區的概念，不可捏造"
    assert d["233"] is None, "percentage_used 與 Media_Wearout 語意不同，不可硬套"


def test_unhealthy_disks_are_flagged():
    doc = smartjson.build_smart_json(_sample(
        smart={"reallocated_sectors": {"value": 90, "worst": 90, "raw": 8}}))
    assert doc["data"]["disks_with_failed_health"] == ["PhysicalDrive0"]
    assert doc["data"]["unhealthy"] == 1


def test_over_temp_is_flagged():
    doc = smartjson.build_smart_json(_sample(temp_c=85), over_temp_c=70)
    assert doc["data"]["disks_with_over_temp"] == ["PhysicalDrive0"]


def test_disk_name_is_filesystem_safe():
    """名稱會成為 RRD 檔名的一部分。"""
    disks = [{"name": "PhysicalDrive0 / ../etc\\x00", "health": {"temp_c": 30}}]
    name = next(iter(smartjson.build_smart_json(disks)["data"]["disks"]))
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name
    assert "/" not in name and "\\" not in name


def test_disk_without_name_is_skipped():
    assert smartjson.build_smart_json(
        [{"name": "", "health": {"temp_c": 30}}])["data"]["disks"] == {}


# --- 編碼：必須通過 LibreNMS 的 base64 判別，並塞得進 1400 位元組 -----------

def test_encoding_round_trips():
    doc = smartjson.build_smart_json(_sample())
    blob = smartjson.encode_extend_output(doc)
    assert json.loads(gzip.decompress(base64.b64decode(blob))) == doc


def test_encoding_matches_librenms_detection_regex():
    """對應 json_app_get()：
        preg_match('/^[A-Za-z0-9\\/\\+\\n]+\\=*\\n*$/', $output)
        && ! preg_match('/^[0-9]+\\n/', $output)
    不符合就會被當成純文字，走進舊版 CSV 解析而失敗。"""
    blob = smartjson.encode_extend_output(smartjson.build_smart_json(_sample()))
    assert smartjson.looks_like_librenms_base64(blob)
    assert re.fullmatch(rb"[A-Za-z0-9/+\n]+=*\n*", blob)


def test_encoding_is_deterministic():
    """gzip 標頭預設塞入當前時間，會讓相同資料每次產生不同位元組，
    快照因此每 5 秒無謂變動，測試也無法比對。必須固定 mtime。"""
    doc = smartjson.build_smart_json(_sample())
    assert smartjson.encode_extend_output(doc) == smartjson.encode_extend_output(doc)


@pytest.mark.parametrize("n", [1, 2, 4])
def test_encoded_size_fits_single_response(n):
    """回應上限 1400 位元組且不分片。未壓縮的 JSON 兩顆磁碟就會爆掉。"""
    disks = [{"name": f"PhysicalDrive{i}", "health": _sample()[0]["health"]}
             for i in range(n)]
    blob = smartjson.encode_extend_output(smartjson.build_smart_json(disks))
    assert len(blob) < 1200, f"{n} 顆磁碟編碼後 {len(blob)} 位元組，逼近回應上限"


def test_compression_actually_helps():
    doc = smartjson.build_smart_json(_sample())
    raw = len(json.dumps(doc, separators=(",", ":")))
    assert len(smartjson.encode_extend_output(doc)) < raw


def test_agent_enforces_a_varbind_size_cap():
    """磁碟很多時輸出會超過單一 varbind 上限。必須砍，而且**必須留記錄**，
    無聲截斷會讓人以為所有磁碟都在監控中。"""
    assert "MAX_EXTEND_BYTES" in AGENT_SRC
    i = AGENT_SRC.find("MAX_EXTEND_BYTES")
    assert re.search(r"while len\(blob\) > MAX_EXTEND_BYTES", AGENT_SRC), "缺少縮減迴圈"
    assert re.search(r"omitted the last.*error=True", AGENT_SRC, re.S), \
        "截斷必須記錄且進事件檢視器"


def test_agent_publishes_discovery_and_output_oids():
    """LibreNMS 探索走 nsExtendStatus，輪詢走 nsExtendOutputFull。少一個都不會動。"""
    assert "NSEXT_CFG + (21,)" in AGENT_SRC, "缺少 nsExtendStatus（探索找不到 app）"
    assert "NSEXT_OUT1 + (2,)" in AGENT_SRC, "缺少 nsExtendOutputFull（無資料可輪詢）"
    for node in ast.parse(AGENT_SRC).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "NSEXT":
            assert tuple(e.value for e in node.value.elts) == (1, 3, 6, 1, 4, 1, 8072, 1, 3, 2)
            return
    pytest.fail("找不到 NSEXT 定義")


# --- 497 天回捲 -------------------------------------------------------------

TIMETICKS_WRAP_DAYS = 2 ** 32 / 100 / 86400


def test_timeticks_wrap_point_is_497_days():
    assert TIMETICKS_WRAP_DAYS == pytest.approx(497.10, abs=0.01)


def test_agent_emits_snmp_engine_time():
    """這是 sysUpTime 回捲後唯一不回捲的 uptime 來源。"""
    assert "SNMPFW + (3, 0)" in AGENT_SRC, "未輸出 snmpEngineTime"
    for node in ast.parse(AGENT_SRC).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SNMPFW":
            assert tuple(e.value for e in node.value.elts) == (1, 3, 6, 1, 6, 3, 10, 2, 1)
            return
    pytest.fail("找不到 SNMPFW 定義")


def test_engine_time_is_seconds_not_centiseconds():
    """snmpEngineTime 的單位是秒。用百分之一秒會讓數值大 100 倍，
    max() 永遠選它，uptime 直接錯 100 倍。"""
    assert "_k32.GetTickCount64() // 1000" in AGENT_SRC


def test_engine_time_is_clamped_to_int32():
    """Integer32 上限 2147483647。超過會編碼成負數。"""
    assert "2147483647" in AGENT_SRC


def test_engine_boots_is_persisted():
    """(boots, time) 這組值不得重複是 RFC 3414 的要求。
    重開機後 time 歸零，boots 就必須加一，因此得保存。"""
    fn = next(n for n in ast.walk(ast.parse(AGENT_SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "_engine_boots")
    body = ast.unparse(fn)
    assert "ENGINE_FILE" in body and "os.replace" in body, "開機計數必須原子寫入"
    assert "boot_key" in body, "必須以開機時刻判定是否換了一次開機"


def test_engine_id_is_stable_across_restarts():
    """SNMPv3 的使用者金鑰以 engineID 做 localization，變了就全部失效。"""
    # 取原始碼文字而非 ast.unparse，後者會把 0x80 正規化成 128，
    # 而這裡要斷言的正是「有沒有設那個最高位元」這個意圖。
    i = AGENT_SRC.index("def _engine_id()")
    body = AGENT_SRC[i:AGENT_SRC.index("\ndef ", i + 1)]
    assert "MachineGuid" in AGENT_SRC, "engineID 應取自機器層級的穩定識別碼"
    assert "hashlib" in body, "GUID 原文超過 27 位元組上限，需雜湊"
    assert "0x80" in body, "RFC 3411 新格式最高位元必須為 1"

    # 同時驗證語意：實際算一次，確認首位元組的最高位元為 1
    pen = 99999
    first = (pen >> 24) & 0xFF | 0x80
    assert first & 0x80, "engineID 首位元組最高位元必須為 1"


# --- max_temp：LibreNMS 的 Max Temp 面板 ------------------------------------

def test_max_temp_is_emitted_when_observed():
    """LibreNMS 的 smart 應用程式以 `if (isset($disk['max_temp']))` 決定是否
    寫入 maxtemp RRD。不提供的話那張面板仍會渲染，但圖是 404 破圖，
    每個客戶都會看到。這是靠截圖才發現的缺陷。"""
    disks = [{"name": "PhysicalDrive0", "health": {"temp_c": 33}, "max_temp": 41}]
    d = smartjson.build_smart_json(disks)["data"]["disks"]["PhysicalDrive0"]
    assert d["max_temp"] == 41
    assert d["194"] == 33, "目前溫度與最高溫是兩個不同的值"


def test_max_temp_absent_when_never_observed():
    """沒觀測到就不給這個鍵，LibreNMS 的 isset() 會跳過，不會產生假資料。"""
    disks = [{"name": "PhysicalDrive0", "health": {"temp_c": 33}}]
    assert "max_temp" not in smartjson.build_smart_json(disks)["data"]["disks"]["PhysicalDrive0"]


def test_max_temp_is_not_taken_from_a_threshold():
    """Windows 的儲存 API 只給 warning / critical 門檻值，那不是
    「這顆碟到過的最高溫」。拿門檻值填 max_temp 是標錯標籤，
    會讓現場看著一條與實際溫度無關的線做判斷。"""
    disks = [{"name": "PhysicalDrive0",
              "health": {"temp_c": 33, "temp_warn_c": 70, "temp_crit_c": 80}}]
    d = smartjson.build_smart_json(disks)["data"]["disks"]["PhysicalDrive0"]
    assert "max_temp" not in d, "門檻值不得被當成最高溫"


def test_agent_persists_observed_max_and_only_writes_on_increase():
    """快照每 5 秒重建；每次都寫檔是一天一萬七千次不必要的磁碟寫入，
    違反「不得拖慢 host」。只有最高溫真的上升時才寫。"""
    fn = next(n for n in ast.walk(ast.parse(AGENT_SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "observed_max_temp")
    body = ast.unparse(fn)
    assert "MAXTEMP_FILE" in body and "os.replace" in body, "必須原子寫入並保存"
    assert "current <= prev" in body, "只有上升時才可以寫檔"
    assert "0 < current < 150" in body, "必須擋掉不合理的溫度值"


# --- 逐碟狀態標記（LibreNMS 的 (OK) / (FAIL) 顯示）--------------------------

def test_health_pass_drives_the_ok_fail_badge():
    """`includes/html/pages/device/apps/smart.inc.php`：

        $healthStatus = match ($diskData['health_pass'] ?? null) {
            1 => ' (OK)', 0 => ' (FAIL)', default => '',
        };

    沒送這個鍵，磁碟清單就只是一個光禿禿的名稱，看不出健康與否。
    """
    d = [{"name": "PhysicalDrive0", "health": {"health_pass": True, "temp_c": 33}}]
    assert smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]["health_pass"] == 1
    d[0]["health"]["health_pass"] = False
    assert smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]["health_pass"] == 0


def test_health_pass_omitted_when_disk_did_not_answer():
    """USB 橋接器等不轉送 SMART 命令。此時不輸出，讓 LibreNMS 的 `?? null`
    分支什麼都不顯示，比顯示一個猜出來的 (OK) 誠實得多。"""
    d = [{"name": "PhysicalDrive0", "health": {"temp_c": 33}}]
    assert "health_pass" not in smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]


def test_health_pass_is_not_derived_from_attributes():
    """重新配置磁區為 0 不代表健康，韌體可能因為別的屬性跌破門檻而已在
    預測故障。真正的判斷是韌體做的，不可從屬性反推。"""
    d = [{"name": "PhysicalDrive0",
          "health": {"smart": {"reallocated_sectors": {"value": 100, "worst": 100, "raw": 0}},
                     "temp_c": 33}}]
    e = smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]
    assert e["5"] == 0, "屬性本身仍要輸出"
    assert "health_pass" not in e, "不得由屬性推導整體健康"


def test_nvme_critical_warning_maps_to_health_pass():
    """NVMe 沒有 ATA 的 RETURN STATUS，critical warning 位元圖是等價來源。"""
    for cw, expected in ((0, 1), (1, 0), (4, 0)):
        d = [{"name": "PhysicalDrive0", "health": {"critical_warning": cw, "temp_c": 41}}]
        assert smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]["health_pass"] == expected


def test_over_temp_flag_is_per_disk():
    d = [{"name": "PhysicalDrive0", "health": {"health_pass": True, "temp_c": 85}}]
    e = smartjson.build_smart_json(d, over_temp_c=70)["data"]["disks"]["PhysicalDrive0"]
    assert e["over_temp"] == 1


def test_dev_error_is_always_present():
    """未送時 LibreNMS 顯示空白；明確送 0 才代表「輪詢正常」。"""
    d = [{"name": "PhysicalDrive0", "health": {"temp_c": 33}}]
    assert smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]["dev_error"] == 0


def test_identification_fields_help_locate_the_physical_disk():
    """現場要換碟時，型號與序號才是找得到的依據。"""
    d = [{"name": "PhysicalDrive0", "health": {"temp_c": 33},
          "model": "SAMSUNG SSD PM871b", "serial": "S3U0NE0K200798", "vendor": "Samsung"}]
    e = smartjson.build_smart_json(d)["data"]["disks"]["PhysicalDrive0"]
    assert e["product"] == "SAMSUNG SSD PM871b"
    assert e["serial"] == "S3U0NE0K200798"
    assert e["disk"] == "PhysicalDrive0"


def test_agent_reads_authoritative_smart_status():
    """整體健康必須來自 ATA SMART RETURN STATUS (0xDA)，不是屬性推導。"""
    dh = (DEPLOY / "diskhealth.py").read_text(encoding="utf-8")
    assert "SMART_RETURN_STATUS = 0xDA" in dh
    assert "SMART_SEND_DRIVE_COMMAND = 0x0007C084" in dh
    assert "smart_overall_health" in dh
    # ATA 規範的回傳魔術值
    assert "0x4F" in dh and "0xC2" in dh, "門檻未超過的判定值"
    assert "0xF4" in dh and "0x2C" in dh, "門檻已超過的判定值"


# --- jtDiskHealthTable（私有 state OID）--------------------------------------

def test_disk_health_state_table_exists():
    """LibreNMS 的裝置概觀頁有 sensors 區塊但**沒有** applications 區塊
    （`resources/views/components/device/overview/` 沒有 applications 元件），
    所以 SMART 的 (OK)/(FAIL) 只出現在 Apps 分頁。

    要讓健康狀態顯示在概觀頁需要 state 類別感測器，而那需要 LibreNMS 端的
    `os_discovery/windows.yaml`，本專案不修改 LibreNMS 伺服器，因此那條路
    不採用。這張表仍然提供，讓使用者能以自訂警報或其他工具取用單一健康值。
    """
    for node in ast.parse(AGENT_SRC).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "JTDISK":
            return
    pytest.fail("找不到 JTDISK 定義")


def test_disk_health_states_are_conservative():
    """`unknown` 必須是獨立狀態，不可把「問不到」預設成健康，
    USB 橋接器不轉送 SMART 命令時，一個假的綠燈比沒有燈更危險。"""
    for name in ("DISK_STATE_OK", "DISK_STATE_WARNING",
                 "DISK_STATE_CRITICAL", "DISK_STATE_UNKNOWN"):
        assert name in AGENT_SRC, f"缺少 {name}"
    i = AGENT_SRC.find("jtDiskHealthTable: per-disk health")
    block = AGENT_SRC[i:i + 2200]
    assert "DISK_STATE_UNKNOWN" in block, "問不到時必須標為 unknown"
    assert "(5, 197, 198)" in block, "重新配置 / 待處理 / 無法修正磁區應降級為 warning"
