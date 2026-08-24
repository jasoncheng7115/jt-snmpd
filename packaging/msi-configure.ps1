#Requires -Version 5.1
<#
    jt-snmpd MSI 自訂動作腳本

    注意：本檔以 UTF-8 with BOM 儲存（PowerShell 5.1 無 BOM 時以 cp950 讀取）。

    由 MSI 的 deferred custom action 呼叫，執行 install.ps1 中「檔案複製之後」
    的所有步驟。兩者共用同一套邏輯，避免「MSI 裝的」與「腳本裝的」
    產生兩種不一致的狀態（spec §5.4 的關鍵設計）。

    MSI 已負責：前置檢查、檔案複製、升級時移除舊版、失敗回滾。
    本腳本負責：內建 SNMP 移轉與停用、config、ACL、服務註冊、防火牆、健康檢查。
#>
[CmdletBinding()]
param(
    [string]$ManagementNetworks = "",
    [string]$Community = "",
    [string]$KeepMsSnmp = "0",

    [switch]$Uninstall,
    [string]$Purge = "0"
)

$ErrorActionPreference = 'Continue'

$SERVICE_NAME  = 'jt-snmpd'
$DATA_DIR      = Join-Path $env:ProgramData 'JT-SNMP'
$STATE_DIR     = Join-Path $DATA_DIR 'state'
$LOG_DIR       = Join-Path $DATA_DIR 'logs'
$SECRETS_DIR   = Join-Path $DATA_DIR 'secrets'
$EXE_NAME      = 'jt-snmpd.exe'
$FW_RULE       = 'JT SNMP Agent (UDP 161)'
$FW_RULE_ICMP  = 'JT SNMP Agent (ICMPv4)'
$MSSNMP_PARAMS = 'HKLM:\SYSTEM\CurrentControlSet\Services\SNMP\Parameters'

# MSI 的自訂動作沒有主控台，所有輸出寫入記錄檔供事後診斷
$MSI_LOG = Join-Path $LOG_DIR 'msi-configure.log'
# PURGE 之後必須停止寫檔——記錄檔就在要清除的目錄裡，
# 再寫一行就會把 logs\ 重建回來，讓「完整清除」實際上留下殘骸（實測踩過）。
$script:LogToFile = $true
function Log {
    param($m)
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    if ($script:LogToFile) {
        try {
            if (-not (Test-Path $LOG_DIR)) { New-Item -ItemType Directory -Force $LOG_DIR | Out-Null }
            Add-Content -Path $MSI_LOG -Value $line -Encoding UTF8
        } catch { }
    }
    Write-Host $line
}

function Stop-AgentService {
    $svc = Get-Service -Name $SERVICE_NAME -ErrorAction SilentlyContinue
    if (-not $svc) { return }
    if ($svc.Status -ne 'Stopped') { Stop-Service -Name $SERVICE_NAME -Force -ErrorAction SilentlyContinue }
    # 停服務回來不代表檔案句柄已釋放（jt-doc-tools v1.1.66~69 的實際 bug）
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Name $SERVICE_NAME -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    Get-Process -Name $SERVICE_NAME -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    & sc.exe delete $SERVICE_NAME | Out-Null
    Start-Sleep -Seconds 2
    Log "已停止並移除舊服務"
}

