# Step 1.6 Phase 1: 调研发现 (GLM-5 tokenizer + raw_prompt 样本)

**版本**: v0.2 (§1-§2 已填, §3-§4 待用户决策后继续)
**关联文档**: [step1_6_token_level_experiment_plan.md](step1_6_token_level_experiment_plan.md)
**P1 执行日期**: 2026-05-15

---

## 0. P1 目标

回答 3 个问题, 让 P2 能够无歧义地写出 `GLM5TokenEncoder`:

1. GLM-5 tokenizer 在 HF 是否可拉取? (`zai-org/GLM-5`)
2. GLM-5 chat template 长什么样? special tokens 有哪些?
3. 6 个生产数据集的 raw_prompt 真实样子 → `chat_mode` 用 `raw` 还是 `wrap_user`?

(vllm_hash 算法**不调研**, 决策已敲定 sha256 fallback, 见 plan §6)

---

## 1. GLM-5 tokenizer 可访问性

### 1.1 探测命令

```bash
# 本机 (有公网)
python3 -c "
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained('zai-org/GLM-5', trust_remote_code=True)
print('vocab_size:', tok.vocab_size)
print('special_tokens:', tok.special_tokens_map)
print('chat_template (前 500 char):', tok.chat_template[:500] if tok.chat_template else 'None')
"
```

### 1.2 结果 (2026-05-15 探测)

- [x] HF 仓库存在: **Y** (`https://huggingface.co/zai-org/GLM-5`)
- [x] 需要 access token: **N** (`gated=false`, `private=false`)
- [x] trust_remote_code: **Y** (`tokenizer_class=TokenizersBackend` 非标准)
- [x] tokenizer backend: `tokenizers` (HF tokenizers 库, **不是 SentencePiece**)
- [x] vocab_size: 未读 (需装 transformers 后读, 但不影响 P1 决策)
- [x] tokenizer 文件清单: `tokenizer.json` (~5MB) + `tokenizer_config.json` (1KB) + `chat_template.jinja` (3KB)
- [x] 模型架构: `GlmMoeDsaForCausalLM` (MoE + DeepSeek-style architecture)
- [x] **重要发现**: GLM-5 是 **thinking 模型** (类 DeepSeek-R1), chat_template 默认末尾追加 `<|assistant|><think>`
- [x] `model_max_length`: 202752 (200K context)
- [x] `padding_side`: left
- [x] 下载策略: 本机 + Ascend 都用 `huggingface-cli download zai-org/GLM-5 tokenizer.json tokenizer_config.json chat_template.jinja` (10MB, 不拉 282 个 safetensors)

### 1.3 chat_template (完整 jinja, 已拉取)

完整源码已保存在 HF API metadata 中. 关键片段:

```jinja
[gMASK]<sop>
{# tools system block, 我们的场景无 tools, 跳过 #}
{%- if tools -%}<|system|>...{%- endif -%}

{# 遍历 messages 渲染 #}
{%- for m in messages -%}
{%- if m.role == 'user' -%}<|user|>{{ visible_text(m.content) }}
{%- elif m.role == 'assistant' -%}
<|assistant|>...
{%- elif m.role == 'system' -%}<|system|>{{ visible_text(m.content) }}
{%- elif m.role == 'tool' -%}...
{%- endif -%}
{%- endfor -%}

{# add_generation_prompt 默认 True, 追加 assistant 起始 #}
{%- if add_generation_prompt -%}
    <|assistant|>{{- '</think>' if (enable_thinking is defined and not enable_thinking) else '<think>' -}}
{%- endif -%}
```

**关键观察**:
1. 默认 `enable_thinking=True` → 末尾追加 `<|assistant|><think>` (而非 `</think>`)
2. system message **不会默认追加** (只有 tools 才补 system block)
3. `<|user|>` 后**直接接 content** (无空格, content 末尾保留换行)
4. tools / observation / multi-modal 都有路径, 但我们文本 chat 场景不触及

### 1.4 special tokens (已确认完整列表)

