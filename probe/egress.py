"""Measure public egress from the inference subnet.

Reads the provider VPC's flow logs (CloudWatch Logs) over a time window and sums
the bytes whose source is in an inference (private) subnet and whose destination
is outside the VPC — traffic that left the VPC for the internet. The privacy
claim is that this is zero while inference is serving.

Bootstrap (model fetch, toolchain build) is *expected* to produce non-zero
egress; run this after the host has settled to test the claim itself. The
``--public`` probe tests reachability; this tests the corollary — that nothing
leaks out while the model is being served.
"""

import argparse
import ipaddress
import sys
import time
from collections import defaultdict

import boto3

from probe.config import settings

# Field order MUST match the log_format set on the FlowLog in the provider stack.
FIELD_NAMES = ("srcaddr", "dstaddr", "bytes", "action", "log_status", "start")
# Link-local (169.254.0.0/16) covers the VPC DNS resolver and the instance
# metadata service. Traffic to it stays in the VPC, so it is not "public egress"
# even though its destination is outside the VPC CIDR.
LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16", strict=False)

WINDOW_DEFAULT_MINUTES = 5


def parse_record(line: str) -> dict | None:
    """Parse one flow-log record into a field dict, or None if it is malformed.

    The record format is fixed by the stack's ``log_format``; the order here is
    coupled to that and to ``FIELD_NAMES``.
    """
    parts = line.split()
    if len(parts) != len(FIELD_NAMES):
        return None
    return dict(zip(FIELD_NAMES, parts, strict=True))


def is_public_egress(
    record: dict,
    vpc_net: ipaddress.ip_network,
    subnet_nets: list[ipaddress.ip_network],
) -> tuple[bool, int, str | None]:
    """Decide whether a record is internet-bound egress from the inference subnet.

    Returns (counts, bytes, destination). A record counts when its source is in an
    inference subnet, its destination is neither in the VPC nor link-local, and the
    flow was accepted (REJECT records are policy denials, not real egress).
    """
    if record["action"] != "ACCEPT" or record["log_status"] != "OK":
        return False, 0, None
    try:
        src = ipaddress.ip_address(record["srcaddr"])
        dst = ipaddress.ip_address(record["dstaddr"])
        nbytes = int(record["bytes"])
    except ValueError:
        return False, 0, None

    if not any(src in net for net in subnet_nets):
        return False, 0, None
    if dst in vpc_net or dst in LINK_LOCAL:
        return False, 0, None
    return True, nbytes, str(dst)


def fetch_events(log_group: str, start_ms: int, end_ms: int):
    """Yield raw flow-log messages for the window from CloudWatch Logs."""
    client = boto3.client("logs", region_name=settings.region)
    paginator = client.get_paginator("filter_log_events")
    for page in paginator.paginate(logGroupName=log_group, startTime=start_ms, endTime=end_ms):
        for event in page.get("events", []):
            yield event["message"]


def report(minutes: int) -> int:
    """Sum public egress over the last ``minutes`` minutes and print the result."""
    if not settings.flow_log_group:
        print("FLOW_LOG_GROUP is not set — run `just deploy` first")
        return 2
    if not settings.provider_vpc_cidr or not settings.inference_subnet_cidrs:
        print("PROVIDER_VPC_CIDR / INFERENCE_SUBNET_CIDRS are not set — run `just deploy` first")
        return 2

    vpc_net = ipaddress.ip_network(settings.provider_vpc_cidr, strict=False)
    subnet_nets = [
        ipaddress.ip_network(c.strip(), strict=False)
        for c in settings.inference_subnet_cidrs.split(",")
        if c.strip()
    ]

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - minutes * 60 * 1000

    per_destination: dict[str, int] = defaultdict(int)
    total = 0
    for line in fetch_events(settings.flow_log_group, start_ms, end_ms):
        record = parse_record(line)
        if record is None:
            continue
        counts, nbytes, dst = is_public_egress(record, vpc_net, subnet_nets)
        if counts:
            assert dst is not None
            per_destination[dst] += nbytes
            total += nbytes

    print(f"window: last {minutes} min (epoch ms {start_ms}–{end_ms})")
    print(f"public egress from inference subnet: {total} bytes")
    if per_destination:
        print("\ndestinations:")
        for dst, nbytes in sorted(per_destination.items(), key=lambda kv: -kv[1]):
            print(f"  {dst}\t{nbytes} bytes")

    if total == 0:
        print("\nPASS — no public egress. Paste the number above into RESULTS.md.")
        return 0
    print(
        "\nFAIL — bytes left the inference subnet for the internet. If bootstrap is "
        "still running, wait for it to settle and re-run."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="report public egress from the inference subnet")
    parser.add_argument(
        "--minutes",
        type=int,
        default=WINDOW_DEFAULT_MINUTES,
        help=f"look-back window in minutes (default {WINDOW_DEFAULT_MINUTES})",
    )
    args = parser.parse_args(argv)
    return report(args.minutes)


if __name__ == "__main__":
    sys.exit(main())
