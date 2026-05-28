# 术语与 Key 口径规范

本文档是全项目的术语锚点。所有代码、注释、测试、文档中涉及 cache key 和 block 身份识别的地方，
必须遵循本规范。任何偏离须在 PR 中显式说明理由。

---

## 1. hash_ids[i] — 原始 block content hash

**定义**：TraceRecord 中 `hash_ids` 列表的第 i 个元素。  
**语义**：block i 的 **per-block 内容哈希**（SipHash-2-4 of 16/128 tokens），由采集器写入离线 trace。  
**类型**：`str`  
**重要限制**：

- `hash_ids[i]` **不是**链式哈希，不编码前序 block 的信息。
- 两个请求可能在同一位置 i 持有相同的 `hash_ids[i]`（相同 token 内容），
  但若它们的前缀路径不同，其 KV tensor 在位置 i 完全不同
  （自注意力包含所有前序 context）。
- 因此，`hash_ids[i]` **不能直接用作 prefix cache 的 lookup key**。

---

## 2. prefix-path key — 链式前缀路径 key

**定义**：由 `sim/core/prefix_key.py::make_prefix_path_keys()` 生成的 SHA-256 链式哈希。  
**类型**：`str`（32 字节 SHA-256 digest 的 hex 编码，64 字符）  
**生成公式**：

```
K[0] = sha256( NONE_SEED || encode(model_id) || encode(hash_ids[0]) )
K[i] = sha256( K[i-1]    || encode(model_id) || encode(hash_ids[i]) )
```

其中 `encode(s) = 4字节大端长度前缀 + UTF-8 字节`（防止拼接歧义）。

**正确性保证**：

```
K[i] == other_K[i]  当且仅当  model_id 相同  且  hash_ids[:i+1] 完全相同
```

这与 vLLM `hash_block_tokens` 的链式语义对齐：block i 的命中条件是整个前缀链
`[block_0 … block_i]` 的 token 内容完全相同。

**与 vLLM 的差异**：

| 项目 | vLLM | 本项目离线模拟 |
|------|------|----------------|
| NONE_SEED | 随机或 PYTHONHASHSEED 初始化 | 固定常量（保证离线可重现） |
| model_id 位置 | 不在 hash 链内（进程级隔离） | 每步都包含（共用单一 dict） |
| 哈希函数 | sha256_cbor | sha256（raw bytes 拼接） |

---

## 3. content_hash — block 原始内容哈希元数据

**定义**：`BlockMeta.content_hash`，存储对应 `hash_ids[i]` 原始字符串。  
**类型**：`str`  
**用途**：调试、non-prefix potential 分析、block 内容层面的统计。  
**约束**：**不参与** prefix cache lookup，不作为策略 dict 的 key。

---

## 4. PrefixCache 的 cache key 规范

`PrefixCache.process_request()` 必须：

1. 调用 `make_prefix_path_keys(record.model_id, record.hash_ids)` 生成 `prefix_keys`；
2. 以 `prefix_keys[i].hex()` 作为所有策略操作（`access`、`add`、`pinned` 集合）的 key；
3. 调用 `policy.add(block_key, content_hash=record.hash_ids[i], ...)` 同时保存原始内容哈希。

**不允许**直接将 `record.hash_ids[i]` 传入策略操作。

---

## 5. FutureIndex 的统计口径

`FutureIndex.build(trace)` 只统计 **prefix-path future occurrence**：

- 对 trace 中每条 record，展开为 `make_prefix_path_keys()` 生成的 prefix-path key 列表；
- 索引存储为 `Dict[str, List[float]]`（hex key → 时间戳列表）；
- **不索引** 原始 `hash_ids[i]` 字符串。

查询语义：`has_future_access(key, after_time)` 返回 `True` 当且仅当 key 在
`after_time` 严格之后（`timestamp > after_time`）至少出现一次。

---

## 6. 全链路 key 类型一致性

