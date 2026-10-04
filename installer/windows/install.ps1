<#
.SYNOPSIS
    FreeCAD collab installer for Windows.

.DESCRIPTION
    Installs the FreeCAD plugin for the current user and/or the collaboration
    server as a background service (a Scheduled Task running as LOCAL SERVICE
    at startup). Run install.cmd to get an interactive menu.

.EXAMPLE
    .\install.ps1 -Action Plugin -ServerUrl http://192.168.1.10:14008
.EXAMPLE
    .\install.ps1 -Action Server -Port 14008 -OpenFirewall
.EXAMPLE
    .\install.ps1 -Action UninstallServer -Purge
.EXAMPLE
    .\install.ps1 -Action Agent -AgentToken fcc_... -ApiKey sk-ant-...
#>
[CmdletBinding()]
param(
    [ValidateSet('', 'Plugin', 'Server', 'All', 'Agent', 'UninstallPlugin', 'UninstallServer', 'UninstallAgent')]
    [string]$Action = '',
    [ValidateRange(1, 65535)]
    [int]$Port = 14008,
    [string]$BindAddress = '0.0.0.0',
    [string]$ServerUrl = '',
    [string]$ModDir = '',
    [string]$AgentToken = '',
    [ValidateSet('', 'anthropic', 'openai')]
    [string]$Provider = '',
    [string]$Model = '',
    [string]$ApiKey = '',
    [string]$BaseUrl = '',
    [switch]$OpenFirewall,
    [switch]$Purge,
    [switch]$Pause
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3

$AppName    = 'freecad-collab-server'
$TaskName   = 'FreeCAD Collab Server'
$RuleName   = 'FreeCAD Collab Server'
$PluginName = 'CollabSync'
$InstallDir = Join-Path $env:ProgramFiles 'FreeCADCollab'
$DataDir    = Join-Path $env:ProgramData 'FreeCADCollab'
$EnvFile    = Join-Path $DataDir 'collab.env'
$ExePath    = Join-Path $InstallDir "$AppName.exe"
$AgentDir   = Join-Path $InstallDir 'agent'
$AgentEnv   = Join-Path $DataDir 'agent.env'
$AgentTask  = 'FreeCAD Collab Agent'

# Well-known SIDs, so this works on localized Windows (e.g. German).
$SidSystem       = 'S-1-5-18'
$SidLocalService = 'S-1-5-19'
$SidAdmins       = 'S-1-5-32-544'

function Write-Step([string]$Text) { Write-Host $Text -ForegroundColor Cyan }
function Write-Done([string]$Text) { Write-Host $Text -ForegroundColor Green }

function Test-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-AccountName([string]$Sid) {
    (New-Object Security.Principal.SecurityIdentifier($Sid)).Translate([Security.Principal.NTAccount]).Value
}

function Find-First([string[]]$Candidates, [string]$What) {
    foreach ($c in $Candidates) {
        if (Test-Path -LiteralPath $c) { return (Resolve-Path -LiteralPath $c).Path }
    }
    throw "$What not found. Use a release bundle, or build first with 'cargo build --release'."
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Get-EnvValue([string]$Key) {
    if (-not (Test-Path -LiteralPath $EnvFile)) { return $null }
    $line = Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match "^$Key=" } | Select-Object -Last 1
    if ($line) { return $line.Substring($Key.Length + 1) }
    return $null
}

function Set-EnvValue([string]$Key, [string]$Value, [string]$Path = $EnvFile) {
    # Replaces the first "KEY=" or commented "# KEY=" line, else appends.
    $lines = @(Get-Content -LiteralPath $Path)
    $found = $false
    $lines = @($lines | ForEach-Object {
        if (-not $found -and $_ -match "^#? ?$Key=") { $found = $true; "$Key=$Value" } else { $_ }
    })
    if (-not $found) { $lines += "$Key=$Value" }
    Write-Utf8NoBom $Path (($lines -join "`r`n") + "`r`n")
}

function Protect-DataDir {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    # Only SYSTEM, Administrators and the service account can read (config holds secrets).
    & icacls.exe $DataDir /inheritance:r /grant:r "*${SidSystem}:(OI)(CI)F" "*${SidAdmins}:(OI)(CI)F" "*${SidLocalService}:(OI)(CI)M" /T /Q | Out-Null
}

function New-HexToken {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

function Get-LanAddress {
    try {
        $ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
            Sort-Object -Property InterfaceMetric |
            Select-Object -First 1
        if ($ip) { return $ip.IPAddress }
    } catch { }
    return '127.0.0.1'
}

function Stop-Server {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
    Get-Process -Name $AppName -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $ExePath } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
}

# ------------------------------------------------------------------ server

function Install-Server {
    $bin = Find-First @(
        (Join-Path $PSScriptRoot "$AppName.exe"),
        (Join-Path $PSScriptRoot "..\..\target\release\$AppName.exe")
    ) "$AppName.exe"

    Write-Step "1/5 Installing $ExePath"
    Stop-Server
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -LiteralPath $bin -Destination $ExePath -Force
    & icacls.exe $InstallDir /grant "*${SidLocalService}:(OI)(CI)RX" /T /Q | Out-Null

    Write-Step "2/5 Preparing $DataDir"
    Protect-DataDir

    Write-Step "3/5 Writing configuration"
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        $dbUrl = 'sqlite://' + ((Join-Path $DataDir 'collab.db') -replace '\\', '/')
        $text = @(
            '# FreeCAD collab server configuration. Restart after editing:',
            "#   Stop-ScheduledTask '$TaskName'; Start-ScheduledTask '$TaskName'",
            '# Paths use forward slashes: backslashes are escape characters in this file.',
            "COLLAB_BIND_ADDR=${BindAddress}:$Port",
            "COLLAB_DATABASE_URL=$dbUrl",
            "COLLAB_ADMIN_TOKEN=$(New-HexToken)",
            "COLLAB_LOG_FILE=$((Join-Path $DataDir 'server.log') -replace '\\', '/')",
            'COLLAB_LOG_FORMAT=pretty',
            'RUST_LOG=info,tower_http=warn,sqlx=warn'
        ) -join "`r`n"
        Write-Utf8NoBom $EnvFile ($text + "`r`n")
    } else {
        Write-Host "    keeping existing $EnvFile"
        if ($PSBoundParameters.ContainsKey('Port') -or $PSBoundParameters.ContainsKey('BindAddress')) {
            Set-EnvValue 'COLLAB_BIND_ADDR' "${BindAddress}:$Port"
        }
    }
    $bind = Get-EnvValue 'COLLAB_BIND_ADDR'
    $listenPort = [int]($bind.Split(':')[-1])

    Write-Step "4/5 Registering the background service"
    $account   = Get-AccountName $SidLocalService
    $action    = New-ScheduledTaskAction -Execute $ExePath -Argument "--env-file `"$EnvFile`" serve" -WorkingDirectory $DataDir
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType ServiceAccount
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description 'FreeCAD collaboration server' -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    if ($OpenFirewall) {
        Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP `
            -LocalPort $listenPort -Program $ExePath -Profile Domain, Private | Out-Null
        Write-Host "    firewall: opened TCP $listenPort for private and domain networks"
    }

    Write-Step "5/5 Checking the server"
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$listenPort/readyz"
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if ($ready) { Write-Host '    server is ready' }
    else { Write-Warning "The server did not answer. Check $(Join-Path $DataDir 'server.log')" }

    $url = "http://$(Get-LanAddress):$listenPort"
    Write-Host ''
    Write-Done 'Server installed.'
    Write-Host "  Admin console: $url/admin"
    Write-Host "  Admin token:   $(Get-EnvValue 'COLLAB_ADMIN_TOKEN')"
    Write-Host "  Config:        $EnvFile"
    Write-Host "  Logs:          $(Join-Path $DataDir 'server.log')"
    if (-not $OpenFirewall) {
        Write-Host "  Other computers need TCP $listenPort open; re-run with -OpenFirewall to do that."
    }
    Write-Host ''
    Write-Host "Next: open the admin console, add a user, and give them the token plus this address: $url"
    return $listenPort
}

