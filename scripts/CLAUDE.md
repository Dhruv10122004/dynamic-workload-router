# scripts/

Install/run scripts for the two machines (plan §2 Install, §16.1). The mid-term
`load_generator.py` moved to legacy/scripts/ in M5.

## Public interface
- `setup.ps1 [-BindIp IP] [-EdgeSlots "2;4"] [-Port 8000]` (Windows): Python 3.10–3.12
  venv, requirements, dashboard build (`npm ci && npm run build` when Node is present),
  `.env` with fresh tokens (never overwrites), prints IPs, the fog-1 token and the
  firewall command (does not run it).
- `start.ps1 [-Sim] [-EnvFile path]`: controller + Edge agent as hidden background
  processes; PID files `var\run\*.pid`, logs `var\logs\<name>.log`; refuses double starts
  and a busy port; waits for /health and fresh workers. `-Sim` adds a simulated Fog worker
  `fog-sim` on this laptop (`DWR_SIM_SLOT_CPUS`, default two highest free cores; session
  token). Controller is pinned away from every slot core.
- `stop.ps1 [-Quiet]`: kills each PID-file process tree (after checking the command line,
  PIDs get reused), verifies it is gone, kills orphan `worker.workloads` processes.
- `dwr-common.ps1`: helpers dot-sourced by the three scripts.
- `setup.sh` (Linux Fog worker): Python 3.10–3.12, `.venv` (rebuilt if it has no pip), requirements, `.env` prompts
  (mode 600), curls `/api/v1/health`.
- `agent.sh start|stop|restart|status` (Linux): the agent as a detached background process
  (safe over ssh), log `var/logs/agent.log`; found by its working directory, so a hand-started
  agent is recognised; stop kills leftover job processes of this checkout and verifies none remain.
- Coming: systemd unit `deploy/systemd/dwr-worker.service`, `doctor.py`.

## Rules
- Windows PowerShell 5.1: no `&&`, `??`, ternary; ASCII only (BOM-less files read as ANSI).
- Launch Python without `-Redirect*` (it makes children inherit our handles and hangs any
  caller that pipes the script); pass `--log-file` instead (D-013).
- Quote every path (the checkout lives under `JAGANNATH MUKHERJEE`).
- `*.sh` must stay LF (.gitattributes); never overwrite an existing `.env`.
- Secrets only in `.env`, never committed.

## Test
Parse: `[System.Management.Automation.Language.Parser]::ParseFile(...)` on each .ps1;
`bash -n scripts/setup.sh scripts/agent.sh`. Real run: `.\scripts\start.ps1 -Sim` then `.\scripts\stop.ps1`
(evidence in docs/evidence/m2.md).