# ---------------- 解除安裝 ----------------
if ($Uninstall) {
    Log "=== 解除安裝開始 ==="
    Stop-AgentService
    Remove-NetFirewallRule -DisplayName "$FW_RULE*" -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName "$FW_RULE_ICMP*" -ErrorAction SilentlyContinue
    Log "防火牆規則已移除"

    # 還原內建 SNMP（spec §5.9.5）
    $restorePath = Join-Path $STATE_DIR 'ms-snmp-restore.json'
    if (Test-Path $restorePath) {
        try {
            $r = Get-Content $restorePath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($r.ms_snmp.disabled_by_us -and $r.ms_snmp.service_existed) {
                $orig = $r.ms_snmp.original_start_type
                if ($orig -and $orig -ne 'Disabled') {
                    Set-Service -Name SNMP -StartupType $orig -ErrorAction SilentlyContinue
                    if ($r.ms_snmp.original_status -eq 'Running') {
                        Start-Service -Name SNMP -ErrorAction SilentlyContinue
                    }
                    Log "Windows 內建 SNMP Service 已還原為 $orig"
                }
            }
        } catch { Log "還原內建 SNMP 失敗：$_" }
    }

    if ($Purge -eq '1') {
        # 先關掉檔案記錄，否則接下來每一行 Log 都會把 logs\ 重新建出來。
        Log "資料目錄清除中（PURGE=1）：$DATA_DIR"
        $script:LogToFile = $false
        # 服務剛停止，DPAPI blob 或記錄檔可能仍被短暫持有；重試而不是靜默略過。
        $purged = $false
        foreach ($attempt in 1..5) {
            Remove-Item $DATA_DIR -Recurse -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path $DATA_DIR)) { $purged = $true; break }
            Start-Sleep -Milliseconds 400
        }
        if ($purged) {
            Log "資料目錄已完整清除（PURGE=1）"
        } else {
            # 不可謊報成功：留下的殘骸會讓下次安裝沿用舊狀態。
            $left = @(Get-ChildItem $DATA_DIR -Recurse -Force -ErrorAction SilentlyContinue).Count
            Log "WARN 資料目錄清除未完成，仍有 $left 個項目：$DATA_DIR"
        }
    } else {
        # spec §5.7：預設保留是刻意的。客戶常以「移除再重裝」排除問題，
        # 若索引被清除，LibreNMS 會整組重新 discovery，舊 RRD 全數變孤兒。
        Log "資料目錄已保留：$DATA_DIR"
    }
    Log "=== 解除安裝完成 ==="
    exit 0
}

# ---------------- 安裝 / 升級 ----------------
# 本腳本位於安裝目錄內，直接由自身位置推導——不必由 MSI 傳入，
# 也就避開 [INSTALLFOLDER] 尾端反斜線跳脫引號的陷阱。
$InstallDir = Split-Path -Parent $PSCommandPath
Log "=== 設定開始 InstallDir=$InstallDir ==="
$exe = Join-Path $InstallDir $EXE_NAME
if (-not (Test-Path $exe)) { Log "FAIL 找不到 $exe"; exit 1 }

Stop-AgentService

# --- 讀取內建 SNMP 設定（spec §5.9）---
$msCfg = [ordered]@{
    service_exists = $false; status = $null; start_type = $null
    communities = @{}; permitted_managers = @()
    sys_contact = $null; sys_location = $null; sys_services = $null
    trap_destinations = @(); extension_agents = @()
}
$svc = Get-Service -Name SNMP -ErrorAction SilentlyContinue
if ($svc) {
    $msCfg.service_exists = $true
    $msCfg.status = "$($svc.Status)"
    $msCfg.start_type = "$($svc.StartType)"
    Log "偵測到內建 SNMP Service：$($svc.Status) / $($svc.StartType)"
}
if (Test-Path $MSSNMP_PARAMS) {
    $vc = Join-Path $MSSNMP_PARAMS 'ValidCommunities'
    if (Test-Path $vc) {
        $props = Get-ItemProperty $vc
        foreach ($n in (Get-Item $vc).Property) { $msCfg.communities[$n] = [int]$props.$n }
    }
    $pm = Join-Path $MSSNMP_PARAMS 'PermittedManagers'
    if (Test-Path $pm) {
        $props = Get-ItemProperty $pm
        foreach ($n in (Get-Item $pm).Property) { $msCfg.permitted_managers += "$($props.$n)" }
    }
    $ra = Join-Path $MSSNMP_PARAMS 'RFC1156Agent'
    if (Test-Path $ra) {
        $p = Get-ItemProperty $ra
        $msCfg.sys_contact = $p.sysContact; $msCfg.sys_location = $p.sysLocation
        $msCfg.sys_services = $p.sysServices
    }
    $tc = Join-Path $MSSNMP_PARAMS 'TrapConfiguration'
    if (Test-Path $tc) {
        foreach ($k in Get-ChildItem $tc) {
            $p = Get-ItemProperty $k.PSPath
            foreach ($n in (Get-Item $k.PSPath).Property) {
                $msCfg.trap_destinations += "$($k.PSChildName) -> $($p.$n)"
            }
        }
    }
    $ea = Join-Path $MSSNMP_PARAMS 'ExtensionAgents'
    if (Test-Path $ea) {
        $p = Get-ItemProperty $ea
        foreach ($n in (Get-Item $ea).Property) { $msCfg.extension_agents += $n }
    }
}