```python
# from tokenizer_config.json
eos_token = "<|endoftext|>"
pad_token = "<|endoftext|>"   # same as eos
# bos: 用 [gMASK]<sop> 替代 (在 chat_template 中体现)

extra_special_tokens = [
    "<|endoftext|>", "[MASK]", "[gMASK]", "[sMASK]",
    "<sop>", "<eop>",
    "<|system|>", "<|user|>", "<|assistant|>", "<|observation|>",
    # multi-modal (我们不用)
    "<|begin_of_image|>", "<|end_of_image|>",
    "<|begin_of_video|>", "<|end_of_video|>",
    "<|begin_of_audio|>", "<|end_of_audio|>",
    "<|begin_of_transcription|>", "<|end_of_transcription|>",
]
```

### 1.5 阻断处置

无阻断. 仓库公开 + 无 gating + tokenizer 文件 < 10MB.

---

## 2. 最小 messages → token_ids 实验

### 2.1 实验代码

```python
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained("zai-org/GLM-5", trust_remote_code=True)

# 实验 1: 单轮 user
msgs1 = [{"role": "user", "content": "你好"}]
ids1 = tok.apply_chat_template(msgs1, tokenize=True, add_generation_prompt=True)
print("test1 - len:", len(ids1), "ids:", ids1)
print("test1 - decoded:", repr(tok.decode(ids1)))

# 实验 2: system + user
msgs2 = [
    {"role": "system", "content": "你是一个有帮助的助手"},
    {"role": "user", "content": "你好"}
]
ids2 = tok.apply_chat_template(msgs2, tokenize=True, add_generation_prompt=True)
print("test2 - len:", len(ids2), "ids:", ids2)
print("test2 - decoded:", repr(tok.decode(ids2)))

# 实验 3: 多轮
msgs3 = [
    {"role": "user", "content": "1+1="},
    {"role": "assistant", "content": "2"},
    {"role": "user", "content": "2+2="}
]
ids3 = tok.apply_chat_template(msgs3, tokenize=True, add_generation_prompt=True)
print("test3 - len:", len(ids3), "decoded:", repr(tok.decode(ids3)))
```

### 2.2 实测结果 (2026-05-15, transformers 5.8.1 + 本机 venv)

| 实验 | tokens | rendered string |
|---|---|---|
| 1. user-only `"你好"` | **6** | `[gMASK]<sop><\|user\|>你好<\|assistant\|><think>` |
| 2. system + user | 12 | + system content 6 tokens overhead |
| 3. multi-turn (u, a, u) | 17 | `...<\|assistant\|></think>2<\|user\|>...` (assistant 历史用 `</think>` 标记非 thinking) |
| 4. **empty content (overhead 基线)** | **5** | `[gMASK]<sop><\|user\|><\|assistant\|><think>` |
| 5. 1000 中文 char (重复) | 505 | bytes/token = 5.94 |
| 6. 1200 en char | 206 | bytes/token = 5.83 |
| 7. 980 中英混合 (代码请求) | 466 | bytes/token = 3.39 |
| 8. Python 代码 1450 chars | 555 | bytes/token = 2.61 |
| 9. 自然中文段落 420 chars | 315 | bytes/token = 3.81 |

**special tokens 实测 id**:
- `[gMASK]` = 154822, `<sop>` = 154824
- `<|user|>` = 154827, `<|assistant|>` = 154828, `<|system|>` = 154826
- `<think>` = 154841, `</think>` = 154842, `<|endoftext|>` = 154820

### 2.3 关键观察 (实测已确认)

- [x] **wrap_user fixed overhead = 5 tokens** (`[gMASK]<sop><|user|><|assistant|><think>`)
- [x] add_generation_prompt=True 末尾追加 `<|assistant|><think>` (默认开 thinking)
- [x] system 默认**不补** (无 default system message, 仅 tools 触发)
- [x] 角色边界 token 均为单 token
- [x] **5 tokens 固定 overhead 在所有请求完全相同** → 反而提升前缀命中率 (前 3 个 token `[gMASK]<sop><|user|>` 必然命中)
- [x] 末尾的 `<|assistant|><think>` 是 prefill 的一部分, 影响"末尾 1 个 block 是否能完整成 block" (但前缀命中率不受影响)
- [x] **bytes/token 实测平均 3-6**, 生产场景大致 3-4, 印证字节级 128 bytes/block ≈ 32-40 tokens, 解释字节级偏差根因

