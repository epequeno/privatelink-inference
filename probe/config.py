"""Probe configuration, read from the environment.

No account ids, ARNs or endpoint ids here: those come from the stack outputs at
run time and are never committed.
"""

import os

from pydantic import BaseModel

DEFAULT_REGION = "us-east-1"


class Settings(BaseModel):
    """Values the probe needs at run time."""

    region: str = DEFAULT_REGION
    service_name: str = ""
    endpoint_dns_name: str = ""
    probe_function_name: str = ""


def load() -> Settings:
    """Build settings from the process environment."""
    return Settings(
        region=os.environ.get("AWS_REGION", DEFAULT_REGION),
        service_name=os.environ.get("PRIVATELINK_SERVICE_NAME", ""),
        endpoint_dns_name=os.environ.get("ENDPOINT_DNS_NAME", ""),
        probe_function_name=os.environ.get("PROBE_FUNCTION_NAME", ""),
    )


settings = load()
