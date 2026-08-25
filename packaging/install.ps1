#Requires -Version 5.1
<#
    jt-snmpd installer

    Note: saved as UTF-8 with BOM. Without one, Windows PowerShell 5.1 reads a
    .ps1 using the system ANSI code page, which breaks parsing.

    This is the interim installer that predates the MSI. Its behaviour deliberately
    mirrors the MSI flow, so logic verified here moves straight into the WiX
    custom action:
      1. Pre-checks
      2. Detect the built-in Windows SNMP service and copy its settings
      3. Stop and remove any previous version (the upgrade path)
      4. Copy files to %ProgramFiles%
      5. Create %ProgramData% and fix its ACL
      6. Disable the built-in SNMP service
      7. Register the service, configure failure recovery and reduce privileges
      8. Create firewall rules
      9. Start it and run a loopback health check
     10. Print the migration report and the paths

    Usage:
      .\install.ps1 -ManagementNetworks 192.168.1.0/24
      .\install.ps1 -ManagementNetworks 10.0.0.0/8 -Community <your-community> -Force
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

# Native tools write to stderr; that must not abort the install (a trap
$ErrorActionPreference = 'Continue'

$SERVICE_NAME  = 'jt-snmpd'
$DISPLAY_NAME  = 'jt-snmpd'
$INSTALL_DIR   = Join-Path $env:ProgramFiles 'jt-snmpd'
$DATA_DIR      = Join-Path $env:ProgramData 'jt-snmpd'
$STATE_DIR     = Join-Path $DATA_DIR 'state'
$LOG_DIR       = Join-Path $DATA_DIR 'logs'
$SECRETS_DIR   = Join-Path $DATA_DIR 'secrets'
$EXE_NAME      = 'jt-snmpd.exe'
$FW_RULE       = 'jt-snmpd (UDP 161)'
$MSSNMP_PARAMS = 'HKLM:\SYSTEM\CurrentControlSet\Services\SNMP\Parameters'

# --- Output (the four-level style from jt-doc-tools) -------------------------
function Log  { param($m) Write-Host "[*] $m" }
function Ok   { param($m) Write-Host "[OK] $m"   -ForegroundColor Green }
function Warn { param($m) Write-Host "[!] $m"    -ForegroundColor Yellow }
function Die  { param($m) Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }

$script:Report = @()
function Report { param($m) $script:Report += $m }

# --- Pre-checks  --------------------------------------------------
function Test-Prerequisites {
    Log 'running pre-checks ...'

    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
              [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Die 'Administrator rights are required. Run this as an administrator.'
    }

    $os = Get-CimInstance Win32_OperatingSystem
    $build = [int]$os.BuildNumber
    if ($build -lt 14393) {
        Die "unsupported OS build ($build). Windows 10 / Server 2016 or newer is required."
    }
    if (-not [Environment]::Is64BitOperatingSystem) { Die 'x64 only.' }
    Ok "OS: $($os.Caption) build $build"

    # Disk space
    $free = (Get-PSDrive -Name ($env:SystemDrive[0]) -ErrorAction SilentlyContinue).Free
    if ($free -and $free -lt 200MB) { Die "not enough free space on the system drive ($([math]::Round($free/1MB)) MB)." }

    # Whatever holds UDP/161
    $ep = Get-NetUDPEndpoint -LocalPort 161 -ErrorAction SilentlyContinue
    if ($ep) {
        foreach ($e in $ep) {
            $p = Get-Process -Id $e.OwningProcess -ErrorAction SilentlyContinue
            if (-not $p) { continue }
            $isMsSnmp = $p.Path -and $p.Path -like "$env:SystemRoot\System32\snmp.exe"
            $isOurs   = $p.Path -and $p.Path -like "$INSTALL_DIR\*"
            if ($isMsSnmp) {
                Log "UDP/161 is held by the built-in Windows SNMP Service (PID $($p.Id)); handling it per §5.9"
            } elseif ($isOurs) {
                Log "UDP/161 is held by an existing $SERVICE_NAME; taking the upgrade path"
            } else {
                # never automatically disable a non-Microsoft service
                Die @"
UDP/161 is held by a non-Microsoft program. Installation stopped; nothing was changed.
    PID      : $($p.Id)
    process   : $($p.ProcessName)
    full path : $($p.Path)
Disable that program manually and run the installer again.
"@
            }
        }
    }
}

