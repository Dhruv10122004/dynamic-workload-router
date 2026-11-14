<#
.SYNOPSIS
  Stop everything start.ps1 started, verify it is gone, and remove leftover job processes.

.DESCRIPTION
  For each var\run\*.pid: checks the PID still runs a DWR command line (PIDs get reused),
  kills its whole process tree (the venv launcher, the interpreter and any job
  subprocesses), waits until it is gone, and deletes the PID file. Then kills any orphan
  `worker.workloads ... --job-id` process. Jobs that were running are not recovered
  (plan sec. 5.5: they end FAILED(agent_restart) once restart recovery lands in M4).
#>
param([switch]$Quiet)
. "$PSScriptRoot\dwr-common.ps1"

$root = Get-DwrRoot
$run = Join-Path $root 'var\run'
$stopped = @()

if (Test-Path -LiteralPath $run) {
    # Workers first, controller last.
    $files = @(Get-ChildItem -LiteralPath $run -Filter '*.pid' | Sort-Object { $_.BaseName -eq 'controller' })
    foreach ($f in $files) {
        $name = $f.BaseName
        $procId = [int](Get-Content -LiteralPath $f.FullName -Raw)
        $pattern = $DwrPatterns[$name]
        if (-not $pattern) { $pattern = '-m (controller|worker\.agent)' }
        if (Test-DwrProcess $procId $pattern) {
            & taskkill.exe /PID $procId /T /F 2>&1 | Out-Null
            $deadline = (Get-Date).AddSeconds(5)
            while ((Get-Process -Id $procId -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
                Start-Sleep -Milliseconds 100
            }
            if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
                Write-Warning "$name (PID $procId) is still alive after kill; PID file kept."
                continue
            }
            $stopped += "$name($procId)"
        } elseif (-not $Quiet) {
            Write-Host "${name}: PID $procId not running (stale PID file)"
        }
        Remove-Item -LiteralPath $f.FullName -Force
    }
}

# Orphan job processes (e.g. the agent was killed by other means).
$orphans = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match '-m worker\.workloads' -and $_.CommandLine -match '--job-id' })
foreach ($o in $orphans) {
    & taskkill.exe /PID $o.ProcessId /T /F 2>&1 | Out-Null
    Write-Host "killed orphan job process PID $($o.ProcessId)"
}

if (-not $Quiet) {
    if ($stopped.Count) { Write-Host "stopped: $($stopped -join ', ')" -ForegroundColor Green }
    else { Write-Host 'nothing was running' }
    $left = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match '-m (controller|worker\.agent|worker\.workloads)' })
    if ($left.Count) { Write-Warning "DWR processes still running: $(($left | ForEach-Object ProcessId) -join ', ')" }
}
