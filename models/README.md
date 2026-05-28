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
| `tokenizer_config.json` | special tokens + `tokenizer_class` (Qwen 系列 chat_template 嵌在这里) |
| `chat_template.jinja` | chat template (GLM-5 用独立文件; Qwen 系列没有此文件, 模板在 tokenizer_config.json 内) |

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
| `qwen_v3_tokenizer/` | [`Qwen/Qwen3-8B`](https://huggingface.co/Qwen/Qwen3-8B) | Apache-2.0 | `Qwen2Tokenizer` backbone; vocab_size 151643; `wrap_user` overhead = 8 tokens (`<\|im_start\|>user\n…<\|im_end\|>\n<\|im_start\|>assistant\n`); chat_template **嵌在 tokenizer_config.json**, 没有独立 `.jinja` 文件. revision `b968826d9c46dd6066d109eabc6255188de91218` |
| `qwen_v35_tokenizer/` | (待 vendor) | — | Qwen-V3.5 系列 |
| `deepseek_v31_tokenizer/` | (待 vendor) | — | DeepSeek-V3.1; MLA, kv_meta 由脚本自 config.json 推导 |
| `deepseek_v4_tokenizer/` | (待 vendor) | — | DeepSeek-V4; MLA |

(凡 "待 vendor" 行: 代码已就绪, 只等 tokenizer 文件 commit; 不阻塞 hf_token 通用代码合入.
 分析代码与具体模型解耦 —— 只要 vendor 进 `models/<name>_tokenizer/`, 用
 `--encoder hf_token --tokenizer-path models/<name>_tokenizer` 即刻可跑, **无需改任何代码**.)

## 添加新模型 — 方式 A: 自本地权重目录 vendor (无网络, 推荐)

若已有模型权重目录 (例如 `/mnt/esfs/DeepSeek-V3.1/`), 它本身就带分词器小文件
(tokenizer.json / tokenizer_config.json / chat_template.jinja / config.json …),
不用联网, 用 `scripts/vendor_tokenizer_from_weights.py` 抽出来即可 (**绝不复制权重**):

```bash
# 1. 看权重目录里到底有哪些模型 (确认确切目录名)
ls /mnt/esfs/

# 2. 预览要复制哪些文件 + kv_meta 推导 (不落盘)
python scripts/vendor_tokenizer_from_weights.py \
    --src  /mnt/esfs/DeepSeek-V3.1 \
    --name deepseek_v31 \
    --dry-run

# 3. 实际抽取 + 生成 kv_meta.json + 冒烟加载验证
python scripts/vendor_tokenizer_from_weights.py \
    --src  /mnt/esfs/DeepSeek-V3.1 \
    --name deepseek_v31 \
    --verify

# 4. commit (gitignore 已自动挡掉权重扩展, 只会进小文件)
git add models/deepseek_v31_tokenizer/
git commit -m "chore(models): vendor deepseek_v31 tokenizer from local weights"
```

脚本做的事:
- 复制白名单小文件 (tokenizer.json / tokenizer_config.json / special_tokens_map.json /
  chat_template.jinja|json / tokenizer.model / vocab.json+merges.txt / added_tokens.json),
  外加 `tokenizer_config.json::auto_map` 引用的 `tokenization_*.py` (trust_remote_code 模型需要);
- 从 `config.json` 推导 `kv_meta.json` (MLA 看 `kv_lora_rank+qk_rope_head_dim`, GQA/MHA 看
  `num_kv_heads*head_dim`; dtype 取 `torch_dtype`). 多模态 config 会自动下钻 `text_config`;
- `--verify`: 用 transformers 离线加载 + 冒烟 encode, 报告 `wrap_user` 固定开销 token 数;
- 防御性跳过一切 `.safetensors/.bin/.gguf/.pt/...` 权重文件.

命名约定 (全小写, 去掉点, 跟 `glm5`/`qwen_v3` 对齐):
`Qwen-V3.5 → qwen_v35`, `DeepSeek-V3.1 → deepseek_v31`, `DeepSeek-V4 → deepseek_v4`.

> kv_meta 注意: 脚本假设 KV cache dtype == 权重 dtype. 若某模型 config `torch_dtype`
> 是 fp8 而 vLLM 实际用 bf16 kv-cache, 会打印 `dtype_note` 警告, 按提示把 `dtype_bytes`
> 改回 2 再 commit. 缺 `config.json` 时只是不生成 kv_meta.json (hit-rate 照常跑, 只少 GB/min 列).

## 添加新模型 — 方式 B: 联网 hf download

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
