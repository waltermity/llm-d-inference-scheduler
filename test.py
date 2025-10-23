import requests, time
from collections import defaultdict
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt
import numpy as np

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

    return {"ttfb_ms": ttfb_ms, "total_ms": total_ms, "per_pod": dict(per_pod)}

if __name__ == "__main__":
    std = run_load("standard", runs=1000)
    prm = run_load("premium", runs=1000)

    # Build DataFrames
    df_ttfb = pd.DataFrame({
        "value_ms": std["ttfb_ms"] + prm["ttfb_ms"],
        "qos": (["standard"] * len(std["ttfb_ms"])) + (["premium"] * len(prm["ttfb_ms"])),
    })
    df_total = pd.DataFrame({
        "value_ms": std["total_ms"] + prm["total_ms"],
        "qos": (["standard"] * len(std["total_ms"])) + (["premium"] * len(prm["total_ms"])),
    })

    # Plot TTFB histogram (overlaid)
    # Shared bins and density for fair comparison
    ttfb_all = df_ttfb["value_ms"].to_numpy()
    ttfb_bins = np.histogram_bin_edges(ttfb_all, bins=40)
    ax = df_ttfb[df_ttfb.qos == "standard"]["value_ms"].plot(
        kind="hist", bins=ttfb_bins, density=True, alpha=0.5,
        label=f"standard (n={len(std['ttfb_ms'])})", figsize=(6, 4)
    )
    df_ttfb[df_ttfb.qos == "premium"]["value_ms"].plot(
        kind="hist", bins=ttfb_bins, density=True, alpha=0.5,
        label=f"premium (n={len(prm['ttfb_ms'])})", ax=ax
    )
    plt.xlabel("TTFB (ms)")
    plt.ylabel("Density")
    plt.title("TTFB histogram by QoS")
    plt.legend()
    plt.tight_layout()
    plt.savefig("hist_ttfb.png", dpi=150)
    plt.close()

    # Plot Total histogram (overlaid)
    # Total latency histogram with shared bins and density
    total_all = df_total["value_ms"].to_numpy()
    total_bins = np.histogram_bin_edges(total_all, bins=40)
    ax = df_total[df_total.qos == "standard"]["value_ms"].plot(
        kind="hist", bins=total_bins, density=True, alpha=0.5,
        label=f"standard (n={len(std['total_ms'])})", figsize=(6, 4)
    )
    df_total[df_total.qos == "premium"]["value_ms"].plot(
        kind="hist", bins=total_bins, density=True, alpha=0.5,
        label=f"premium (n={len(prm['total_ms'])})", ax=ax
    )
    plt.xlabel("Total latency (ms)")
    plt.ylabel("Density")
    plt.title("Total latency histogram by QoS")
    plt.legend()
    plt.tight_layout()
    plt.savefig("hist_total.png", dpi=150)
    plt.close()

    # Optional: ECDFs (very clear for comparing distributions)
    def ecdf(x):
        x = np.sort(np.asarray(x))
        y = np.arange(1, len(x) + 1) / len(x)
        return x, y
    x_s, y_s = ecdf(std["ttfb_ms"]); x_p, y_p = ecdf(prm["ttfb_ms"])
    plt.figure(figsize=(6,4))
    plt.plot(x_s, y_s, label=f"standard (n={len(std['ttfb_ms'])})")
    plt.plot(x_p, y_p, label=f"premium (n={len(prm['ttfb_ms'])})")
    plt.xlabel("TTFB (ms)"); plt.ylabel("ECDF")
    plt.title("TTFB ECDF by QoS"); plt.grid(True, alpha=0.2); plt.legend()
    plt.tight_layout(); plt.savefig("ecdf_ttfb.png", dpi=150); plt.close()

    # Optional: save raw samples
    df_samples = pd.concat([
        df_ttfb.assign(metric="ttfb"),
        df_total.assign(metric="total"),
    ], ignore_index=True)
    df_samples.to_csv("latency_samples.csv", index=False)

    # Box plots (TTFB, Total) by QoS
    plt.figure(figsize=(6,4))
    df_ttfb.boxplot(by="qos", column="value_ms", grid=False)
    plt.title("TTFB by QoS"); plt.suptitle(""); plt.xlabel("QoS"); plt.ylabel("ms")
    plt.tight_layout(); plt.savefig("box_ttfb.png", dpi=150); plt.close()

    plt.figure(figsize=(6,4))
    df_total.boxplot(by="qos", column="value_ms", grid=False)
    plt.title("Total latency by QoS"); plt.suptitle(""); plt.xlabel("QoS"); plt.ylabel("ms")
    plt.tight_layout(); plt.savefig("box_total.png", dpi=150); plt.close()

    # CCDF (tail) on log scale
    def ccdf(x):
        x = np.sort(np.asarray(x))
        y = 1.0 - (np.arange(1, len(x)+1) / len(x))
        return x, y

    xs, ys = ccdf(std["ttfb_ms"]); xp, yp = ccdf(prm["ttfb_ms"])
    plt.figure(figsize=(6,4))
    plt.semilogy(xs, ys, label=f"standard (n={len(std['ttfb_ms'])})")
    plt.semilogy(xp, yp, label=f"premium (n={len(prm['ttfb_ms'])})")
    plt.xlabel("TTFB (ms)"); plt.ylabel("CCDF (1-CDF)")
    plt.title("TTFB tail (log scale)"); plt.grid(True, which="both", alpha=0.2); plt.legend()
    plt.tight_layout(); plt.savefig("ccdf_ttfb.png", dpi=150); plt.close()

    xs, ys = ccdf(std["total_ms"]); xp, yp = ccdf(prm["total_ms"])
    plt.figure(figsize=(6,4))
    plt.semilogy(xs, ys, label=f"standard (n={len(std['total_ms'])})")
    plt.semilogy(xp, yp, label=f"premium (n={len(prm['total_ms'])})")
    plt.xlabel("Total (ms)"); plt.ylabel("CCDF (1-CDF)")
    plt.title("Total latency tail (log scale)"); plt.grid(True, which="both", alpha=0.2); plt.legend()
    plt.tight_layout(); plt.savefig("ccdf_total.png", dpi=150); plt.close()

    # Percentile bars (p50/p95/p99) per QoS and metric
    def pct_of(arr, p): return np.percentile(arr, p)
    stats = []
    for qos, arr in (("standard", std["ttfb_ms"]), ("premium", prm["ttfb_ms"])):
        stats.append(("ttfb", qos, pct_of(arr,50), pct_of(arr,95), pct_of(arr,99)))
    for qos, arr in (("standard", std["total_ms"]), ("premium", prm["total_ms"])):
        stats.append(("total", qos, pct_of(arr,50), pct_of(arr,95), pct_of(arr,99)))
    df_stats = pd.DataFrame(stats, columns=["metric","qos","p50","p95","p99"])

    for metric in ["ttfb","total"]:
        sub = df_stats[df_stats.metric == metric]
        x = np.arange(len(sub["qos"]))
        w = 0.25
        fig, ax = plt.subplots(figsize=(6,4))
        ax.bar(x - w, sub["p50"], width=w, label="p50")
        ax.bar(x,       sub["p95"], width=w, label="p95")
        ax.bar(x + w, sub["p99"], width=w, label="p99")
        ax.set_xticks(x); ax.set_xticklabels(sub["qos"])
        ax.set_ylabel("ms"); ax.set_title(f"{metric.upper()} percentiles by QoS")
        ax.legend(); fig.tight_layout()
        fig.savefig(f"bars_{metric}_percentiles.png", dpi=150); plt.close(fig)

    # Per-pod hits (stacked) if headers present
    if std["per_pod"] or prm["per_pod"]:
        pods = sorted(set(list(std["per_pod"].keys()) + list(prm["per_pod"].keys())))
        std_counts = [std["per_pod"].get(p, 0) for p in pods]
        prm_counts = [prm["per_pod"].get(p, 0) for p in pods]
        x = np.arange(len(pods)); w = 0.6
        fig, ax = plt.subplots(figsize=(max(6, len(pods)*0.6), 4))
        ax.bar(x, std_counts, width=w, label="standard")
        ax.bar(x, prm_counts, width=w, bottom=std_counts, label="premium")
        ax.set_xticks(x); ax.set_xticklabels(pods, rotation=45, ha="right")
        ax.set_ylabel("Requests"); ax.set_title("Per-pod hit counts by QoS")
        ax.legend(); fig.tight_layout()
        fig.savefig("bars_per_pod_hits.png", dpi=150); plt.close(fig)