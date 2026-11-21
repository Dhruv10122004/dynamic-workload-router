# worker/workloads/

The job programs themselves; each job runs as its own subprocess. Implements plan §4.

## Public interface
- CLI: `python -m worker.workloads <type> --job-id <id> --job-dir <dir> --cpus 1,2 --params '<json>'`
- Exit codes: 0 success, 2 invalid params, 3 cancelled cooperatively, 1 other error.
- Output: `<job-dir>/result.json` (atomic) with `exec_s`, `cpu_s`, `peak_rss_mb`,
  `checksum`, `pid` (the interpreter's own PID), `cpus_affinity` (actual pinning) plus
  type-specific fields: data_digest `bytes_hashed`; mem_touch `touch_s`, `hold_s_actual`,
  `allocated_mb`.
- `__init__.IMPLEMENTED`: type -> module; this is what the agent advertises.
- `common.py`: `JobContext.check_cancel()` / `.sleep()`, `pin()`, `peak_rss_mb()`,
  `write_result()`, exit-code constants.
- Implemented: `cpu_hash`, `data_digest` (reads `<job-dir>/input.bin`, downloaded by the
  supervisor; checksum = chained sha256(prev + data) per round), `mem_touch`
  (`exec_s = touch_s + hold_s_actual`; honours test-only `actual_mb_override`).

## Rules
- Pin to `--cpus` before any timed work.
- Check the `cancel` sentinel at least every 100 ms, including while sleeping/holding.
- Single-threaded, pure Python: no numpy, no thread pools (one slot = one core).
- Re-validate params with `shared.catalog.check_params` (exit 2 on failure). Never import
  pydantic or shared.schemas here: it costs ~0.5 s of start-up per job (D-026).
- Time with `perf_counter` (incl. sleep deadlines): Windows `monotonic` ticks every ~15.6 ms.
- A new type needs: module with `run(ctx, params) -> dict`, entry in `IMPLEMENTED`,
  catalog entry in shared/catalog.py, tests.

## Test
`.\.venv\Scripts\python -m pytest tests/unit/test_workload_cpu_hash.py tests/unit/test_workloads_m2.py`
