# 指标与图表释义 — token-level HTML 报告读法

> **适用范围**: `outputs/<model>/per_user_reports/<uid>/user_report.html` (Step 1.6+, token-level encoder)
> **配套文档**: [`USAGE.md`](../USAGE.md) (使用流程)
>
> 主线: token-level (block_unit=tokens, 与 vLLM 一致) + 真实 GB/min KV cache 压力估算.
> byte-level (regression baseline, 字节级 prompt block) 见文末 Appendix.

## 0. 一句话使用顺序

> 先看 §1 模型层 `reuse_inversion_ratio` + `GB/min p80` 定算法大方向 → §3 user-level `unique blocks (含当天总 GB)` 看个体规模 → §5 cache 压力时序 + cumulative WS 收敛 → §6 reuse_time CDF 看复用窗口 → §7 chain forest 看共享前缀 + LCP top-10 → §8 算法推荐.

---

## 1. 指标释义

### 1.1 模型层 (§1)

| 指标 | 定义 | 决策意义 |
|---|---|---|
| **ideal_hit_rate** (aggregate) | `sum(hit_blocks) / sum(total_blocks)` 跨所有 top-K user; token 级与 vLLM 一致 | **prefix cache 优化的上限**. 实际 ≤ 这个值. 高 → 有优化空间; 低 → 跳过这个场景 |
| **rpm avg + p80** | 每分钟请求数 (整模型, 不限 top-K) | 流量强度. 高 RPM + 多租户 → A 路由聚合 |
| **unique_rpm avg** | 每分钟新 unique block 数 (top-K user 之和) | cache 周转速率, 与 GB/min 等价 |
| **GB/min p80** (token mode) / **new_block/min p80** (byte mode) | model-level cache 压力 P80; token 模式 = `blocks/min × block_size × kv_bytes_per_token / 1024³` | 与物理 KV 容量比较: ≪ → 现有 OK; ≈ → 容量分区/淘汰; ≫ → offloading/池化 |
| **reuse_inversion_ratio** | `max(user.hit) / min(user.hit)`, ≥ 2.0 触发 inversion | 用户复用率差距. ≥ 2.0 → 路由分流必要 (避免低 reuse 用户挤掉高 reuse 用户) |

人工补字段 (`model_report.json` 顶级):

| 字段 | 含义 | 决策意义 |
|---|---|---|
| `model_params_class` ⚠️ | `small_le_32B` / `large_200B_moe` | 大模型不能多实例物理隔离 |
| `instance_count` ⚠️ | 部署实例数 | A 路由可行性: > 1 才能物理隔离 |
| `cache_capacity_blocks` ⚠️ | 单实例 cache 容量 (block 数) | C 池化决策核心: < unique → 必须 C |

### 1.2 用户层 (§3 user metrics)

| 指标 | 定义 | 决策意义 |
|---|---|---|
| **requests** | 该 user 请求数 + 占模型 % | 用户体量 |
| **ideal hit rate** | 该 user 的 LCP 累加 / total_blocks (token 级) | 该用户的优化上限 |
| **max prompt length** | 字符数 + utf-8 字节数 + avg B/req | 配合 block_size 推算单请求 block 数 |
| **total blocks / unique blocks / hit blocks** | 字面意义 | unique blocks 副标题自动算 `≈ X.XXX GB total` (当天累计 KV cache 字节) |
| **rpm / unique_rpm** | 该 user 内的 req/min + new-block/min |  |
| **chain_length_ratio** (chain_forest_summary) | `dominant_chain_length / avg_blocks_per_request` | chain 占 request 的体量; > 0.3 长 (B(2)/A(2) 候选); ≤ 0.3 短 |
| **share_of_model_unique** | `user_unique / sum(Top-K user unique)` | hit 高 → cache 主贡献者; hit 低 → 污染源 |

---

## 2. 图表看点

### 2.1 §4 Requests per minute (柱状图)

- 趋势: 单调 / spike / 时段分布
- 平均 vs 峰值: spike 是否被 §4 spike 检测 (≥ 5×) 自动捕获
- 空窗期: 业务断续 → pin 的 chain 可能被 idle 驱逐

### 2.2 §3 user-level inter-arrival 表 (gaps p50/p75/p80/p95/max)

- `p50 = 0`: 大量同秒 batch 到达 → A 层 batch 聚合 ROI 高
- `p50 ≥ 60s`: 单请求散开 → cache 压力小, batch 机会少
- `max` 极大: 业务断续期

