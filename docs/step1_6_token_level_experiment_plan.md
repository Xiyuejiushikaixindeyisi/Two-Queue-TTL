# Step 1.6: Token-Level KV Cache 命中率实验计划 (GLM-5)

**版本**: v1.0
**起草**: 2026-05-15
**状态**: 草案 (待用户审查 → 实施)
**目标读者**: 实施者 (本机调研 + 另一台 Ascend dev 环境执行)

---

## 0. TL;DR

把现有"字节级 ideal_hit_rate / LCP"工具链精确化为"token 级", 对齐推理框架真实路径.

### 0.1 标准 6 步流程 (用户精炼)

```
原始请求 (CSV row)
    ↓ Step 1: 按模型构造真实 prompt / chat template
        → tokenizer.apply_chat_template (GLM-5)
    ↓ Step 2: 使用对应 tokenizer
        → token_ids: list[int]
    ↓ Step 3: 按 vLLM 推理框架的 block size 切 block
        → block_size = 128 tokens
    ↓ Step 4: 构造 prefix-path block hash
        → keys: list[bytes]  (sha256 链, fallback 实现)
    ↓ Step 5: 计算 ideal prefix cache hit rate
        → 现有 trie / LCP / forest 复用
```

### 0.2 对齐项 (硬约束)

| 项 | 实施 |
|---|---|
| Chat template | `transformers.AutoTokenizer.from_pretrained("zai-org/GLM-5").apply_chat_template` |
| Tokenizer | **GLM-5 必须用 GLM-5 tokenizer** (无降级) |
| Block size | `128 tokens` (vllm-ascend 强制) |
| Hash 算法 | **SHA256 fallback** (`sha256(parent || ",".join(str(t) for t in tokens))`) — hash 函数不影响 hit_rate 数字 |
| chat_mode | **wrap_user** (默认; P1 调研后可能调整) |

新旧两套工具**完全独立**, 4 个分层各自有测试单元. 输入: 6 个 GLM-5 生产 CSV (`/data/<model>/raw/<model>.csv`). 输出: 6 份 APP 级 `per_user_report.html`, 标识 "Token-Level".

**下游 (LCP / trie / chain forest / 时序聚合 / HTML 渲染) 完全复用**, 不重写.

---

## 1. 实验目标 (Why)

### 1.1 当前痛点

现有工具 (`per_user_chain_analyzer.py` / `per_user_report_analyzer.py`) 用**字节级 SHA256 chain** 估算 ideal_hit_rate. 这导致:

| 偏差源 | 影响 |
|---|---|
| `block_size=128 bytes` 与 vllm 的 `128 tokens` 不同 (中文 1 token ≈ 2-3 bytes) | block 切分位置不对齐 → 真实命中边界外推 |
| `utf-8 bytes` ≠ tokenizer 输出 → 不带 chat template (无 `<|im_start|>` 等系统/角色标记) | 字节级把"raw prompt"当一个完整字符串, 但 vllm 实际看到的是"system msg + user msg + format tokens"拼接 → **前缀更长**, 字节级低估前缀重合, 但 block 切分错位又会高估命中 |
| ~~`SHA256` ≠ `vllm_hash`~~ | **hash 函数对 ideal_hit_rate 数字无影响** (只要 deterministic). 决策: 直接 sha256 fallback, 不复刻 vllm_hash |

实测净效应: 字节级 ideal_hit_rate **系统性偏高 0~30pp** (短 prompt 业务最严重, 长 prompt < 5pp). 详见 `docs/metrics_glossary.md §3`.

### 1.2 实验目标

1. **数字参考化**: 6 个 GLM-5 数据集的 token 级 ideal_hit_rate, 作为**参考数字** (无生产基准 prefix_cache_hit_rate 对照, 不做"误差 ≤ Xpp"硬验收).
2. **LCP 真实化**: APP 级 report 中"主链 LCP 长度"以 tokens 计 (而非 bytes), 能直接对应"省下多少 prefill compute".
3. **架构沉淀**: token-level pipeline 作为可选编码后端, byte-level 不退役, 两套并行 (新数据集快速 sanity check 仍走字节级, 精确分析走 token 级).
4. **6 份业务可读的 HTML 报告**, 标识清楚 "Token-Level (GLM-5 tokenizer, sha256 fallback, block_size=128 tokens)".

---

