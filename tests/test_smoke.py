"""Smoke tests: configuration and URL building. No AWS calls, no network."""

from probe.config import DEFAULT_REGION, load
from probe.probe import endpoint_url


def test_region_defaults_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    assert load().region == DEFAULT_REGION


def test_endpoint_url_shape() -> None:
    assert endpoint_url("vpce-1234.example.vpce.amazonaws.com") == (
        "http://vpce-1234.example.vpce.amazonaws.com/health"
    )


def test_unset_endpoint_is_empty_not_a_url() -> None:
    # Guards against a caller building "http:///health" and reporting a DNS error
    # as though it were evidence about the VPC.
    assert load().endpoint_dns_name == ""