### 2.3 §5 cache 压力 — new_block/min + GB/min 图 + quantile 表 — 最重要

- 时序图 x=minute, y=blocks (token 模式同时显示 GB/min p80/p95 参考线)
- **quantile 表 (P50/P80/P95/Max)** 两行: `new_block/min` + `GB/min` (token 模式才有第 2 行)
- **GB/min 公式**: `blocks/min × block_size × kv_bytes_per_token / 1024³`
  - block_size: 128 tokens (token-level encoder)
  - kv_bytes_per_token: 见 [`models/<name>_tokenizer/kv_meta.json`](../models/README.md). 例: Qwen3-8B GQA = 147,456; GLM-5 MLA = 89,856
- **决策**: 与物理 KV cache 容量量级比较
  - GB/min ≪ 容量 → 现有 prefix cache 已 OK, 跳过
  - GB/min ≈ 容量 → 容量分区 / 淘汰算法 (LRU 变种)
  - GB/min ≫ 容量 → offloading / 多级缓存 / 池化

### 2.4 §5 Cumulative unique blocks (working set 线图)

- **斜率**: 陡 = cache 快速膨胀; 平 = 大量复用
- **是否收敛**: 曲线趋平 → WS 饱和 → C 容量保障可行; 持续上升 → C 收益有限
- **最终高度**: cache 容量需求下界 (容量 ≥ 最终高度 才能 hold 全部 unique)

### 2.5 §6 reuse time CDF + 4 分位数

- **p50 / p75 / p80 / p95**: 复用时间分布
- **核心算式**: `cache_pressure_gb_per_min × reuse_time_p95_minutes ≈ 需要的 cache 容量`
- 短 reuse_time → 容量需求低; 长 → 需大 cache 才能维持 hit_rate

### 2.6 §6 per-request LCP histogram (含 top-10)

→ 见 §5 详解.

### 2.7 §7 Chain forest

- **chain 数量**: 1 单业务 / 2-3 主辅 / ≥ 4 多任务 (A(2) 候选)
- **chain length**: ≤ 50 短 → B(2) pin 划算; ≥ 500 长 → A(1) chain affinity 比 pin 更优
- **leaf_cov vs max_prefix_cov**: 差距大 → chain 头几个 block 有更广共享 (shadow group 信号)
- **decoded content**: 判断真业务前缀 vs wrapper boilerplate

### 2.8 §8 算法推荐 (v2 子类型)

- **3 个 badge**: A 子类 / B 子类 / C 子类
- **A(4) 黄色警告**: 大模型暂缓, 先补 instance_count + cache_capacity_blocks
- **B(2) 灰色说明**: 淘汰打分公式待真机实测调参
- **反常 ⚠️**: 长 chain + 低 cov + 低 hit, 回 §7 看 decoded

---

## 3. 算法逻辑

### 3.1 token-level encoder (block 切分 + prefix_path_key)

**代码**: `lib/prompt_encoder.py:HFTokenEncoder.encode()` + `lib/hf_tokenizer.py:apply_template()`

```python
def encode(raw_prompt):
    token_ids = apply_template(self.tokenizer, raw_prompt, self.chat_mode)  # HF tokenizer
    keys = []
    prev = b""
    for block in chunked(token_ids, self.block_size_tokens):  # 默认 128 tokens
        payload = ",".join(str(t) for t in block).encode("utf-8")
        prev = sha256(prev + payload).digest()                # hash chain
        keys.append(prev)
    return keys
```

**核心性质**: `prefix_path_key` 是**哈希链** — 某 key 见过 ⟹ 它前面所有 key 必然见过 (hash 抗碰撞). 让 LCP 计算从 O(N²) 降到 O(N) 单次扫描.

**chat_mode**:
- `wrap_user` (推荐): raw_prompt → `[{"role":"user","content":...}]` → `apply_chat_template(add_generation_prompt=True)` → encode
- `raw`: raw_prompt 已是 chat 字符串, 直接 encode
- `messages`: raw_prompt 是 JSON-encoded messages list

不同模型的 wrap_user overhead 不同 (GLM-5: 5 tokens, Qwen3: 8 tokens), chat_template 自动处理, **不影响 hit_rate 算法**.

### 3.2 LCP 计算 (per-request)

**代码**: `scripts/per_user_report_analyzer.py` (single-pass 内)

