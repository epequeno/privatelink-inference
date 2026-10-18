"""In-VPC probe for the SageMaker endpoint.

Runs inside the consumer VPC, whose subnets are PRIVATE_ISOLATED: no internet
gateway, no NAT, no public IP. Three things it reports, and together they are the
claim:

1. ``dns`` — where ``runtime.sagemaker.<region>.amazonaws.com`` resolves. With the
   interface endpoint's private DNS enabled it must be an RFC1918 address inside
   the VPC, not a public one.
2. ``internet`` — whether this VPC can reach a public host at all. It cannot, and
   the exact error is the evidence.
3. ``samples`` — N timed ``InvokeEndpoint`` calls through the interface endpoint.

If 2 fails and 3 succeeds, the invoke did not travel over the internet. boto3 and
botocore are in the Lambda runtime, so this needs no bundling.
"""

import json
import os
import socket
import time

import boto3
from botocore.config import Config

ENDPOINT_NAME = os.environ["SAGEMAKER_ENDPOINT_NAME"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
RUNTIME_HOST = f"runtime.sagemaker.{REGION}.amazonaws.com"
PUBLIC_PROBE = ("checkip.amazonaws.com", 443)
PROMPT = "Say hello in one word."
MAX_NEW_TOKENS = 10
TIMEOUT_S = 10


def dns_report() -> dict:
    try:
        infos = socket.getaddrinfo(RUNTIME_HOST, 443, proto=socket.IPPROTO_TCP)
        addresses = sorted({info[4][0] for info in infos})
        return {"host": RUNTIME_HOST, "addresses": addresses}
    except Exception as exc:  # noqa: BLE001 - the failure text is the finding
        return {"host": RUNTIME_HOST, "error": f"{type(exc).__name__}: {exc}"}


def internet_report() -> dict:
    started = time.perf_counter()
    try:
        # One address, one bounded attempt: create_connection((host, port)) walks
        # every A record, which turned a 10 s timeout into 80 s of probing.
        address = socket.getaddrinfo(PUBLIC_PROBE[0], PUBLIC_PROBE[1], proto=socket.IPPROTO_TCP)[0][
            4
        ]
        with socket.create_connection(address, timeout=TIMEOUT_S):
            return {
                "reachable": True,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
    except Exception as exc:  # noqa: BLE001 - the failure text is the finding
        return {
            "reachable": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"{type(exc).__name__}: {exc}",
        }


def invoke_samples(client, samples: int) -> list[dict]:
    body = json.dumps(
        {
            "inputs": PROMPT,
            "parameters": {
                "max_new_tokens": MAX_NEW_TOKENS,
                "details": True,
                "return_full_text": False,
            },
        }
    )
    out = []
    for _ in range(samples):
        started = time.perf_counter()
        response = client.invoke_endpoint(
            EndpointName=ENDPOINT_NAME,
            Body=body,
            ContentType="application/json",
            Accept="application/json",
        )
        raw = response["Body"].read()
        payload = json.loads(raw)
        elapsed_s = time.perf_counter() - started
        # TGI answers with an object for one input and an array for a batch; the
        # DLC's /invocations route has answered with both shapes across versions.
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        details = payload.get("details") or {}
        tokens = details.get("generated_tokens") or len(payload.get("generated_text", "").split())
        out.append(
            {
                "elapsed_ms": round(elapsed_s * 1000, 1),
                "completion_tokens": tokens,
                "tokens_per_sec": round(tokens / elapsed_s, 1) if elapsed_s > 0 else 0.0,
                # Kept so a reader can check what was actually counted, rather
                # than trusting the token arithmetic.
                "raw_preview": raw[:200].decode("utf-8", "replace"),
            }
        )
    return out


def handler(event, context):
    samples = int(event.get("samples", 5))
    result = {
        "ok": False,
        "endpoint": ENDPOINT_NAME,
        "dns": dns_report(),
        "internet": internet_report(),
    }

    client = boto3.client(
        "sagemaker-runtime",
        region_name=REGION,
        config=Config(retries={"max_attempts": 1, "mode": "standard"}),
    )

    # One warm-up call so a container that is still finishing its first request
    # does not land in the timed samples. Its result is reported, not timed.
    try:
        result["warmup"] = invoke_samples(client, 1)[0]
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        result["error"] = f"invoke: {type(exc).__name__}: {exc}"
        return result

    try:
        result["samples"] = invoke_samples(client, samples)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"invoke: {type(exc).__name__}: {exc}"
        return result

    result["ok"] = True
    return result
