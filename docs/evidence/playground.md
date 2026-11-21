# Classroom Playground: evidence (2026-11-21)

Not a PROJECT_PLAN_V1 milestone: an owner-requested addition supporting the M7 demo
(DECISIONS D-027, D-028). Plan: `~/.claude/plans/validated-singing-pudding.md` (approved).
Run by Claude on the real machines: Windows controller + edge-1 (mains), Linux pop-os fog-1
(on battery at the time), phone hotspot; Linux code synced over SSH. Awaiting owner review.

## Automated tests
`.\.venv\Scripts\python -m pytest -q` → 275 passed. New: `tests/integration/test_playground.py`
(catalog; four clicks → Edge, Edge, Fog, queue, then "picked from the queue"; free-play run;
custom validation/start/stop; live lanes with slot, cores and live stats; Fog offline tile
never queued, late result, reconnect pick-up; restart closes the run and cancels its jobs),
`tests/unit/test_playground_unit.py` (deterministic presets, "Without Fog" replays "Edge
saturates", every preset/task within catalog bounds), `test_job_stats_report_live_cpu_and_rss`,
migration of `jobs.slot` on a copy of the real DB. `npm run lint` clean; AC-5.1 scan passes.

## Hands-on clicks (real machines)
Four "Big hash" clicks, "Why here?" headlines as returned by the API:
```
Your 1st click (hash 2M): a slot was free, so edge-1 (edge) took it straight away.
Your 2nd click (hash 2M): a slot was free, so edge-1 (edge) took it straight away.
Your 3rd click (hash 2M): edge-1: all its slots are busy, so fog-1 (fog) took it straight away.
Your 4th click (hash 2M): no laptop could take it (edge-1: all its slots are busy · fog-1: all its
  slots are busy), so it went to the Cloud overflow queue. A Cloud tier would take it here.
#4 later: Picked from the queue by edge-1 after 2.0 s. | Ran on LAPTOP-L5C3UP7P as PID 26472,
  slot 0 (core 2). | Finished: SUCCEEDED, 1.3 s of work, 4.0 s in total.
#3: Ran on pop-os as PID 7082, slot 0 (core 1). | Finished: SUCCEEDED, 2.2 s of work.
```

## Presets (real machines, after the mains-power reference run)
| Preset | Jobs | Done on edge / fog | Peak queue | 95% within | Notes |
|---|---|---|---|---|---|
| Quiet day (0.3×, retuned) | 28 | 24 / 4 | 2 | 4.2 s | wording now says "almost everything on Edge" |
| Edge saturates (1.5×) | 184 | mid-run: queue 10–15, tiles with PIDs | – | – | screenshot `img/playground-saturate.png` |
| Rush hour (2.5× bursts, 45 s, retuned) | 174 | 143 / 31 | 109 | 63.6 s | drained in 110 s (was 400 jobs / 222 s at 3× 90 s) |
| Without Fog (EdgeOnly, same jobs) | 184 | 184 / 0 | 47 | 32.6 s | comparison table populated |
| Fog gets busy | 173 | 150 / 23 | 24 | 14.9 s | only fog-1 marked host_busy (stress via `--follow-runs`) |
| Both laptops busy | 172 | all completed | 65 | – | both host_busy +26 s; queue 40 → 64 while busy; both free at +68 s; picks "after 39.1 s" (edge-1), "39.8 s" (fog-1) |
| Fog disconnects | 253 | all completed, 0 failed | 60 | – | see below |
| Cancel a long job | 2 | – | – | – | edge PID 3388 (89 MB), fog PID 7581 (81 MB): KILLED(cancel), verified in 200 ms / 120 ms |

**Fog disconnects** (Wi-Fi off 23:19:33–23:20:23 by a detached `nmcli` command): cue card
"Now turn OFF the Fog laptop's Wi-Fi" at +20 s; 12 s later fog-1 STALE, its job #25 shown as
"offline: still running there, result pending" (never in the queue), edge-1 working, 16 jobs
queued with "fog-1: offline" (`img/playground-fog-offline.png`). After reconnect: "#25 hash 2M
result arrived after fog-1 reconnected: SUCCEEDED" (transitions PENDING → ASSIGNED → UNKNOWN →
SUCCEEDED), then "#96 … picked from the queue by fog-1 after 13.5 s", #99, #100. fog-1 journal:
100 jobs, max spawns per job = 1.

## Found and fixed during the hardware run
- Lanes looked idle with the battery-measured default timings (jobs finished within one
  poll): ran the reference on mains (`experiments/reference.json`), updated the built-in
  fallback to mains timings.
- Jobs sent to a laptop but not yet started (incl. a data job downloading its input over the
  hotspot) showed below the card while their slot said "idle": they now fill a reserved lane
  with a phase text ("sent to the laptop…", "downloading its 4 MB input…").
- A controller restart mid-activity left its queued jobs running for a closed run: `recover()`
  now cancels them (test added).
- Under "Both laptops busy" the controller laptop itself runs at ~85% CPU (it is Edge); one
  monitoring request was reset once. Documented in D-027.

## Layout clean-up (2026-11-22)
A `.bar` class added for the activity progress bar clashed with the page header's `.bar`, which
squeezed the header to 18 px on every tab. Fixed and restructured: preset grid + "Running now"
panel, even three-column lower area, 2×4 activity numbers, one button style, frozen elapsed time
after an activity ends. Checked at 1366 px and 1920 px (`img/playground-layout-1366.png`).
The Workers view also exposed D-029: own (job) CPU read ~0 and was counted as foreign; fixed
(fog-1 under load: host 52%, own 50%, foreign 2%).
