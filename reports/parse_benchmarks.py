#!/usr/bin/env python3
"""
GuideLLM Benchmark Report Parser

Parses GuideLLM JSON output files and displays key metrics in a readable table.
Works with GuideLLM v0.5.x JSON format.

Usage:
    python parse_benchmarks.py                    # Parse all *-benchmarks.json in current dir
    python parse_benchmarks.py results/           # Parse all *-benchmarks.json in a folder
    python parse_benchmarks.py sync.json sweep.json  # Parse specific files
    python parse_benchmarks.py --csv              # Output as CSV
"""

import json
import glob
import sys
import os
from pathlib import Path


def extract_stats(stat_block):
    """Extract mean, median, p50, p90, p95, p99, min, max from a GuideLLM stats block."""
    if not isinstance(stat_block, dict):
        return {}

    result = {
        "mean": stat_block.get("mean"),
        "median": stat_block.get("median"),
        "min": stat_block.get("min"),
        "max": stat_block.get("max"),
        "std_dev": stat_block.get("std_dev"),
        "count": stat_block.get("count"),
    }

    for p in stat_block.get("percentiles", []):
        if isinstance(p, dict):
            pct = p.get("percentile")
            val = p.get("value")
            if pct is not None:
                result[f"p{int(pct)}"] = val

    return result


def parse_benchmark(benchmark):
    """Parse a single benchmark entry and return a flat metrics dict."""
    config = benchmark.get("config", {})
    metrics = benchmark.get("metrics", {})

    profile_info = config.get("profile", {})
    if isinstance(profile_info, dict):
        strategy_type = profile_info.get("strategy_type", "unknown")
    elif isinstance(profile_info, str):
        strategy_type = profile_info
    else:
        strategy_type = "unknown"

    strategy = config.get("strategy", {})
    if isinstance(strategy, dict):
        rate = strategy.get("rate")
        stype = strategy.get("type_", strategy_type)
    else:
        rate = None
        stype = strategy_type

    ttft = extract_stats(metrics.get("time_to_first_token_ms", {}).get("total", {}))
    itl = extract_stats(metrics.get("time_per_output_token_ms", {}).get("total", {}))
    req_lat = extract_stats(metrics.get("request_latency", {}).get("total", {}))
    prompt_tok = extract_stats(metrics.get("prompt_token_count", {}).get("total", {}))
    output_tok = extract_stats(metrics.get("output_token_count", {}).get("total", {}))
    rps = extract_stats(metrics.get("requests_per_second", {}).get("total", {}))
    concurrency = extract_stats(metrics.get("request_concurrency", {}).get("total", {}))

    requests = benchmark.get("requests", {})
    successful = requests.get("successful", [])
    errored = requests.get("errored", [])
    total_reqs = len(successful) if isinstance(successful, list) else 0
    error_reqs = len(errored) if isinstance(errored, list) else 0

    duration = benchmark.get("duration", 0)

    return {
        "strategy": stype,
        "rate": rate,
        "duration_s": round(duration, 1),
        "total_requests": total_reqs,
        "error_requests": error_reqs,
        "ttft_mean_ms": ttft.get("mean"),
        "ttft_median_ms": ttft.get("median"),
        "ttft_p99_ms": ttft.get("p99"),
        "ttft_min_ms": ttft.get("min"),
        "ttft_max_ms": ttft.get("max"),
        "itl_mean_ms": itl.get("mean"),
        "itl_median_ms": itl.get("median"),
        "itl_p99_ms": itl.get("p99"),
        "req_latency_mean_s": req_lat.get("mean"),
        "req_latency_median_s": req_lat.get("median"),
        "req_latency_p99_s": req_lat.get("p99"),
        "rps_mean": rps.get("mean"),
        "concurrency_mean": concurrency.get("mean"),
        "prompt_tokens_mean": prompt_tok.get("mean"),
        "output_tokens_mean": output_tok.get("mean"),
    }


def fmt(val, decimals=1):
    if val is None:
        return "-"
    if isinstance(val, float):
        return f"{val:.{decimals}f}"
    return str(val)