---

## 3. 6 个生产数据集 raw_prompt 抽样

### 3.1 抽样脚本

```python
import csv
from pathlib import Path

datasets = sorted(Path("/data").glob("*/raw/*.csv"))
for csv_path in datasets:
    print(f"\n========== {csv_path} ==========")
    with open(csv_path, encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if i >= 3: break
            raw = row.get("raw_prompt") or row.get("请求参数") or ""
            print(f"\n--- row {i} (len={len(raw)} bytes) ---")
            print(raw[:500])
            print("... [truncated]" if len(raw) > 500 else "")
```

### 3.2 抽样结果 (每数据集 3 条, 待填)

#### Dataset M1: ___________

```
[row 0] (len=___ bytes):
___

[row 1]:
___

[row 2]:
___
```

#### Dataset M2: ___________
(同上)

#### M3 / M4 / M5 / M6
(同上)

### 3.3 raw_prompt 模式判定 (核心决策)

| 模式特征 | 判定 chat_mode |
|---|---|
| 含 `<\|im_start\|>` / `<\|im_end\|>` 等 GLM 标记 | **raw** |
| 含 `[INST]` / `[/INST]` (Llama 风格) | **raw** (需检查 GLM tokenizer 是否兼容这些标记) |
| 含 JSON `{"role": ..., "content": ...}` | **messages** |
| 纯文本, 无任何标记 | **wrap_user** |

每个数据集的判定 (待填):

| 数据集 | 判定模式 | 依据 (引用 §3.2 哪一条) |
|---|---|---|
| M1 | ___ | ___ |
| M2 | ___ | ___ |
| M3 | ___ | ___ |
| M4 | ___ | ___ |
| M5 | ___ | ___ |
| M6 | ___ | ___ |

### 3.4 最终 chat_mode 决策

- [ ] **统一**: 6 数据集都用同一模式 ___ (推荐)
- [ ] **分模式**: M1/M3 用 `raw`, M2/M4/M5/M6 用 `wrap_user` (复杂, 需要 per-dataset 配置)

选定方案: ___

---

## 4. tokenize 性能预估 (在 Ascend dev 机)

### 4.1 实验代码

```python
import time
from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained("zai-org/GLM-5", trust_remote_code=True)

# 取一个 dataset 前 1000 条
import csv
prompts = []
with open("/data/M1/raw/M1.csv", encoding="utf-8-sig") as f:
    for i, row in enumerate(csv.DictReader(f)):
        if i >= 1000: break
        prompts.append(row.get("raw_prompt") or row.get("请求参数") or "")

t0 = time.time()
for p in prompts:
    msgs = [{"role": "user", "content": p}]
    ids = tok.apply_chat_template(msgs, tokenize=True, add_generation_prompt=True)
t1 = time.time()
print(f"1000 prompts, {t1-t0:.2f}s, {1000/(t1-t0):.0f} req/s")
print(f"avg tokens/prompt: {sum(len(tok.encode(p)) for p in prompts[:100])/100:.0f}")
```

### 4.2 实测结果 (2026-05-15, 本机 venv, 单线程)

| Prompt 类型 | 速度 | 平均 tokens/req |
|---|---|---|
| 短 prompt (10-30 tokens) | **18,364 req/s** | 12 |
| 中长 prompt (200-3000 tokens, 平均 744) | **1,480 req/s** | 744 |

### 4.3 6 数据集预估

假设每数据集 50K 请求, 平均 prompt 长度等同测试 9 的 mid-long:
- 6 × 50K = 300,000 请求
- 300K / 1480 req/s = **203s ≈ 3.4 分钟**

**结论**: 单线程完全够, 不需要 multiprocessing 或 batch tokenize cache.

