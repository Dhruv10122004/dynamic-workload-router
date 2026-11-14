# Design decisions

Decisions not already dictated by PROJECT_PLAN_V1.md. Never rewrite an entry;
supersede it with a new one.

### D-001 (2026-11-14, M0) Python 3.11 venv and pinned versions
Context: §3 requires Python 3.10–3.12 and pinned dependencies; the Windows laptop
has Python 3.11 and an MSYS 3.12 without packages on the Git Bash PATH.
Decision: project venv `.venv` from `py -3.11`; pin the versions already proven on this
machine (fastapi 0.115.12, starlette 0.46.2, uvicorn 0.34.2, pydantic 2.13.3, httpx 0.28.1,
psutil 7.2.2, pytest 9.1.1) in requirements.txt and pyproject.toml. No uvicorn[standard]
extras (fewer native wheels on the old Linux laptop).
Alternatives rejected: unpinned ranges (legacy app/requirements.txt style); poetry/uv
(extra tooling on two machines). Plan §: 3, M0.

### D-002 (2026-11-14, M0) Running the legacy tests from the repo root
Context: AC-0.1; legacy tests are named `tests_telemetry.py` and do `from main import app`.
Decision: pytest `python_files = test_*.py, tests_*.py`, `testpaths = tests, app/Tests`;
root conftest.py appends (not prepends) `app/` to sys.path so it cannot shadow V1 packages.
Alternatives rejected: renaming/editing legacy files (forbidden by AC-0.2). Plan §: M0.

### D-003 (2026-11-14, M0) Runtime data lives under var/
Context: controller DB, worker journal and job dirs must never be committed, but the root
.gitignore belongs to the frozen mid-term baseline.
Decision: all runtime data defaults to `./var/...`; a committed `var/.gitignore` ignores
everything else in it. Plan §: 5.1 (`DWR_DATA_DIR=./var/worker`), 16.1.

### D-004 (2026-11-14, M0/M1) Controller .env keys
Context: §5.1 defines worker keys; §8 says worker tokens are bound to worker_ids "in the
controller's .env" and the controller binds 127.0.0.1 plus a LAN/Tailscale IP, without
naming keys.
Decision: `DWR_WORKER_TOKENS=id:token,id:token`, `DWR_BIND_HOSTS=127.0.0.1,<ip>` (one
socket per host, served by one uvicorn server), `DWR_PORT`, `DWR_ADMIN_TOKEN`,
`DWR_CONTROLLER_DATA_DIR`, `DWR_TEST_MODE`, `DWR_STALE_S`. Controller and Edge agent may
share one `.env`; the agent also takes `--env-file` for running several agents on one host.
Alternatives rejected: a separate controller config file format (one more thing to set up).
Plan §: 3, 5.1, 8.
