# scripts/

Install/run scripts for the two machines (plan §2 Install, §16.1). The legacy
`load_generator.py` and `__init__.py` here are frozen mid-term code; they move to
legacy/ in M5. Do not edit them.

## Public interface
- `setup.sh` (Linux Fog worker): picks Python 3.10–3.12, creates `.venv`, installs
  requirements.txt, prompts for `.env` if absent (mode 600), curls `/api/v1/health`.
  Run: `./scripts/setup.sh`
- Coming: `setup.ps1`, `start.ps1 [-Sim]`, `stop.ps1` (M2), systemd unit
  `deploy/systemd/dwr-worker.service`, `doctor.py`.

## Rules
- Quote every path (the Windows checkout lives under `JAGANNATH MUKHERJEE`).
- `*.sh` must stay LF (.gitattributes); scripts must be re-runnable without damage
  (never overwrite an existing `.env`).
- Secrets only in `.env`, never echoed or committed.

## Test
`bash -n scripts/setup.sh` (syntax). Real run: on the Linux laptop (M1 evidence).
