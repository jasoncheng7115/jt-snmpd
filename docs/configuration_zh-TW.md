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

## 相關文件

- [SNMPv3](https://jasoncheng7115.github.io/jt-snmpd/snmpv3_zh-TW.html)
- [命名與路徑](https://jasoncheng7115.github.io/jt-snmpd/naming-and-paths_zh-TW.html)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)
- [手動移除](https://jasoncheng7115.github.io/jt-snmpd/manual-removal_zh-TW.html)
