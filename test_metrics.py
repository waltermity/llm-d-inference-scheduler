import os
import re
import json
import time
import math
import threading
from typing import List, Tuple, Dict, Optional
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# Config (env override)
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8080")
COMPLETIONS_ENDPOINT = f"{GATEWAY_URL}/v1/completions"
MODEL_NAME = os.getenv("MODEL_NAME", "qwen2-5-0-5b-instruct")

METRICS_URL = os.getenv("METRICS_URL", "http://localhost:9091/metrics")
# If Prometheus text, set to metric name to scrape; if JSON, this is the JSON key or dot.path
METRIC_NAME = os.getenv("METRIC_NAME", "idle_util")

RUNS = int(os.getenv("RUNS", "100"))                  # iterations
PHASE_REQUESTS = int(os.getenv("PHASE_REQUESTS", "1000"))  # requests per phase (standard/premium)
CONCURRENCY = int(os.getenv("CONCURRENCY", "1"))     # worker threads
SAMPLE_INTERVAL = float(os.getenv("SAMPLE_INTERVAL", "0.5"))  # seconds

TIMEOUT_S = float(os.getenv("REQ_TIMEOUT_S", "60"))    # per request timeout

def pct(values: List[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = int((len(s) - 1) * (p / 100.0))
    return s[k]

def parse_prometheus_text(metric_name: str, text: str) -> Optional[float]:
    # Match: metric_name{...} 12.34 or metric_name 12.34
    pattern = re.compile(rf'^{re.escape(metric_name)}(?:\{{[^}}]*\}})?\s+([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*$', re.M)
    m = pattern.search(text)
    return float(m.group(1)) if m else None

def parse_json_path(d: dict, path: str) -> Optional[float]:
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    try:
        return float(cur)
    except Exception:
        return None

def fetch_metric(metrics_url: str, metric_name: str, session: Optional[requests.Session] = None) -> Optional[float]:
    sess = session or requests.Session()
    try:
        r = sess.get(metrics_url, timeout=5)
        ct = r.headers.get("Content-Type", "")
        body = r.text
        # Try Prometheus text first
        val = parse_prometheus_text(metric_name, body)
        if val is not None:
            return val
        # Try JSON fallback
        try:
            j = r.json()
            val = parse_json_path(j, metric_name)
            return val
        except Exception:
            return None
    except Exception:
        return None

class MetricSampler:
    def __init__(self, url: str, name: str, interval: float = 0.5):
        self.url = url
        self.name = name
        self.interval = interval
        self.values = deque()
        self._stop = threading.Event()
        self._thr = None
        self._sess = requests.Session()

    def start(self):
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def stop(self):
        self._stop.set()
        if self._thr:
            self._thr.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            v = fetch_metric(self.url, self.name, self._sess)
            if v is not None and math.isfinite(v):
                self.values.append((time.time(), v))
            time.sleep(self.interval)

    def avg(self) -> float:
        vals = [v for _, v in self.values]
        return sum(vals) / len(vals) if vals else float("nan")

def do_request(session: requests.Session, qos_value: str) -> Tuple[float, float, Optional[str]]:
    headers = {
        "Content-Type": "application/json",
        "x-qos": qos_value,
        "Accept": "application/json",
    }
    payload = {"model": MODEL_NAME, "prompt": "Hi"}

    t0 = time.perf_counter()
    r = session.post(COMPLETIONS_ENDPOINT, headers=headers, json=payload, timeout=TIMEOUT_S, stream=True)
    r.raise_for_status()

    # Time to first byte
    t1 = None
    for chunk in r.iter_content(chunk_size=1):
        if chunk:
            t1 = time.perf_counter()
            break
    if t1 is None:
        t1 = time.perf_counter()

    # Drain
    for _ in r.iter_content(chunk_size=65536):
        pass
    t2 = time.perf_counter()

    pod_hdr = r.headers.get("x-inference-pod")
    return (t1 - t0) * 1000.0, (t2 - t0) * 1000.0, pod_hdr

def run_phase(name: str, qos: str, num_requests: int, concurrency: int) -> Dict:
    session = requests.Session()
    ttfb_ms: List[float] = []
    total_ms: List[float] = []
    per_pod: Dict[str, int] = defaultdict(int)
    errors: int = 0

    sampler = MetricSampler(METRICS_URL, METRIC_NAME, interval=SAMPLE_INTERVAL)
    sampler.start()

    def task():
        nonlocal errors
        try:
            t1, t2, pod = do_request(session, qos)
            ttfb_ms.append(t1)
            total_ms.append(t2)
            if pod:
                per_pod[pod] += 1
        except Exception:
            errors += 1

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(task) for _ in range(num_requests)]
        for _ in as_completed(futures):
            pass

    sampler.stop()

    stats = {
        "phase": name,
        "qos": qos,
        "requests": num_requests,
        "errors": errors,
        "ttfb_ms": ttfb_ms,
        "total_ms": total_ms,
        "per_pod": dict(per_pod),
        "metric_under_load_avg": sampler.avg(),
    }
    return stats

def summarize(label: str, values: List[float]) -> str:
    if not values:
        return f"{label}: n=0"
    return (
        f"{label}: n={len(values)} "
        f"p50={pct(values,50):.1f} p90={pct(values,90):.1f} "
        f"p95={pct(values,95):.1f} p99={pct(values,99):.1f} "
        f"min={min(values):.1f} max={max(values):.1f}"
    )

def main():
    print(f"Gateway: {COMPLETIONS_ENDPOINT}")
    print(f"Metrics: {METRICS_URL} metric={METRIC_NAME}")
    print(f"Runs={RUNS}, per-phase requests={PHASE_REQUESTS}, concurrency={CONCURRENCY}, sample_interval={SAMPLE_INTERVAL}s\n")

    global_ttfb = []
    global_total = []
    global_underload_metrics_std = []
    global_underload_metrics_prem = []
    global_errors = 0
    global_pod_hits: Dict[str, int] = defaultdict(int)

    idle_session = requests.Session()

    for i in range(1, RUNS + 1):
        # Idle metric snapshot before load
        idle_metric = fetch_metric(METRICS_URL, METRIC_NAME, idle_session)

        print(f"[Run {i}/{RUNS}] Idle {METRIC_NAME}={idle_metric} — starting STANDARD x{PHASE_REQUESTS} ...")
        std_stats = run_phase("standard", "standard", PHASE_REQUESTS, CONCURRENCY)
        print(
            f"[Run {i}] STANDARD done | "
            f"{summarize('TTFB(ms)', std_stats['ttfb_ms'])} | "
            f"{summarize('Total(ms)', std_stats['total_ms'])} | "
            f"errors={std_stats['errors']} | "
            f"under-load {METRIC_NAME}~avg={std_stats['metric_under_load_avg']}"
        )

        print(f"[Run {i}] Starting PREMIUM x{PHASE_REQUESTS} ...")
        prem_stats = run_phase("premium", "premium", PHASE_REQUESTS, CONCURRENCY)
        print(
            f"[Run {i}] PREMIUM done | "
            f"{summarize('TTFB(ms)', prem_stats['ttfb_ms'])} | "
            f"{summarize('Total(ms)', prem_stats['total_ms'])} | "
            f"errors={prem_stats['errors']} | "
            f"under-load {METRIC_NAME}~avg={prem_stats['metric_under_load_avg']}"
        )

        # Accumulate
        global_ttfb.extend(std_stats["ttfb_ms"])
        global_ttfb.extend(prem_stats["ttfb_ms"])
        global_total.extend(std_stats["total_ms"])
        global_total.extend(prem_stats["total_ms"])
        global_underload_metrics_std.append(std_stats["metric_under_load_avg"])
        global_underload_metrics_prem.append(prem_stats["metric_under_load_avg"])
        global_errors += std_stats["errors"] + prem_stats["errors"]
        for pod, cnt in std_stats["per_pod"].items():
            global_pod_hits[pod] += cnt
        for pod, cnt in prem_stats["per_pod"].items():
            global_pod_hits[pod] += cnt

    print("\n==== Aggregate Results ====")
    print(summarize("TTFB(ms)", global_ttfb))
    print(summarize("Total(ms)", global_total))
    if global_underload_metrics_std:
        print(
            f"Under-load {METRIC_NAME} (standard): "
            f"avg={sum(global_underload_metrics_std)/len(global_underload_metrics_std):.4f} "
            f"min={min(global_underload_metrics_std):.4f} "
            f"max={max(global_underload_metrics_std):.4f}"
        )
    if global_underload_metrics_prem:
        print(
            f"Under-load {METRIC_NAME} (premium): "
            f"avg={sum(global_underload_metrics_prem)/len(global_underload_metrics_prem):.4f} "
            f"min={min(global_underload_metrics_prem):.4f} "
            f"max={max(global_underload_metrics_prem):.4f}"
        )
    if global_pod_hits:
        print("Per-pod hits:")
        for pod, cnt in sorted(global_pod_hits.items(), key=lambda x: -x[1]):
            print(f"- {pod}: {cnt}")
    print(f"Total errors: {global_errors}")

if __name__ == "__main__":
    main()