## 2. 4 层差异分析: 字节级 (现状) vs Token 级 (新)

两套 pipeline 共有 4 个独立分层. 用户明确要求:**逐层独立 + 各有测试单元**.

```
┌──────────────────────────────┬────────────────────────────────┬────────────────────────────────┐
│ 分层                          │ 字节级 (现状)                   │ Token 级 (新)                   │
├──────────────────────────────┼────────────────────────────────┼────────────────────────────────┤
│ Layer 1: iter_raw_records    │ csv.DictReader 读 4 列          │ 同上 (零修改)                    │
│  - CSV 解析 + 中文别名         │ → (req_id, user_id,            │ → (req_id, user_id,             │
│  - UTF-8 BOM                  │    raw_prompt, ts)             │    raw_prompt, ts)              │
├──────────────────────────────┼────────────────────────────────┼────────────────────────────────┤
│ Layer 2: chat_template_apply  │ ❌ 无                           │ ✅ tokenizer.apply_chat_template │
│  - 决定 vllm 实际看到的输入    │    (raw_prompt 直接进 layer 3) │    输入: messages list          │
│                               │                                │    输出: token_ids list[int]    │
├──────────────────────────────┼────────────────────────────────┼────────────────────────────────┤
│ Layer 3: split_into_blocks    │ utf-8 bytes → 128 bytes/block  │ token_ids → 128 tokens/block    │
│  - 切分单元                    │ split_blocks(raw, 128)         │ split_tokens(token_ids, 128)    │
├──────────────────────────────┼────────────────────────────────┼────────────────────────────────┤
│ Layer 4: hash_chain           │ SHA256(prev || block_bytes)    │ vllm_hash(parent, tuple(toks))  │
│  - 前缀链式 hash               │ compute_prefix_path_keys       │ compute_vllm_hash_chain         │
└──────────────────────────────┴────────────────────────────────┴────────────────────────────────┘
              ↓ 两套都输出: block_keys: list[bytes]
              ↓
┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 下游 (Layer 5+): 完全复用                                                                       │
│   - trie 插入 (verify_chain_path_closure.trie_insert)                                          │
│   - LCP 主链 (find_lcp / find_chain_forest)                                                    │
│   - per-request LCP histogram (p30/p80/top10)                                                  │
│   - reuse_time CDF / cache pressure / traffic spike / 用户分布                                   │
│   - HTML 渲染 (render_chains_html / render_user_report_html)                                    │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 关键决策点

- **Layer 4 输出统一为 `bytes`** (32 bytes / digest, sha256). 这样 trie / set / dict 全部不改.
- **Layer 2 的 messages 解析**: 生产 raw_prompt 可能是已经拼好的 chat 字符串 (含 `[INST]`, `<|im_start|>` 等), 也可能是纯用户输入. 通过 CLI flag 切换:
  - **mode=raw**: raw_prompt 已是 chat 字符串 → tokenize 直接编码, 不再 apply_chat_template
  - **mode=wrap_user** (**默认**): raw_prompt 是纯用户输入 → 包成 `[{"role":"user","content":prompt}]` 再 apply
  - **mode=messages**: raw_prompt 是 JSON-encoded messages list → 直接 apply
  - P1 抽 3-5 条 CSV 样本看清楚后, 可以调整默认 mode (本次决策: wrap_user, 偏差日志记录调整).

---

## 3. 架构: Strategy Pattern + 接口抽象

### 3.1 核心抽象

```python
# lib/prompt_encoder.py

from typing import Protocol

class PromptEncoder(Protocol):
    """字节级 / token 级共用接口.

    输入: raw_prompt 字符串 (CSV 一行)
    输出: block_keys: list[bytes] (32 bytes/key, 用于 trie / set / dict)

    实现必须满足: 同样的 raw_prompt → 同样的 block_keys (deterministic).
    实现可以缓存内部状态 (如 tokenizer), 但不应保留跨请求的状态.
    """

    name: str  # "byte_v1" | "glm5_token_v1" — 用于 HTML 标识 + 数据 JSON 元数据

    def encode(self, raw_prompt: str) -> list[bytes]:
        ...
```

### 3.2 两个实现

```python
# lib/prompt_encoder.py (续)

