# models/

Vendored HuggingFace tokenizer files for offline / air-gapped deployment.

## Why vendor in git?

Ascend dev environments are air-gapped from huggingface.co. Without these
files, `--encoder hf_token`/`--encoder glm5_token` would block on
`hf download`. Storing the 3 small files (~20 MB each) in git lets Ascend
runners `git pull` and immediately run the token-level pipeline.

Weight files (sharded `.safetensors`/`.bin`/`.gguf`/`.pt`) are deliberately
**not** committed — the repo `.gitignore` allowlists `models/*_tokenizer/`
exceptions but blocks weight extensions inside them.

## Layout convention

Each tokenizer lives in `models/<name>_tokenizer/` and ships only:

| File | Purpose |
|---|---|
| `tokenizer.json` | BPE vocab + merges |
| `tokenizer_config.json` | special tokens + `tokenizer_class` |
| `chat_template.jinja` | chat template (含 thinking 模式 `<think>` tag, 若模型支持) |

`lib.hf_tokenizer.load_tokenizer(path)` 走 `AutoTokenizer.from_pretrained(path, trust_remote_code=True)`,
对所有 vendor 目录通用. 使用方式:

```python
from lib.prompt_encoder import HFTokenEncoder
encoder = HFTokenEncoder(tokenizer_path="models/qwen_v3_tokenizer")
```

或 CLI: `--encoder hf_token --tokenizer-path models/<name>_tokenizer`.

## Available tokenizers

| Directory | HF repo | License | Notes |
|---|---|---|---|
| `glm5_tokenizer/` | [`zai-org/GLM-5`](https://huggingface.co/zai-org/GLM-5) | MIT | thinking model; `wrap_user` overhead = 5 tokens; vocab_size 154820. revision `4e6698ba8e85059d749020e3c4d2123719f23926` |
| `qwen_v3_tokenizer/` | (待 vendor) | — | Qwen-V3 系列 |
| `qwen_v35_tokenizer/` | (待 vendor) | — | Qwen-V3.5 系列 |

(凡 "待 vendor" 行: 代码已就绪, 只等 tokenizer 文件 commit; 不阻塞 hf_token 通用代码合入.)

## Refresh procedure (添加新模型 / 升级版本)

```bash
# 1. 在能联网的机器上拉 tokenizer
.venv_glm5/bin/hf download <hf-repo-id> \
  tokenizer.json tokenizer_config.json chat_template.jinja \
  --local-dir models/<name>_tokenizer

# 2. commit
git add models/<name>_tokenizer/
git commit -m "chore(models): add <name> tokenizer @ <sha>"

# 3. Ascend / 其他离线机
git pull
# 即刻可用: --encoder hf_token --tokenizer-path models/<name>_tokenizer
```

## `glm5_token` 向后兼容

CLI `--encoder glm5_token` 仍然支持, 默认 `--tokenizer-path models/glm5_tokenizer`,
内部走 `GLM5TokenEncoder` (`HFTokenEncoder` 子类, `name="glm5_token_v1"`).
已落盘的 `user_summary.json` metadata 字段不受影响.
