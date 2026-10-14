"""Probe the inference endpoint both ways.

``--private`` invokes a Lambda inside the consumer VPC, which has no internet
route at all. If it succeeds, the model was reached through the interface endpoint
and nothing else.

``--public`` makes the same call from wherever you are. It is *expected to fail*,
and the failure is the finding: capture it verbatim rather than describing it.
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


def endpoint_url(dns_name: str) -> str:
    """Build the health-check URL for an endpoint DNS name."""
    return f"http://{dns_name}{HEALTH_PATH}"


def private() -> int:
    """Invoke the in-VPC probe function and report what it saw."""
    if not settings.probe_function_name:
        print("PROBE_FUNCTION_NAME is not set — run `just deploy` first")
        return 2

    client = boto3.client("lambda", region_name=settings.region)
    started = time.perf_counter()
    response = client.invoke(
        FunctionName=settings.probe_function_name,
        Payload=json.dumps({"source": "probe"}).encode(),
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    payload = json.loads(response["Payload"].read() or b"{}")

    print(json.dumps(payload, indent=2))
    print(f"\nround trip through the VPC: {elapsed_ms:.0f} ms")

    if not payload.get("ok"):
        print("\nPRIVATE PATH FAILED — that is the interesting result, not a missing number")
        return 1
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
    args = parser.parse_args(argv)

    return private() if args.private else public()


if __name__ == "__main__":
    sys.exit(main())