```python
lcp = 0
for k in keys:                  # 当前 request 的 block keys 顺序遍历
    if k in seen_keys:
        lcp += 1
    else:
        break                   # hash-chain 保证: 一旦 miss, 后面都不需要查
hit_blocks += lcp
per_request_lcp.append(lcp)
seen_keys.update(keys)
```

- LCP = 当前 request 从 block 0 起**连续命中**的 block 数
- `ideal_hit_rate = sum(hit_blocks) / sum(total_blocks)` (**无淘汰上限**, 实际 ≤ 这个值)

### 3.3 全局 chain (greedy max-child walk)

**代码**: `scripts/verify_chain_path_closure.py` / `scripts/per_user_chain_analyzer.py`

```
1. 把所有 request 的 keys 插入 trie (每 node 维护 count + sample_request_id)
2. 从 root 出发:
   while node.children:
       max_child = argmax(children.count)
       cov   = max_child.count / total_requests   # 全局覆盖率
       ratio = max_child.count / node.count       # 局部"赢家通吃"比例
       if cov   < coverage_threshold:  break  # 主 chain 已稀薄
       if ratio < branch_threshold:    break  # 主 chain 不再强势
       chain.append(max_child); node = max_child
```

两个停止条件:
- `coverage_threshold` (默认 0.05): max_child 在**全模型**中占比 ≥ 5%
- `branch_threshold` (默认 **0.25**): max_child 在**当前 parent** 中占比 ≥ 25% (2026-05-12 修订, 见 [archive/model_portraits.md §3.8](archive/model_portraits.md))

### 3.4 多 chain forest (per-user)

**代码**: `scripts/multi_chain_finder.py`

不再贪心走单条 max-child, 而是 trie DFS 找**所有满足条件的叶子**:
- 沿途记录每个 chain 的 `coverage_pcts[]`
- pruning: `min_chain_length` + `min_chain_coverage` + `max_chains`
- 同一 trie 可产出多条独立 chain

---

## 4. chain_threshold_sweep 图 (`per_user_chains.html` §4)

由 `per_user_chain_analyzer.py` (`--no-threshold-sweep` 关闭) + `render_chains_html.py` 渲染.

### 4.1 横纵轴

- **x 轴**: `branch_threshold ∈ [0.00, 1.00]`, 21 个点 (步进 0.05)
- **y 轴**: 在该 threshold 下检测到的全局 chain length (block 数)

### 4.2 三段曲线含义

```
chain_length
   ↑
   │     ┌──── 平台期 ────┐
 L │─────────────────────┐    L = trie 中主前缀的"自然长度"
   │                     │
   │                     │
 0 │                     └──────────────  → x
   0      ────平台────    X            1.0
                          ↑
                     主 chain 第一个 branch 处的 ratio
```

| 区段 | x 范围 | 含义 |
|---|---|---|
| **平台期** `[0, X]` | branch_threshold ≤ 主 chain 沿途每个 branch_ratio | 主 chain 整条通过, chain length = L |
| **陡降点** `X` | 等于主 chain 的**最弱一处** branch_ratio | 第一个 branch_ratio 不够, chain 截断 |
| **零区域** `(X, 1.0]` | 严苛到 root 处 max_child 都不够 | chain = 0 |

### 4.3 4 条可读信息

1. **L (平台高度)** = 主前缀自然深度 — 业务 system_prompt 多深
2. **X (陡降点)** = 主 chain 最弱环节强度 — X 越大主 chain 越强势
3. **平台是否真"平"** — 真平 = 沿途 branch_ratio 接近; 阶梯下降 = 中间有"强环节"
4. **选 threshold 时**: 取平台中段, 远离陡降点; 默认 0.25 经多模型实测的安全中段

---

## 5. Per-request LCP histogram (§6)

### 5.1 数据生成

每个 request 算一个 LCP (§3.2), 收集列表.

```python
max_lcp = max(per_request_lcp)
bucket_size = max(1, max_lcp // 30)        # 自动 ~30 个等宽 bucket
buckets[lcp // bucket_size] += 1
```

- **x 轴**: LCP 值 (block 数); ~30 个等宽 bucket, 每 bucket 宽度 = `max_lcp / 30`
- **y 轴**: 落在该 bucket 的 request **数** (频次)
- 另出 **top-10 LCP 值表** (具体 LCP 值 × 该值有多少 request)

