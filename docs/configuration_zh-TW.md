---
layout: default
title: 事後調整設定（繁體中文）
description: Changing the community, networks and SNMPv3 accounts after installation
---

[← 回到說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/configuration.html) | **繁體中文**

# 事後調整設定

安裝時問過的每一個值，裝完之後都改得動，不必重裝。

**流程一律是:改檔案 → 重新啟動服務。** 設定只在**服務啟動時**讀一次，這是刻意的:
每次建立快照都重讀的話，操作人員編輯到一半的檔案會被讀進去。

```
sc stop jt-snmpd && sc start jt-snmpd
```

---

## 1. SNMPv2c:community 與管理網段

檔案在:

```
C:\ProgramData\jt-snmpd\config.json
```

該目錄的存取權限只給 SYSTEM 與 Administrators，所以要用**具有系統管理員權限**的編輯器開。

```json
{
  "schema_version": 1,
  "community": "your-community",
  "allowed_networks": ["192.168.1.0/24"],
  "port": 161,
  "enable_arp_table": false,
  "rate_pps": 50,
  "rate_burst": 300,
  "v3_only": false
}
```

| 鍵 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `community` | 字串 | 安裝時填的 | v2c 的 community。空字串會被忽略，等於沿用內建預設（也就是空的，代理服務會拒絕服務 v2c）|
| `allowed_networks` | 字串陣列 | 安裝時填的 | **前置閘門的來源位址允許清單**。不在清單內的封包在任何解析之前就被丟棄。空陣列等於只允許回路 |
| `port` | 整數 | 161 | 1 到 65535 |
| `enable_arp_table` | 布林 | `false` | 開啟 `ipNetToPhysicalTable`。**那是一份現成的橫向移動目標清單**，所以預設關閉 |
| `rate_pps` | 整數 | 50 | 每個來源位址每秒允許的封包數 |
| `rate_burst` | 整數 | 300 | 突發容許量。一次完整 walk 大約是「varbind 數 ÷ 25」個請求，這個值要大於它，否則正常輪詢會被自己的限速擋掉 |
| `v3_only` | 布林 | `false` | 設 `true` 就完全不註冊 v2c。見下方第 3 節 |

**改完之後怎麼確認真的生效**:重啟服務，然後看記錄檔第一行:

```
C:\ProgramData\jt-snmpd\logs\jt-snmpd.log

2026-08-27 14:18:43 config loaded from C:\ProgramData\jt-snmpd\config.json:
                    community, allowed_networks(1), port, enable_arp_table, v3_only
```

**那一行會列出實際被採用的鍵。** 沒有列到的鍵就是沒有生效 —— 型別不對、值超出範圍、
或是鍵名打錯，都會讓它被安靜跳過。這一行是唯一能確認的地方。

---

## 2. SNMPv3 帳號

**不在 `config.json` 裡。** 金鑰是加密保存的，用 CLI 管理:

```
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user list
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user add librenms
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user remove librenms
```

密碼是**互動輸入**的，不接受寫在命令列參數上 —— 參數在指令執行期間會出現在處理程序清單裡，
機器上任何使用者都看得到，而且會留在主控台歷程記錄。無人值守的情境可以從標準輸入餵:

```
(echo auth-passphrase& echo priv-passphrase) | "C:\Program Files\jt-snmpd\jt-snmpd.exe" user add librenms
```

