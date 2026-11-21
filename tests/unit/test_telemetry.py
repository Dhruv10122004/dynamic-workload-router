"""Own (DWR job) CPU measurement (plan §5.2, D-029): a job must count from its first sample."""
import subprocess
import sys
import time

import psutil

from worker.telemetry import TelemetrySampler, foreign_cpu_pct

BURN = "import time\nt = time.perf_counter()\nwhile time.perf_counter() - t < 3: pass\n"


def test_new_job_counts_from_its_first_sample():
    ncpu = psutil.cpu_count() or 1
    one_core = 100.0 / ncpu
    sampler = TelemetrySampler()
    p = subprocess.Popen([sys.executable, "-c", BURN])
    try:
        root = psutil.Process(p.pid)
        time.sleep(1.0)
        first = sampler.own_cpu_pct([root])      # the old cpu_percent() approach returned 0 here
        time.sleep(1.0)
        second = sampler.own_cpu_pct([root])
    finally:
        p.kill()
        p.wait()
    assert first > 0.4 * one_core, (first, one_core)
    assert 0.6 * one_core < second <= 1.1 * one_core, (second, one_core)


def test_no_jobs_means_no_own_cpu_and_foreign_is_the_rest():
    sampler = TelemetrySampler()
    time.sleep(0.2)
    assert sampler.own_cpu_pct([]) == 0.0
    assert foreign_cpu_pct(40.0, 15.0) == 25.0 and foreign_cpu_pct(10.0, 15.0) == 0.0