# --- Copy the built-in SNMP settings  -----------------------------
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

    # community: the -Community argument wins, otherwise migrate a read-only one
    $community = $Community
    if (-not $community) {
        foreach ($name in $MsCfg.communities.Keys) {
            $access = $MsCfg.communities[$name]
            switch ($access) {
                4  { if (-not $community) { $community = $name } }
                8  { if (-not $community) { $community = $name }
                     Warn "community $name was READ WRITE; downgraded to read-only (this agent does not support SET)"
                     Report "[!] community $name was writable; downgraded to read-only" }
                16 { if (-not $community) { $community = $name }
                     Warn "community $name was READ CREATE; downgraded to read-only" }
                     Report "[!] community $name was writable; downgraded to read-only" }
                default { Log "community $name has access $access (NONE/NOTIFY); not imported" }
            }
        }
    }
    if ($community) {
        Report "[OK] community imported"
        if ($community -in @('public','private')) {
            Warn "community $community is a well-known default; SNMPv3 is strongly recommended"
            Report "[!] community $community is a well-known default; consider SNMPv3"
        }
        Report "[!] this installation enabled SNMPv2c (disabled by default in this agent)"
    }

    # Management networks: the argument wins, otherwise PermittedManagers, which
    # need name resolution
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
                Log "PermittedManagers $m resolved to $ip"
            } else {
                Warn "PermittedManagers $m could not be resolved to an address; skipped"
                Report "[!] PermittedManagers $m could not be resolved; not added to the ACL"
            }
        }
        if ($nets.Count -gt 0) { Report "[OK] imported $($nets.Count) PermittedManagers entries as the source ACL" }
    }

    # An empty PermittedManagers means "accept anything"; never migrate that as Any/Any
    if ($nets.Count -eq 0) {
        Die @"
No management networks were supplied and none could be taken from the existing configuration.
This agent denies by default and does not allow Any/Any.
Specify them with -ManagementNetworks, for example:
    .\install.ps1 -ManagementNetworks 192.168.1.0/24
"@
    }

    return @{ community = $community; networks = $nets }
}

# --- Install -----------------------------------------------------------------
function Stop-ExistingService {
    $svc = Get-Service -Name $SERVICE_NAME -ErrorAction SilentlyContinue
    if (-not $svc) { return $false }
    Log 'existing installation detected; taking the upgrade path ...'
    if ($svc.Status -ne 'Stopped') {
        Stop-Service -Name $SERVICE_NAME -Force -ErrorAction SilentlyContinue
    }
    # Stopping the service does not mean its file handles are released (the actual
    # bug in jt-doc-tools v1.1.66-69)
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
    Log "copying files to $INSTALL_DIR ..."
    if (Test-Path $INSTALL_DIR) {
        # For a .pyd or .dll already loaded as an image Windows returns "access
        # denied": it cannot be deleted, but it can be renamed
            Remove-Item $INSTALL_DIR -Recurse -Force -ErrorAction Stop
        } catch {
            $stamp = Get-Date -Format 'yyyyMMddHHmmss'
            Rename-Item $INSTALL_DIR "$INSTALL_DIR.old.$stamp" -ErrorAction SilentlyContinue
        }
    }
    New-Item -ItemType Directory -Force $INSTALL_DIR | Out-Null
    Copy-Item "$Src\*" $INSTALL_DIR -Recurse -Force
    if (-not (Test-Path (Join-Path $INSTALL_DIR $EXE_NAME))) {
        Die "$EXE_NAME not found after copying"
    }
    Ok "program installed to $INSTALL_DIR"
}

function Initialize-DataDir {
    Log "creating the data directory $DATA_DIR ..."
    foreach ($d in @($DATA_DIR, $STATE_DIR, $LOG_DIR, $SECRETS_DIR)) {
        if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force $d | Out-Null }
    }
    # the default ACL on C:\ProgramData lets Users create
    # subdirectories, so an attacker can create ours first and keep write access.
    # Creating it only when absent is not enough; the ACL has to be reset to
    # SYSTEM and Administrators only.
    $acl.SetAccessRuleProtection($true, $false)      # disable inheritance and drop existing rules
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) { # LocalSystem, Administrators
        $account = (New-Object System.Security.Principal.SecurityIdentifier $sid)
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $account, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-32-544'))
    Set-Acl -Path $DATA_DIR -AclObject $acl
    Ok "data directory ACL set to SYSTEM and Administrators only"
}

