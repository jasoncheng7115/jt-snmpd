#Requires -Version 5.1
<#
    jt-snmpd 安裝程式

    注意：本檔以 UTF-8 with BOM 儲存。Windows PowerShell 5.1 在沒有 BOM 時
    會以系統 ANSI 代碼頁（正體中文為 cp950）讀取 .ps1，中文會打斷語法。

    這是 MSI 之前的過渡安裝程式。行為刻意對齊未來 MSI 的流程，
    讓現在驗證過的邏輯可以直接搬進 WiX 自訂動作：

      1. 前置檢查（spec §5.6）
      2. 偵測 Windows 內建 SNMP，抄走設定（spec §5.9）
      3. 停止並移除舊版（升級路徑，spec §5.7）
      4. 複製檔案到 %ProgramFiles%
      5. 建立 %ProgramData% 並修正 ACL（spec §3.7）
      6. 停用內建 SNMP 服務（spec §5.9.5，可還原）
      7. 註冊服務、設定失效復原與特權縮減（spec §6.2）
      8. 建立防火牆規則（spec §3.3，強制、不得 Any/Any）
      9. 啟動並做 loopback 健康檢查（spec §5.7 第 7 步、§6.5）
     10. 輸出移轉報告與路徑資訊（spec §5.9.7、§5.10）

    用法：
      .\install.ps1 -ManagementNetworks 192.168.1.0/24
      .\install.ps1 -ManagementNetworks 10.0.0.0/8 -Community mon2 -Force
      .\install.ps1 -Uninstall [-Purge]
#>
[CmdletBinding()]
param(
    [string[]]$ManagementNetworks,
    [string]$Community,
    [string]$SourceDir = "",
    [switch]$Uninstall,
    [switch]$Purge,
    [switch]$Force,
    [switch]$KeepMsSnmp
)

# native 工具寫 stderr 不應中斷安裝（jt-doc-tools 踩過的坑）
$ErrorActionPreference = 'Continue'

$SERVICE_NAME  = 'jt-snmpd'
$DISPLAY_NAME  = 'JT SNMP Agent'
$INSTALL_DIR   = Join-Path $env:ProgramFiles 'JT SNMP Agent'
$DATA_DIR      = Join-Path $env:ProgramData 'JT-SNMP'
$STATE_DIR     = Join-Path $DATA_DIR 'state'
$LOG_DIR       = Join-Path $DATA_DIR 'logs'
$SECRETS_DIR   = Join-Path $DATA_DIR 'secrets'
$EXE_NAME      = 'jt-snmpd.exe'
$FW_RULE       = 'JT SNMP Agent (UDP 161)'
$MSSNMP_PARAMS = 'HKLM:\SYSTEM\CurrentControlSet\Services\SNMP\Parameters'

# --- 輸出（沿用 jt-doc-tools 的四段式）---------------------------------------
function Log  { param($m) Write-Host "[*] $m" }
function Ok   { param($m) Write-Host "[OK] $m"   -ForegroundColor Green }
function Warn { param($m) Write-Host "[!] $m"    -ForegroundColor Yellow }
function Die  { param($m) Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }

$script:Report = @()
function Report { param($m) $script:Report += $m }

# --- 前置檢查（spec §5.6）----------------------------------------------------
function Test-Prerequisites {
    Log '前置檢查...'

    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
              [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Die '需要系統管理員權限。請以系統管理員身分執行。'
    }

    $os = Get-CimInstance Win32_OperatingSystem
    $build = [int]$os.BuildNumber
    if ($build -lt 14393) {
        Die "不支援的 OS 版本（build $build）。需要 Windows 10 / Server 2016 以上。"
    }
    if (-not [Environment]::Is64BitOperatingSystem) { Die '僅支援 x64。' }
    Ok "OS: $($os.Caption) build $build"

    # 磁碟空間
    $free = (Get-PSDrive -Name ($env:SystemDrive[0]) -ErrorAction SilentlyContinue).Free
    if ($free -and $free -lt 200MB) { Die "系統磁碟剩餘空間不足（$([math]::Round($free/1MB)) MB）。" }

    # UDP/161 佔用者（spec §5.6 / §5.9.6）
    $ep = Get-NetUDPEndpoint -LocalPort 161 -ErrorAction SilentlyContinue
    if ($ep) {
        foreach ($e in $ep) {
            $p = Get-Process -Id $e.OwningProcess -ErrorAction SilentlyContinue
            if (-not $p) { continue }
            $isMsSnmp = $p.Path -and $p.Path -like "$env:SystemRoot\System32\snmp.exe"
            $isOurs   = $p.Path -and $p.Path -like "$INSTALL_DIR\*"
            if ($isMsSnmp) {
                Log "UDP/161 由 Windows 內建 SNMP Service 佔用（PID $($p.Id)）——將依 §5.9 處理"
            } elseif ($isOurs) {
                Log "UDP/161 由既有的 $SERVICE_NAME 佔用——走升級流程"
            } else {
                # spec §5.9.6：絕不自動停用任何非 Microsoft 的服務
                Die @"
UDP/161 被非 Microsoft 程式佔用，安裝中止（不會動它）：
    PID      : $($p.Id)
    程序     : $($p.ProcessName)
    完整路徑 : $($p.Path)
請先手動停用該程式後重跑安裝。
"@
            }
        }
    }
}

