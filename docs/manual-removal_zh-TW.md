---
layout: default
title: 手動移除（繁體中文）
description: What to do when the installer cannot install, upgrade or uninstall
---

[← 回到說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/manual-removal.html) | **繁體中文**

# 安裝程式走不完的時候

MSI 本來就該自己處理安裝、升級與解除安裝，在生命週期測試裡它也做到了。
這一頁是為了它做不到的那些時候：安裝倒回、解除安裝回報成功但服務還在跑、
「應用程式與功能」裡已經看不到這個產品但 UDP/161 還在回應。

以下每一步都是安裝程式某個動作的手動版本。請在**以系統管理員身分執行的
PowerShell** 下操作。

> 動手刪任何東西之前，請先看 [§5](#5-還原內建-snmp-service)。
> 「內建 Windows SNMP Service 原本是什麼狀態」這份記錄就放在資料目錄裡，
> 先刪目錄等於把唯一一份記錄丟掉。

---

## 1. 先看清楚現在到底有什麼

```powershell
# CLI
Get-Service jt-snmpd -ErrorAction SilentlyContinue
Get-CimInstance Win32_Service -Filter "Name='jt-snmpd'" | Select-Object Name, State, StartMode, PathName
Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' |
  Where-Object DisplayName -like '*JT SNMP*' |
  Select-Object DisplayName, DisplayVersion, PSChildName
Get-NetFirewallRule -DisplayName 'jt-snmpd*' -ErrorAction SilentlyContinue |
  Select-Object DisplayName, Enabled
Test-Path 'C:\Program Files\jt-snmpd'
Test-Path 'C:\ProgramData\jt-snmpd'
Get-NetUDPEndpoint -LocalPort 161 -ErrorAction SilentlyContinue
```

第三道指令輸出的 `PSChildName` 就是 **ProductCode**，也就是 `msiexec` 要用的
那組大括號 GUID。它每一版都會變。

## 2. 先試正規的解除安裝

```powershell
# CLI
msiexec /x '{把 ProductCode 填在這裡}' /qn /l*v "$env:TEMP\jt-uninstall.log"
```

失敗的話，`$env:TEMP\jt-uninstall.log` 會指出是哪一個動作失敗。
在檔案裡搜 `Return value 3`，緊接在它上面的幾行就是原因。
同時請看 `C:\ProgramData\jt-snmpd\logs\`，設定步驟會把自己的記錄寫在那裡，
通常會直接寫出它做不到什麼。

只有在正規解除安裝真的走不完時，才需要往下做。

## 3. 停止並移除服務

```powershell
# CLI
Stop-Service jt-snmpd -Force -ErrorAction SilentlyContinue

# 停不下來的話，直接結束處理程序
$svc = Get-CimInstance Win32_Service -Filter "Name='jt-snmpd'"
if ($svc.ProcessId) { Stop-Process -Id $svc.ProcessId -Force }

sc.exe delete jt-snmpd
```

`sc.exe delete` 只是把服務標記為待刪除。如果項目還在，代表還有東西持有它的
控制代碼，通常是開著的「服務」主控台或事件檢視器。關掉它們，或重新啟動；
刪除會在下次開機時完成。

## 4. 移除防火牆規則

```powershell
# CLI
Get-NetFirewallRule -DisplayName 'jt-snmpd*' | Remove-NetFirewallRule
```

安裝時會建立兩條：`jt-snmpd (UDP 161)` 與 `jt-snmpd (ICMPv4)`。
服務移除之後留著它們不構成資安問題，而且下次安裝會重建，
但在這裡一併清掉比較乾淨。

## 5. 還原內建 SNMP Service

**這一步最值得做對。** 安裝程式是把內建 Windows SNMP Service **停用**而不是移除，
並記錄它原本的狀態，好在解除安裝時放回去。那份記錄在：

```
C:\ProgramData\jt-snmpd\state\ms-snmp-restore.json
```

刪資料目錄之前先讀它：

```powershell
# CLI
Get-Content 'C:\ProgramData\jt-snmpd\state\ms-snmp-restore.json' -Raw | ConvertFrom-Json
```

裡面記著我們動它之前的啟動類型與執行狀態。照著放回去：

```powershell
# CLI，把值換成記錄裡的內容
Set-Service -Name SNMP -StartupType Automatic
Start-Service SNMP
```

如果記錄不見了或讀不出來，就得從貴單位自己的資料判斷這台機器在裝 jt-snmpd 之前
到底有沒有在跑內建 SNMP Service。**不要用猜的。** 維持停用是比較安全的那個錯：
那台機器會沒有監控，而沒有監控是看得到的。反過來把它打開，
可能是把一個現場刻意關掉的服務又開回去。

## 6. 刪除檔案

```powershell
# CLI
Remove-Item 'C:\Program Files\jt-snmpd' -Recurse -Force
Remove-Item 'C:\ProgramData\jt-snmpd' -Recurse -Force
# 從 0.9.5 以前升級上來的機器，如果搬移中斷過，改名前的目錄可能還在：
Remove-Item 'C:\ProgramData\JT-SNMP' -Recurse -Force -ErrorAction SilentlyContinue
```

`C:\ProgramData\jt-snmpd` 裡有設定檔、記錄檔、SNMP engine 身分，以及介面索引映射。
正常解除安裝**刻意**保留這個目錄：管理員常以「移除再重裝」來排除問題，
而把索引映射一起丟掉會讓 LibreNMS 重新探索每一個 port，
舊的歷史 RRD 全部失去對應。確定要永久移除這個 agent 時再刪。

檔案刪不掉代表還有東西在跑，請回到 §3。

## 7. 清掉卡住的 Windows Installer 註冊

偶爾會遇到檔案與服務都不見了，但 Windows 仍然認為產品還裝著，
新的 MSI 會以「此產品的其他版本已安裝」拒絕安裝。

```powershell
# CLI，先問 Windows Installer 它認為註冊了什麼
Get-Package -ProviderName msi | Where-Object Name -like '*JT SNMP*'
Get-Package -ProviderName msi -Name 'jt-snmpd' | Uninstall-Package
```

這樣還是不行的話，可以直接從登錄檔移除註冊。
**這是最後手段**，它繞過了 Windows Installer 自己的帳，
而且必須在 §3 到 §6 都做完之後才做：

```powershell
# CLI，先看清楚再刪
Remove-Item "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{PRODUCTCODE}" -Recurse
```

刪之前請先用 `reg export` 備份那個機碼。做完之後，
請先確認全新安裝可以成功，再認定這台機器已經處理好。

## 8. 確認機器已經乾淨

```powershell
# CLI，以下每一項都應該是空的或 False
Get-Service jt-snmpd -ErrorAction SilentlyContinue
Get-NetFirewallRule -DisplayName 'jt-snmpd*' -ErrorAction SilentlyContinue
Test-Path 'C:\Program Files\jt-snmpd'
Test-Path 'C:\ProgramData\jt-snmpd'
Get-NetUDPEndpoint -LocalPort 161 -ErrorAction SilentlyContinue
Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' |
  Where-Object DisplayName -like '*JT SNMP*'
```

如果 `Get-NetUDPEndpoint -LocalPort 161` 還是有東西，
先確認是哪個處理程序佔著，不要直接假設那是我們：

```powershell
# CLI
Get-NetUDPEndpoint -LocalPort 161 | ForEach-Object {
  Get-Process -Id $_.OwningProcess | Select-Object Id, ProcessName, Path
}
```

有可能是你在 §5 還原回去的內建 SNMP Service，那樣才是正確的結果；
也可能是本來就在那裡的第三方 agent。

---

## 如果失敗的是安裝

| 現象 | 可能原因 | 怎麼處理 |
|---|---|---|
| MSI 以 1603 結束並倒回 | 設定步驟失敗。因為是倒回，機器會回到原本的狀態 | 先看 `C:\ProgramData\jt-snmpd\logs\`，再看 MSI 的詳細記錄 |
| 出現「無法判定 community」 | 用無訊息安裝但沒給 `COMMUNITY`，而內建服務也沒有可沿用的 | 在命令列補上 `COMMUNITY=`，或改用圖形介面安裝 |
| 安裝中止，說 UDP/161 已被佔用 | 有第三方 agent 佔著那個埠 | 這是刻意的：我們不會去停別人的 agent。請自行決定該由誰佔用 161 |
| 服務啟動後又停止 | 健康檢查發現 agent 沒有在 loopback 上回應 | `C:\ProgramData\jt-snmpd\logs\` 會寫原因，通常是設定檔內容有問題 |
| Windows 說產品已安裝 | 前一次的註冊卡住了 | 見 §7 |

要診斷安裝問題，請收集詳細記錄：

```powershell
# CLI
msiexec /i jt-snmpd-0.9.6-x64.msi /qn /l*v "$env:TEMP\jt-install.log" `
  MANAGEMENTNETWORKS=10.0.0.0/24 COMMUNITY=你的community
```

---

## 請回報

如果安裝程式做不到這一頁裡某件必須手動完成的事，那是一個值得回報的缺陷，
不只是一台待修的機器。請附上 MSI 的詳細記錄與 `C:\ProgramData\jt-snmpd\logs\`
的內容開 issue，記得先把 community 字串移除。

<https://github.com/jasoncheng7115/jt-snmpd/issues>

---

## 相關文件

- [說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/)
- [程式碼簽章](https://jasoncheng7115.github.io/jt-snmpd/code-signing_zh-TW.html)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)
