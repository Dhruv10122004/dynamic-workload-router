# Shared helpers for setup.ps1 / start.ps1 / stop.ps1. Windows PowerShell 5.1 compatible.
# Dot-source it:  . "$PSScriptRoot\dwr-common.ps1"

$ErrorActionPreference = 'Stop'

function Get-DwrRoot { (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }

function Get-DwrPython([string]$Root) {
    $py = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $py)) { throw "No venv at '$Root\.venv'. Run scripts\setup.ps1 first." }
    return $py
}

# KEY=VALUE lines into an ordered hashtable (same rules as shared/envfile.py, minus env overlay).
function Read-DwrEnv([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "Env file not found: '$Path'. Run scripts\setup.ps1 or copy .env.example." }
    $h = [ordered]@{}
    foreach ($raw in [IO.File]::ReadAllLines($Path)) {
        $line = $raw.Trim()
        if ($line -eq '' -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
        $i = $line.IndexOf('=')
        $value = $line.Substring($i + 1)
        $hash = $value.IndexOf(' #')
        if ($hash -ge 0) { $value = $value.Substring(0, $hash) }
        $h[$line.Substring(0, $i).Trim()] = $value.Trim().Trim('"').Trim("'")
    }
    return $h
}

# "1;2" or "1,2;3" -> int[] of every core named.
function Get-DwrCores([string]$Spec) {
    if (-not $Spec) { return @() }
    return @($Spec -split '[;,]' | Where-Object { $_.Trim() -ne '' } | ForEach-Object { [int]$_.Trim() })
}

# Is $ProcessId alive and running a command line that matches $Pattern?
function Test-DwrProcess([int]$ProcessId, [string]$Pattern) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    return ($null -ne $p -and $p.CommandLine -match $Pattern)
}

# Start python with $Arguments in the repo, hidden, logs redirected, extra env vars set
# only for the child. Returns the Process. ($Arguments must already be quoted as needed.)
function Start-DwrProcess([string]$Name, [string[]]$Arguments, [hashtable]$Env, [string]$Root, [string]$Python) {
    $logs = Join-Path $Root 'var\logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $saved = @{}
    foreach ($k in $Env.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process') }
    try {
        foreach ($k in $Env.Keys) { [Environment]::SetEnvironmentVariable($k, $Env[$k], 'Process') }
        # No -Redirect* here: redirection makes the child inherit this shell's handles, so a
        # caller piping start.ps1's output would wait forever. Python writes its own log.
        $logArg = '"' + (Join-Path $logs "$Name.log") + '"'
        $proc = Start-Process -FilePath $Python -ArgumentList ($Arguments + @('--log-file', $logArg)) `
            -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    }
    $run = Join-Path $Root 'var\run'
    New-Item -ItemType Directory -Force -Path $run | Out-Null
    [IO.File]::WriteAllText((Join-Path $run "$Name.pid"), "$($proc.Id)")
    return $proc
}

function Get-DwrHealth([int]$Port) {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 2 }
    catch { return $null }
}

function Show-DwrLogTail([string]$Root, [string]$Name) {
    $log = Join-Path $Root "var\logs\$Name.log"
    if (Test-Path -LiteralPath $log) {
        Write-Host "---- last lines of $log ----" -ForegroundColor Yellow
        Get-Content -LiteralPath $log -Tail 15 | ForEach-Object { Write-Host "  $_" }
    }
}

$DwrPatterns = @{
    'controller' = '-m controller'
    'edge'       = '-m worker\.agent'
    'fog-sim'    = '-m worker\.agent'
}