# --- 抄走內建 SNMP 設定（spec §5.9）------------------------------------------
function Get-MsSnmpConfig {
    $cfg = [ordered]@{
        service_exists = $false; status = $null; start_type = $null
        communities = @{}; permitted_managers = @()
        sys_contact = $null; sys_location = $null; sys_services = $null
        trap_destinations = @(); extension_agents = @()
    }
    $svc = Get-Service -Name SNMP -ErrorAction SilentlyContinue
    if ($svc) {
        $cfg.service_exists = $true
        $cfg.status = "$($svc.Status)"
        $cfg.start_type = "$($svc.StartType)"
    }
    if (-not (Test-Path $MSSNMP_PARAMS)) { return $cfg }

    $vc = Join-Path $MSSNMP_PARAMS 'ValidCommunities'
    if (Test-Path $vc) {
        $props = Get-ItemProperty $vc
        foreach ($n in (Get-Item $vc).Property) { $cfg.communities[$n] = [int]$props.$n }
    }
    $pm = Join-Path $MSSNMP_PARAMS 'PermittedManagers'
    if (Test-Path $pm) {
        $props = Get-ItemProperty $pm
        foreach ($n in (Get-Item $pm).Property) { $cfg.permitted_managers += "$($props.$n)" }
    }
    $ra = Join-Path $MSSNMP_PARAMS 'RFC1156Agent'
    if (Test-Path $ra) {
        $p = Get-ItemProperty $ra
        $cfg.sys_contact  = $p.sysContact
        $cfg.sys_location = $p.sysLocation
        $cfg.sys_services = $p.sysServices
    }
    $tc = Join-Path $MSSNMP_PARAMS 'TrapConfiguration'
    if (Test-Path $tc) {
        foreach ($k in Get-ChildItem $tc) {
            $p = Get-ItemProperty $k.PSPath
            foreach ($n in (Get-Item $k.PSPath).Property) {
                $cfg.trap_destinations += "$($k.PSChildName) -> $($p.$n)"
            }
        }
    }
    $ea = Join-Path $MSSNMP_PARAMS 'ExtensionAgents'
    if (Test-Path $ea) {
        $p = Get-ItemProperty $ea
        foreach ($n in (Get-Item $ea).Property) { $cfg.extension_agents += $n }
    }
    return $cfg
}

