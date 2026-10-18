# `data/sft` — the training job's input

A deterministic sample of the legal-kit SFT corpus
(`~/code/legal-kit/data/sft_reasoning/{train,val}.jsonl`, 3,012 / 335 rows, built
from CUAD/ContractNLI contract text and generated reasoning chains). Committed so
the SageMaker training job is reproducible from this repo alone; the sampler is
`scripts/sample_sft.py`.

- `train.jsonl` — 300 rows, 100 per label (`entailed`, `contradicted`, `neutral`)
- `validation.jsonl` — 50 rows, drawn from the *validation* split
- Seed 42; rows longer than 3,000 characters dropped so every row fits the
  1024-token training window

Regenerate:

```sh
uv run python scripts/sample_sft.py --source ~/code/legal-kit/data/sft_reasoning
```

The point of the job is the AWS surface and the measurement discipline, not model
quality: 300 rows and one epoch will not produce a good model. See
`RESULTS.md` for what the job actually measured.