演算法用 `--auth` 與 `--priv` 指定，完整清單與取捨見
[SNMPv3](https://jasoncheng7115.github.io/jt-snmpd/snmpv3_zh-TW.html)。

**改完一樣要重新啟動服務。**

---

## 3. 只允許 SNMPv3、完全排除 v2c

```json
{ "v3_only": true }
```

重啟之後代理服務完全不註冊 v2c，記錄會寫:

```
v3_only is set: SNMPv2c is not registered on this agent
```

**設了 `v3_only` 卻沒有任何可用的 v3 帳號時，服務會拒絕啟動**，並在記錄裡說明原因與
兩條出路。那是刻意的:開著卻沒有人進得來，從 Windows 看起來一切正常，
操作人員會去查網路，而問題其實在一個設定檔裡。

切換的建議順序是:**先建好 v3 帳號並確認 LibreNMS 那端接上，再設 `v3_only`。**
反過來做會有一段時間兩種都不通。

---

## 4. 群組原則能覆寫什麼（以及不能覆寫什麼）

```
HKLM\SOFTWARE\Policies\JasonTools\JTSNMPD
```

**這個機碼只控制兩個值:`SysContact` 與 `SysLocation`。** 它們會覆寫從內建 SNMP
移轉過來的值。

| | 由誰決定 |
|---|---|
| `SysContact` / `SysLocation` | 群組原則 > 內建 SNMP 的登錄值 |
| community、管理網段、連接埠、限速、`v3_only` | **只看 `config.json`**，群組原則不會覆寫 |
| SNMPv3 帳號 | 只看 `secrets\usm.dat`，用 CLI 管理 |

要在數百台上統一改 community 或管理網段，目前的作法是**以 `/qn` 重新派送 MSI**
並帶上新的屬性 —— 安裝程式會改寫 `config.json`。升級不會弄丟 `index-map.json`，
所以 LibreNMS 的連接埠與歷史圖表不受影響。

---

## 5. 改壞了會怎樣

**設定檔語法錯誤**:記錄會寫 `config file at ... could not be read`，
代理服務**照常啟動**並使用內建預設值 —— 而內建預設的 community 是空的，
所以實際效果是不再回應 v2c。**這是刻意的**:讓它安靜地用「上次的設定」繼續跑，
會讓操作人員以為改生效了。

**檔案不見**:記錄寫 `no config file at ...; using built-in defaults`，同上。

**值的型別不對**:那一個鍵被安靜跳過，其餘照常套用。看 `config loaded from ...`
那一行列出的鍵，就知道哪一個沒進去。

**要回到安裝當下的設定**:以 `/qn` 重新執行同一顆 MSI 並帶上原本的屬性即可。

---

## 6. 完整範例

### 只用 SNMPv2c（最常見）

`C:\ProgramData\jt-snmpd\config.json`：

```json
{
  "schema_version": 1,
  "community": "mon-readonly-2026",
  "allowed_networks": ["192.168.1.0/24", "10.20.0.0/16"],
  "port": 161,
  "enable_arp_table": false,
  "rate_pps": 50,
  "rate_burst": 300,
  "v3_only": false
}
```

LibreNMS 端：Devices → Add Device，SNMP Version 選 **v2c**，Community 填
`mon-readonly-2026`。

從 LibreNMS 主機先確認：

```
snmpwalk -v2c -c mon-readonly-2026 <主機> 1.3.6.1.2.1.1
```

---

### v2c 與 v3 並存（切換期間）

設定檔跟上面一樣（`v3_only` 維持 `false`），另外在被監控主機上建帳號：

```
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user add librenms
  authentication passphrase: ****************
  confirm:                   ****************
  privacy passphrase:        ****************
  confirm:                   ****************

added librenms (SHA-256 + AES-128).
Only the localized keys were stored; the passphrases were not.

sc stop jt-snmpd && sc start jt-snmpd
```

兩種都會回應。這是**升級與切換期間該待的狀態** —— 舊的監控設定還在運作，
新的可以逐台驗證。

先在 LibreNMS 主機上確認 v3 通得了，再去改裝置設定：

```
snmpwalk -v3 -l authPriv -u librenms \
  -a SHA-256 -A '<認證密碼>' \
  -x AES     -X '<加密密碼>' \
  <主機> 1.3.6.1.2.1.1
```

LibreNMS 端改成 v3：裝置頁 → 齒輪 → Edit → SNMP

| 欄位 | 填入 |
|---|---|
| SNMP Version | v3 |
| Auth Level | authPriv |
| Auth User Name | `librenms` |
| Auth Password | 認證密碼 |
| Auth Algorithm | SHA-256 |
| Crypto Password | 加密密碼 |
| Crypto Algorithm | AES |

**切換不會讓 LibreNMS 重新探索**，連接埠、儲存、感測器的既有項目與歷史圖表都保留。

---

### 只用 SNMPv3、完全排除 v2c

**順序很重要:先確認 v3 通了，最後才設 `v3_only`。**

```json
{
  "schema_version": 1,
  "community": "",
  "allowed_networks": ["192.168.1.0/24"],
  "port": 161,
  "enable_arp_table": false,
  "rate_pps": 50,
  "rate_burst": 300,
  "v3_only": true
}
```

重啟之後記錄會寫：

```
v3_only is set: SNMPv2c is not registered on this agent
SNMPv3 user 'librenms' registered (SHA-256 + AES-128)
LISTENING 0.0.0.0:161 varbinds=652
```

驗證：v2c 應該完全不回應，v3 正常。

```
snmpget -v2c -c anything <主機> 1.3.6.1.2.1.1.5.0
  → Timeout: No Response

snmpget -v3 -l authPriv -u librenms -a SHA-256 -A '...' -x AES -X '...' <主機> 1.3.6.1.2.1.1.5.0
  → STRING: "WIN11-PRO-1"
```

**設了 `v3_only` 卻沒有帳號時，服務會拒絕啟動**，記錄會寫：

```
ERROR refusing to start: v3_only is set but no SNMPv3 user could be loaded,
so nothing could authenticate. Provision one with `jt-snmpd.exe user add <name>`,
or clear v3_only in config.json to serve v2c again
```

---

## 相關文件

- [SNMPv3](https://jasoncheng7115.github.io/jt-snmpd/snmpv3_zh-TW.html)
- [命名與路徑](https://jasoncheng7115.github.io/jt-snmpd/naming-and-paths_zh-TW.html)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)
- [手動移除](https://jasoncheng7115.github.io/jt-snmpd/manual-removal_zh-TW.html)
