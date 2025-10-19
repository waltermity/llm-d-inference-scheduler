import requests, time
from collections import defaultdict

gateway_url = "http://localhost:30080"
completion_endpoint = f"{gateway_url}/v1/completions"

def pct(values, p):
    if not values:
        return float("nan")
    s = sorted(values)
    k = int((len(s)-1) * (p/100.0))
    return s[k]

def run_load(qos_value: str, runs: int = 20):
    ttfb_ms, total_ms = [], []
    per_pod = defaultdict(int)

    for i in range(runs):
        headers = {
            "Content-Type": "application/json",
            "x-qos": qos_value,
            "Accept": "application/json",
        }
        payload = {"model": "food-review", "prompt": "How are you today?"}

        t0 = time.perf_counter()
        resp = requests.post(completion_endpoint, headers=headers, json=payload, timeout=60, stream=True)
        resp.raise_for_status()

        # time-to-first-byte
        t1 = None
        for chunk in resp.iter_content(chunk_size=1):
            if chunk:
                t1 = time.perf_counter()
                break
        if t1 is None:
            t1 = time.perf_counter()

        # read the rest to measure total latency
        for _ in resp.iter_content(chunk_size=65536):
            pass
        t2 = time.perf_counter()

        # optional pod identity (if provided by upstream)
        pod_hdr = resp.headers.get("x-inference-pod")
        if pod_hdr:
            per_pod[pod_hdr] += 1

        ttfb_ms.append((t1 - t0) * 1000.0)
        total_ms.append((t2 - t0) * 1000.0)

    print(f"\nQoS={qos_value} results over {runs} runs")
    print(f"- TTFB ms:   p50={pct(ttfb_ms,50):.1f}ms p95={pct(ttfb_ms,95):.1f}ms min={min(ttfb_ms):.1f}ms max={max(ttfb_ms):.1f}ms")
    print(f"- Total ms:  p50={pct(total_ms,50):.1f}ms p95={pct(total_ms,95):.1f}ms min={min(total_ms):.1f}ms max={max(total_ms):.1f}ms")
    if per_pod:
        print("- Pod hits:")
        for pod, cnt in per_pod.items():
            print(f"  {pod}: {cnt}")

if __name__ == "__main__":
    run_load("standard", runs=30)
    run_load("premium", runs=30)