function Uninstall-Server {
    Write-Step 'Removing the server'
    Stop-Server
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    if (Test-Path -LiteralPath $InstallDir) { Remove-Item -LiteralPath $InstallDir -Recurse -Force }
    if ($Purge) {
        if (Test-Path -LiteralPath $DataDir) { Remove-Item -LiteralPath $DataDir -Recurse -Force }
        Write-Done 'Server removed, including all documents and configuration.'
    } else {
        Write-Done "Server removed. Documents and config kept in $DataDir (use -Purge to delete)."
    }
}

# ------------------------------------------------------------------ agent

function Install-Agent {
    $run = Find-First @(
        (Join-Path $PSScriptRoot 'agent\run_agent.py'),
        (Join-Path $PSScriptRoot '..\..\agent\run_agent.py')
    ) 'Agent folder'
    $src = Split-Path -Parent $run
    $plugin = Find-First @(
        (Join-Path $PSScriptRoot $PluginName),
        (Join-Path $PSScriptRoot "..\..\plugin\$PluginName")
    ) "Plugin folder $PluginName"

    Write-Step "1/4 Installing the AI agent into $AgentDir"
    if (Get-ScheduledTask -TaskName $AgentTask -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $AgentTask -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    if (Test-Path -LiteralPath $AgentDir) { Remove-Item -LiteralPath $AgentDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $AgentDir | Out-Null
    foreach ($item in 'collab_agent', 'run_agent.py', 'collab-agent.cmd') {
        Copy-Item -LiteralPath (Join-Path $src $item) -Destination $AgentDir -Recurse -Force
    }
    Copy-Item -LiteralPath $plugin -Destination (Join-Path $AgentDir $PluginName) -Recurse -Force
    Get-ChildItem -LiteralPath $AgentDir -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
    & icacls.exe $InstallDir /grant "*${SidLocalService}:(OI)(CI)RX" /T /Q | Out-Null

    Write-Step "2/4 Writing $AgentEnv"
    Protect-DataDir
    if (-not (Test-Path -LiteralPath $AgentEnv)) {
        Copy-Item -LiteralPath (Join-Path $src 'agent.env.example') -Destination $AgentEnv
        if (-not $ServerUrl -and (Test-Path -LiteralPath $EnvFile)) {
            $ServerUrl = "http://127.0.0.1:$((Get-EnvValue 'COLLAB_BIND_ADDR').Split(':')[-1])"
        }
    } else {
        Write-Host "    keeping existing values in $AgentEnv"
    }
    $values = [ordered]@{
        COLLAB_SERVER_URL = $ServerUrl; COLLAB_AGENT_TOKEN = $AgentToken; COLLAB_AGENT_PROVIDER = $Provider
        COLLAB_AGENT_MODEL = $Model; COLLAB_AGENT_API_KEY = $ApiKey; COLLAB_AGENT_BASE_URL = $BaseUrl
    }
    foreach ($k in $values.Keys) { if ($values[$k]) { Set-EnvValue $k $values[$k] $AgentEnv } }

    Write-Step '3/4 Looking for FreeCAD'
    $fc = Get-ChildItem -Path (Join-Path $env:ProgramFiles 'FreeCAD*\bin\FreeCADCmd.exe') -ErrorAction SilentlyContinue |
        Sort-Object FullName | Select-Object -Last 1
    if ($fc) { Write-Host "    using $($fc.FullName)" }
    else { Write-Warning 'FreeCADCmd.exe was not found. Install FreeCAD on this computer before starting the agent.' }

    Write-Step '4/4 Registering the background task'
    $log = Join-Path $DataDir 'agent.log'
    $cmdLine = "/c `"`"$(Join-Path $AgentDir 'collab-agent.cmd')`" `"$AgentEnv`" >> `"$log`" 2>&1`""
    $action    = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmdLine -WorkingDirectory $DataDir
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId (Get-AccountName $SidLocalService) -LogonType ServiceAccount
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $AgentTask -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description 'FreeCAD collaboration AI agent' -Force | Out-Null

    $ready = [bool](Get-EnvValueFrom $AgentEnv 'COLLAB_AGENT_TOKEN')
    if ((Get-EnvValueFrom $AgentEnv 'COLLAB_AGENT_PROVIDER') -ne 'openai' -and -not (Get-EnvValueFrom $AgentEnv 'COLLAB_AGENT_API_KEY')) { $ready = $false }
    Write-Host ''
    Write-Done 'AI agent installed.'
    if ($ready -and $fc) {
        Start-ScheduledTask -TaskName $AgentTask
        Write-Host '  The agent is running.'
    } else {
        Write-Host "  Not started yet: set COLLAB_AGENT_TOKEN and your model settings in $AgentEnv,"
        Write-Host "  then run: Start-ScheduledTask '$AgentTask'"
    }
    Write-Host "  Log: $log"
    Write-Host ''
    Write-Host 'Next: invite the agent to a document, then write @<agent name> in the Collab chat.'
}

function Get-EnvValueFrom([string]$Path, [string]$Key) {
    $line = Get-Content -LiteralPath $Path | Where-Object { $_ -match "^$Key=" } | Select-Object -Last 1
    if ($line) { return $line.Substring($Key.Length + 1) }
    return $null
}

function Uninstall-Agent {
    Write-Step 'Removing the AI agent'
    Unregister-ScheduledTask -TaskName $AgentTask -Confirm:$false -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $AgentDir) { Remove-Item -LiteralPath $AgentDir -Recurse -Force }
    if ($Purge -and (Test-Path -LiteralPath $AgentEnv)) { Remove-Item -LiteralPath $AgentEnv -Force }
    Write-Done 'AI agent removed.'
}

# ------------------------------------------------------------------ plugin

function Get-ModDirs {
    if ($ModDir) { return @($ModDir) }
    $base = Join-Path $env:APPDATA 'FreeCAD'
    $dirs = @()
    if (Test-Path -LiteralPath $base) {
        $dirs += (Join-Path $base 'Mod')
        # FreeCAD >= 1.1 keeps per-version folders such as v1-1.
        Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^v\d+-\d+$' } |
            ForEach-Object { $dirs += (Join-Path $_.FullName 'Mod') }
    }
    if ($dirs.Count -eq 0) { $dirs = @(Join-Path $base 'Mod') }
    return $dirs | Select-Object -Unique
}

function Install-Plugin([string]$Url) {
    if ($Url -and $Url -notmatch '^https?://\S+$') { throw 'The server address must start with http:// or https://' }
    $src = Find-First @(
        (Join-Path $PSScriptRoot $PluginName),
        (Join-Path $PSScriptRoot "..\..\plugin\$PluginName")
    ) "Plugin folder $PluginName"

    Write-Step 'Installing the FreeCAD plugin'
    foreach ($d in Get-ModDirs) {
        $dest = Join-Path $d $PluginName
        New-Item -ItemType Directory -Force -Path $d | Out-Null
        if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
        Copy-Item -LiteralPath $src -Destination $dest -Recurse -Force
        Get-ChildItem -LiteralPath $dest -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
        $defaults = Join-Path $dest 'collab_sync\defaults.json'
        if (Test-Path -LiteralPath $defaults) { Remove-Item -LiteralPath $defaults -Force }
        if ($Url) { Write-Utf8NoBom $defaults ((@{ server_url = $Url } | ConvertTo-Json -Compress) + "`n") }
        Write-Host "    installed $dest"
    }
    Write-Done "Plugin installed for $env:USERNAME."
    Write-Host 'Next: restart FreeCAD, pick the Collab workbench, open Collab > Settings and paste your token.'
}

function Uninstall-Plugin {
    foreach ($d in Get-ModDirs) {
        $dest = Join-Path $d $PluginName
        if (Test-Path -LiteralPath $dest) {
            Remove-Item -LiteralPath $dest -Recurse -Force
            Write-Host "    removed $dest"
        }
    }
    Write-Done 'Plugin removed.'
}

# ------------------------------------------------------------------ main

function Invoke-Elevated([string]$ElevatedAction) {
    # Server steps need administrator rights; re-launch just those elevated.
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
                 '-Action', $ElevatedAction, '-Port', $Port, '-BindAddress', $BindAddress, '-Pause')
    if ($OpenFirewall) { $argList += '-OpenFirewall' }
    foreach ($pair in @(@('-ServerUrl', $ServerUrl), @('-AgentToken', $AgentToken), @('-Provider', $Provider),
                        @('-Model', $Model), @('-ApiKey', $ApiKey), @('-BaseUrl', $BaseUrl))) {
        if ($pair[1]) { $argList += @($pair[0], "`"$($pair[1])`"") }
    }
    if ($Purge) { $argList += '-Purge' }
    $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "The elevated installer failed (exit code $($p.ExitCode))." }
}

function Show-Menu {
    Write-Host 'FreeCAD collab installer'
    Write-Host '  1) Install the plugin for FreeCAD (for everyone who collaborates)'
    Write-Host '  2) Install the server (one computer only, needs administrator rights)'
    Write-Host '  3) Install both'
    Write-Host '  4) Uninstall the plugin'
    Write-Host '  5) Uninstall the server'
    Write-Host '  6) Install the AI agent (needs administrator rights and FreeCAD on this computer)'
    Write-Host '  7) Uninstall the AI agent'
    $choice = Read-Host 'Choose 1-7'
    switch ($choice) {
        '1' { $script:ServerUrl = Read-Host 'Server address (e.g. http://192.168.1.10:14008, empty to skip)'; return 'Plugin' }
        '2' { $fw = Read-Host 'Allow other computers through Windows Firewall? (y/n)'; $script:OpenFirewall = [bool]($fw -match '^[yYjJ]'); return 'Server' }
        '3' { $fw = Read-Host 'Allow other computers through Windows Firewall? (y/n)'; $script:OpenFirewall = [bool]($fw -match '^[yYjJ]'); return 'All' }
        '4' { return 'UninstallPlugin' }
        '5' { return 'UninstallServer' }
        '6' {
            $script:AgentToken = Read-Host 'Agent token (from the admin console; empty to fill in later)'
            $script:ApiKey = Read-Host 'Anthropic API key (empty to fill in later or to use another provider)'
            return 'Agent'
        }
        '7' { return 'UninstallAgent' }
        default { throw "No such choice: $choice" }
    }
}

$exitCode = 0
try {
    if (-not $Action) { $Action = Show-Menu }
    $needsAdmin = $Action -in @('Server', 'UninstallServer', 'Agent', 'UninstallAgent')
    switch ($Action) {
        'Plugin'          { Install-Plugin $ServerUrl }
        'UninstallPlugin' { Uninstall-Plugin }
        'All' {
            # Plugin goes into this user's profile, so install it before elevating.
            if (-not $ServerUrl) { $ServerUrl = "http://127.0.0.1:$Port" }
            Install-Plugin $ServerUrl
            if (Test-Admin) { [void](Install-Server) } else { Invoke-Elevated 'Server' }
        }
        default {
            if ($needsAdmin -and -not (Test-Admin)) { Invoke-Elevated $Action }
            elseif ($Action -eq 'Server') { [void](Install-Server) }
            elseif ($Action -eq 'Agent') { Install-Agent }
            elseif ($Action -eq 'UninstallAgent') { Uninstall-Agent }
            else { Uninstall-Server }
        }
    }
} catch {
    Write-Host "error: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}
if ($Pause) { [void](Read-Host 'Press Enter to close') }
exit $exitCode
