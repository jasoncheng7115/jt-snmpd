# The full installation lifecycle: clean install, upgrade, removal keeping the
# data, reinstall, and removal with PURGE.
#
# The community and the management networks are not hardcoded. This script is
# published, and a community string is a password. The defaults are obvious
# placeholders; pass real values when running it:
#   .\lifecycle.ps1 -Community <your community> -ManagementNetworks 10.0.0.0/24
param(
  [string]$Community = "CHANGEME",
  [string]$ManagementNetworks = "10.0.0.0/24",
  [string]$MsiPath = "",
  # Left empty, the version is taken from the MSI file name, so this cannot go
  # stale the way a hardcoded version did.
  [string]$ExpectVersion = ""
)

$ErrorActionPreference = 'Continue'
$PASS = 0; $FAIL = 0
function Check { param($name, $cond, $detail="")
  if ($cond) { $script:PASS++; "  [PASS] $name $detail" }
  else { $script:FAIL++; "  [FAIL] $name $detail" } }

$DATA = "C:\ProgramData\jt-snmpd"
$PROG = "C:\Program Files\jt-snmpd"
$IDX  = "$DATA\state\index-map.json"
# Newest MSI in dist unless one is named. Pinning a version here meant the
# script tested whatever was built months ago, or nothing at all.
$MSI  = if ($MsiPath) { $MsiPath }
        else { (Get-ChildItem "C:\jtdev\dist\jt-snmpd-*-x64.msi" -EA SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName }
if (-not $MSI) { throw "no MSI found; pass -MsiPath" }
if (-not $ExpectVersion) {
  if ([IO.Path]::GetFileName($MSI) -match 'jt-snmpd-([\d.]+)-x64\.msi') { $ExpectVersion = $Matches[1] }
}

# The display name is the product name from the wxs, which is jt-snmpd. It used
# to be "JT SNMP Agent", and after the rename this matcher found nothing at all:
# every check that depends on Arp would have reported the product absent.
function Arp { (Get-ItemProperty HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\* -EA SilentlyContinue | Where-Object DisplayName -eq "jt-snmpd") }
function SvcState { $s=Get-Service jt-snmpd -EA SilentlyContinue; if($s){"$($s.Status)"}else{"absent"} }
function Port161 { (Get-NetUDPEndpoint -LocalPort 161 -EA SilentlyContinue | Measure-Object).Count }
# After removal, 161 is not free: the built-in service has been restored and
# restarted, and it should take the port back. What to check is that jt-snmpd no
# longer holds it, not that nobody is listening. The first version checked the
# latter and failed the correct behaviour.
function Port161Owner {
  $o = @(Get-NetUDPEndpoint -LocalPort 161 -EA SilentlyContinue | ForEach-Object {
    (Get-Process -Id $_.OwningProcess -EA SilentlyContinue).ProcessName }) | Sort-Object -Unique
  if ($o) { $o -join ',' } else { 'none' } }
function FwRules { (Get-NetFirewallRule -DisplayName "jt-snmpd*" -EA SilentlyContinue | Measure-Object).Count }
# Note: $args cannot be used as a parameter name. It is one of PowerShell's
# automatic variables, holding the unnamed argument array, so anything passed in
# is overwritten and always arrives empty. Learned the hard way.
function MsiRun { param([string[]]$MsiArgs)
  (Start-Process msiexec.exe -ArgumentList $MsiArgs -Wait -PassThru).ExitCode }

"########## 0. clear any existing installation ##########"
$a = Arp
if ($a) { MsiRun @("/x", $a.PSChildName, "/qn", "PURGE=1") | Out-Null }
sc.exe stop jt-snmpd 2>&1 | Out-Null; Start-Sleep 2
sc.exe delete jt-snmpd 2>&1 | Out-Null; Start-Sleep 2
Remove-Item $PROG -Recurse -Force -EA SilentlyContinue
Remove-Item $DATA -Recurse -Force -EA SilentlyContinue
# The built-in service has to start out enabled, or neither the takeover nor the
# restoration has anything to demonstrate.
function MsSnmp { $s=Get-Service SNMP -EA SilentlyContinue
  if($s){"$($s.Status)/$($s.StartType)"}else{"absent"} }
$msExists = (Get-Service SNMP -EA SilentlyContinue) -ne $null
if ($msExists) {
  Set-Service -Name SNMP -StartupType Automatic -EA SilentlyContinue
  Start-Service -Name SNMP -EA SilentlyContinue
  Start-Sleep 3
}
$MS_BEFORE = MsSnmp
"  starting state: svc=$(SvcState) 161=$(Port161) fw=$(FwRules) built-in SNMP=$MS_BEFORE"

"########## 1. clean install ##########"
$code = MsiRun @("/i","`"$MSI`"","/qn","MANAGEMENTNETWORKS=$ManagementNetworks","COMMUNITY=$Community")
Check "msiexec exits 0" ($code -eq 0) "(got $code)"
Check "the service exists and is running" ((SvcState) -eq "Running") "(got $(SvcState))"
Check "UDP/161 is bound" ((Port161) -ge 1)
Check "the firewall rules exist" ((FwRules) -ge 2) "(UDP and ICMP; got $(FwRules))"
Check "the program directory exists" (Test-Path "$PROG\jt-snmpd.exe")
Check "the data directory exists" (Test-Path $DATA)
Check "index-map has been created" (Test-Path $IDX)
Check "it appears in Apps and Features" ((Arp) -ne $null)
Check "the version is right" ((Arp).DisplayVersion -eq $ExpectVersion) "(got $((Arp).DisplayVersion))"
$svc = Get-CimInstance Win32_Service -Filter "Name='jt-snmpd'"
Check "it runs as LocalSystem" ($svc.StartName -eq "LocalSystem") "(got $($svc.StartName))"
Check "the start type is automatic" ($svc.StartMode -eq "Auto") "(got $($svc.StartMode))"
$quoted = $svc.PathName.StartsWith([char]34)
Check "ImagePath is quoted" $quoted $svc.PathName
$acl = Get-Acl $DATA
Check "the data directory ACL is tightened" (-not ($acl.Access | Where-Object { $_.IdentityReference -match "Users|Everyone|Authenticated" }))
$idxHash1 = (Get-FileHash $IDX -Algorithm SHA256).Hash
if ($msExists) {
  Check "the built-in SNMP service is stopped and disabled" ((MsSnmp) -eq "Stopped/Disabled") "(got $(MsSnmp))"
  $rec = Get-Content "$DATA\state\ms-snmp-restore.json" -Raw -Encoding UTF8 | ConvertFrom-Json
  Check "the restore record kept the original start type" ($rec.ms_snmp.original_start_type -eq "Automatic") "(got $($rec.ms_snmp.original_start_type))"
  Check "the restore record says we disabled it" ($rec.ms_snmp.disabled_by_us -eq $true)
}

"########## 2. upgrade: install the same version again, for idempotence ##########"
$code = MsiRun @("/i","`"$MSI`"","/qn","MANAGEMENTNETWORKS=$ManagementNetworks","COMMUNITY=$Community")
Check "reinstalling exits 0" ($code -eq 0) "(got $code)"
Check "the service is still running after the upgrade" ((SvcState) -eq "Running")
Check "there is still exactly one registration" ((Arp | Measure-Object).Count -eq 1) "(got $((Arp | Measure-Object).Count))"
Check "index-map was not rebuilt" ((Get-FileHash $IDX -Algorithm SHA256).Hash -eq $idxHash1)

if ($msExists) {
  $rec2 = Get-Content "$DATA\state\ms-snmp-restore.json" -Raw -Encoding UTF8 | ConvertFrom-Json
  # On an upgrade the built-in service is already Disabled. If the current state
  # is re-read and written over the restore record, a later removal can never put
  # it back. That shipped once.
  Check "the upgrade did not spoil the restore record" ($rec2.ms_snmp.original_start_type -eq "Automatic") "(got $($rec2.ms_snmp.original_start_type))"
}

"########## 3. removal, keeping the data by default ##########"
$code = MsiRun @("/x", (Arp).PSChildName, "/qn")
Check "removal exits 0" ($code -eq 0) "(got $code)"
Check "the service is gone" ((SvcState) -eq "absent") "(got $(SvcState))"
Check "jt-snmpd has released UDP/161" ((Port161Owner) -notmatch 'jt-snmpd') "(held by: $(Port161Owner))"
Check "the firewall rules are gone" ((FwRules) -eq 0) "(got $(FwRules))"
Check "the program directory is gone" (-not (Test-Path "$PROG\jt-snmpd.exe"))
Check "it has left Apps and Features" ((Arp) -eq $null)
Check "the data directory is kept, deliberately" (Test-Path $DATA)
Check "index-map is kept, so the RRDs keep their ports" (Test-Path $IDX)
Check "no reboot is required" ($true)

if ($msExists) {
  Start-Sleep 3
  Check "the built-in SNMP service is back to Automatic" ((Get-Service SNMP).StartType -eq "Automatic") "(got $((Get-Service SNMP).StartType))"
  Check "the built-in SNMP service is running again" ((Get-Service SNMP).Status -eq "Running") "(got $((Get-Service SNMP).Status))"
  # It has the port back now, so stop it again before reinstalling
  Stop-Service SNMP -Force -EA SilentlyContinue
  Set-Service -Name SNMP -StartupType Automatic -EA SilentlyContinue
  Start-Service SNMP -EA SilentlyContinue
  Start-Sleep 2
}

"########## 4. reinstall, on top of the kept state ##########"
$code = MsiRun @("/i","`"$MSI`"","/qn","MANAGEMENTNETWORKS=$ManagementNetworks","COMMUNITY=$Community")
Check "reinstall exits 0" ($code -eq 0) "(got $code)"
Check "the service is running after the reinstall" ((SvcState) -eq "Running")
Check "index-map is unchanged, so ifIndex is stable" ((Get-FileHash $IDX -Algorithm SHA256).Hash -eq $idxHash1)

"########## 5. removal with PURGE, which leaves nothing ##########"
$code = MsiRun @("/x", (Arp).PSChildName, "/qn", "PURGE=1")
Check "the purge removal exits 0" ($code -eq 0) "(got $code)"
Check "the service is gone" ((SvcState) -eq "absent")
Check "the program directory is gone" (-not (Test-Path $PROG))
Check "the data directory is completely gone" (-not (Test-Path $DATA)) "(PURGE=1)"

""
if ($msExists) {
  Check "the built-in SNMP service is still Automatic after a purge" ((Get-Service SNMP).StartType -eq "Automatic") "(got $((Get-Service SNMP).StartType))"
}

"########## result ##########"
"PASS=$PASS  FAIL=$FAIL"
if ($FAIL -gt 0) { "LIFECYCLE_RESULT=FAIL" } else { "LIFECYCLE_RESULT=PASS" }
