"""Smoke tests: configuration and URL building. No AWS calls, no network."""

import ipaddress

from probe.config import DEFAULT_REGION, load
from probe.egress import FIELD_NAMES, is_public_egress, parse_record
from probe.probe import endpoint_url, percentile, summarize_samples


def test_region_defaults_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    assert load().region == DEFAULT_REGION


def test_endpoint_url_shape() -> None:
    assert endpoint_url("vpce-1234.example.vpce.amazonaws.com") == (
        "http://vpce-1234.example.vpce.amazonaws.com:443/health"
    )


def test_unset_endpoint_is_empty_not_a_url() -> None:
    # Guards against a caller building "http:///health" and reporting a DNS error
    # as though it were evidence about the VPC.
    assert load().endpoint_dns_name == ""


VPC = ipaddress.ip_network("10.0.0.0/16")
SUBNETS = [ipaddress.ip_network("10.0.1.0/24"), ipaddress.ip_network("10.0.2.0/24")]


def _record(src, dst, bytes_, action="ACCEPT", status="OK"):
    line = f"{src} {dst} {bytes_} {action} {status} 1634123456"
    assert len(line.split()) == len(FIELD_NAMES)
    return parse_record(line)


def test_parse_record_returns_fields_in_order() -> None:
    rec = parse_record("10.0.1.5 8.8.8.8 1000 ACCEPT OK 1634123456")
    assert rec["srcaddr"] == "10.0.1.5"
    assert rec["dstaddr"] == "8.8.8.8"
    assert rec["bytes"] == "1000"
    assert rec["action"] == "ACCEPT"
    assert rec["log_status"] == "OK"


def test_parse_record_rejects_wrong_field_count() -> None:
    assert parse_record("10.0.1.5 8.8.8.8 1000 ACCEPT") is None
    assert parse_record("") is None


def test_internet_egress_from_inference_subnet_counts() -> None:
    counts, nbytes, dst = is_public_egress(_record("10.0.1.5", "8.8.8.8", 1000), VPC, SUBNETS)
    assert counts and nbytes == 1000 and dst == "8.8.8.8"


def test_source_outside_inference_subnet_does_not_count() -> None:
    # NAT/IGW traffic originates in the public subnet (10.0.0.0/24), not private.
    counts, _, _ = is_public_egress(_record("10.0.0.5", "8.8.8.8", 1000), VPC, SUBNETS)
    assert not counts


def test_intra_vpc_destination_does_not_count() -> None:
    counts, _, _ = is_public_egress(_record("10.0.1.5", "10.0.1.99", 500), VPC, SUBNETS)
    assert not counts


def test_link_local_dns_is_not_public_egress() -> None:
    # The VPC resolver (169.254.169.253) stays in the VPC; counting it would
    # inflate the number and misreport the claim.
    counts, _, _ = is_public_egress(_record("10.0.1.5", "169.254.169.253", 80), VPC, SUBNETS)
    assert not counts


def test_rejected_flows_are_not_egress() -> None:
    counts, _, _ = is_public_egress(
        _record("10.0.1.5", "8.8.8.8", 1000, action="REJECT"), VPC, SUBNETS
    )
    assert not counts


def test_malformed_ip_does_not_raise() -> None:
    counts, nbytes, _ = is_public_egress(_record("not-an-ip", "8.8.8.8", 1000), VPC, SUBNETS)
    assert not counts and nbytes == 0


def test_percentile_median_odd_length() -> None:
    assert percentile([3.0, 1.0, 2.0], 50) == 2.0


def test_percentile_interpolates_even_length() -> None:
    # 4 values: p50 interpolates between the 2nd and 3rd sorted values
    assert percentile([10.0, 20.0, 30.0, 40.0], 50) == 25.0


def test_percentile_single_value() -> None:
    assert percentile([42.0], 50) == 42.0


def test_percentile_empty_returns_zero() -> None:
    # Guards against crashing when the Lambda failed before producing samples.
    assert percentile([], 50) == 0.0


def test_percentile_edges() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0) == 10.0
    assert percentile(values, 100) == 40.0


def test_summarize_samples_p50_and_mean_tps() -> None:
    samples = [
        {"elapsed_ms": 100.0, "completion_tokens": 10, "tokens_per_sec": 100.0},
        {"elapsed_ms": 200.0, "completion_tokens": 10, "tokens_per_sec": 50.0},
        {"elapsed_ms": 300.0, "completion_tokens": 10, "tokens_per_sec": 33.3},
    ]
    summary = summarize_samples(samples)
    assert summary["p50_latency_ms"] == 200.0  # median of 100, 200, 300
    assert summary["mean_tokens_per_sec"] == round((100.0 + 50.0 + 33.3) / 3, 1)
    assert summary["n"] == 3


def test_summarize_empty_samples() -> None:
    summary = summarize_samples([])
    assert summary["p50_latency_ms"] == 0.0
    assert summary["mean_tokens_per_sec"] == 0.0
    assert summary["n"] == 0
