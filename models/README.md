# models/

Vendored tokenizer files for offline / air-gapped deployment.

## `models/glm5_tokenizer/`

GLM-5 tokenizer assets, snapshotted from
[`huggingface.co/zai-org/GLM-5`](https://huggingface.co/zai-org/GLM-5) (MIT license).

| File | Size | Purpose |
|---|---|---|
| `tokenizer.json` | 20 MB | BPE merges + vocab (vocab_size = 154820) |
| `tokenizer_config.json` | 760 B | Special tokens + `tokenizer_class=TokenizersBackend` |
| `chat_template.jinja` | 3 KB | GLM-5 chat template (含 thinking 模式 `<think>` tag) |

Origin: HF API metadata snapshot of revision
`4e6698ba8e85059d749020e3c4d2123719f23926` (lastModified 2026-04-05).

Loaded by `lib/glm5_tokenizer.py::load_tokenizer()`:

```python
from lib.prompt_encoder import GLM5TokenEncoder
encoder = GLM5TokenEncoder(tokenizer_path="models/glm5_tokenizer")
```

### Why vendored in git?

Ascend dev environments are air-gapped from huggingface.co. Without these
files, `--encoder glm5_token` would block on `hf download`. Storing the 3
files (~20 MB) in git lets Ascend runners `git pull` and immediately run
the token-level pipeline.

Weight files (282 × ~5 GB safetensors) are deliberately **not** committed
— this directory's `.gitignore` rule allows tokenizer files but blocks
`*.safetensors` / `*.bin` / `*.gguf` / `*.pt`.

### Refresh procedure

If GLM-5 publishes a new tokenizer revision:

```bash
.venv_glm5/bin/hf download zai-org/GLM-5 \
  tokenizer.json tokenizer_config.json chat_template.jinja \
  --local-dir models/glm5_tokenizer
git add models/glm5_tokenizer/
git commit -m "chore(models): bump GLM-5 tokenizer to <new-sha>"
```
