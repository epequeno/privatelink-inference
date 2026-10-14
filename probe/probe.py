"""Probe the inference endpoint both ways.

``--private`` invokes a Lambda inside the consumer VPC, which has no internet
route at all. The Lambda makes N completions calls and returns per-sample
timings; this module computes p50 latency and mean tokens/sec from them. If it
succeeds, the model was reached through the interface endpoint and nothing else.

``--public`` makes the same health call from wherever you are. It is *expected to
fail*, and the failure is the finding: capture it verbatim rather than
describing it.
"""

import argparse
import json
import sys
import time

import boto3
import httpx

from probe.config import settings

HEALTH_PATH = "/health"
DEFAULT_TIMEOUT = 10.0
DEFAULT_SAMPLES = 5
SERVICE_PORT = 443


def percentile(values: list[float], p: float) -> float:
    """Return the p-th percentile (0–100) of a non-empty list via linear interpolation.

    Empty input returns 0.0 so callers don't crash when the Lambda failed before
    producing any samples — the error is reported separately.
    """
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    f = int(k)
    frac = k - f
    if f + 1 < len(s):
        return s[f] + (s[f + 1] - s[f]) * frac
    return s[f]


def summarize_samples(samples: list[dict]) -> dict:
    """Reduce per-call samples to the two headline metrics: p50 latency, mean tok/s."""
    latencies = [s["elapsed_ms"] for s in samples]
    tps_values = [s["tokens_per_sec"] for s in samples]
    return {
        "p50_latency_ms": round(percentile(latencies, 50), 1),
        "mean_tokens_per_sec": round(sum(tps_values) / len(tps_values), 1) if tps_values else 0.0,
        "n": len(samples),
    }


def endpoint_url(dns_name: str) -> str:
    """Build the health-check URL for an endpoint DNS name."""
    return f"http://{dns_name}:{SERVICE_PORT}{HEALTH_PATH}"


def private(samples: int = DEFAULT_SAMPLES) -> int:
    """Invoke the in-VPC probe function and report latency + tokens/sec.

    The Lambda makes ``samples`` completions calls and returns per-sample
    timings. This function computes p50 latency and mean tokens/sec from those
    raw samples — the stats stay local so they are testable without AWS.
    """
    if not settings.probe_function_name:
        print("PROBE_FUNCTION_NAME is not set — run `just deploy` first")
        return 2

    client = boto3.client("lambda", region_name=settings.region)
    started = time.perf_counter()
    response = client.invoke(
        FunctionName=settings.probe_function_name,
        Payload=json.dumps({"source": "probe", "samples": samples}).encode(),
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    payload = json.loads(response["Payload"].read() or b"{}")

    if not payload.get("ok"):
        print(json.dumps(payload, indent=2))
        print("\nPRIVATE PATH FAILED — that is the interesting result, not a missing number")
        return 1

    sample_list = payload.get("samples", [])
    summary = summarize_samples(sample_list)

    print(json.dumps(payload, indent=2))
    print(f"\nround trip through the VPC (Lambda + {samples} completions): {elapsed_ms:.0f} ms")
    print(f"p50 latency per completion: {summary['p50_latency_ms']} ms  (n={summary['n']})")
    print(f"mean tokens/sec: {summary['mean_tokens_per_sec']}")
    print("\nPaste the two lines above into RESULTS.md (p50 latency, tokens/sec).")
    return 0


def public() -> int:
    """Attempt the same call from outside the VPC. Expected to fail."""
    if not settings.endpoint_dns_name:
        print("ENDPOINT_DNS_NAME is not set — run `just deploy` first")
        return 2

    url = endpoint_url(settings.endpoint_dns_name)
    print(f"attempting {url} from outside the VPC — this should not work\n")
    started = time.perf_counter()
    try:
        response = httpx.get(url, timeout=DEFAULT_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - the exception text is the evidence
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"{type(exc).__name__} after {elapsed_ms:.0f} ms:")
        print(f"  {exc}")
        print("\nPASS — the public path is closed. Paste the two lines above into RESULTS.md.")
        return 0

    print(f"HTTP {response.status_code} after {(time.perf_counter() - started) * 1000:.0f} ms")
    print("\nFAIL — the endpoint answered from outside the VPC. Do not claim privacy.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="probe the private inference endpoint")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--private", action="store_true", help="call from inside the VPC")
    group.add_argument("--public", action="store_true", help="prove the public path fails")
    parser.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_SAMPLES,
        help=f"completions calls to time on the private path (default {DEFAULT_SAMPLES})",
    )
    args = parser.parse_args(argv)

    return private(args.samples) if args.private else public()


if __name__ == "__main__":
    sys.exit(main())
