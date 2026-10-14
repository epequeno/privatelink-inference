"""CDK app — provider VPC and consumer VPC, in that order.

Two stacks rather than one because the claim being tested is about the boundary
between them: a single stack could not demonstrate that the consumer has no other
route, nor would it resemble the cross-account shape this pattern is usually used
for.
"""

import aws_cdk as cdk
from stacks.consumer import ConsumerStack
from stacks.provider import ProviderStack

app = cdk.App()

# Region only — deliberately no account id, so nothing account-specific is
# committed to a repo that may be published.
env = cdk.Environment(region=app.node.try_get_context("region") or "us-east-1")

provider = ProviderStack(app, "PrivatelinkProviderStack", env=env)
ConsumerStack(
    app,
    "PrivatelinkConsumerStack",
    env=env,
    service_name=provider.service.vpc_endpoint_service_name,
)

app.synth()