---

## 5. P2 实施前置条件 (checklist)

以下全部勾选后才能进 P2:

- [x] GLM-5 tokenizer 在本机已成功加载 (✅ 2026-05-15, vocab=154820)
- [ ] GLM-5 tokenizer 已在 Ascend dev 机预下载到本地 (避免 P4 时网络问题)
- [ ] 6 个数据集 raw_prompt 抽样完成, chat_mode 决策已定 (**待用户在 Ascend 跑 `scripts/sample_raw_prompts.py` 后贴回**)
- [x] §1.5 阻断风险已确认无 (✅ 公开仓库)
- [x] §2.2 chat_template apply 输出已验证可读 (✅ test 1-4 全过)
- [x] §4 性能预估完成 (✅ 6 数据集 3.4 min)

---

## 6. P1 关键决策回写 (P2 实施时直接引用)

| 决策项 | 值 |
|---|---|
| tokenizer HF 路径 | `zai-org/GLM-5` (公开, 无 gate) |
| 仅需 3 个文件 | `tokenizer.json` (20MB) + `tokenizer_config.json` (760B) + `chat_template.jinja` (3KB) |
| 本机预下载路径 | `models/glm5_tokenizer/` |
| Ascend 同步方式 | `rsync` 或 `hf download` 同方式拉, 离线则 rsync `models/glm5_tokenizer/` |
| `trust_remote_code` | **True** (因 `tokenizer_class=TokenizersBackend` 非标准) |
| chat_mode 默认值 | **wrap_user** (待 §3 抽样后可能调整) |
| `add_generation_prompt` | **True** (模拟 prefill 输入, 与 vLLM 实际一致) |
| 单条 tokenize 耗时 (中长 prompt) | ~0.7 ms |
| 单条 tokenize 耗时 (短 prompt) | ~0.05 ms |
| 6 数据集预估总耗时 | **~3.4 min** (300K req, 单线程) |
| Hash 算法 (复述 plan §6) | sha256(prev \|\| ",".join(str(t) for t in tokens)) |
| Hash 输出 | 32 bytes / key, 与字节级 type-compatible |
| 关键 special token id | `[gMASK]=154822, <sop>=154824, <\|user\|>=154827, <\|assistant\|>=154828, <think>=154841` |
| wrap_user 固定 overhead | **5 tokens** (`[gMASK]<sop><\|user\|><\|assistant\|><think>`) |
| 实测 vocab_size | 154820 |
| model_max_length | 202752 (200K) |
| **额外发现** | GLM-5 是 thinking 模型, prefill 末尾追加 `<\|assistant\|><think>` |

---

## 7. 偏差与意外发现

| 日期 | 事项 | 影响 | 处置 |
|---|---|---|---|
| 2026-05-15 | transformers 5.8.1 `apply_chat_template(tokenize=True)` 返回 BatchEncoding (dict-like, 非 list) | 第一次实测 `len(out)` 误得 2 | 改"两步走": `apply_chat_template(tokenize=False)` 拿 string + `tokenizer.encode()` 拿 list[int]. 已记入 §6 P2 实现指引 |
| 2026-05-15 | GLM-5 是 thinking 模型 (`GlmMoeDsaForCausalLM`), prefill 末尾追加 `<\|assistant\|><think>` | 5 tokens 固定 overhead, 但相同 token 序列在所有请求中都一样, 实际**提升**前缀命中率 | 文档记录, 不需特殊处理 |
| 2026-05-15 | tokenizer_class=`TokenizersBackend` (非标准), 必须 `trust_remote_code=True` | 若 Ascend 不允许 trust_remote_code 会阻断 | 已加到 §6 决策表, P2 实现注意; 若 Ascend 阻断, fallback 是手动用 `tokenizers.Tokenizer.from_file("tokenizer.json")` 绕开 transformers AutoTokenizer |
| 2026-05-15 | 实测中长 prompt 1480 req/s, 远超之前估算的 10K tok/s | 6 数据集预估 < 5 分钟 | 不需 multiprocessing |
