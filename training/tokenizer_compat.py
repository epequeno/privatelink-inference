"""Make a tokenizer saved by transformers 5.x loadable by the serving image.

The training image ships transformers 5.3 and writes ``extra_special_tokens`` as a
**list** of token strings. The transformers bundled in the TGI 3.3.6 serving image
predates that and expects a mapping, so loading the artifact fails with

    AttributeError: 'list' object has no attribute 'keys'

before the model is ever read. Rewriting the list as an identity mapping is enough:
the 4.x loader only needs the names, and the token strings are the same either way.

Deliberately dependency-free so `just verify` can test it without torch.
"""

import json
from pathlib import Path


def normalize_tokenizer_config(model_dir: Path) -> bool:
    """Rewrite ``extra_special_tokens`` to a mapping in place. Returns True if changed."""
    path = model_dir / "tokenizer_config.json"
    if not path.exists():
        return False
    config = json.loads(path.read_text())
    special = config.get("extra_special_tokens")
    if not isinstance(special, list):
        return False
    config["extra_special_tokens"] = {token: token for token in special}
    path.write_text(json.dumps(config, indent=2) + "\n")
    return True
