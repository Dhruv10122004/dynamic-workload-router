"""Workload implementations, one module per catalog type.

IMPLEMENTED is what this worker advertises as supported_types.
"""
IMPLEMENTED: dict[str, str] = {
    "cpu_hash": "worker.workloads.cpu_hash",
    "data_digest": "worker.workloads.data_digest",
    "mem_touch": "worker.workloads.mem_touch",
}