function Resolve-Migration {
    param($MsCfg)

    # community：優先 -Community 參數，其次移轉唯讀 community（spec §5.9.4）
    $community = $Community
    if (-not $community) {
        foreach ($name in $MsCfg.communities.Keys) {
            $access = $MsCfg.communities[$name]
            switch ($access) {
                4  { if (-not $community) { $community = $name } }
                8  { if (-not $community) { $community = $name }
                     Warn "community '$name' 原為 READ WRITE，已降級為唯讀（本 agent 不支援 SET）"
                     Report "[!] community '$name' 原為可寫，已降級為唯讀" }
                16 { if (-not $community) { $community = $name }
                     Warn "community '$name' 原為 READ CREATE，已降級為唯讀"
                     Report "[!] community '$name' 原為可寫，已降級為唯讀" }
                default { Log "community '$name' 存取權限 $access（NONE/NOTIFY），不匯入" }
            }
        }
    }
    if ($community) {
        Report "[OK] 已匯入 community"
        if ($community -in @('public','private')) {
            Warn "community '$community' 是公認的預設值，強烈建議改用 SNMPv3"
            Report "[!] community '$community' 為公認預設值，建議改用 SNMPv3"
        }
        Report "[!] 本次安裝已啟用 SNMPv2c（本 agent 預設為停用）"
    }

    # 管理網段：優先參數，其次 PermittedManagers（需解析主機名稱，spec §5.9.3）
    $nets = @()
    if ($ManagementNetworks) {
        $nets = $ManagementNetworks
    } elseif ($MsCfg.permitted_managers.Count -gt 0) {
        foreach ($m in $MsCfg.permitted_managers) {
            $ip = $null
            if ($m -match '^\d{1,3}(\.\d{1,3}){3}$') {
                $ip = $m
            } else {
                try {
                    $ip = ([System.Net.Dns]::GetHostAddresses($m) |
                           Where-Object AddressFamily -eq 'InterNetwork' |
                           Select-Object -First 1).IPAddressToString
                } catch { $ip = $null }
            }
            if ($ip) {
                $nets += $ip
                Log "PermittedManagers '$m' 解析為 $ip"
            } else {
                Warn "PermittedManagers '$m' 無法解析為 IP，已略過"
                Report "[!] PermittedManagers '$m' 無法解析，未納入 ACL"
            }
        }
        if ($nets.Count -gt 0) { Report "[OK] 已匯入 $($nets.Count) 個 PermittedManagers 作為來源 ACL" }
    }

    # spec §5.9.4 ①：PermittedManagers 為空 = 接受任何來源，絕不移轉為 Any/Any
    if ($nets.Count -eq 0) {
        Die @"
未提供管理網段，且無法從既有設定取得。
本 agent 預設 deny，不允許 Any/Any（spec §3.3）。
請以 -ManagementNetworks 指定，例如：
    .\install.ps1 -ManagementNetworks 192.168.1.0/24
"@
    }

    return @{ community = $community; networks = $nets }
}

# --- 安裝 --------------------------------------------------------------------
function Stop-ExistingService {
    $svc = Get-Service -Name $SERVICE_NAME -ErrorAction SilentlyContinue
    if (-not $svc) { return $false }
    Log '偵測到既有安裝，執行升級流程...'
    if ($svc.Status -ne 'Stopped') {
        Stop-Service -Name $SERVICE_NAME -Force -ErrorAction SilentlyContinue
    }
    # 停服務回來不代表檔案句柄已釋放（jt-doc-tools v1.1.66~69 的實際 bug）
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Name $SERVICE_NAME -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Get-Process -Name $SERVICE_NAME -ErrorAction SilentlyContinue) {
        Get-Process -Name $SERVICE_NAME | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    & sc.exe delete $SERVICE_NAME | Out-Null
    Start-Sleep -Seconds 2
    return $true
}

function Install-Files {
    param($Src)
    Log "複製檔案到 $INSTALL_DIR ..."
    if (Test-Path $INSTALL_DIR) {
        # Windows 對已載入為映像的 .pyd/.dll 回傳「拒絕存取」，刪不掉但改得了名
        try {
            Remove-Item $INSTALL_DIR -Recurse -Force -ErrorAction Stop
        } catch {
            $stamp = Get-Date -Format 'yyyyMMddHHmmss'
            Rename-Item $INSTALL_DIR "$INSTALL_DIR.old.$stamp" -ErrorAction SilentlyContinue
        }
    }
    New-Item -ItemType Directory -Force $INSTALL_DIR | Out-Null
    Copy-Item "$Src\*" $INSTALL_DIR -Recurse -Force
    if (-not (Test-Path (Join-Path $INSTALL_DIR $EXE_NAME))) {
        Die "複製後找不到 $EXE_NAME"
    }
    Ok "程式已安裝至 $INSTALL_DIR"
}

function Initialize-DataDir {
    Log "建立資料目錄 $DATA_DIR ..."
    foreach ($d in @($DATA_DIR, $STATE_DIR, $LOG_DIR, $SECRETS_DIR)) {
        if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force $d | Out-Null }
    }
    # spec §3.7：C:\ProgramData 預設 ACL 允許 Users 建立子資料夾，
    # 攻擊者可搶先建立目錄並保留寫入權。不能只 create-if-not-exists，
    # 必須重設 ACL 為 SYSTEM/Administrators only。
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)      # 停用繼承並移除既有規則
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) { # LocalSystem, Administrators
        $account = (New-Object System.Security.Principal.SecurityIdentifier $sid)
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $account, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-32-544'))
    Set-Acl -Path $DATA_DIR -AclObject $acl
    Ok "資料目錄 ACL 已設為 SYSTEM/Administrators only"
}

