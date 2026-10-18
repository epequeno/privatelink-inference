"""CDK app — provider VPC and consumer VPC, in that order.

Two stacks rather than one because the claim being tested is about the boundary
between them: a single stack could not demonstrate that the consumer has no other
route, nor would it resemble the cross-account shape this pattern is usually used
for.
"""

import aws_cdk as cdk
from stacks.consumer import ConsumerStack
from stacks.provider import ProviderStack
from stacks.sagemaker import SageMakerStack

app = cdk.App()

# Region only — deliberately no account id, so nothing account-specific is
# committed to a repo that may be published.
env = cdk.Environment(region=app.node.try_get_context("region") or "us-east-1")

provider = ProviderStack(app, "PrivatelinkProviderStack", env=env)

# Phase 2 only: `just sagemaker-deploy` writes the registered model package ARN
# into infra/cdk.context.json (gitignored), which adds the endpoint. Without it
# this stack stops at the bucket, the roles and the registry group, which is what
# the first `cdk deploy --all` needs.
sagemaker = SageMakerStack(
    app,
    "PrivatelinkSageMakerStack",
    env=env,
    model_package_arn=app.node.try_get_context("model_package_arn"),
    alarm_email=app.node.try_get_context("alarm_email"),
)

ConsumerStack(
    app,
    "PrivatelinkConsumerStack",
    env=env,
    service_name=provider.service.vpc_endpoint_service_name,
    sagemaker_endpoint_name=sagemaker.endpoint_name,
)

# Cost Explorer groups by these, which is how the spend figures in RESULTS.md are attributed.
for stack in (provider, sagemaker):
    cdk.Tags.of(stack).add("project", "privatelink-inference")

app.synth()