function Disable-MsSnmp {
    param($MsCfg)
    if (-not $MsCfg.service_exists) { return $false }
    if ($KeepMsSnmp) {
    Warn '-KeepMsSnmp was given, so the built-in Windows SNMP Service stays (161 will conflict)'
        return $false
    }
    Log 'disabling the built-in Windows SNMP Service (disabled, not removed; reversible) ...'
    Stop-Service -Name SNMP -Force -ErrorAction SilentlyContinue
    Set-Service  -Name SNMP -StartupType Disabled -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Ok "built-in SNMP Service disabled (was $($MsCfg.start_type) / $($MsCfg.status))"
    Report "[OK] built-in Windows SNMP Service disabled (was $($MsCfg.start_type) / $($MsCfg.status))"
    return $true
}

function Register-Service {
    $exe = Join-Path $INSTALL_DIR $EXE_NAME
    Log 'registering the service ...'
    # binPath must be quoted: the default installation path contains a space
    # (the unquoted service path finding)
    if (-not (Get-Service -Name $SERVICE_NAME -ErrorAction SilentlyContinue)) {
        Die 'service registration failed'
    }
    & sc.exe description $SERVICE_NAME 'SNMP agent serving Windows host monitoring data over standard MIBs' | Out-Null
    # Three-stage automatic recovery; failureflag 1 makes a non-zero exit code trigger it too
    & sc.exe failure $SERVICE_NAME reset= 86400 actions= restart/60000/restart/60000/restart/300000 | Out-Null
    & sc.exe failureflag $SERVICE_NAME 1 | Out-Null
    # Privilege reduction
    & sc.exe privs $SERVICE_NAME SeChangeNotifyPrivilege/SeSystemProfilePrivilege/SeIncreaseQuotaPrivilege | Out-Null
    $svc = Get-CimInstance Win32_Service -Filter "Name='$SERVICE_NAME'"
    Ok "service registered: $($svc.StartName) / $($svc.StartMode)"
    if ($svc.PathName -notmatch '^"') { Warn "service ImagePath is not quoted: $($svc.PathName)" }
}

function Set-FirewallRule {
    param($Networks)
    Log 'creating firewall rules ...'
    # Disabling the built-in service also disables its firewall rule, so ours has to be created (measured)
    Remove-NetFirewallRule -DisplayName "$FW_RULE*" -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $FW_RULE -Direction Inbound -Protocol UDP `
        -LocalPort 161 -RemoteAddress $Networks -Action Allow -Profile Any `
        -Description 'jt-snmpd inbound SNMP' | Out-Null
    Ok "firewall rules created (sources limited to $($Networks -join ', '))"
}

function Start-AndVerify {
    param($CommunityName)
    Log 'starting the service ...'
    Start-Service -Name $SERVICE_NAME
    # By default an MSI only confirms the service started, and
    # a service that started is not one that answers SNMP (the "alive but dead"
    # case in §6.5).
    $okResp = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if ((Get-Service -Name $SERVICE_NAME).Status -ne 'Running') { continue }
        if (Test-SnmpLoopback -CommunityName $CommunityName) { $okResp = $true; break }
    }
    if (-not $okResp) {
        Warn 'the service started but did not answer a loopback SNMP query within 30 seconds'
        Warn "check $LOG_DIR\jt-snmpd.log"
        return $false
    }
    Ok 'service started and passed the loopback self-test'
    return $true
}

