"""Agent restart recovery (plan §5.5). Runs once at startup, before the first heartbeat.

Jobs do not survive an agent restart. For every non-terminal journal row:
1. kill its surviving process (same pid + create_time + `--job-id <id>`), verified;
2. kill any workload process of this agent's jobs directory that is still alive
   (e.g. a crash between spawn and journal step 2), verified;
3. record the outcome: the real one if a complete result.json exists, else
   FAILED(agent_restart).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from shared.states import JobState
from worker import proctree
from worker.journal import Journal
from worker.supervisor import read_result

log = logging.getLogger("dwr.recovery")


@dataclass
class RecoveryReport:
    killed: dict[str, bool] = field(default_factory=dict)   # job_id -> termination verified
    outcomes: dict[str, str] = field(default_factory=dict)  # job_id -> terminal state recorded


def _journaled_process(row) -> psutil.Process | None:
    """The process recorded in step 2, if that exact process still runs this job."""
    if row["pid"] is None:
        return None
    try:
        p = psutil.Process(row["pid"])
        if row["pid_create_time"] is not None and abs(p.create_time() - row["pid_create_time"]) > 0.01:
            return None  # pid reused by another process
        cmd = p.cmdline()
    except psutil.Error:
        return None
    return p if "--job-id" in cmd and row["job_id"] in cmd else None


def recover(journal: Journal, jobs_dir: Path) -> RecoveryReport:
    report = RecoveryReport()
    rows = journal.nonterminal_rows()

    # Steps 1 and 2: every workload process of ours, journaled or not, dies (verified).
    targets: dict[str, list[psutil.Process]] = {}
    for row in rows:
        p = _journaled_process(row)
        if p is not None:
            targets.setdefault(row["job_id"], []).extend(proctree.tree(p))
    for job_id, procs in proctree.find_job_processes(jobs_dir).items():
        for p in procs:
            targets.setdefault(job_id, []).extend(proctree.tree(p))
    for job_id, procs in targets.items():
        unique = list({p.pid: p for p in procs}.values())
        verified = proctree.kill_verified(unique)
        report.killed[job_id] = verified
        log.warning("recovery: killed leftover processes of job %s %s (verified=%s)",
                    job_id, [p.pid for p in unique], verified)

    # Step 3: outcomes.
    for row in rows:
        job_id = row["job_id"]
        job_dir = Path(row["job_dir"]) if row["job_dir"] else jobs_dir / job_id
        result = read_result(job_dir)
        verified = report.killed.get(job_id)
        extra = {} if verified is None else {"termination_verified": int(verified)}
        if result is not None and row["state"] == JobState.RUNNING.value:
            journal.mark_terminal(job_id, JobState.SUCCEEDED, result=result, **extra)
            report.outcomes[job_id] = JobState.SUCCEEDED.value
        else:
            note = "agent restarted; job not resumed"
            if verified is not None:
                note += f"; leftover process killed (verified={verified})"
            journal.mark_terminal(job_id, JobState.FAILED, fail_reason="agent_restart", error=note,
                                  result=result, **extra)
            report.outcomes[job_id] = JobState.FAILED.value
        log.warning("recovery: job %s (%s at restart) -> %s", job_id, row["state"], report.outcomes[job_id])
    return report