### 5.2 6 种典型形态

| 形态 | 解读 | 业务类型 |
|---|---|---|
| **单峰在 0** | 几乎全 miss | 邮件分类、unique 极高 user |
| **单峰在高位** | 几乎全命中 | 成熟 chain 用户、business warm-up |
| **双峰 (0 + chain_len 附近)** | 部分 miss + 部分 chain 命中 | 典型 chain pattern; 中间空 = chain 是"原子" |
| **三峰** | 多 chain 用户 | 多业务 router |
| **平滑分布** | LCP 渐变, 无明显聚集 | 长文档复用 |
| **极长尾** | 少数 request 命中超长 prefix | outlier / boilerplate 重复 |

### 5.3 关键看点

- **0 峰的 count** = 首次见 prompt 数 (cache miss 下界)
- **高位峰位置** ≈ chain_length (验证 chain 是否真在"被走完")
- **0 峰 vs 高位峰高度比** = chain pin ROI 上界
- **p95 高 vs max 高**: p95 高 = 普遍命中长 prefix (业务良好); 仅 max 高 = 个别 outlier

---

## 6. block → GB KV cache 换算 (token 模式)

### 6.1 公式

token-level encoder 的 block 是 **128 tokens × 多层 attention KV tensor**, 与 vLLM KV cache **完全对应**:

```
GB per_unique_block = block_size_tokens × kv_bytes_per_token / 1024³

例: Qwen3-8B (128 tok × 147,456 B/tok) ≈ 0.01758 GB / block (18 MB)
    GLM-5    (128 tok × 89,856 B/tok)  ≈ 0.01072 GB / block (11 MB)

GB/min = blocks/min × per_unique_block_gb
```

### 6.2 kv_bytes_per_token 来源

每个 vendor tokenizer 配套一份 `kv_meta.json` (在 `models/<name>_tokenizer/` 下), 含完整 derivation breakdown:

| 模型 | 架构 | 公式 | 值 |
|---|---|---|---|
| Qwen3-8B | GQA | `2 (K+V) × num_layers (36) × num_kv_heads (8) × head_dim (128) × dtype_bytes (bf16=2)` | 147,456 B/tok |
| GLM-5 | MLA (DSA) | `num_layers (78) × (kv_lora_rank 512 + qk_rope_head_dim 64) × dtype_bytes (bf16=2)` (K/V 共享 latent, 不乘 2) | 89,856 B/tok |

**MLA vs GQA**: GLM-5/DeepSeek 系列用 Multi-head Latent Attention, K 和 V 压缩成 latent vector, KV cache 比 GQA 小一个数量级. 新增模型时**必须用对应架构公式**, 不能套用 GQA 通式. 见 `models/README.md` refresh procedure.

### 6.3 量化场景

如果部署用 fp8 / int8 (而非 bf16/fp16), kv_meta.json 的 `dtype_bytes` 减半 (fp16 → fp8) 或减 4 倍 (fp16 → int4), 重 vendor 一份对应的 `kv_meta.json`. 公式不变.

---

## Appendix A. byte-level (regression baseline) 仅供参考

byte-level encoder (`--encoder byte` / `ByteLevelEncoder`) 把 raw_prompt 按 128 字节切片做 hash 链, 是**早期未引入 tokenizer 时的回归基线**. 它的 `ideal_hit_rate` 与 vLLM 真实命中率**系统性偏高 0-30pp** (字节级 LCP 比 token 级粗, prompt 中间一个字节差异就 break 整个 block; token 级允许同一字符变体合并到同一 token).

**只用于回归测试**: 验证新版本 byte encoder 与历史数字一致. 算法决策**必须看 token 模式数字**.

主要差异:

| 维度 | byte_v1 (baseline) | token (主线) |
|---|---|---|
| block 单位 | utf-8 字节 (128 B) | tokens (128) |
| KV cache 对应 | 无对应, 仅 prompt 字节 | 与 vLLM 一致 |
| GB/min 估算 | 不算 (block 不对应 KV 张量) | `blocks/min × 128 × kv_bytes_per_token / 1024³` |
| HTML §3 unique blocks 副标题 | "byte 模式无 GB 估算" | "≈ X.XXX GB total (128 tok × Y,YYY B/tok)" |
| ideal_hit_rate | 系统性偏高 0-30pp | 与 vLLM 一致 |

byte 模式的旧版本本节文档已归档, 见 git history.
