<#
.SYNOPSIS
  Start the controller and the Edge worker agent on this Windows laptop (plan sec. 2, M2).

.DESCRIPTION
  Each runs as its own hidden background process; PIDs go to var\run\*.pid, logs to
  var\logs\. Refuses to start if a previous instance is still running or the port is taken.

  -Sim also starts a simulated Fog worker ("fog-sim", tier fog) on this machine, on its
  own cores (DWR_SIM_SLOT_CPUS, default: the two highest-numbered free cores), with a
  token generated for this session. Use it to exercise Edge->Fog without the Linux laptop.

.EXAMPLE
  .\scripts\start.ps1
  .\scripts\start.ps1 -Sim
  .\scripts\start.ps1 -EnvFile .\other.env
#>
param(
    [switch]$Sim,
    [string]$EnvFile = '.env'
)
. "$PSScriptRoot\dwr-common.ps1"

$root = Get-DwrRoot
$py = Get-DwrPython $root
if (-not [IO.Path]::IsPathRooted($EnvFile)) { $EnvFile = Join-Path $root $EnvFile }
$cfg = Read-DwrEnv $EnvFile
$port = 8000
if ($cfg['DWR_PORT']) { $port = [int]$cfg['DWR_PORT'] }
$run = Join-Path $root 'var\run'

# ---- refuse double starts --------------------------------------------------------------
$names = @('controller', 'edge')
if ($Sim) { $names += 'fog-sim' }
foreach ($n in $names) {
    $pidFile = Join-Path $run "$n.pid"
    if (Test-Path -LiteralPath $pidFile) {
        $old = [int](Get-Content -LiteralPath $pidFile -Raw)
        if (Test-DwrProcess $old $DwrPatterns[$n]) { throw "$n is already running (PID $old). Run scripts\stop.ps1 first." }
        Remove-Item -LiteralPath $pidFile -Force   # stale
    }
}
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $port is already in use. Run scripts\stop.ps1, or find the owner with: Get-NetTCPConnection -LocalPort $port"
}

# ---- cores ---------------------------------------------------------------------------
$ncpu = [Environment]::ProcessorCount
$edgeCores = Get-DwrCores $cfg['DWR_SLOT_CPUS']
$ctlEnv = @{}
$simCores = @()
if ($Sim) {
    if ($cfg['DWR_SIM_SLOT_CPUS']) { $simSlots = $cfg['DWR_SIM_SLOT_CPUS'] }
    else {
        $free = @((1..($ncpu - 1)) | Where-Object { $edgeCores -notcontains $_ } | Sort-Object -Descending | Select-Object -First 2)
        if ($free.Count -eq 0) { throw 'No free core for fog-sim; set DWR_SIM_SLOT_CPUS in .env' }
        $simSlots = ($free | Sort-Object) -join ';'
    }
    $simCores = Get-DwrCores $simSlots
    $simToken = [guid]::NewGuid().ToString('N')
    $ctlEnv['DWR_WORKER_TOKENS'] = "$($cfg['DWR_WORKER_TOKENS']),fog-sim:$simToken"
}
if (-not $cfg['DWR_CONTROLLER_CPUS']) {
    # Pin the controller away from every slot core (Edge, and fog-sim when simulating).
    $ctlCores = @((0..($ncpu - 1)) | Where-Object { ($edgeCores -notcontains $_) -and ($simCores -notcontains $_) })
    if ($ctlCores.Count -eq 0) { throw 'Slots use every core; leave at least one for the controller.' }
    $ctlEnv['DWR_CONTROLLER_CPUS'] = $ctlCores -join ','
}

# ---- controller ----------------------------------------------------------------------
$envArg = '"' + $EnvFile + '"'
$ctl = Start-DwrProcess 'controller' @('-m', 'controller', '--env-file', $envArg) $ctlEnv $root $py
Write-Host "controller   PID $($ctl.Id)  http://127.0.0.1:$port  (cores $($ctlEnv['DWR_CONTROLLER_CPUS']))"
$deadline = (Get-Date).AddSeconds(25)
while (-not (Get-DwrHealth $port)) {
    if ($ctl.HasExited -or (Get-Date) -gt $deadline) {
        Show-DwrLogTail $root 'controller'
        & "$PSScriptRoot\stop.ps1" -Quiet
        throw 'Controller did not come up (log above).'
    }
    Start-Sleep -Milliseconds 300
}

# ---- workers -------------------------------------------------------------------------
$expect = @()
if ($cfg['DWR_WORKER_ID']) {
    $edge = Start-DwrProcess 'edge' @('-m', 'worker.agent', '--env-file', $envArg) @{} $root $py
    Write-Host "edge agent   PID $($edge.Id)  id=$($cfg['DWR_WORKER_ID'])  slots $($cfg['DWR_SLOT_CPUS'])"
    $expect += $cfg['DWR_WORKER_ID']
} else {
    Write-Warning 'DWR_WORKER_ID not set in the env file: no Edge agent started.'
}
if ($Sim) {
    $simEnv = @{
        'DWR_WORKER_ID' = 'fog-sim'; 'DWR_TIER' = 'fog'; 'DWR_WORKER_TOKEN' = $simToken
        'DWR_CONTROLLER_URL' = "http://127.0.0.1:$port"; 'DWR_SLOT_CPUS' = $simSlots
        'DWR_MEM_BUDGET_MB' = $(if ($cfg['DWR_MEM_BUDGET_MB']) { $cfg['DWR_MEM_BUDGET_MB'] } else { '1536' })
        'DWR_MEM_FLOOR_MB' = $(if ($cfg['DWR_MEM_FLOOR_MB']) { $cfg['DWR_MEM_FLOOR_MB'] } else { '512' })
        'DWR_DATA_DIR' = './var/worker-sim'
    }
    $fog = Start-DwrProcess 'fog-sim' @('-m', 'worker.agent', '--env-file', '-') $simEnv $root $py
    Write-Host "fog-sim      PID $($fog.Id)  id=fog-sim  slots $simSlots  (simulated Fog on this machine)"
    $expect += 'fog-sim'
}

$deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 500
    $h = Get-DwrHealth $port
    $missing = @($expect | Where-Object { -not $h -or $h.fresh_workers -notcontains $_ })
} while ($missing.Count -gt 0 -and (Get-Date) -lt $deadline)
if ($missing.Count -gt 0) {
    foreach ($m in $missing) { if ($m -eq 'fog-sim') { Show-DwrLogTail $root 'fog-sim' } else { Show-DwrLogTail $root 'edge' } }
    Write-Warning "Not heartbeating yet: $($missing -join ', ') (logs above; the stack is left running)."
} else {
    Write-Host "ready: fresh workers = $($h.fresh_workers -join ', ')" -ForegroundColor Green
}
Write-Host "logs: $root\var\logs   stop: .\scripts\stop.ps1"
