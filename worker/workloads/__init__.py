"""Workload implementations, one module per catalog type.

IMPLEMENTED is what this worker advertises as supported_types.
"""
IMPLEMENTED: dict[str, str] = {
    "cpu_hash": "worker.workloads.cpu_hash",
}
