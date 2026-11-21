import asyncio
import httpx
import time

TARGET_URL = "http://localhost:8002/workload?iterations=200000"
STATS_URL = "http://localhost:8002/stats"
TOTAL_REQUESTS = 30
CONCURRENCY_LIMIT = 5

async def send_workload(client: httpx.AsyncClient, req_id: int, semaphore: asyncio.Semaphore):
    async with semaphore:
        start = time.time()
        try:
            response = await client.post(TARGET_URL, timeout=30.0)
            elapsed = round((time.time() - start) * 1000, 2)
            print(f"[Req {req_id:02d}] Status: {response.status_code} | Duration: {elapsed}ms")
        except Exception as e:
            print(f"[Req {req_id:02d}] Failed: {e}")

async def monitor_stats(client: httpx.AsyncClient, stop_event: asyncio.Event):
    """Polls /stats while the load test is actively running."""
    while not stop_event.is_set():
        try:
            res = await client.get(STATS_URL, timeout=5.0)
            if res.status_code == 200:
                stats = res.json()
                print(
                    f"--> [TELEMETRY SNAPSHOT] Active: {stats['active_requests']} | "
                    f"CPU: {stats['cpu_percent']}% | Avg Latency: {stats['avg_latency_ms']}ms"
                )
        except Exception:
            pass
        await asyncio.sleep(0.5)

async def main():
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    stop_monitoring = asyncio.Event()

    print(f"Starting load test against Fog tier ({TARGET_URL})...")
    async with httpx.AsyncClient() as client:
        # Start background metrics poller
        monitor_task = asyncio.create_task(monitor_stats(client, stop_monitoring))

        # Dispatch burst of concurrent workloads
        tasks = [send_workload(client, i + 1, semaphore) for i in range(TOTAL_REQUESTS)]
        await asyncio.gather(*tasks)

        # Stop telemetry poller
        stop_monitoring.set()
        await monitor_task

    print("Load test completed.")

if __name__ == "__main__":
    asyncio.run(main())