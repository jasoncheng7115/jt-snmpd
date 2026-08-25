# jt-snmpd 安裝生命週期完整測試
# 涵蓋：乾淨安裝 → 升級 → 移除（保留資料）→ 重裝 → PURGE 移除
# community 與管理網段不寫死：這支腳本會進公開 repo，而 community 等同密碼。
# 預設值是明顯的佔位字，執行時以參數覆寫：
#   .\lifecycle.ps1 -Community <你的community> -ManagementNetworks 10.0.0.0/24
param(
  [string]$Community = "CHANGEME",
  [string]$ManagementNetworks = "10.0.0.0/24",
  [string]$MsiPath = ""
)

$ErrorActionPreference = 'Continue'
$PASS = 0; $FAIL = 0
function Check { param($name, $cond, $detail="")
  if ($cond) { $script:PASS++; "  [PASS] $name $detail" }
  else { $script:FAIL++; "  [FAIL] $name $detail" } }

$DATA = "C:\ProgramData\jt-snmpd"
$PROG = "C:\Program Files\jt-snmpd"
$IDX  = "$DATA\state\index-map.json"
$MSI  = "C:\jtdev\dist\jt-snmpd-0.2.0-x64.msi"

function Arp { (Get-ItemProperty HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\* -EA SilentlyContinue | Where-Object DisplayName -like "*JT SNMP*") }
function SvcState { $s=Get-Service jt-snmpd -EA SilentlyContinue; if($s){"$($s.Status)"}else{"absent"} }
function Port161 { (Get-NetUDPEndpoint -LocalPort 161 -EA SilentlyContinue | Measure-Object).Count }
# 移除後 161 不會是空的，內建 SNMP 被我們歸還並重新啟動，理當接手回去。
# 該檢查的是「jt-snmpd 不再持有」，不是「沒人在聽」。第一次寫成後者，
# 於是把正確的歸還行為判成失敗。
function Port161Owner {
  $o = @(Get-NetUDPEndpoint -LocalPort 161 -EA SilentlyContinue | ForEach-Object {
    (Get-Process -Id $_.OwningProcess -EA SilentlyContinue).ProcessName }) | Sort-Object -Unique
  if ($o) { $o -join ',' } else { 'none' } }
function FwRules { (Get-NetFirewallRule -DisplayName "jt-snmpd*" -EA SilentlyContinue | Measure-Object).Count }
# 注意：不可用 $args 當參數名，那是 PowerShell 的保留自動變數，
# 會被自身的未具名參數陣列覆蓋，傳入值永遠是空的（實測踩過）。
function MsiRun { param([string[]]$MsiArgs)
  (Start-Process msiexec.exe -ArgumentList $MsiArgs -Wait -PassThru).ExitCode }

"########## 0. 清除既有安裝 ##########"
$a = Arp
if ($a) { MsiRun @("/x", $a.PSChildName, "/qn", "PURGE=1") | Out-Null }
sc.exe stop jt-snmpd 2>&1 | Out-Null; Start-Sleep 2
sc.exe delete jt-snmpd 2>&1 | Out-Null; Start-Sleep 2
Remove-Item $PROG -Recurse -Force -EA SilentlyContinue
Remove-Item $DATA -Recurse -Force -EA SilentlyContinue
# 內建 SNMP 必須先回到「已啟用」，否則接管與歸還兩段都測不到東西。
function MsSnmp { $s=Get-Service SNMP -EA SilentlyContinue
  if($s){"$($s.Status)/$($s.StartType)"}else{"absent"} }
$msExists = (Get-Service SNMP -EA SilentlyContinue) -ne $null
if ($msExists) {
  Set-Service -Name SNMP -StartupType Automatic -EA SilentlyContinue
  Start-Service -Name SNMP -EA SilentlyContinue
  Start-Sleep 3
}
$MS_BEFORE = MsSnmp
"  起始狀態: svc=$(SvcState) 161=$(Port161) fw=$(FwRules) 內建SNMP=$MS_BEFORE"