# --- 決定 community（spec §5.9.4 安全規則優先於忠實移轉）---
$comm = $Community
if (-not $comm) {
    foreach ($name in $msCfg.communities.Keys) {
        $access = $msCfg.communities[$name]
        if ($access -eq 4) { if (-not $comm) { $comm = $name }; Log "匯入唯讀 community" }
        elseif ($access -in @(8,16)) {
            if (-not $comm) { $comm = $name }
            Log "[!] community 原為可寫（access=$access），已降級為唯讀"
        } else { Log "community access=$access（NONE/NOTIFY），不匯入" }
    }
}
if ($comm -in @('public','private')) {
    Log "[!] community 為公認預設值，強烈建議改用 SNMPv3"
}

# --- 決定管理網段 ---
$nets = @()
if ($ManagementNetworks) {
    $nets = $ManagementNetworks -split '[,;\s]+' | Where-Object { $_ }
} elseif ($msCfg.permitted_managers.Count -gt 0) {
    foreach ($m in $msCfg.permitted_managers) {
        if ($m -match '^\d{1,3}(\.\d{1,3}){3}$') { $nets += $m; Log "PermittedManagers $m" }
        else {
            try {
                $ip = ([System.Net.Dns]::GetHostAddresses($m) |
                       Where-Object AddressFamily -eq 'InterNetwork' |
                       Select-Object -First 1).IPAddressToString
                if ($ip) { $nets += $ip; Log "PermittedManagers '$m' 解析為 $ip" }
            } catch { Log "[!] PermittedManagers '$m' 無法解析，未納入 ACL" }
        }
    }
}
if ($nets.Count -eq 0) {
    # spec §3.3 / §5.9.4 ①：絕不移轉為 Any/Any
    Log "FAIL 未提供管理網段且無法從既有設定取得。預設 deny，不允許 Any/Any。"
    exit 1
}
Log "管理網段：$($nets -join ', ')"

# --- 資料目錄與 ACL（spec §3.7）---
foreach ($d in @($DATA_DIR, $STATE_DIR, $LOG_DIR, $SECRETS_DIR)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force $d | Out-Null }
}
try {
    # C:\ProgramData 的預設 ACL 允許 Users 建立子資料夾，攻擊者可搶先建立
    # 目錄並保留寫入權。不能只 create-if-not-exists，必須重設 ACL。
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $account = New-Object System.Security.Principal.SecurityIdentifier $sid
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $account, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-32-544'))
    Set-Acl -Path $DATA_DIR -AclObject $acl
    Log "資料目錄 ACL 已設為 SYSTEM/Administrators only"
} catch { Log "[!] ACL 設定失敗：$_" }

# --- 寫 config 與還原資訊 ---
$cfg = [ordered]@{
    schema_version = 1; community = $comm; allowed_networks = @($nets)
    port = 161; enable_arp_table = $false; installed_at = (Get-Date).ToString('s')
    installed_by = 'msi'
}
$cfg | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $DATA_DIR 'config.json') -Encoding UTF8