class ByteLevelEncoder:
    name = "byte_v1"
    block_size_bytes = 128

    def encode(self, raw_prompt: str) -> list[bytes]:
        encoded = raw_prompt.encode("utf-8")
        blocks = [encoded[i:i+self.block_size_bytes]
                  for i in range(0, len(encoded), self.block_size_bytes)]
        keys, prev = [], b""
        for b in blocks:
            prev = hashlib.sha256(prev + b).digest()
            keys.append(prev)
        return keys


class GLM5TokenEncoder:
    name = "glm5_token_v1"
    block_size_tokens = 128

    def __init__(
        self,
        model_path: str = "zai-org/GLM-5",
        chat_mode: str = "wrap_user",  # raw | wrap_user | messages
    ):
        from transformers import AutoTokenizer  # 延迟导入: byte-level 用户不需要
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        self.chat_mode = chat_mode

    def encode(self, raw_prompt: str) -> list[bytes]:
        # Layer 2: chat_template_apply
        token_ids = self._tokenize(raw_prompt)
        # Layer 3: split into 128-token blocks
        blocks = [token_ids[i:i+self.block_size_tokens]
                  for i in range(0, len(token_ids), self.block_size_tokens)]
        # Layer 4: sha256 chain (fallback, 不复刻 vllm_hash)
        keys, prev = [], b""
        for b in blocks:
            payload = ",".join(str(t) for t in b).encode("utf-8")
            h = hashlib.sha256()
            h.update(prev)
            h.update(payload)
            prev = h.digest()
            keys.append(prev)
        return keys

    def _tokenize(self, raw_prompt: str) -> list[int]:
        if self.chat_mode == "raw":
            return self.tokenizer.encode(raw_prompt, add_special_tokens=False)
        elif self.chat_mode == "wrap_user":
            messages = [{"role": "user", "content": raw_prompt}]
            return self.tokenizer.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True)
        elif self.chat_mode == "messages":
            messages = json.loads(raw_prompt)
            return self.tokenizer.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True)
        else:
            raise ValueError(f"unknown chat_mode: {self.chat_mode}")
```

### 3.3 现有 analyzer 改造点 (最小 diff)

`per_user_chain_analyzer.py` / `per_user_report_analyzer.py` 当前结构:

```python
blocks = split_blocks(raw_prompt, args.block_size)        # Layer 3
keys = compute_prefix_path_keys(blocks)                    # Layer 4
# ↓ 下游 trie / LCP / forest 用 keys
```

改成:

```python
keys = encoder.encode(raw_prompt)   # Layer 2-4 全部委托给 encoder
# ↓ 下游不变
```

`encoder` 由 CLI flag `--encoder {byte,glm5_token}` 决定, 默认 `byte` (向后兼容).

---

## 4. 新建 / 修改 模块清单

### 4.1 新建文件 (5 个)

| 文件 | 用途 | 依赖 |
|---|---|---|
| `lib/__init__.py` | 标记 lib 包 | — |
| `lib/prompt_encoder.py` | `PromptEncoder` Protocol + `ByteLevelEncoder` + `GLM5TokenEncoder` | hashlib, transformers (按需) |
| `lib/glm5_template.py` | GLM-5 chat template 封装 + messages 解析 helper | transformers |
| `tests/test_byte_encoder.py` | 字节级编码器单元测试 | pytest |
| `tests/test_token_encoder.py` | GLM-5 token 编码器 + sha256 fallback 单元测试 | pytest, transformers |

> 已删除: `lib/vllm_hash.py` 和 `tests/test_vllm_hash.py` (用户决策: hash 函数对 hit_rate 无影响, 直接 sha256 fallback).

### 4.2 修改文件 (4 个, 都是最小 diff)

| 文件 | 改动 |
|---|---|
| `scripts/per_user_chain_analyzer.py` | 加 `--encoder` flag, 用 encoder.encode() 替代 split_blocks + compute_prefix_path_keys |
| `scripts/per_user_report_analyzer.py` | 同上 |
| `scripts/render_user_report_html.py` | HTML 头加 encoder 标识 (e.g. "Token-Level: GLM-5, block_size=128 tokens") |
| `scripts/v2_run_pipeline.py` | 加 `--encoder` 透传到子命令 |

### 4.3 暂不动的文件

`scripts/quick_hit_rate.py` 保持字节级 — 这是 sanity check 工具, 不需要拖慢 (tokenizer 加载耗时).

---

## 5. 数据流水线

```
本机:
  docs/ + lib/ + tests/ 写完, 单元测试通过
        ↓
  push to git
        ↓
  Ascend dev 机:
    pip install transformers>=4.45 (GLM-5 需要)
    (可选) pip install -e vllm-ascend (验证 hash 算法)
        ↓
    git pull
        ↓
    pytest tests/                                 (验证 4 层独立 + token 一致性)
        ↓
    数据准备: 6 个 CSV 已放在 /data/<model>/raw/<model>.csv
        ↓
    for model in M1..M6:
      python3 scripts/v2_run_pipeline.py \
        --csv /data/<model>/raw/<model>.csv \
        --output-dir /data/<model>/out_token/ \
        --encoder glm5_token \
        --chat-mode wrap_user
        ↓
    产物: /data/<model>/out_token/per_user_reports/<user>.html  (6 模型 × N 用户)
        ↓
    (横向对比) 同时也跑一次 --encoder byte → /data/<model>/out_byte/, 写一份 byte vs token .md
