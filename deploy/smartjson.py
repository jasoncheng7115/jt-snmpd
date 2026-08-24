# -*- coding: utf-8 -*-
"""把磁碟健康資料組成 LibreNMS `smart` 應用程式期望的 JSON。

**為什麼走 NET-SNMP-EXTEND-MIB 而不是 entPhySensorTable**

第一版把 NVMe 耐用度與可用備援空間送成 `entPhySensorType = other(1)`，
在 LibreNMS 上**完全看不到**。原因在 `includes/discovery/sensors/entity-sensor.inc.php`：

    $entitysensor = ['voltsDC'=>'voltage', 'voltsAC'=>'voltage', 'amperes'=>'current',
                     'watts'=>'power', 'hertz'=>'freq', 'percentRH'=>'humidity',
                     'rpm'=>'fanspeed', 'celsius'=>'temperature', 'dBm'=>'dbm'];
    ...
    if (isset($entitysensor[$entry['entPhySensorType']]) && ...)

`other` 不在對照表裡，整筆直接跳過。只有溫度（`celsius`）活了下來——
所以現場只看得到溫度，看不到任何 SMART 指標。entPhySensorTable 對
「計數型」資料是一條死路，不是我們送錯值，是這張表沒有對應的語意。

LibreNMS 讀 SMART 的正規路徑是 `json_app_get()`：

    snmp_get($device, 'nsExtendOutputFull."smart"', '-Oqv', 'NET-SNMP-EXTEND-MIB')

**完全走 SNMP**，不需要在被監控端安裝 LibreNMS agent、不需要 smartctl、
不需要任何外部腳本。我們本來就用 ctypes 直接讀到了 SMART 屬性，
只要把它序列化成同樣的 JSON 放進 `nsExtendOutputFull` 就成立。

**為什麼一定要壓縮**

回應上限 1400 位元組（不分片）。未壓縮的 SMART JSON 光一顆磁碟就接近上限，
兩顆必爆。LibreNMS 的 `json_app_get()` 支援 base64(gzip(json))：

    if (preg_match('/^[A-Za-z0-9\\/\\+\\n]+\\=*\\n*$/', $output)
        && ! preg_match('/^[0-9]+\\n/', $output)) {
        $output = gzdecode(base64_decode($output));
    }

JSON 重複性高，實測壓縮後只剩三成左右。
"""

from __future__ import annotations

import base64
import gzip
import json

# LibreNMS 的 smart 應用程式會讀的 SMART 屬性 ID（見其 RRD 定義）。
# 全部都要出現在 JSON 裡：PHP 端直接 $disk['5'] 取值，缺鍵會產生警告，
# 而 null 會被 is_numeric() 判為非數值 → 該欄位存成 U（未知），語意正確。
LIBRENMS_SMART_IDS = ("5", "9", "10", "173", "177", "183", "184", "187", "188",
                      "190", "194", "196", "197", "198", "199", "231", "232", "233")

# 自我測試記錄的統計。我們尚未讀取 SMART self-test log（SMART_READ_LOG 0x06），
# 因此一律 null——填 0 會讓 LibreNMS 顯示「測試全部通過」，那是捏造。
SELFTEST_KEYS = ("completed", "interrupted", "read_failure", "unknown_failure",
                 "extended", "short", "conveyance", "selective")

# NVMe 沒有 ATA 的屬性表。只映射語意上確定等價的欄位，其餘留 null。
# 寧可少報，不可對不上——一個對錯的 ID 會讓現場照著錯誤指標做決策。
NVME_TO_SMART_ID = {
    "power_on_hours": "9",      # Power_On_Hours
    "temp_c": "194",            # Temperature_Celsius
    "media_errors": "187",      # Reported_Uncorrect
    "avail_spare_pct": "232",   # Available_Reserved_Space
}

# ATA：diskhealth 以名稱記錄屬性，這裡換回 LibreNMS 要的 ID。
ATA_NAME_TO_SMART_ID = {
    "reallocated_sectors": "5",
    "power_on_hours": "9",
    "wear_leveling_count": "177",
    "airflow_temperature_c": "190",
    "temperature_c": "194",
    "pending_sectors": "197",
    "uncorrectable_sectors": "198",
    "ssd_life_left_pct": "231",
    "available_reserved_space_pct": "232",
    "media_wearout_indicator": "233",
}


def _blank_disk() -> dict:
    d: dict = {i: None for i in LIBRENMS_SMART_IDS}
    d.update({k: None for k in SELFTEST_KEYS})
    return d


def build_disk_entry(health: dict, max_temp: int | None = None) -> dict:
    """把一顆磁碟的 probe() 結果轉成 LibreNMS 的屬性字典。

    未知的一律 None（→ JSON null → RRD 的 U）。絕不以 0 代替「沒量到」：
    reallocated sectors 填 0 的意思是「這顆磁碟很健康」，
    而真相是「我們沒讀到這個屬性」。
    """
    out = _blank_disk()
    if not health:
        return out

    # 優先用 ATA 屬性表（資訊最完整）
    for name, attr in (health.get("smart") or {}).items():
        sid = ATA_NAME_TO_SMART_ID.get(name)
        if sid and isinstance(attr, dict) and isinstance(attr.get("raw"), int):
            out[sid] = attr["raw"]

    # 由 ID 直接取得的屬性（涵蓋沒有名稱對照的 ID，例如 10/183/184/188/196/199）
    for aid, raw in (health.get("smart_by_id") or {}).items():
        sid = str(int(aid))
        if sid in out and out[sid] is None and isinstance(raw, int):
            out[sid] = raw

    # NVMe 欄位補上 ATA 表沒有的部分
    for key, sid in NVME_TO_SMART_ID.items():
        if out.get(sid) is None and isinstance(health.get(key), int):
            out[sid] = health[key]

    # 溫度可能來自 StorageProperty（既非 ATA 也非 NVMe 屬性表）
    if out["194"] is None and isinstance(health.get("temp_c"), int):
        out["194"] = health["temp_c"]

    # LibreNMS 的 smart 應用程式有一張「Max Temp(C)」圖，來源是 `max_temp` 鍵
    # （`if (isset($disk['max_temp']))`）。不提供的話那張面板仍會被渲染，
    # 但圖是 404 破圖——每個客戶都會看到。
    #
    # Windows 的儲存 API 給的是**門檻值**（warning / critical），不是
    # 「這顆碟這輩子到過的最高溫」，拿門檻值去填是標錯標籤。
    # 因此改用我們自己實際觀測到的最高溫（跨重啟持久化），
    # 語意是「jt-snmpd 安裝以來觀測到的最高溫」，這是真的量到的數字。
    if isinstance(max_temp, int):
        out["max_temp"] = max_temp

    return out