# 升級時**不可**用當下狀態覆寫還原記錄：此刻的內建 SNMP 已經被上一次安裝
# 停用了，重讀只會得到 Disabled/Stopped。寫回去之後解除安裝那段的
# `if ($orig -ne 'Disabled')` 判斷就永遠不成立——安裝→升級→移除之後，
# 內建 SNMP 再也回不來。要記的是「**我們第一次動手之前**的樣子」，
# 因此既有記錄一律優先，只有第一次安裝才寫入。
$RESTORE_FILE = Join-Path $STATE_DIR 'ms-snmp-restore.json'
$msSnmpBlock = $null
if (Test-Path $RESTORE_FILE) {
    try {
        $prev = Get-Content $RESTORE_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($prev.ms_snmp) {
            $msSnmpBlock = [ordered]@{
                service_existed     = [bool]$prev.ms_snmp.service_existed
                original_start_type = $prev.ms_snmp.original_start_type
                original_status     = $prev.ms_snmp.original_status
                disabled_by_us      = [bool]$prev.ms_snmp.disabled_by_us
            }
            Log ("沿用既有還原記錄：內建 SNMP 原為 " +
                 "$($msSnmpBlock.original_start_type) / $($msSnmpBlock.original_status)")
        }
    } catch { Log "WARN 既有還原記錄無法解析，將以當下狀態重建：$_" }
}
if (-not $msSnmpBlock) {
    $msSnmpBlock = [ordered]@{
        service_existed = $msCfg.service_exists
        original_start_type = $msCfg.start_type
        original_status = $msCfg.status
        disabled_by_us = ($KeepMsSnmp -ne '1') -and $msCfg.service_exists
    }
}

$restore = [ordered]@{
    schema_version = 1; migrated_at = (Get-Date).ToString('s')
    ms_snmp = $msSnmpBlock
    imported = [ordered]@{
        communities = @($msCfg.communities.Keys | ForEach-Object {
            if ($_.Length -gt 4) { $_.Substring(0,4) + '***' } else { '***' } })
        permitted_managers = @($msCfg.permitted_managers)
        sys_contact = $msCfg.sys_contact; sys_location = $msCfg.sys_location
    }
    not_imported = [ordered]@{
        trap_destinations = @($msCfg.trap_destinations)
        extension_agents = @($msCfg.extension_agents)
        sys_services = $msCfg.sys_services
    }
}
$restore | ConvertTo-Json -Depth 6 | Set-Content $RESTORE_FILE -Encoding UTF8

# --- 停用內建 SNMP（停用，不移除；spec §5.9.5）---
if ($msCfg.service_exists -and $KeepMsSnmp -ne '1') {
    Stop-Service -Name SNMP -Force -ErrorAction SilentlyContinue
    Set-Service -Name SNMP -StartupType Disabled -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    # 驗證而非假設：群組原則或第三方管控可能擋下這兩個動作。若沒真的停掉，
    # 內建 SNMP 仍佔著 UDP/161，我們會綁定失敗——與其讓後面的健康檢查
    # 出現一個看不出原因的逾時，不如在這裡就講清楚是誰佔著。
    $after = Get-Service -Name SNMP -ErrorAction SilentlyContinue
    if ($after -and ($after.Status -ne 'Stopped' -or $after.StartType -ne 'Disabled')) {
        Log ("FAIL 內建 SNMP Service 停用失敗，目前為 " +
             "$($after.Status) / $($after.StartType)。" +
             "可能受群組原則管控；請手動停用後重試，或以 KEEPMSSNMP=1 並改用其他連接埠安裝。")
        exit 1
    }
    Log "內建 SNMP Service 已停用（原為 $($msCfg.start_type) / $($msCfg.status)）"
}