```

---

## 6. Hash 算法决策 (已敲定: sha256 fallback)

### 6.1 决策与理由

**决策**: 直接用 `sha256(parent || ",".join(str(t) for t in tokens))` 作为 block hash, 不复刻 vllm_hash.

**理由** (用户决策):
- Hash 函数只要 deterministic, **就不影响 ideal_hit_rate 数字** (两个相同 token 序列 → 相同 hash; 两个不同的 → 不同 hash, 仅此而已).
- 复刻 vllm_hash 唯一的好处是"hash 值能和生产 vllm metrics 比对", 但本实验不需要此对照 (验收不依赖生产 prefix_cache_hit_rate, §9.3).
- 节省 P1 调研工作量约 0.3 day.

### 6.2 实现细节

```python
# lib/prompt_encoder.py 中 GLM5TokenEncoder.encode() 内嵌
keys, prev = [], b""
for block_tokens in blocks:  # block_tokens: list[int], len ≤ 128
    payload = ",".join(str(t) for t in block_tokens).encode("utf-8")
    h = hashlib.sha256()
    h.update(prev)
    h.update(payload)
    prev = h.digest()
    keys.append(prev)
```

特性:
- 输出 32 bytes / key, 与字节级 sha256 链 type-compatible (下游 trie / set 完全复用)
- Deterministic
- 前缀链式 (`keys[k]` 依赖 `keys[k-1]`), 这是 LCP 计算的必要条件
- 与 vllm_hash **不 bit-exact**, HTML 头标注 `hash_algo=sha256_fallback`

### 6.3 不影响项 (Mythbusters)

| 担心 | 实际 |
|---|---|
| sha256 比 builtin hash 慢 | 慢一倍, 但绝对值 < 0.01ms / block, 100K block trace 多耗 1 秒 |
| sha256 vs vllm_hash 数值不同, 导致 ideal_hit 不同 | **否**. 相同输入 → 相同 hash (无论哪种算法), 命中判断只看"是否相等" |
| 32 bytes vs 8 bytes 影响 trie/set 性能 | 不影响. dict 用 32 bytes key 与 8 bytes key 性能差 < 5% |

---

## 7. GLM-5 chat template apply 调研重点 (Phase 1)

### 7.1 待回答问题

1. **GLM-5 tokenizer 能否 from HF 直接拉?** (`zai-org/GLM-5` 是否公开, 是否需要 HF token)
2. **GLM-5 chat template 含哪些 special tokens?** (用最小 messages 跑一次 `apply_chat_template`, 打印 token_ids + decoded string)
3. **生产 raw_prompt 是哪种 chat_mode?**
   - 看 6 个 CSV 中 raw_prompt 字段的真实内容. 是否含 `<|im_start|>`/`[INST]` 等 → 决定用 raw 还是 wrap_user
4. **tokenizer 加载速度?** (大模型 tokenizer 通常 < 2 秒, 应无问题)
5. **add_generation_prompt 设 True 还是 False?**
   - vllm 实际跑的时候是 True (server 接到请求后, 在 messages 末尾追加 assistant 起始 token, 然后 prefill)
   - 我们要模拟的是"prefill 输入", 所以 True

### 7.2 调研脚本 (Phase 1 产出)

```python
# tests/explore_glm5_template.py (不进 pytest, 仅手动跑)
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained("zai-org/GLM-5", trust_remote_code=True)