def _disk_status_fields(health: dict, over_temp: bool) -> dict:
    """LibreNMS 的 smart 應用程式頁以這幾個逐碟鍵決定顯示什麼狀態標記
    （`includes/html/pages/device/apps/smart.inc.php`）::

        health_pass  1 → " (OK)"          0 → " (FAIL)"
        over_temp    1 → " (Overheating)"
        dev_error    1 → " (Polling Error)"

    三者都是 `?? null` 分支——**沒送就什麼都不顯示**。第一版沒送，
    所以磁碟清單只有一個光禿禿的 `PhysicalDrive0`，看不出健康與否。

    `health_pass` 只在磁碟**自己回答了**的時候才輸出（ATA SMART RETURN
    STATUS 或 NVMe critical warning）。從屬性推導是不對的：重新配置磁區為 0
    不代表健康，韌體可能因為別的屬性跌破門檻而已在預測故障；反過來說少量
    重新配置磁區在某些型號上完全正常。真正的判斷是韌體做的，我們去問它。
    """
    out: dict = {"dev_error": 0}
    if isinstance(health.get("health_pass"), bool):
        out["health_pass"] = 1 if health["health_pass"] else 0
    elif "critical_warning" in health:
        # NVMe：critical warning 位元圖，任一位元為 1 代表控制器已示警
        cw = health["critical_warning"]
        if isinstance(cw, int):
            out["health_pass"] = 1 if cw == 0 else 0
    out["over_temp"] = 1 if over_temp else 0
    return out


def build_smart_json(disks: list[dict], *, over_temp_c: int = 70) -> dict:
    """組出 LibreNMS JSON 應用程式的標準外框。

    `disks` 每項需含 `name`（RRD 檔名的一部分，需穩定且可當檔名）
    與 `health`（`diskhealth.probe()` 的結果）；`max_temp` 為選填的
    「觀測到的最高溫」。
    """
    entries: dict[str, dict] = {}
    over_temp: list[str] = []
    unhealthy: list[str] = []

    for d in disks:
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        # 名稱會成為 RRD 檔名的一部分，必須是安全字元
        name = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)[:64]
        health = d.get("health") or {}
        entry = build_disk_entry(health, d.get("max_temp"))

        t = entry.get("194")
        hot = isinstance(t, int) and t >= over_temp_c
        if hot:
            over_temp.append(name)
        entry.update(_disk_status_fields(health, hot))

        # 詳細資料表（smart.inc.php 的 $diskFields）——現場要換哪一顆碟時
        # 需要型號與序號才找得到。這是客戶自己機器的資產資訊，
        # 停留在客戶自己的監控系統內。
        for key, src in (("disk", "name"), ("serial", "serial"),
                         ("vendor", "vendor"), ("product", "model")):
            v = d.get(src) if src == "name" else health.get(src) or d.get(src)
            if isinstance(v, str) and v.strip():
                entry[key] = v.strip()[:64]
        entries[name] = entry
        # 重新配置磁區或待處理磁區不為零 = 這顆磁碟正在壞
        for sid in ("5", "197", "198"):
            v = entry.get(sid)
            if isinstance(v, int) and v > 0 and name not in unhealthy:
                unhealthy.append(name)

    return {
        "version": 1,
        "error": 0,
        "errorString": "",
        "data": {
            "disks": entries,
            "exit_nonzero": 0,
            "unhealthy": len(unhealthy),
            "dev_error": 0,
            "disks_with_failed_tests": [],
            "disks_with_failed_health": unhealthy,
            "disks_with_over_temp": over_temp,
            "disks_with_dev_error": [],
        },
    }


def encode_extend_output(payload: dict) -> bytes:
    """序列化 → gzip → base64，成為 `nsExtendOutputFull` 的值。

    `mtime=0` 是刻意的：gzip 標頭預設塞入當前時間，會讓相同資料每次
    產生不同位元組，快照因此每 5 秒無謂地變動，測試也無法比對。
    """
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, compresslevel=9, mtime=0))


def looks_like_librenms_base64(value: bytes) -> bool:
    """驗證輸出符合 LibreNMS 的判別條件，否則它會當成純文字處理。

    對應 `json_app_get()`：

        preg_match('/^[A-Za-z0-9\\/\\+\\n]+\\=*\\n*$/', $output)
        && ! preg_match('/^[0-9]+\\n/', $output)
    """
    if not value:
        return False
    body = value.rstrip(b"=")
    if not body:
        return False
    allowed = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/+\n")
    if not set(body) <= allowed:
        return False
    # 不可看起來像舊版格式（開頭是數字後接換行）
    head = value.split(b"\n", 1)[0]
    return not head.isdigit()