function Disable-MsSnmp {
    param($MsCfg)
    if (-not $MsCfg.service_exists) { return $false }
    if ($KeepMsSnmp) {
        Warn '依 -KeepMsSnmp，保留 Windows 內建 SNMP Service（161 會衝突）'
        return $false
    }
    Log '停用 Windows 內建 SNMP Service（不移除功能，可還原）...'
    Stop-Service -Name SNMP -Force -ErrorAction SilentlyContinue
    Set-Service  -Name SNMP -StartupType Disabled -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Ok "內建 SNMP Service 已停用（原為 $($MsCfg.start_type) / $($MsCfg.status)）"
    Report "[OK] 已停用 Windows SNMP Service（原為 $($MsCfg.start_type) / $($MsCfg.status)）"
    return $true
}

function Register-Service {
    $exe = Join-Path $INSTALL_DIR $EXE_NAME
    Log '註冊服務...'
    # binPath 必須加引號 —— 預設安裝路徑本身含空白（unquoted service path）
    & $exe --startup auto install | Out-Null
    if (-not (Get-Service -Name $SERVICE_NAME -ErrorAction SilentlyContinue)) {
        Die '服務註冊失敗'
    }
    & sc.exe description $SERVICE_NAME '以標準 MIB 提供 Windows 主機監控資料的 SNMP Agent' | Out-Null
    # 失效自動復原三段式；failureflag 1 讓非零結束碼也觸發（spec §6.2）
    & sc.exe failure $SERVICE_NAME reset= 86400 actions= restart/60000/restart/60000/restart/300000 | Out-Null
    & sc.exe failureflag $SERVICE_NAME 1 | Out-Null
    # 特權縮減（spec §3.6）
    & sc.exe privs $SERVICE_NAME SeChangeNotifyPrivilege/SeSystemProfilePrivilege/SeIncreaseQuotaPrivilege | Out-Null
    $svc = Get-CimInstance Win32_Service -Filter "Name='$SERVICE_NAME'"
    Ok "服務已註冊：$($svc.StartName) / $($svc.StartMode)"
    if ($svc.PathName -notmatch '^"') { Warn "服務 ImagePath 未加引號：$($svc.PathName)" }
}

function Set-FirewallRule {
    param($Networks)
    Log '建立防火牆規則...'
    # 停用內建 SNMP 會連帶停用它的防火牆規則，故必須自建（實測）
    Remove-NetFirewallRule -DisplayName "$FW_RULE*" -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $FW_RULE -Direction Inbound -Protocol UDP `
        -LocalPort 161 -RemoteAddress $Networks -Action Allow -Profile Any `
        -Description 'jt-snmpd inbound SNMP' | Out-Null
    Ok "防火牆規則已建立（來源限 $($Networks -join ', ')）"
}

function Start-AndVerify {
    param($CommunityName)
    Log '啟動服務...'
    Start-Service -Name $SERVICE_NAME
    # spec §5.7 第 7 步：MSI 預設只確認「服務啟動成功」，
    # 但服務啟動成功不等於能回應 SNMP（§6.5 的「假活著」）。
    $deadline = (Get-Date).AddSeconds(30)
    $okResp = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if ((Get-Service -Name $SERVICE_NAME).Status -ne 'Running') { continue }
        if (Test-SnmpLoopback -CommunityName $CommunityName) { $okResp = $true; break }
    }
    if (-not $okResp) {
        Warn '服務已啟動，但 30 秒內未能回應 loopback SNMP 查詢'
        Warn "請檢查 $LOG_DIR\jt-snmpd.log"
        return $false
    }
    Ok '服務已啟動並通過 loopback 自我測試'
    return $true
}

