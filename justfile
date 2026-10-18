# privatelink-inference: a model endpoint with no public route to it
set shell := ["bash", "-uc"]

default:
    @just --list

# Fail loudly if uv is missing, rather than failing confusingly in a later recipe.
toolchain:
    @command -v uv >/dev/null || { echo "uv not installed — see https://docs.astral.sh/uv/"; exit 1; }
    @uv --version

# The CDK CLI is a Node tool, not a pip package: aws-cdk-lib provides the
# constructs but not the executable.
cdk-toolchain:
    @command -v cdk >/dev/null || { echo "cdk CLI missing — run: npm install -g aws-cdk"; exit 1; }
    @cdk --version

sync: toolchain
    @uv sync --quiet

lint: sync
    @uv run ruff check .
    @uv run ruff format --check .

fmt: sync
    @uv run ruff format .
    @uv run ruff check --fix .

test: sync
    @uv run pytest

# The gate: lint, tests, and both stacks must synthesise.
verify: lint test synth

# The model package ARN is a placeholder account: it exists only so the endpoint
# constructs (model, endpoint config, endpoint, dashboard, alarm) are part of the
# offline gate instead of appearing for the first time during a real deploy.
synth: sync cdk-toolchain
    @cd infra && uv run cdk synth --quiet -c model_package_arn="arn:aws:sagemaker:us-east-1:123456789012:model-package/qwen-legal-lora/1"

# Provider (VPC A: inference host, internal NLB, endpoint service) then consumer
# (VPC B: interface endpoint, probe host).
deploy: sync cdk-toolchain
    @cd infra && uv run cdk deploy --all --require-approval never

# The measured path. Pass --public to run the negative test instead.
probe *ARGS: sync
    @uv run python -m probe.probe {{ARGS}}

# Public egress from the inference subnet, summed over a window. 0 while serving.
# Pass --minutes N to widen the window. Exits 0 when no egress, 1 when bytes
# leaked (or bootstrap is still running).
egress *ARGS: sync
    @uv run python -m probe.egress {{ARGS}}

# SageMaker Training Job: uploads code + data, runs the job, stores the run JSON.
# Pass --wait to block until it finishes.
sagemaker-train *ARGS: sync
    @uv run python -m probe.sagemaker train {{ARGS}}

# Model Registry entry from the most recent training job's artifact.
sagemaker-register: sync
    @uv run python -m probe.sagemaker register

# Phase 2 deploy: records the package ARN, then adds the endpoint to the stack.
sagemaker-deploy: sync cdk-toolchain
    @uv run python -m probe.sagemaker write-context
    @cd infra && uv run cdk deploy PrivatelinkSageMakerStack --require-approval never

# --private: timed InvokeEndpoint calls through the interface endpoint.
# --public:  the negative test — the same VPC has no internet route.
sagemaker-probe *ARGS: sync
    @uv run python -m probe.sagemaker probe {{ARGS}}

# Endpoint creation duration, from CloudFormation events.
sagemaker-spinup: sync
    @uv run python -m probe.sagemaker spinup

# CloudWatch endpoint metrics (Invocations, latency, 4XX/5XX). Pass --minutes N.
metrics *ARGS: sync
    @uv run python -m probe.sagemaker metrics {{ARGS}}

# All three stacks. The NLB, the interface endpoints and the SageMaker endpoint
# bill hourly even when idle, so `down` is not optional. Clearing the model
# package context means a later `deploy` cannot resurrect a destroyed endpoint.
down: sync cdk-toolchain
    @uv run python -m probe.sagemaker delete-packages
    @cd infra && uv run cdk destroy --all --force
    @uv run python -m probe.sagemaker clear-context

check: verify
