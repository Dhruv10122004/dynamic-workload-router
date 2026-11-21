# legacy/: the mid-term prototype

The mid-term demo (simulated Edge/Fog/Cloud services, a scored router, a load generator
and Docker files), moved here unchanged with `git mv` in M5 (plan §11), so its history
is kept. The exact pre-V1 tree is also tagged `midterm-baseline`.

Not used by V1. Its one test still runs with the main suite (`legacy/app/Tests`). The
mid-term dashboard is in git history: `git show midterm-baseline:frontend/src/App.jsx`.

Run it as before, from this folder:
- `docker compose up` (Dockerfile and compose paths are relative to `legacy/`)
- `python run_demo.py`
