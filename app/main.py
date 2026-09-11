import os
import time
import hashlib
import asyncio
from fastapi import FastAPI, Query

# Read tier configuration from environment variables
TIER_NAME = os.getenv("TIER_NAME", "local")

app = FastAPI(
    title=f"Workload Service - {TIER_NAME.upper()}",
    description="Simulated microservice tier for dynamic workload offloading experiments."
)

START_TIME = time.time()

def run_cpu_bound_task(iterations: int) -> dict:
    """
    Executes a CPU-intensive cryptographic hashing loop.
    This simulates computational work (e.g., image processing, AI inference, data compression)
    and forces the host CPU to work, triggering Docker CPU quotas.
    """
    start = time.perf_counter()
    data = b"edge-fog-cloud-workload-simulation"
    
    # Busy-loop doing SHA-256 rounds
    for _ in range(iterations):
        data = hashlib.sha256(data).digest()
        
    duration = time.perf_counter() - start
    return {
        "iterations": iterations,
        "compute_time_seconds": round(duration, 4),
        "checksum": data.hex()[:8]
    }

@app.get("/")
def get_root():
    return {
        "service": "dynamic-workload-tier",
        "tier": TIER_NAME,
        "status": "running"
    }

@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "tier": TIER_NAME,
        "uptime_seconds": round(time.time() - START_TIME, 2)
    }

@app.get("/workload")
@app.post("/workload")
async def process_workload(
    iterations: int = Query(default=150000, description="Number of hashing iterations (controls CPU duration)")
):
    """
    CPU-bound workload endpoint.
    Runs computation in a separate thread so the async event loop can still handle other requests.
    """
    result = await asyncio.to_thread(run_cpu_bound_task, iterations)
    return {
        "tier": TIER_NAME,
        **result
    }
