---
layout: default
title: SNMPv3（繁體中文）
description: Authenticated, encrypted polling - how to provision it and what it costs
---

[← 回到說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/snmpv3.html) | **繁體中文**

# SNMPv3

> **狀態:已驗證。** 先用 net-snmp 對打驗過(那正是 LibreNMS 用來輪詢的實作),
> 再在四台實機上驗:Windows 10、Windows 11、Server 2016(網域控制站)、Server 2022。
> 正式 LibreNMS 上納管的三台都從 v2c 切換成 v3,**沒有任何一台被重新探索**,
> 連接埠、儲存與感測器的既有項目全部保留。

SNMPv2c 的 community 字串是明文傳送的,而且完全不做認證。看得到封包的人就讀得到內容,
猜得到 community 的人就能查詢主機。SNMPv3 把這兩件事都補上:每個請求都用 HMAC 認證,
若採 `authPriv`,內容還會加密。

jt-snmpd 的 v3 是**與 v2c 並存**,不是取代它,所以升級不會讓既有部署從監控上消失。

---

## 1. 建立帳號

密碼是**互動輸入**的,不接受寫在命令列參數上。參數在指令執行期間會出現在處理程序清單裡,
機器上任何使用者都看得到,而且會留在主控台歷程記錄。同樣的理由,安裝程式也不接受把金鑰
當成 MSI 屬性傳入 —— 那會進 msiexec 記錄檔,以及事件識別碼 1033 與 11707。

在被監控主機上,用具有系統管理員權限的命令提示字元:

```
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user add librenms
```

它會分別詢問認證密碼與加密密碼,各需輸入兩次。兩者都至少 12 個字元,而且**必須不同**
—— 一次外洩不該等於兩次。

```
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user list
"C:\Program Files\jt-snmpd\jt-snmpd.exe" user remove librenms
```

改完之後重新啟動服務才會生效:

```
sc stop jt-snmpd && sc start jt-snmpd
```

### 演算法

預設是**認證 SHA-256、加密 AES-128**,這組合是 net-snmp 到處都談得通的。
要改用 `--auth` 與 `--priv`:

| 參數 | 可用值 |
|---|---|
| `--auth` | `SHA-224`、`SHA-256`(預設)、`SHA-384`、`SHA-512` |
| `--priv` | `AES-128`(預設)、`AES-192`、`AES-256` |

**MD5、SHA-1、DES、3DES 一律拒絕。** pysnmp 這四種都有實作,所以指定它們原本是「會動」的,
而會動正是最糟的結果:操作人員會以為流量受到保護,實際上並沒有。

**AES-192 與 AES-256 會發出警告。** 那是互通性風險,不是密碼本身的弱點。
這兩者從未被 SNMPv3 標準化,存在兩套互不相容的金鑰延伸方式(Blumenthal 草案與 Reeder 的版本),
而 Debian 與 Ubuntu 編譯 net-snmp 時並未啟用 pysnmp 使用的那一套。
換句話說,這樣設定的代理服務,很可能**連不上當初要接的那台 LibreNMS**。
要互通就選 AES-128。

---

## 2. 在 LibreNMS 加入裝置

Devices → Add Device,然後:

| 欄位 | 填入 |
|---|---|
| SNMP Version | v3 |
| Auth Level | authPriv |
| Auth User Name | `user add` 時取的名字 |
| Auth Password | 認證密碼 |
| Auth Algorithm | SHA-256 |
| Crypto Password | 加密密碼 |
| Crypto Algorithm | AES |

加進去之前,可以先在 LibreNMS 主機上確認:

```
snmpwalk -v3 -l authPriv -u librenms \
  -a SHA-256 -A '<認證密碼>' \
  -x AES     -X '<加密密碼>' \
  <主機> 1.3.6.1.2.1.1
```

---

## 3. 關掉 v2c