# 1. 看 special tokens
print(tok.special_tokens_map)

# 2. 看 chat template (jinja 源码)
print(tok.chat_template)

# 3. 跑一次最小 messages
msgs = [{"role": "user", "content": "你好"}]
ids = tok.apply_chat_template(msgs, tokenize=True, add_generation_prompt=True)
print("token_ids:", ids)
print("decoded:", tok.decode(ids))
print("len:", len(ids))

# 4. 看 raw_prompt 实际样子 (从一个 CSV 抽 3 条)
import csv
with open("/data/sample/raw/sample.csv", encoding="utf-8-sig") as f:
    for i, row in enumerate(csv.DictReader(f)):
        if i >= 3: break
        print("---")
        print(row.get("raw_prompt") or row.get("请求参数"))
```

---

## 8. 测试策略

### 8.1 单元测试 (4 层各自独立)

**`tests/test_byte_encoder.py`**:

```python
def test_byte_encoder_deterministic():
    enc = ByteLevelEncoder()
    assert enc.encode("hello") == enc.encode("hello")

def test_byte_encoder_prefix_growth():
    enc = ByteLevelEncoder()
    a = enc.encode("a" * 130)   # 2 blocks
    b = enc.encode("a" * 260)   # 3 blocks
    # prefix 2 blocks 必须相同
    assert b[:2] == a[:2]

def test_byte_encoder_matches_quick_hit_rate():
    # 用一个 5 行的小 CSV, 字节级 ideal_hit_rate 应与 quick_hit_rate.py 输出 bit-exact 一致
    ...
```

**`tests/test_token_encoder.py`**:

```python
def test_glm5_tokenizer_loads():
    enc = GLM5TokenEncoder()
    assert enc.tokenizer is not None

def test_token_encoder_deterministic():
    enc = GLM5TokenEncoder(chat_mode="wrap_user")
    assert enc.encode("你好") == enc.encode("你好")

def test_token_encoder_prefix_growth():
    enc = GLM5TokenEncoder(chat_mode="wrap_user")
    # 同前缀 + 加长后缀, 前 K block 必须相同
    ...

def test_apply_chat_template_consistency():
    """token 化结果与 transformers 直接 apply 一致 (验证 layer 2 没改输入)"""
    enc = GLM5TokenEncoder(chat_mode="wrap_user")
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained("zai-org/GLM-5", trust_remote_code=True)
    msgs = [{"role":"user","content":"你好"}]
    expected = tok.apply_chat_template(msgs, tokenize=True, add_generation_prompt=True)
    assert enc._tokenize("你好") == expected
```

**sha256 fallback 不需要独立 test 文件**, 因为 hash 逻辑内嵌在 `GLM5TokenEncoder.encode()` 中, 由 `test_token_encoder.py::test_token_encoder_prefix_growth` 和 `test_token_encoder_deterministic` 隐式覆盖.

若希望显式断言 hash 行为, 可在 `test_token_encoder.py` 加:

```python
def test_token_hash_chain_prefix_property():
    """前缀相同 → 前 K 个 hash 相同 (LCP 计算的必要条件)"""
    enc = GLM5TokenEncoder(chat_mode="wrap_user")
    a = enc.encode("你好")
    b = enc.encode("你好, 今天天气怎么样?")
    # 至少前 1 个 block 的 hash 应该相同 (因为 token prefix 重合, block 切分对齐)
    assert a[0] == b[0] if len(a) >= 1 and len(b) >= 1 else True
```

### 8.2 集成冒烟测试

`tests/test_pipeline_integration.py`:

```python
def test_byte_vs_token_direction():
    """同一份小 CSV, token 级 hit_rate ≤ byte 级 hit_rate (验证差距方向正确).

    依据: 字节级因 block 切分错位和无 chat template, 系统性偏高.
    """
    small_csv = "tests/fixtures/mini_trace.csv"  # 5 用户, 50 请求
    byte_hit = run_pipeline(small_csv, encoder="byte")["ideal_hit_rate"]
    token_hit = run_pipeline(small_csv, encoder="glm5_token")["ideal_hit_rate"]
    assert token_hit <= byte_hit + 0.05  # 允许 5pp 噪声
