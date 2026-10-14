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

synth: sync cdk-toolchain
    @cd infra && uv run cdk synth --quiet

# Provider (VPC A: inference host, internal NLB, endpoint service) then consumer
# (VPC B: interface endpoint, probe host).
deploy: sync cdk-toolchain
    @cd infra && uv run cdk deploy --all --require-approval never

# The measured path. Pass --public to run the negative test instead.
probe *ARGS: sync
    @uv run python -m probe.probe {{ARGS}}

# Public egress from the inference subnet. Not implemented — exits non-zero.
egress: sync
    @echo "egress report not implemented yet — see RESULTS.md"
    @exit 1

# Both stacks. The NLB and the interface endpoint bill hourly even when idle.
down: sync cdk-toolchain
    @cd infra && uv run cdk destroy --all --force

check: verify