function Test-SnmpLoopback {
    param($CommunityName)
    # 手工組一個 v2c GET sysUpTime.0，不依賴任何外部 SNMP 工具
    $comm = [Text.Encoding]::ASCII.GetBytes($CommunityName)
    $oid  = [byte[]](0x2B,0x06,0x01,0x02,0x01,0x01,0x03,0x00)   # 1.3.6.1.2.1.1.3.0
    $vb   = [byte[]](0x30, (2+$oid.Length+2)) + [byte[]](0x06, $oid.Length) + $oid + [byte[]](0x05,0x00)
    $vbl  = [byte[]](0x30, $vb.Length) + $vb
    $pdu  = [byte[]](0xA0, (3+3+3+$vbl.Length)) + [byte[]](0x02,0x01,0x01) +
            [byte[]](0x02,0x01,0x00) + [byte[]](0x02,0x01,0x00) + $vbl
    $body = [byte[]](0x02,0x01,0x01) + [byte[]](0x04, $comm.Length) + $comm + $pdu
    $msg  = [byte[]](0x30, $body.Length) + $body
    try {
        $c = New-Object System.Net.Sockets.UdpClient
        $c.Client.ReceiveTimeout = 3000
        $c.Connect('127.0.0.1', 161)
        [void]$c.Send($msg, $msg.Length)
        $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
        $resp = $c.Receive([ref]$ep)
        $c.Close()
        return ($resp.Length -gt 0)
    } catch { return $false }
}

function Write-Config {
    param($Resolved, $MsCfg)
    $cfg = [ordered]@{
        schema_version    = 1
        community         = $Resolved.community
        allowed_networks  = @($Resolved.networks)
        port              = 161
        enable_arp_table  = $false
        installed_at      = (Get-Date).ToString('s')
    }
    $cfg | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $DATA_DIR 'config.json') -Encoding UTF8

    # 還原資訊（spec §5.9.5）—— community 明文不寫入此檔
    $restore = [ordered]@{
        schema_version = 1
        migrated_at    = (Get-Date).ToString('s')
        ms_snmp = [ordered]@{
            service_existed      = $MsCfg.service_exists
            original_start_type  = $MsCfg.start_type
            original_status      = $MsCfg.status
            disabled_by_us       = (-not $KeepMsSnmp) -and $MsCfg.service_exists
        }
        imported = [ordered]@{
            communities        = @($MsCfg.communities.Keys | ForEach-Object {
                                     if ($_.Length -gt 4) { $_.Substring(0,4) + '***' } else { '***' } })
            permitted_managers = @($MsCfg.permitted_managers)
            sys_contact        = $MsCfg.sys_contact
            sys_location       = $MsCfg.sys_location
        }
        not_imported = [ordered]@{
            trap_destinations = @($MsCfg.trap_destinations)
            extension_agents  = @($MsCfg.extension_agents)
            sys_services      = $MsCfg.sys_services
        }
    }
    $restore | ConvertTo-Json -Depth 6 |
        Set-Content (Join-Path $STATE_DIR 'ms-snmp-restore.json') -Encoding UTF8
}

function Write-MigrationReport {
    param($MsCfg, $Resolved)
    $lines = @()
    $lines += '========================================================'
    $lines += '  Windows SNMP Service 設定移轉報告'
    $lines += '========================================================'
    $lines += ''
    if ($MsCfg.service_exists -or (Test-Path $MSSNMP_PARAMS)) {
        $lines += '來源設定位置：'
        $lines += '  HKLM\SYSTEM\CurrentControlSet\Services\SNMP\Parameters'
        $lines += ''
        $lines += $script:Report
        if ($MsCfg.sys_contact -or $MsCfg.sys_location) {
            $lines += "[OK]   已沿用 sysContact / sysLocation"
        }
        if ($MsCfg.sys_services -and $MsCfg.sys_services -ne 76) {
            $lines += "[!]    原 sysServices 為 $($MsCfg.sys_services)，本 agent 固定為 76"
        }
        foreach ($t in $MsCfg.trap_destinations) {
            $lines += "[!]    trap 目的地未移轉，trap 將停止發送：$t"
        }
        foreach ($e in $MsCfg.extension_agents) {
            $lines += "[!]    ExtensionAgent '$e' 未移轉，其提供的 OID 將不再可用"
        }
        $snmptrap = Get-Service -Name SNMPTRAP -ErrorAction SilentlyContinue
        if ($snmptrap) { $lines += "[OK]   SNMPTRAP 服務未變更（目前 $($snmptrap.Status)）" }
    } else {
        $lines += '未偵測到 Windows 內建 SNMP Service，無須移轉。'
    }
    $lines += ''
    $lines += '本次設定：'
    $lines += "  community        $($Resolved.community)"
    $lines += "  管理網段         $($Resolved.networks -join ', ')"
    $lines += ''
    $lines += '如需還原 Windows SNMP Service：'
    $lines += "  powershell -File install.ps1 -Uninstall"
    $lines += '========================================================'

    $path = Join-Path $LOG_DIR 'ms-snmp-migration-report.txt'
    $lines | Set-Content $path -Encoding UTF8
    $lines | ForEach-Object { Write-Host $_ }
    return $path
}