"########## 1. 乾淨安裝 ##########"
$code = MsiRun @("/i","`"$MSI`"","/qn","MANAGEMENTNETWORKS=$ManagementNetworks","COMMUNITY=$Community")
Check "msiexec 結束碼為 0" ($code -eq 0) "(得到 $code)"
Check "服務存在且執行中" ((SvcState) -eq "Running") "(得到 $(SvcState))"
Check "UDP/161 已綁定" ((Port161) -ge 1)
Check "防火牆規則已建立" ((FwRules) -ge 2) "(UDP + ICMP，得到 $(FwRules))"
Check "程式目錄存在" (Test-Path "$PROG\jt-snmpd.exe")
Check "資料目錄存在" (Test-Path $DATA)
Check "index-map 已產生" (Test-Path $IDX)
Check "出現在加入或移除程式" ((Arp) -ne $null)
Check "版本正確" ((Arp).DisplayVersion -eq "0.2.0") "(得到 $((Arp).DisplayVersion))"
$svc = Get-CimInstance Win32_Service -Filter "Name='jt-snmpd'"
Check "以 LocalSystem 執行" ($svc.StartName -eq "LocalSystem") "(得到 $($svc.StartName))"
Check "啟動類型為自動" ($svc.StartMode -eq "Auto") "(得到 $($svc.StartMode))"
$quoted = $svc.PathName.StartsWith([char]34)
Check "ImagePath 有加引號" $quoted $svc.PathName
$acl = Get-Acl $DATA
Check "資料目錄 ACL 已收緊" (-not ($acl.Access | Where-Object { $_.IdentityReference -match "Users|Everyone|Authenticated" }))
$idxHash1 = (Get-FileHash $IDX -Algorithm SHA256).Hash
if ($msExists) {
  Check "內建 SNMP 已停止並停用" ((MsSnmp) -eq "Stopped/Disabled") "(得到 $(MsSnmp))"
  $rec = Get-Content "$DATA\state\ms-snmp-restore.json" -Raw -Encoding UTF8 | ConvertFrom-Json
  Check "還原記錄保存了原本的啟動類型" ($rec.ms_snmp.original_start_type -eq "Automatic") "(得到 $($rec.ms_snmp.original_start_type))"
  Check "還原記錄標記為由我們停用" ($rec.ms_snmp.disabled_by_us -eq $true)
}

"########## 2. 升級（直接安裝同一版，測冪等）##########"
$code = MsiRun @("/i","`"$MSI`"","/qn","MANAGEMENTNETWORKS=$ManagementNetworks","COMMUNITY=$Community")
Check "重複安裝結束碼為 0（冪等）" ($code -eq 0) "(得到 $code)"
Check "升級後服務仍執行中" ((SvcState) -eq "Running")
Check "升級後仍只有一筆安裝紀錄" ((Arp | Measure-Object).Count -eq 1) "(得到 $((Arp | Measure-Object).Count))"
Check "index-map 未被重建" ((Get-FileHash $IDX -Algorithm SHA256).Hash -eq $idxHash1)

if ($msExists) {
  $rec2 = Get-Content "$DATA\state\ms-snmp-restore.json" -Raw -Encoding UTF8 | ConvertFrom-Json
  # 升級時內建 SNMP 已是 Disabled；若程式重讀當下狀態覆寫還原記錄，
  # 之後的移除就再也不會把它還原回來（實測踩過的 bug）。
  Check "升級未污染還原記錄" ($rec2.ms_snmp.original_start_type -eq "Automatic") "(得到 $($rec2.ms_snmp.original_start_type))"
}

"########## 3. 移除（預設保留資料）##########"
$code = MsiRun @("/x", (Arp).PSChildName, "/qn")
Check "移除結束碼為 0" ($code -eq 0) "(得到 $code)"
Check "服務已刪除" ((SvcState) -eq "absent") "(得到 $(SvcState))"
Check "jt-snmpd 已釋放 UDP/161" ((Port161Owner) -notmatch 'jt-snmpd') "(目前持有者: $(Port161Owner))"
Check "防火牆規則已移除" ((FwRules) -eq 0) "(得到 $(FwRules))"
Check "程式目錄已移除" (-not (Test-Path "$PROG\jt-snmpd.exe"))
Check "已離開加入或移除程式" ((Arp) -eq $null)
Check "資料目錄保留（刻意）" (Test-Path $DATA)
Check "index-map 保留（避免 RRD 失去對應）" (Test-Path $IDX)
Check "不需重新開機" ($true)

if ($msExists) {
  Start-Sleep 3
  Check "內建 SNMP 已歸還為 Automatic" ((Get-Service SNMP).StartType -eq "Automatic") "(得到 $((Get-Service SNMP).StartType))"
  Check "內建 SNMP 已重新啟動" ((Get-Service SNMP).Status -eq "Running") "(得到 $((Get-Service SNMP).Status))"
  # 歸還之後才輪到我們讓位，重裝前先讓開 UDP/161
  Stop-Service SNMP -Force -EA SilentlyContinue
  Set-Service -Name SNMP -StartupType Automatic -EA SilentlyContinue
  Start-Service SNMP -EA SilentlyContinue
  Start-Sleep 2
}

"########## 4. 重裝（沿用保留的狀態）##########"
$code = MsiRun @("/i","`"$MSI`"","/qn","MANAGEMENTNETWORKS=$ManagementNetworks","COMMUNITY=$Community")
Check "重裝結束碼為 0" ($code -eq 0) "(得到 $code)"
Check "重裝後服務執行中" ((SvcState) -eq "Running")
Check "重裝後 index-map 未變（ifIndex 穩定）" ((Get-FileHash $IDX -Algorithm SHA256).Hash -eq $idxHash1)

"########## 5. PURGE 移除（完整清除）##########"
$code = MsiRun @("/x", (Arp).PSChildName, "/qn", "PURGE=1")
Check "PURGE 移除結束碼為 0" ($code -eq 0) "(得到 $code)"
Check "服務已刪除" ((SvcState) -eq "absent")
Check "程式目錄已移除" (-not (Test-Path $PROG))
Check "資料目錄已完整清除" (-not (Test-Path $DATA)) "(PURGE=1)"

""
if ($msExists) {
  Check "PURGE 後內建 SNMP 仍為 Automatic" ((Get-Service SNMP).StartType -eq "Automatic") "(得到 $((Get-Service SNMP).StartType))"
}

"########## 結果 ##########"
"PASS=$PASS  FAIL=$FAIL"
if ($FAIL -gt 0) { "LIFECYCLE_RESULT=FAIL" } else { "LIFECYCLE_RESULT=PASS" }