def print_table(rows):
    """Pretty-print the metrics as a formatted table."""
    print()
    print("=" * 120)
    print("GuideLLM Benchmark Results Summary")
    print("=" * 120)

    header = (
        f"{'Source File':<28} {'Profile':<14} {'Dur(s)':>6} {'Reqs':>5} {'Errs':>5} "
        f"{'TTFT mean':>10} {'TTFT med':>10} {'TTFT p99':>10} "
        f"{'ITL mean':>10} {'ITL med':>10} {'ITL p99':>10} "
        f"{'Lat mean':>10} {'RPS':>6}"
    )
    print(header)
    print("-" * 120)

    for row in rows:
        label = row["_label"]
        r = row["metrics"]
        profile = r["strategy"]
        if r["rate"]:
            profile += f":{r['rate']:.1f}" if isinstance(r["rate"], float) else f":{r['rate']}"

        line = (
            f"{label:<28} {profile:<14} {fmt(r['duration_s'], 0):>6} "
            f"{fmt(r['total_requests'], 0):>5} {fmt(r['error_requests'], 0):>5} "
            f"{fmt(r['ttft_mean_ms']):>10} {fmt(r['ttft_median_ms']):>10} {fmt(r['ttft_p99_ms']):>10} "
            f"{fmt(r['itl_mean_ms']):>10} {fmt(r['itl_median_ms']):>10} {fmt(r['itl_p99_ms']):>10} "
            f"{fmt(r['req_latency_mean_s'], 2):>10} {fmt(r['rps_mean'], 2):>6}"
        )
        print(line)

    print("-" * 120)
    print()
    print("Legend:")
    print("  TTFT = Time To First Token (ms)  |  ITL = Inter-Token Latency / Time Per Output Token (ms)")
    print("  Lat  = End-to-end Request Latency (s)  |  RPS = Requests Per Second")
    print("  med  = median  |  p99 = 99th percentile")
    print()


def print_csv(rows):
    """Output as CSV for spreadsheet import."""
    fields = [
        "source_file", "profile", "rate", "duration_s", "total_requests", "error_requests",
        "ttft_mean_ms", "ttft_median_ms", "ttft_p99_ms", "ttft_min_ms", "ttft_max_ms",
        "itl_mean_ms", "itl_median_ms", "itl_p99_ms",
        "req_latency_mean_s", "req_latency_median_s", "req_latency_p99_s",
        "rps_mean", "concurrency_mean", "prompt_tokens_mean", "output_tokens_mean",
    ]
    print(",".join(fields))
    for row in rows:
        r = row["metrics"]
        vals = [
            row["_label"], r["strategy"], fmt(r["rate"]), fmt(r["duration_s"]),
            fmt(r["total_requests"], 0), fmt(r["error_requests"], 0),
            fmt(r["ttft_mean_ms"]), fmt(r["ttft_median_ms"]), fmt(r["ttft_p99_ms"]),
            fmt(r["ttft_min_ms"]), fmt(r["ttft_max_ms"]),
            fmt(r["itl_mean_ms"]), fmt(r["itl_median_ms"]), fmt(r["itl_p99_ms"]),
            fmt(r["req_latency_mean_s"], 3), fmt(r["req_latency_median_s"], 3), fmt(r["req_latency_p99_s"], 3),
            fmt(r["rps_mean"], 2), fmt(r["concurrency_mean"], 2),
            fmt(r["prompt_tokens_mean"], 0), fmt(r["output_tokens_mean"], 0),
        ]
        print(",".join(vals))


def main():
    csv_mode = "--csv" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--csv"]

    if not args:
        json_files = sorted(glob.glob("*-benchmarks.json"))
    elif len(args) == 1 and os.path.isdir(args[0]):
        json_files = sorted(glob.glob(os.path.join(args[0], "*-benchmarks.json")))
    else:
        json_files = args

    if not json_files:
        print("No GuideLLM JSON files found. Place this script next to *-benchmarks.json files.")
        print(f"Usage: python {sys.argv[0]} [directory_or_files...] [--csv]")
        sys.exit(1)

    all_rows = []

    for filepath in json_files:
        filename = Path(filepath).name
        try:
            with open(filepath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"Warning: Could not parse {filepath}: {e}", file=sys.stderr)
            continue

        benchmarks = data.get("benchmarks", [])
        for i, b in enumerate(benchmarks):
            label = filename.replace("-benchmarks.json", "")
            if len(benchmarks) > 1:
                label += f"[{i}]"

            metrics = parse_benchmark(b)
            all_rows.append({"_label": label, "metrics": metrics})

    if not all_rows:
        print("No benchmark data found in the provided files.")
        sys.exit(1)

    if csv_mode:
        print_csv(all_rows)
    else:
        print_table(all_rows)
        print(f"Parsed {len(json_files)} file(s), {len(all_rows)} benchmark(s) total.")
        print(f"Tip: Run with --csv flag to get CSV output for spreadsheets.")


if __name__ == "__main__":
    main()