# --- 註冊服務 ---
& $exe --startup auto install 2>&1 | Out-Null
if (-not (Get-Service -Name $SERVICE_NAME -ErrorAction SilentlyContinue)) {
    Log "FAIL 服務註冊失敗"; exit 1
}
& sc.exe description $SERVICE_NAME '以標準 MIB 提供 Windows 主機監控資料的 SNMP Agent' | Out-Null
# 失效自動復原三段式；failureflag 1 讓非零結束碼也觸發（spec §6.2）
& sc.exe failure $SERVICE_NAME reset= 86400 actions= restart/60000/restart/60000/restart/300000 | Out-Null
& sc.exe failureflag $SERVICE_NAME 1 | Out-Null
# 特權縮減（spec §3.6）
& sc.exe privs $SERVICE_NAME SeChangeNotifyPrivilege/SeSystemProfilePrivilege/SeIncreaseQuotaPrivilege | Out-Null
$s = Get-CimInstance Win32_Service -Filter "Name='$SERVICE_NAME'"
Log "服務已註冊：$($s.StartName) / $($s.StartMode)"
if ($s.PathName -notmatch '^"') { Log "[!] ImagePath 未加引號：$($s.PathName)" }

# --- 防火牆（spec §3.3，強制、預設 deny）---
Remove-NetFirewallRule -DisplayName "$FW_RULE*" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $FW_RULE -Direction Inbound -Protocol UDP `
    -LocalPort 161 -RemoteAddress $nets -Action Allow -Profile Any `
    -Description 'jt-snmpd inbound SNMP' | Out-Null
# 停用內建 SNMP 會連帶停用它的 ICMP 規則，而 LibreNMS 靠 ping 判定存活（實測）
Remove-NetFirewallRule -DisplayName "$FW_RULE_ICMP*" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $FW_RULE_ICMP -Direction Inbound -Protocol ICMPv4 `
    -IcmpType 8 -RemoteAddress $nets -Action Allow -Profile Any `
    -Description 'jt-snmpd ICMP echo for NMS availability' | Out-Null
Log "防火牆規則已建立（UDP/161 + ICMPv4，來源限 $($nets -join ', ')）"

# --- 啟動並做 loopback 健康檢查（spec §5.7 第 7 步）---
function Test-SnmpLoopback {
    param($CommunityName)
    $c2 = [Text.Encoding]::ASCII.GetBytes($CommunityName)
    $oid = [byte[]](0x2B,0x06,0x01,0x02,0x01,0x01,0x03,0x00)
    $vb = [byte[]](0x30,(2+$oid.Length+2)) + [byte[]](0x06,$oid.Length) + $oid + [byte[]](0x05,0x00)
    $vbl = [byte[]](0x30,$vb.Length) + $vb
    $pdu = [byte[]](0xA0,(3+3+3+$vbl.Length)) + [byte[]](0x02,0x01,0x01) +
           [byte[]](0x02,0x01,0x00) + [byte[]](0x02,0x01,0x00) + $vbl
    $body = [byte[]](0x02,0x01,0x01) + [byte[]](0x04,$c2.Length) + $c2 + $pdu
    $msg = [byte[]](0x30,$body.Length) + $body
    try {
        $u = New-Object System.Net.Sockets.UdpClient
        $u.Client.ReceiveTimeout = 3000
        $u.Connect('127.0.0.1', 161)
        [void]$u.Send($msg, $msg.Length)
        $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
        $resp = $u.Receive([ref]$ep); $u.Close()
        return ($resp.Length -gt 0)
    } catch { return $false }
}

Start-Service -Name $SERVICE_NAME
$deadline = (Get-Date).AddSeconds(30)
$healthy = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if ((Get-Service -Name $SERVICE_NAME).Status -ne 'Running') { continue }
    if (Test-SnmpLoopback -CommunityName $comm) { $healthy = $true; break }
}
if (-not $healthy) {
    # MSI 預設只確認「服務啟動成功」，但服務啟動成功不等於能回應 SNMP
    # （spec §6.5 的「假活著」）。健康檢查失敗即讓 MSI 交易回滾。
    Log "FAIL 服務已啟動但 30 秒內未回應 loopback SNMP 查詢"
    exit 1
}
Log "服務已啟動並通過 loopback 自我測試"
Log "=== 設定完成 ==="
exit 0