```

### 8.3 不测什么

- 不测 HTML 渲染细节 (已经经过 9 模型实战验证, 改动只是 header 标识)
- 不测 trie / LCP / chain forest 内部逻辑 (这些是从 Step 1 沿用的成熟模块, 已经 cb93287 修过 sparse trace bug)

---

## 9. 6 数据集实验路径与验收

### 9.1 数据集准备 (在 Ascend dev 机)

```
/data/M1/raw/M1.csv
/data/M2/raw/M2.csv
...
/data/M6/raw/M6.csv
```

> 用户提供 6 个数据集时, 应一起提供:
> - 每个数据集对应的"业务说明" (chatbot / RAG / code-gen / ...)
> - 每个数据集对应的"生产 prefix_cache_hit_rate" (用于验收 §9.3)

### 9.2 执行命令 (单数据集)

```bash
# 步骤 1: token 级精确分析
python3 scripts/v2_run_pipeline.py \
  --csv /data/M1/raw/M1.csv \
  --model-name M1 \
  --output-dir /data/M1/out_token/ \
  --encoder glm5_token \
  --chat-mode wrap_user \
  --block-size 128

# 步骤 2: 字节级对照 (复用现有工具, 无修改)
python3 scripts/v2_run_pipeline.py \
  --csv /data/M1/raw/M1.csv \
  --model-name M1 \
  --output-dir /data/M1/out_byte/ \
  --encoder byte \
  --block-size 128