| 组件 | key 类型 | 备注 |
|------|----------|------|
| `AbstractCachePolicy` 接口 | `str` | prefix-path hex |
| `LRUPolicy` / `TTLLRUPolicy` / `TwoQueueTTLPolicy` | `str` | 同上 |
| `BlockMeta.block_key` | `str` | prefix-path hex |
| `BlockMeta.content_hash` | `str` | 原始 hash_ids[i] |
| `FutureIndex` 内部索引 | `Dict[str, ...]` | hex str |
| `MetricsCollector._future_access` | `Dict[str, ...]` | hex str |
| `eviction_log` 条目中的 key | `str` | `meta.block_key` |
| `pinned` 集合 | `Set[str]` | prefix-path hex |

**严禁** 在任何上述组件中使用 `bytes` 类型或原始 `hash_ids[i]` 字符串作为 lookup key。

---

## 6.5 输入形态 → key 体系对照 (现网分析 vs 仿真)

平台有两套 key 生成路径, **取决于输入数据的形态**。看到一个脚本/数据时, 先对号入座:

| 输入形态 | 例子 | 用哪套 key | 怎么得到 block key | 谁用 |
|---|---|---|---|---|
| **raw prompt** | 生产 CSV 的 `请求参数`、txt 文本 | `lib.prompt_encoder` (`HFTokenEncoder.encode`) | tokenize 后**直接链化** `K[i]=sha256(K[i-1]‖block_tokens)` | `dataset_hit_rate` / `model_report` / `app_report` / `convert_trace` |
| **converted trace** | `convert_trace --mode chat` 产出的 `hash_ids_*` 列 | 列里**已是**链化好的 hex key | `bytes.fromhex(hex)` 直接当 block key | `per_user_report_4variant`、`app_report` 的 CSV 内部 |
| **simulation trace** | `sim/` 仿真用 trace 的 `hash_ids` (未链化的 block 内容 hash) | `sim.core.prefix_key` **再链化** | `K[i]=sha256(K[i-1]‖model_id‖hash_ids[i])` | `run_simulation` / `experiment_plan_ab` / … (Step 2/3, 已 out-of-scope) |

关键区别: **raw prompt** 路径自己 tokenize + 链化 (现网分析主线, 见 [USAGE](../USAGE.md) ⚡/📄 章节);
**simulation trace** 路径拿到的是别人算好的 *未链化* block 内容 hash, 必须用 `sim.core.prefix_key`
按 §2 规则再链化才能当 cache key (直接用 `hash_ids[i]` 是错的, 见 §1)。两者最终都满足 §6 的
「全链路 prefix-path hex」一致性。

---

## 7. TwoQueueTTLPolicy — promotion_threshold 语义

### hit_count 的定义

`BlockMeta.hit_count` 只计 **cache hit 次数**，不包含首次插入（`add()`）。

| 事件 | hit_count 变化 |
|------|--------------|
| `add()` 插入 cache | 初始化为 0，**不递增** |
| `access()` 命中（block 在 cache 中） | `+1` |
| `access()` 未命中（block 不在 cache）| 不变（返回 False，不更新 meta） |

### promotion_threshold 的含义

晋升条件：`hit_count >= promotion_threshold`，在 `access()` 中每次命中后立即检查。

| promotion_threshold 值 | 含义 | trace 中最少出现次数 |
|------------------------|------|---------------------|
| 1 | 第一次后续命中即晋升（激进保护） | 2 次（1 miss + 1 hit） |
| 2（默认） | 需要两次后续命中才晋升（保守保护） | 3 次（1 miss + 2 hits） |
| N | 需要 N 次后续命中才晋升 | N+1 次 |

**默认值 `promotion_threshold=2`** 的语义：

```
Request 1: [block_x, ...]  →  block_x: MISS  (add,  hit_count=0)
Request 2: [block_x, ...]  →  block_x: HIT   (access, hit_count=1, 未晋升)
Request 3: [block_x, ...]  →  block_x: HIT   (access, hit_count=2, 触发晋升 → Protected)
```

一个 block 至少需要在 trace 中出现 **3 次**，才能在默认配置下进入 Protected 队列。

### 测试场景标注规范

| promotion_threshold | 场景标注 |
|--------------------|---------|
| 1 | `aggressive_promotion`（激进晋升，第 2 次出现即保护） |
| 2 | `default_promotion`（默认配置，第 3 次出现才保护） |
| ≥3 | `conservative_promotion`（保守晋升） |

Trace C 的两个变体分别对应这两种配置（见 `tests/integration/test_policy_comparison.py`）。
