"""Regenerate every table and plot of a block from its run directories alone (plan §12.4-12.7, AC-6.2).

    python -m experiments.analyze experiments/results/sweep [--slo-factor 1.5] [--slo-fail 0.01]

Reads <block>/**/manifest.json + jobs.csv (+ telemetry.csv, events.csv) and writes
<block>/analysis/: runs.csv (one row per run), summary.csv (mean and sd per strategy and
load), report.md, and PNG plots. Nothing is read from the controller.

Definitions (DECISIONS D-025):
- completed = SUCCEEDED; completion_s = end - submit (controller clock).
- failure rate = (FAILED + KILLED) / jobs; queue_timeout is a FAILED. Jobs cancelled at
  the end of the drain are "unfinished" (not completed, not failures).
- overflow_jobs = jobs with no eligible worker at their first attempt; overflow_demand_s =
  their exec_s, or the reference Edge time of their size if they never ran.
- peak_backlog / overflow_time_s from each job's PENDING interval [submit, assign or end].
- SLO (plan §12.5): p95 <= factor x LocalFirst's p95 at the lowest load, and failure rate <= 1%.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from experiments.common import percentile
from experiments.gen_trace import variant_key

TIER_RANK = {"edge": 0, "fog": 1, "cloud": 2}
PER_RUN = ["jobs", "completed", "failed", "unfinished", "failure_rate", "mean_completion_s", "p95_completion_s",
           "mean_queue_wait_s", "p95_queue_wait_s", "makespan_s", "throughput_jobs_per_s", "offload_pct_count",
           "offload_pct_cpu_s", "transfer_bytes", "overflow_jobs", "overflow_demand_s", "overflow_demand_estimated",
           "peak_backlog", "overflow_time_s"]
SUMMARY_METRICS = ["p95_completion_s", "mean_completion_s", "failure_rate", "overflow_jobs", "overflow_demand_s",
                   "peak_backlog", "overflow_time_s", "offload_pct_count", "offload_pct_cpu_s", "throughput_jobs_per_s",
                   "makespan_s"]


def _f(v: str) -> float | None:
    return None if v in ("", None) else float(v)


def load_run(run_dir: Path) -> dict:
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    with open(run_dir / "jobs.csv", encoding="utf-8") as f:
        jobs = list(csv.DictReader(f))
    tel_path, ev_path = run_dir / "telemetry.csv", run_dir / "events.csv"
    telemetry = list(csv.DictReader(open(tel_path, encoding="utf-8"))) if tel_path.is_file() else []
    events = list(csv.DictReader(open(ev_path, encoding="utf-8"))) if ev_path.is_file() else []
    cfg = m["run"]["config"]
    sweep = cfg.get("sweep") or {}
    strategy = sweep.get("strategy") or m["strategy"]["policy"] + ("" if m["strategy"]["host_busy_enabled"] else "-hb-off")
    load = sweep.get("load", m["trace"].get("load"))
    return {"dir": run_dir, "manifest": m, "jobs": jobs, "telemetry": telemetry, "events": events,
            "strategy": strategy, "load": load, "rep": sweep.get("rep"), "run_id": m["run"]["run_id"]}


def backlog(jobs: list[dict], run_end: float) -> tuple[int, float]:
    """(peak PENDING count, time the PENDING queue was non-empty) from per-job intervals."""
    edges = []
    for j in jobs:
        s = _f(j["submit_ts"])
        e = _f(j["assign_ts"]) or _f(j["end_ts"]) or run_end
        if s is not None and e > s:
            edges += [(s, 1), (e, -1)]
    edges.sort(key=lambda x: (x[0], x[1]))   # exits before entries at the same instant
    n = peak = 0
    busy_since, total = None, 0.0
    for t, d in edges:
        n += d
        peak = max(peak, n)
        if n > 0 and busy_since is None:
            busy_since = t
        elif n == 0 and busy_since is not None:
            total += t - busy_since
            busy_since = None
    return peak, total


def first_tier(r: dict) -> str | None:
    tiers = {w["tier"] for w in r["manifest"]["run"]["config"]["workers"]}
    return min(tiers, key=lambda t: TIER_RANK.get(t, 99)) if tiers else None


def run_metrics(r: dict) -> dict:
    m, jobs = r["manifest"], r["jobs"]
    run = m["run"]
    run_end = run["end_ts"] or max((_f(j["end_ts"]) or 0) for j in jobs)
    drained = set(m.get("cancelled_at_drain") or [])
    done = [j for j in jobs if j["state"] == "SUCCEEDED"]
    # A job the harness cancelled at the end of the drain ends CANCELLED or KILLED(cancel): it is
    # unfinished, not a failure (D-025).
    failed = [j for j in jobs if j["state"] in ("FAILED", "KILLED") and j["job_id"] not in drained]
    comp = [_f(j["completion_s"]) for j in done]
    wait = [_f(j["queue_wait_s"]) for j in done]
    first = first_tier(r)
    off = [j for j in done if j["tier"] and j["tier"] != first]
    cpu_all = sum(_f(j["cpu_s"]) or 0 for j in done)
    ref = ((m.get("reference") or {}).get("mean_exec_s")) or {}
    demand, estimated = 0.0, 0
    for j in jobs:
        if j["overflow"] not in ("1", "True", "true"):
            continue
        if _f(j["exec_s"]) is not None:
            demand += _f(j["exec_s"])
        else:
            est = ref.get(variant_key(j["type"], json.loads(j["params_json"])))
            if est is not None:
                demand += est
                estimated += 1
    peak, busy = backlog(jobs, run_end)
    submits = [_f(j["submit_ts"]) for j in jobs if j["submit_ts"]]
    ends = [_f(j["end_ts"]) for j in done]
    run_len = m.get("run_length_s") or (run_end - run["start_ts"])
    return {
        "strategy": r["strategy"], "load": r["load"], "rep": r["rep"], "run_id": r["run_id"],
        "jobs": len(jobs), "completed": len(done), "failed": len(failed),
        "unfinished": sum(1 for j in jobs if j["job_id"] in drained or j["state"] not in
                          ("SUCCEEDED", "FAILED", "KILLED", "CANCELLED", "REJECTED")),
        "failure_rate": len(failed) / len(jobs) if jobs else None,
        "mean_completion_s": statistics.mean(comp) if comp else None,
        "p95_completion_s": percentile(comp, 95),
        "mean_queue_wait_s": statistics.mean(wait) if wait else None,
        "p95_queue_wait_s": percentile(wait, 95),
        "makespan_s": (max(ends) - min(submits)) if ends and submits else None,
        "throughput_jobs_per_s": len(done) / run_len if run_len else None,
        "offload_pct_count": 100 * len(off) / len(done) if done else None,
        "offload_pct_cpu_s": 100 * sum(_f(j["cpu_s"]) or 0 for j in off) / cpu_all if cpu_all else None,
        "transfer_bytes": sum(int(j["transfer_bytes"] or 0) for j in jobs),
        "overflow_jobs": sum(1 for j in jobs if j["overflow"] in ("1", "True", "true")),
        "overflow_demand_s": demand, "overflow_demand_estimated": estimated,
        "peak_backlog": peak, "overflow_time_s": busy,
        "network": m.get("network"), "median_rtt_ms": json.dumps(m.get("median_rtt_ms")),
        "trace_sha256": m["trace"]["sha256"], "git_commit": m.get("git_commit"),
    }


def summarize(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["strategy"], r["load"])].append(r)
    out = []
    for (strategy, load), rs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        s = {"strategy": strategy, "load": load, "reps": len(rs)}
        for k in SUMMARY_METRICS:
            v = [r[k] for r in rs if r[k] is not None]
            s[f"{k}_mean"] = statistics.mean(v) if v else None
            s[f"{k}_sd"] = statistics.stdev(v) if len(v) > 1 else (0.0 if v else None)
        out.append(s)
    return out


def slo(summary: list[dict], factor: float, max_fail: float) -> tuple[float | None, dict]:
    lf = [s for s in summary if s["strategy"] == "LocalFirst" and s["p95_completion_s_mean"] is not None]
    if not lf:
        return None, {}
    base = min(lf, key=lambda s: s["load"])["p95_completion_s_mean"]
    verdict = {}
    for s in summary:
        ok = (s["p95_completion_s_mean"] is not None and s["p95_completion_s_mean"] <= factor * base
              and (s["failure_rate_mean"] or 0) <= max_fail)
        verdict[(s["strategy"], s["load"])] = ok
    return base, verdict


def l_star(summary: list[dict], verdict: dict, strategy: str) -> float | None:
    """Highest load such that this strategy is acceptable at it and at every lower load."""
    best = None
    for s in sorted((s for s in summary if s["strategy"] == strategy), key=lambda s: s["load"]):
        if not verdict.get((strategy, s["load"])):
            break
        best = s["load"]
    return best


def ablation_rows(runs: list[dict]) -> list[dict]:
    out = []
    for r in runs:
        stress = r["manifest"]["run"]["config"].get("stress")
        if not stress:
            continue
        start = r["manifest"]["run"]["start_ts"]
        w0, w1 = start + stress["at_s"], start + stress["at_s"] + stress["for_s"]
        jobs = r["jobs"]
        later = [j for j in jobs if j["tier"] and j["tier"] != first_tier(r)]
        in_win = [j for j in later if _f(j["assign_ts"]) is not None and w0 <= _f(j["assign_ts"]) < w1]
        ex_in = [_f(j["exec_s"]) for j in in_win if j["state"] == "SUCCEEDED"]
        ex_out = [_f(j["exec_s"]) for j in later if j not in in_win and j["state"] == "SUCCEEDED"]
        busy_ev = [(e["kind"], float(e["ts"]) - start, e["worker_id"]) for e in r["events"]
                   if e["kind"] in ("host_busy", "host_free")]
        m = run_metrics(r)
        out.append({"strategy": r["strategy"], "rep": r["rep"], "run_id": r["run_id"],
                    "p95_completion_s": m["p95_completion_s"], "failure_rate": m["failure_rate"],
                    "offloaded_jobs_in_stress_window": len(in_win),
                    "offloaded_exec_s_in_window_mean": statistics.mean(ex_in) if ex_in else None,
                    "offloaded_exec_s_outside_mean": statistics.mean(ex_out) if ex_out else None,
                    "host_busy_events": json.dumps([(k, round(t, 1), w) for k, t, w in busy_ev]),
                    "stress_window_s": f"{stress['at_s']}-{stress['at_s'] + stress['for_s']}"})
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def plots(out: Path, summary: list[dict], runs: list[dict]) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    made = []
    strategies = sorted({s["strategy"] for s in summary})
    loads = sorted({s["load"] for s in summary if s["load"] is not None})
    if len(loads) > 1:
        for metric, label in (("p95_completion_s", "p95 completion time (s)"),
                              ("overflow_demand_s", "overflow demand (slot-s, Cloud would need)"),
                              ("failure_rate", "failure rate"),
                              ("offload_pct_cpu_s", "local offload (% of CPU-s off Edge)"),
                              ("peak_backlog", "peak backlog (jobs)")):
            fig, ax = plt.subplots(figsize=(6.4, 4))
            for st in strategies:
                pts = sorted((s["load"], s[f"{metric}_mean"], s[f"{metric}_sd"]) for s in summary
                             if s["strategy"] == st and s[f"{metric}_mean"] is not None)
                if pts:
                    x, y, e = zip(*pts)
                    ax.errorbar(x, y, yerr=e, marker="o", capsize=3, label=st)
            ax.set_xlabel("load (x Edge capacity)")
            ax.set_ylabel(label)
            ax.grid(alpha=0.3)
            ax.legend()
            fig.tight_layout()
            name = f"{metric}_vs_load.png"
            fig.savefig(out / name, dpi=120)
            plt.close(fig)
            made.append(name)
    for r in runs:   # host CPU timelines for stressed runs (ablation)
        stress = r["manifest"]["run"]["config"].get("stress")
        if not stress or not r["telemetry"]:
            continue
        start = r["manifest"]["run"]["start_ts"]
        fig, ax = plt.subplots(figsize=(7, 3.2))
        by_w = defaultdict(list)
        for t in r["telemetry"]:
            by_w[t["worker_id"]].append((float(t["ts"]) - start, _f(t["foreign_cpu_pct"]), t["host_busy"] == "1"))
        for i, (w, pts) in enumerate(sorted(by_w.items())):
            line, = ax.plot([p[0] for p in pts], [p[1] for p in pts], label=f"{w} foreign CPU %")
            busy = [p[0] for p in pts if p[2]]
            if busy:   # one marker row per worker, so rows never hide each other
                ax.scatter(busy, [101 - 4 * i] * len(busy), marker="|", color=line.get_color(),
                           label=f"{w} host_busy")
        ax.axvspan(stress["at_s"], stress["at_s"] + stress["for_s"], alpha=0.15, color="red", label="stress")
        ax.set_xlabel("s since run start")
        ax.set_ylim(0, 105)
        ax.legend(fontsize=7, loc="upper right")
        ax.set_title(f"{r['strategy']} rep {r['rep']}", fontsize=9)
        fig.tight_layout()
        name = f"timeline_{r['strategy']}_rep{r['rep']}.png"
        fig.savefig(out / name, dpi=110)
        plt.close(fig)
        made.append(name)
    return made


def _fmt(v, nd=2):
    if v is None:
        return "–"
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def report(out: Path, block: Path, runs: list[dict], rows: list[dict], summary: list[dict], base, verdict,
           abl: list[dict], images: list[str], factor: float, max_fail: float) -> None:
    lines = [f"# Results: {block.name}", "",
             f"{len(runs)} runs read from `{block}` (generated by experiments/analyze.py; do not edit).", ""]
    nets = sorted({str(r["network"]) for r in rows})
    commits = sorted({str(r["git_commit"]) for r in rows})
    plugged = {(r["manifest"]["run"]["config"].get("harness_power") or {}).get("plugged_in") for r in runs}
    power = [{True: "mains", False: "BATTERY", None: "unknown"}[p] for p in sorted(plugged, key=str)]
    lines += [f"- network: {', '.join(nets)}", f"- git commit(s): {', '.join(commits)}",
              f"- controller/Edge laptop power: {'; '.join(power)}",
              f"- repetitions per cell: {sorted({s['reps'] for s in summary})}", ""]
    lines += ["## Summary (mean ± sd over repetitions)", "",
              "| strategy | load | reps | p95 s | mean s | failure % | overflow jobs | overflow demand s | "
              "peak backlog | overflow time s | offload % CPU-s | SLO |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in summary:
        ok = verdict.get((s["strategy"], s["load"]))
        lines.append(
            f"| {s['strategy']} | {s['load']} | {s['reps']} | {_fmt(s['p95_completion_s_mean'])} ± "
            f"{_fmt(s['p95_completion_s_sd'])} | {_fmt(s['mean_completion_s_mean'])} | "
            f"{_fmt(None if s['failure_rate_mean'] is None else 100 * s['failure_rate_mean'], 1)} | "
            f"{_fmt(s['overflow_jobs_mean'], 1)} | {_fmt(s['overflow_demand_s_mean'], 1)} ± "
            f"{_fmt(s['overflow_demand_s_sd'], 1)} | {_fmt(s['peak_backlog_mean'], 1)} | "
            f"{_fmt(s['overflow_time_s_mean'], 1)} | {_fmt(s['offload_pct_cpu_s_mean'], 1)} | "
            f"{'–' if ok is None else ('yes' if ok else 'no')} |")
    lines.append("")
    if base is not None:
        lines += [f"SLO baseline: LocalFirst p95 at the lowest load = {base:.2f} s; acceptable = p95 ≤ "
                  f"{factor} × baseline ({factor * base:.2f} s) and failure rate ≤ {100 * max_fail:.0f}% "
                  f"(plan §12.5, values to be confirmed by the owner).", ""]
        for st in sorted({s["strategy"] for s in summary}):
            lines.append(f"- {st}: acceptable up to load L* = {_fmt(l_star(summary, verdict, st))}")
        lines.append("")
    eo = {s["load"]: s for s in summary if s["strategy"] == "EdgeOnly"}
    lf = {s["load"]: s for s in summary if s["strategy"] == "LocalFirst"}
    common = sorted(set(eo) & set(lf))
    if common:
        lines += ["## Overflow absorbed by adding Fog (LocalFirst vs EdgeOnly)", "",
                  "| load | EdgeOnly demand s | LocalFirst demand s | absorbed % |", "|---|---|---|---|"]
        for L in common:
            a, b = eo[L]["overflow_demand_s_mean"], lf[L]["overflow_demand_s_mean"]
            pct = 100 * (1 - b / a) if a else None
            lines.append(f"| {L} | {_fmt(a, 1)} | {_fmt(b, 1)} | {_fmt(pct, 1)} |")
        lines += ["", "Overflow demand counts each overflow job's exec_s (or its reference Edge time if it never "
                  "ran); it approximates Cloud slot-seconds, and Cloud speed differs (plan §12.4).", ""]
    if abl:
        lines += ["## Host-busy ablation", "",
                  "| strategy | rep | p95 s | failure % | offloaded jobs in stress window | offloaded exec_s in window | "
                  "outside | host_busy events (kind, s, worker) |", "|---|---|---|---|---|---|---|---|"]
        for a in abl:
            lines.append(f"| {a['strategy']} | {a['rep']} | {_fmt(a['p95_completion_s'])} | "
                         f"{_fmt(None if a['failure_rate'] is None else 100 * a['failure_rate'], 1)} | "
                         f"{a['offloaded_jobs_in_stress_window']} | {_fmt(a['offloaded_exec_s_in_window_mean'])} | "
                         f"{_fmt(a['offloaded_exec_s_outside_mean'])} | {a['host_busy_events']} |")
        lines.append("")
    if images:
        lines += ["## Plots", ""] + [f"![{i}]({i})" for i in images] + [""]
    lines += ["## Caveats", "",
              "- Two physical machines; results describe this setup, not a general claim.",
              "- Fog transfers depend on the network recorded above.",
              "- Host-busy detection lags 5–10 s; short CPU spikes are invisible (plan §7.2).", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tables and plots for one results block")
    ap.add_argument("block", type=Path)
    ap.add_argument("--slo-factor", type=float, default=1.5)
    ap.add_argument("--slo-fail", type=float, default=0.01)
    ap.add_argument("--no-plots", action="store_true")
    a = ap.parse_args(argv)
    run_dirs = sorted(p.parent for p in a.block.rglob("manifest.json") if "analysis" not in p.parts)
    if not run_dirs:
        print(f"no run directories under {a.block}")
        return 1
    runs = [load_run(d) for d in run_dirs]
    rows = [run_metrics(r) for r in runs]
    summary = summarize(rows)
    base, verdict = slo(summary, a.slo_factor, a.slo_fail)
    abl = ablation_rows(runs)
    out = a.block / "analysis"
    out.mkdir(exist_ok=True)
    write_csv(out / "runs.csv", rows)
    write_csv(out / "summary.csv", summary)
    write_csv(out / "ablation.csv", abl)
    images = [] if a.no_plots else plots(out, summary, runs)
    report(out, a.block, runs, rows, summary, base, verdict, abl, images, a.slo_factor, a.slo_fail)
    print(f"{len(runs)} runs -> {out} (runs.csv, summary.csv, report.md, {len(images)} plots)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