function Test-SnmpLoopback {
    param($CommunityName)
    # A v2c GET of sysUpTime.0 assembled by hand, so no external SNMP tool is needed
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

    # Restore information. The community is never written here in clear text
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
    $lines += '  Windows SNMP Service migration report'
    $lines += '========================================================'
    $lines += ''
    if ($MsCfg.service_exists -or (Test-Path $MSSNMP_PARAMS)) {
        $lines += 'Source configuration:'
        $lines += '  HKLM\SYSTEM\CurrentControlSet\Services\SNMP\Parameters'
        $lines += ''
        $lines += $script:Report
        if ($MsCfg.sys_contact -or $MsCfg.sys_location) {
            $lines += "[OK]   sysContact and sysLocation carried over"
        }
        if ($MsCfg.sys_services -and $MsCfg.sys_services -ne 76) {
            $lines += "[!]    sysServices was $($MsCfg.sys_services); this agent always reports 76"
        }
        foreach ($t in $MsCfg.trap_destinations) {
            $lines += "[!]    trap destination not migrated, traps will stop being sent: $t"
        }
        foreach ($e in $MsCfg.extension_agents) {
            $lines += "[!]    ExtensionAgent $e not migrated; the OIDs it provided are no longer available"
        }
        $snmptrap = Get-Service -Name SNMPTRAP -ErrorAction SilentlyContinue
        if ($snmptrap) { $lines += "[OK]   the SNMPTRAP service was not changed (currently $($snmptrap.Status))" }
    } else {
        $lines += 'No built-in Windows SNMP Service was detected; nothing to migrate.'
    }
    $lines += ''
    $lines += 'This installation:'
    $lines += "  community        $($Resolved.community)"
    $lines += "  management networks  $($Resolved.networks -join ', ')"
    $lines += ''
    $lines += 'To restore the Windows SNMP Service:'
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
    Write-Host '  jt-snmpd installed'
    Write-Host '========================================================'
    Write-Host ''
    Write-Host "  service       $SERVICE_NAME (automatic start, running)"
    Write-Host "  listening on  ${ips}:161"
    Write-Host "  protocol      SNMPv2c (community: $($Resolved.community))"
    Write-Host "  source ACL    $($Resolved.networks -join ', ')"
    Write-Host ''
    Write-Host "  config file   $DATA_DIR\config.json"
    Write-Host "  log files     $LOG_DIR\"
    Write-Host "  state files   $STATE_DIR\"
    Write-Host "  program dir   $INSTALL_DIR\"
    Write-Host ''
    Write-Host '  Next: after adding this device to LibreNMS, run a discovery once.'
    Write-Host '        Without it LibreNMS only polls and never sees the full OID set.'
    Write-Host '========================================================'
}

# --- Uninstall ---------------------------------------------------------------
function Invoke-Uninstall {
    Log 'uninstalling jt-snmpd ...'
    Stop-ExistingService | Out-Null
    Remove-NetFirewallRule -DisplayName "$FW_RULE*" -ErrorAction SilentlyContinue
    Ok 'firewall rules removed'

    # Restore the built-in SNMP service
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
                    Ok "built-in Windows SNMP Service restored to $orig"
                }
            }
        } catch { Warn "failed to restore the built-in SNMP service: $_" }
    }

    if (Test-Path $INSTALL_DIR) {
        try { Remove-Item $INSTALL_DIR -Recurse -Force -ErrorAction Stop }
        catch { Rename-Item $INSTALL_DIR "$INSTALL_DIR.old" -ErrorAction SilentlyContinue }
        Ok 'program directory removed'
    }

    if ($Purge) {
        if (Test-Path $DATA_DIR) { Remove-Item $DATA_DIR -Recurse -Force -ErrorAction SilentlyContinue }
        Ok 'data directory completely removed (PURGE)'
    } else {
        # keeping ProgramData by default is deliberate. Customers
        # commonly uninstall and reinstall to troubleshoot, and clearing the index
        # map makes LibreNMS rediscover everything, orphaning the existing RRDs.
        Ok "data directory kept: $DATA_DIR (add -Purge to remove it)"
    Write-Host ''
    Ok 'uninstall complete; no reboot required'
}

# --- Main --------------------------------------------------------------------
if ($Uninstall) { Invoke-Uninstall; exit 0 }

if (-not $SourceDir) {
    $SourceDir = Join-Path (Split-Path -Parent $PSCommandPath) 'jt-snmpd'
}
if (-not (Test-Path (Join-Path $SourceDir $EXE_NAME))) {
    Die "$EXE_NAME not found in the source directory: $SourceDir"
}

Write-Host ''
Write-Host "jt-snmpd installer" -ForegroundColor Cyan
Write-Host ''

Test-Prerequisites
$msCfg = Get-MsSnmpConfig
if ($msCfg.service_exists) {
    Log "built-in Windows SNMP Service detected ($($msCfg.status) / $($msCfg.start_type))"
    Log "  community: $($msCfg.communities.Count) entries, PermittedManagers: $($msCfg.permitted_managers.Count)"
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
if (-not $healthy) { Die 'installation finished but the health check did not pass; check the log files' }

Write-MigrationReport -MsCfg $msCfg -Resolved $resolved | Out-Null
Write-Summary -Resolved $resolved
exit 0