function Write-Summary {
    param($Resolved)
    $ips = (Get-NetIPAddress -AddressFamily IPv4 |
            Where-Object { $_.IPAddress -ne '127.0.0.1' } |
            Select-Object -ExpandProperty IPAddress) -join ', '
    Write-Host ''
    Write-Host '========================================================'
    Write-Host '  JT SNMP Agent 安裝完成'
    Write-Host '========================================================'
    Write-Host ''
    Write-Host "  服務名稱    $SERVICE_NAME（自動啟動，執行中）"
    Write-Host "  監聽位址    ${ips}:161"
    Write-Host "  通訊協定    SNMPv2c（community: $($Resolved.community)）"
    Write-Host "  來源 ACL    $($Resolved.networks -join ', ')"
    Write-Host ''
    Write-Host "  設定檔      $DATA_DIR\config.json"
    Write-Host "  記錄檔      $LOG_DIR\"
    Write-Host "  狀態檔      $STATE_DIR\"
    Write-Host "  程式目錄    $INSTALL_DIR\"
    Write-Host ''
    Write-Host '  下一步：在 LibreNMS 加入此裝置後，務必執行一次 discovery，'
    Write-Host '          否則只會 poll 而抓不到完整的 OID 集合。'
    Write-Host '========================================================'
}

# --- 解除安裝 ----------------------------------------------------------------
function Invoke-Uninstall {
    Log '解除安裝 jt-snmpd ...'
    Stop-ExistingService | Out-Null
    Remove-NetFirewallRule -DisplayName "$FW_RULE*" -ErrorAction SilentlyContinue
    Ok '防火牆規則已移除'

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
                    Ok "Windows 內建 SNMP Service 已還原為 $orig"
                }
            }
        } catch { Warn "還原內建 SNMP 失敗：$_" }
    }

    if (Test-Path $INSTALL_DIR) {
        try { Remove-Item $INSTALL_DIR -Recurse -Force -ErrorAction Stop }
        catch { Rename-Item $INSTALL_DIR "$INSTALL_DIR.old" -ErrorAction SilentlyContinue }
        Ok '程式目錄已移除'
    }

    if ($Purge) {
        if (Test-Path $DATA_DIR) { Remove-Item $DATA_DIR -Recurse -Force -ErrorAction SilentlyContinue }
        Ok '資料目錄已完整清除（PURGE）'
    } else {
        # spec §5.7：預設保留 ProgramData 是刻意的。客戶常以「移除再重裝」
        # 排除問題，若索引被清除，LibreNMS 會整組重新 discovery，舊 RRD 全變孤兒。
        Ok "資料目錄已保留：$DATA_DIR（如需清除請加 -Purge）"
    }
    Write-Host ''
    Ok '解除安裝完成，不需重新開機'
}

# --- 主流程 ------------------------------------------------------------------
if ($Uninstall) { Invoke-Uninstall; exit 0 }

if (-not $SourceDir) {
    $SourceDir = Join-Path (Split-Path -Parent $PSCommandPath) 'jt-snmpd'
}
if (-not (Test-Path (Join-Path $SourceDir $EXE_NAME))) {
    Die "來源目錄找不到 $EXE_NAME：$SourceDir"
}

Write-Host ''
Write-Host "JT SNMP Agent 安裝程式" -ForegroundColor Cyan
Write-Host ''

Test-Prerequisites
$msCfg = Get-MsSnmpConfig
if ($msCfg.service_exists) {
    Log "偵測到 Windows 內建 SNMP Service（$($msCfg.status) / $($msCfg.start_type)）"
    Log "  community: $($msCfg.communities.Count) 組，PermittedManagers: $($msCfg.permitted_managers.Count) 筆"
}
$resolved = Resolve-Migration -MsCfg $msCfg
$upgrade  = Stop-ExistingService
Install-Files -Src $SourceDir
Initialize-DataDir
Write-Config -Resolved $resolved -MsCfg $msCfg
Disable-MsSnmp -MsCfg $msCfg | Out-Null
Register-Service
Set-FirewallRule -Networks $resolved.networks
$healthy = Start-AndVerify -CommunityName $resolved.community
if (-not $healthy) { Die '安裝完成但健康檢查未通過，請檢查記錄檔' }

Write-MigrationReport -MsCfg $msCfg -Resolved $resolved | Out-Null
Write-Summary -Resolved $resolved
exit 0