# 步骤 3: 对比脚本 (新写一个小工具)
python3 scripts/compare_byte_vs_token.py /data/M1/out_byte /data/M1/out_token
```

### 9.3 验收标准

每个数据集都要产出:

1. **APP 级 HTML** (`/data/<model>/out_token/per_user_reports/<user>.html`) — 必须能在浏览器打开, 顶部清楚标识 "Token-Level (GLM-5 tokenizer, sha256 fallback, block_size=128 tokens)".
2. **数据 JSON** (`/data/<model>/out_token/per_user_report_<user>.json`) — 包含 `encoder_meta: {name, block_size, chat_mode, hash_algo}` 元数据.
3. **byte vs token 对比表** (一份 .md, 6 行) — 列: `model | ideal_hit_byte | ideal_hit_token | delta_pp | 主链 LCP_byte | 主链 LCP_token`.
4. **方向性合理**: 6 个数据集中 ≥ 4 个 token 级 hit_rate ≤ byte 级 hit_rate (符合"字节级系统性偏高 0-30pp"假设). 不要求精确数字, 仅要求方向.

> **不做硬验收的项** (用户决策): 不要求 token vs 生产 prefix_cache_hit_rate 误差, 数据集不带生产基准, ideal_hit_rate 只是**参考数字**.

### 9.4 性能预算

- 单个 CSV (10K 请求, 平均 prompt 2K bytes ≈ 800 tokens) tokenize 总耗时: 预估 < 60 秒 (transformers tokenizer 单线程 ~ 10K tokens/s)
- 6 数据集总耗时: < 10 分钟, 可忽略
- 内存: token_ids 缓存峰值 < 1GB

---

## 10. 实施步骤 (Phase 拆分)

| Phase | 工时 | 内容 | 交付物 |
|---|---|---|---|
| **P1: 调研** | 0.3 day | §7 GLM-5 tokenizer + chat template + raw_prompt 样本 (vllm_hash 已决策跳过) | `docs/step1_6_phase1_findings.md` 单独文档 |
| **P2: 抽象层 + token encoder** | 0.8 day | 写 `lib/prompt_encoder.py` + `lib/glm5_template.py` + 测试 | `tests/test_*.py` 全绿 |
| **P3: 接入现有 pipeline** | 0.5 day | 改 `per_user_chain_analyzer.py` / `per_user_report_analyzer.py` / `render_user_report_html.py` / `v2_run_pipeline.py` 加 `--encoder` flag | byte 模式 CI 不退化 |
| **P4: 6 数据集运行 + 对比** | 0.5 day | 在 Ascend dev 机跑 6 模型, 产出 6 HTML + byte/token 对比 .md | `/data/M*/out_token/*.html` + 对比 .md |

**总工时: ~2.1 day**.

### 10.1 实施风格守则 (沿用项目惯例)

- 每个 Phase 前后**先更新本文档 §12**, 锚定决策点
- 测试不通过不进下一阶段
- byte 模式作为 regression baseline, 任何 Phase 都不能破坏 byte 模式
- HTML 改动凡涉及 "Token-Level" 标识, 用户审一遍再批量跑

---

## 11. 风险与备选方案

| 风险 | 概率 | 影响 | 备选 |
|---|---|---|---|
| `zai-org/GLM-5` 在 HF 需要 access token / 仓库不存在 | 中 | **实验阻断** | 用户决策: **必须用 GLM-5 tokenizer, 无降级**. P1 一旦确认不可用, 立即停 P2, 联系用户决定 (申请 token / 离线拉取 / 暂停实验) |
| ~~vllm_hash 算法定位困难~~ | — | — | 已规避: 决策直接 sha256 fallback (§6) |
| 生产 raw_prompt 是已格式化字符串, wrap_user 模式会**重复**添加 special tokens | 高 | token 级数字虚低 | P1 调研后切到 `chat_mode=raw`; 在 `step1_6_phase1_findings.md` 记录决策 |
| Ascend dev 机无 transformers / 网络受限不能拉 HF | 中 | P4 卡死 | 在本机预下载 tokenizer 到 `models/glm5/`, push 到 git LFS 或 rsync 过去 |
| 6 数据集 raw_prompt 含大量 truncate (e.g. "...续" / 截断符号) | 低 | tokenize 出错 | encode 时 try/except, 失败请求计入 `n_failed_tokenize` 字段, HTML 显示占比 |
| token 化后某些请求 token 数 < 128 (1 block 都装不满) | 中 | LCP 长度信号弱 | 与 byte 级一致, 单 block 仍走 trie, 自然降级 |

---

## 12. 偏差日志 (实施中填)

> 实施每个 Phase 后, 把"预期 vs 实际"的差异记录在这里, 防止后续遗忘.
> 格式: `[YYYY-MM-DD] [Phase] 事项 | 预期 | 实际 | 处置`
> P1 调研详细结果在 `docs/step1_6_phase1_findings.md`, 此处只记结论 + 调整.

### Phase 1 调研结论 (待填)
- [ ] GLM-5 tokenizer HF 路径: ______ (是否可公开拉取: Y/N)
- [ ] 6 个数据集中 raw_prompt 真实样子: 是否已含 chat 标记? → 选定 chat_mode: ______
- [ ] 若 GLM-5 不可用 → 暂停决策: ______

### Phase 2/3/4 偏差 (待填)
- [ ] _______________

---

## 13. 与本项目其他文档的关系

| 文档 | 关系 |
|---|---|
| `docs/step1_runbook.md` | 本文档是 step1 的"精确化补丁", 不替代 step1 |
| `docs/metrics_glossary.md §3` | 偏差来源已记录, 本实验是验证 + 修正 |
| `docs/step2_experiment_priorities.md` | step2 实验排期基于字节级数字; 如果 token 级与字节级差距 ≤ 5pp, **排期不变**; 如果差距大, step2 P0-P3 需要重新校准 |
| `docs/step3_algorithm_decision_matrix.md` | A/B/C 决策矩阵的"用户 hit_rate"阈值用的是字节级. token 级实验后可能需要调整 §9.2 的阈值带 |
| `scripts/quick_hit_rate.py` | 不动. 字节级仍是"新数据快速 sanity check"的默认工具 |

---

## 14. 决策记录 (2026-05-15 用户确认)

| # | 决策项 | 选定 | 理由 |
|---|---|---|---|
| 1 | **chat_mode 默认值** | `wrap_user` | 最保守, P1 抽样后若 raw_prompt 已含 chat 标记则调整为 `raw` |
| 2 | **Hash 算法** | sha256 fallback, 不复刻 vllm_hash | hash 函数对 ideal_hit_rate 数字无影响 (只需 deterministic), 节省 0.3 day 调研 |
| 3 | **GLM-5 不可用时** | **不降级**, 必须用 GLM-5 tokenizer | 模型与 tokenizer 必须严格对齐, 否则数字不可信. 阻断时联系用户 |
| 4 | **验收基准** | 不带生产 prefix_cache_hit_rate, ideal_hit_rate 是参考数字 | 数据集不提供生产基准, §9.3 验收只看"方向性合理" |
| 5 | **Phase 1 调研产出** | 单独写 `docs/step1_6_phase1_findings.md` | 调研详情独立, 本计划文档保持稳定 |

---

*本文档 P1 调研后小修偏差日志, P2-P4 实施完后做一次终版定稿.*
