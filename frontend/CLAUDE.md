# frontend/

The dashboard (React + Vite). Implements plan §10 / M5. It is served by the controller
from `frontend/dist` on the same origin and polls `/api/v1` every 1 s. It shows only
what the API returns.

## Public interface
- Views (hash routes): `#Overview` (GET /overview, /events), `#Workers` (GET /workers;
  drain/enable), `#Jobs` (GET /jobs?order=desc; cancel), `#Decisions` (GET /decisions),
  `#Submit` (form generated from GET /catalog; placement from /workers).
- `src/api.js`: `api(path, {method, body})` (relative `/api/v1`, optional `X-Admin-Token`
  from localStorage for remote viewers), `usePoll(path, ms)` (independent polls; keeps
  the last good data on error), formatters.
- Build: `cd frontend; npm ci; npm run build` → `dist/` (setup.ps1 does this). Dev:
  `npm run dev` proxies `/api` to `DWR_DEV_API` (default the local controller on :8000).

## Rules
- No hardcoded tiers, metrics, scores or API addresses in `src/` (AC-5.1). Tier names,
  states and numbers come from API fields. `tests/integration/test_dashboard_api.py`
  greps for violations.
- Never fabricate or smooth data; show `–` or blank when the API has no value.
- A stale worker and a host-busy worker must be visibly marked (AC-5.2). A stale card
  says its figures are the last reported ones.
- One failing poll must not blank the page (AC-5.3).
- No external resources at runtime (system fonts only): the demo may be offline.
- `dist/` and `node_modules/` are not committed.

## Test
`cd frontend; npm run lint; npm run build`, then
`.\.venv\Scripts\python -m pytest tests/integration/test_dashboard_api.py`.
Visual check: docs/evidence/m5.md (headless Edge screenshots).