等到所有監控系統都改用 v3 之後,在
`C:\ProgramData\jt-snmpd\config.json` 設定 `v3_only`,再重新啟動服務:

```json
{ "v3_only": true }
```

代理服務就完全不會註冊 v2c。如果設了 `v3_only` 卻**沒有任何 v3 帳號能載入,服務會拒絕啟動**。
這是刻意的:讓它開著卻沒有任何人進得來,從 Windows 看起來一切正常,實際上不回應任何人,
操作人員會去查網路,而問題其實在一個設定檔裡。

---

## 4. 金鑰放在哪裡,以及它保護了什麼

`%ProgramData%\jt-snmpd\secrets\usm.dat`,以 **DPAPI machine scope** 加密。
該目錄的存取權限只給 SYSTEM 與 Administrators。

存進去的是 **localized key,不是密碼**。localized key 是由密碼**加上這台機器的 engineID**
推導出來的,所以它只能認證到這一台代理服務。密碼只在建立帳號當下使用一次,不會被寫下來。

這樣做的重點在於整批機器。如果存的是密碼,那麼讀到任何一台的秘密檔案
—— 一份被偷走的備份或一個權限沒設好的共用資料夾就夠了 —— 等於拿到**每一台**共用該密碼的機器。
而幾百台由同一個原則布建正是常態,所以那才是實際的損失規模。改存 localized key 之後,
同樣的竊取只值一台機器。

**DPAPI 沒有做到的事。** 這個加密區塊只有寫入它的那台機器解得開,這是「複製走也沒用」的來源。
但它擋不住、也不可能擋住**該機器上的系統管理員**:服務是以 LocalSystem 無人看管地執行的,
必須能在沒有任何人在場的情況下讀出金鑰,所以凡是它能自動解開的保護,系統管理員也能解開。
誠實的說法是:它保護的是靜態存放與被複製帶走的檔案,不是這台機器自己的系統管理員。

---

## 5. 複製出來的虛擬機,以及唯一一件一定會咬到你的事

engineID 必須唯一。jt-snmpd 由 Windows 的 MachineGuid 推導它,並且會記下當初是用哪個
MachineGuid 推導的。

如果 MachineGuid 變了 —— 機器從樣板複製出來,或重新部署映像 —— 代理服務會產生新的 engineID、
把 snmpEngineBoots 歸零,並寫進記錄。它非這麼做不可:五十台複製機用同一個 engineID,
會讓監控系統把它們當成同一個引擎而只保留一組 boots/time,
於是整批機器的認證開始**間歇性失敗,而且記錄裡完全看不出原因**。

**localized key 撐不過這件事。** 它綁在當初那個 engineID 上,無法轉換,
而它來自的密碼是刻意沒有保留的。所以複製機上已存的帳號等於報廢。
代理服務會偵測到並且把話講明白,而不是留下一個沒人看得懂的認證失敗:

```
[!] SNMPv3: the SNMPv3 keys were localized against engineID 8001869f04... but
    this engine is 8001869f04... A localized key is bound to the engineID it was
    made for and cannot be converted, so every SNMPv3 user has to be provisioned
    again. This normally means the machine was cloned from a template or
    reimaged
```

**所以不要把帳號封裝進映像裡。** 開機之後再布建 —— 用群組原則,或用 CLI。
封裝之前還沒建立任何 v3 帳號的樣板,就沒有東西會壞。

---

## 6. SNMPv3 不會改變的事

- **代理服務仍然是唯讀的。** v3 的通訊協定本身帶有 SET;本代理服務在**任何版本下都沒有實作
  SET**。
- **前置閘門仍然排在最前面。** 來源位址允許清單、封包大小上限、速率限制,
  全部發生在任何密碼學運算之前。這個順序是刻意的:v3 讓阻斷服務攻擊變得更便宜,
  因為每個封包都要算一次 HMAC。
- **它不能取代防火牆規則。** 管理網段在安裝時仍然是必填,而且仍然預設拒絕。
