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
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:30080")
COMPLETIONS_ENDPOINT = f"{GATEWAY_URL}/v1/completions"
MODEL_NAME = os.getenv("MODEL_NAME", "food-review")
# Adjust to correct port
METRICS_API_BASE = os.getenv("METRICS_API_BASE", "http://127.0.0.1:8081")
NAMESPACE = os.getenv("NAMESPACE", "default")
EPP_NAME_CONTAINS = os.getenv("EPP_NAME_CONTAINS", "endpoint-picker")

METRICS_URL = os.getenv("METRICS_URL", "http://localhost:8081/metrics")
METRIC_NAME = os.getenv("METRIC_NAME", "idle_util")

RUNS = int(os.getenv("RUNS", "100"))
PHASE_REQUESTS = int(os.getenv("PHASE_REQUESTS", "10"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "1"))
SAMPLE_INTERVAL = float(os.getenv("SAMPLE_INTERVAL", "0.5"))
TIMEOUT_S = float(os.getenv("REQ_TIMEOUT_S", "60"))

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

# NEW: quantity parsers (cpu -> mcores, mem -> MiB)
def parse_cpu_to_mcores(q: str) -> float:
    # supports: n (nano), m (milli), cores (no suffix)
    q = q.strip()
    if q.endswith("n"):
        return float(q[:-1]) / 1e6
    if q.endswith("m"):
        return float(q[:-1])
    # plain cores or float
    return float(q) * 1000.0

def parse_mem_to_mib(q: str) -> float:
    q = q.strip()
    # binary units
    if q.endswith("Ki"):
        return float(q[:-2]) / 1024.0
    if q.endswith("Mi"):
        return float(q[:-2])
    if q.endswith("Gi"):
        return float(q[:-2]) * 1024.0
    if q.endswith("Ti"):
        return float(q[:-2]) * 1024.0 * 1024.0
    # decimal units
    if q.endswith("K"):
        return float(q[:-1]) / (1024.0 / 1000.0)
    if q.endswith("M"):
        return float(q[:-1]) * (1000.0 / 1024.0)
    if q.endswith("G"):
        return float(q[:-1]) * (1000.0 / 1024.0) * 1000.0
    # bytes
    if q.endswith("B"):
        return float(q[:-1]) / (1024.0 * 1024.0)
    # assume MiB
    try:
        return float(q)
    except Exception:
        return float("nan")

def fetch_pods_metrics(api_base: str, namespace: str, session: Optional[requests.Session] = None) -> List[dict]:
    """Return list of pod metrics entries from metrics.k8s.io for a namespace."""
    sess = session or requests.Session()
    url = f"{api_base}/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods"
    r = sess.get(url, timeout=5)
    r.raise_for_status()
    j = r.json()
    return j.get("items", [])

def summarize_group_avgs(items: List[dict], name_contains: Optional[str] = None, label_eq: Optional[Tuple[str, str]] = None) -> Tuple[float, float, int]:
    """
    Compute average cpu(mcores) and mem(MiB) across matched pods.
    Returns (avg_cpu_mcores, avg_mem_mib, matched_pods_count).
    """
    total_cpu = 0.0
    total_mem = 0.0
    count = 0
    for it in items:
        name = it.get("metadata", {}).get("name", "")
        labels = it.get("metadata", {}).get("labels", {}) or {}
        if name_contains and name_contains not in name:
            continue
        if label_eq:
            k, v = label_eq
            if labels.get(k) != v:
                continue
        # sum containers in pod
        pod_cpu = 0.0
        pod_mem = 0.0
        for ctr in it.get("containers", []):
            usage = ctr.get("usage", {}) or {}
            cpu_q = usage.get("cpu")
            mem_q = usage.get("memory")
            if cpu_q:
                pod_cpu += parse_cpu_to_mcores(cpu_q)
            if mem_q:
                pod_mem += parse_mem_to_mib(mem_q)
        total_cpu += pod_cpu
        total_mem += pod_mem
        count += 1
    if count == 0:
        return float("nan"), float("nan"), 0
    return total_cpu / count, total_mem / count, count

class K8sPhaseMetricsSampler:
    """Samples metrics.k8s.io for EPP and per-QoS backend pods during a phase."""
    def __init__(self, api_base: str, namespace: str, epp_name_contains: str, qos_value: str, interval: float = 0.5):
        self.api_base = api_base
        self.namespace = namespace
        self.epp_name_contains = epp_name_contains
        self.qos_value = qos_value
        self.interval = interval
        self._stop = threading.Event()
        self._thr = None
        self._sess = requests.Session()
        self.epp_samples = []     # (ts, cpu_mcores, mem_mib, pods)
        self.qos_samples = []     # (ts, cpu_mcores, mem_mib, pods)

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
            try:
                items = fetch_pods_metrics(self.api_base, self.namespace, self._sess)
                epp_cpu, epp_mem, epp_n = summarize_group_avgs(items, name_contains=self.epp_name_contains)
                qos_cpu, qos_mem, qos_n = summarize_group_avgs(items, label_eq=("llm-d.ai/qos", self.qos_value))
                now = time.time()
                if epp_n > 0 and math.isfinite(epp_cpu):
                    self.epp_samples.append((now, epp_cpu, epp_mem, epp_n))
                if qos_n > 0 and math.isfinite(qos_cpu):
                    self.qos_samples.append((now, qos_cpu, qos_mem, qos_n))
            except Exception:
                pass
            time.sleep(self.interval)

    def _avg(self, samples: List[tuple], idx: int) -> float:
        vals = [s[idx] for s in samples]
        return sum(vals) / len(vals) if vals else float("nan")

    def epp_cpu_avg(self) -> float: return self._avg(self.epp_samples, 1)
    def epp_mem_avg(self) -> float: return self._avg(self.epp_samples, 2)
    def qos_cpu_avg(self) -> float: return self._avg(self.qos_samples, 1)
    def qos_mem_avg(self) -> float: return self._avg(self.qos_samples, 2)

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

    # sample k8s metrics for EPP and QoS pods during the phase
    k8s_sampler = K8sPhaseMetricsSampler(
        METRICS_API_BASE, NAMESPACE, EPP_NAME_CONTAINS, qos, interval=SAMPLE_INTERVAL
    )
    k8s_sampler.start()

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

    k8s_sampler.stop()

    stats = {
        "phase": name,
        "qos": qos,
        "requests": num_requests,
        "errors": errors,
        "ttfb_ms": ttfb_ms,
        "total_ms": total_ms,
        "per_pod": dict(per_pod),
        # averages during load
        "epp_cpu_mcores_avg": k8s_sampler.epp_cpu_avg(),
        "epp_mem_mib_avg": k8s_sampler.epp_mem_avg(),
        "qos_cpu_mcores_avg": k8s_sampler.qos_cpu_avg(),
        "qos_mem_mib_avg": k8s_sampler.qos_mem_avg(),
    }
    return stats

def summarize(label: str, values: List[float]) -> str:
    if not values:
        return f"{label}: n=0"
    return (
        f"{label}: n={len(values)} "
        f"p50={pct(values,50):.1f}ms p90={pct(values,90):.1f}ms "
        f"p95={pct(values,95):.1f}ms p99={pct(values,99):.1f}ms "
        f"min={min(values):.1f}ms max={max(values):.1f}ms"
    )

def main():
    print(f"Gateway: {COMPLETIONS_ENDPOINT}")
    print(f"Metrics API: {METRICS_API_BASE} (ns={NAMESPACE}) — EPP contains='{EPP_NAME_CONTAINS}'")
    print(f"Runs={RUNS}, per-phase requests={PHASE_REQUESTS}, concurrency={CONCURRENCY}, sample_interval={SAMPLE_INTERVAL}s\n")

    std_ttfb: List[float] = []
    std_total: List[float] = []
    std_errors = 0
    std_pod_hits: Dict[str, int] = defaultdict(int)

    prem_ttfb: List[float] = []
    prem_total: List[float] = []
    prem_errors = 0
    prem_pod_hits: Dict[str, int] = defaultdict(int)

    for i in range(1, RUNS + 1):
        print(f"[Run {i}/{RUNS}] Starting STANDARD x{PHASE_REQUESTS} ...")
        std_stats = run_phase("standard", "standard", PHASE_REQUESTS, CONCURRENCY)
        print(
            f"[Run {i}] STANDARD done | "
            f"{summarize('TTFB(ms)', std_stats['ttfb_ms'])} | "
            f"{summarize('Total(ms)', std_stats['total_ms'])} | "
            f"errors={std_stats['errors']} | "
            f"EPP(avg): cpu={std_stats['epp_cpu_mcores_avg']:.1f}m, mem={std_stats['epp_mem_mib_avg']:.1f}Mi | "
            f"QoS(avg): cpu={std_stats['qos_cpu_mcores_avg']:.1f}m, mem={std_stats['qos_mem_mib_avg']:.1f}Mi"
        )

        print(f"[Run {i}] Starting PREMIUM x{PHASE_REQUESTS} ...")
        prem_stats = run_phase("premium", "premium", PHASE_REQUESTS, CONCURRENCY)
        print(
            f"[Run {i}] PREMIUM done | "
            f"{summarize('TTFB(ms)', prem_stats['ttfb_ms'])} | "
            f"{summarize('Total(ms)', prem_stats['total_ms'])} | "
            f"errors={prem_stats['errors']} | "
            f"EPP(avg): cpu={prem_stats['epp_cpu_mcores_avg']:.1f}m, mem={prem_stats['epp_mem_mib_avg']:.1f}Mi | "
            f"QoS(avg): cpu={prem_stats['qos_cpu_mcores_avg']:.1f}m, mem={prem_stats['qos_mem_mib_avg']:.1f}Mi"
        )

        # Accumulate per QoS
        std_ttfb.extend(std_stats["ttfb_ms"])
        std_total.extend(std_stats["total_ms"])
        std_errors += std_stats["errors"]
        for pod, cnt in std_stats["per_pod"].items():
            std_pod_hits[pod] += cnt

        prem_ttfb.extend(prem_stats["ttfb_ms"])
        prem_total.extend(prem_stats["total_ms"])
        prem_errors += prem_stats["errors"]
        for pod, cnt in prem_stats["per_pod"].items():
            prem_pod_hits[pod] += cnt

    print("\n==== Aggregate Results (STANDARD) ====")
    print(summarize("TTFB(ms)", std_ttfb))
    print(summarize("Total(ms)", std_total))
    if std_pod_hits:
        print("Per-pod hits (standard):")
        for pod, cnt in sorted(std_pod_hits.items(), key=lambda x: -x[1]):
            print(f"- {pod}: {cnt}")
    print(f"Total errors (standard): {std_errors}")

    print("\n==== Aggregate Results (PREMIUM) ====")
    print(summarize("TTFB(ms)", prem_ttfb))
    print(summarize("Total(ms)", prem_total))
    if prem_pod_hits:
        print("Per-pod hits (premium):")
        for pod, cnt in sorted(prem_pod_hits.items(), key=lambda x: -x[1]):
            print(f"- {pod}: {cnt}")
    print(f"Total errors (premium): {prem_errors}")

if __name__ == "__main__":
    main()