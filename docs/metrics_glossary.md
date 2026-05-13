# 指标与图表释义（per-user HTML 报告读法）

> **创建时间：** 2026-05-13
> **适用范围：** `outputs/<model>/per_user_reports/*/user_report.html`
> **配套文档：** [`docs/step3_algorithm_decision_matrix.md`](step3_algorithm_decision_matrix.md) §9.2 工具 v2 spec

per-user HTML 报告里每个指标 / 每张图分别代表什么、看什么、对应什么决策？本文是 cheat sheet。

---

## 0. 一句话使用顺序

> 先看 §0 的 `reuse_inversion_ratio` + `share_of_unique` 找污染源 → §3 Cumulative 看 WS 是否收敛决定 C → §4 LCP 直方图验证 chain 真伪 → §5 decoded 排除 boilerplate → §6 取子类型。

---

## 1. 指标释义

### 1.1 模型层（§0）

| 指标 | 定义 | 决策意义 | 数量级参考 |
|---|---|---|---|
| **rpm_avg** | 每分钟平均请求数 = `total_requests / trace_minutes` | 模型层流量强度。高 RPM + 多租户 → A 路由聚合；低 RPM → A0 baseline | DSK 千 RPM，Qwen-32B-8K 百级，Qwen-8B-8K 十级 |
| **unique_rpm_avg** | 每分钟新写入 unique block 数 = `total_unique_blocks_topk / trace_minutes` | cache 周转速率。高 → cache 永远在被填，pin 不住 → C(1) 强池化 + B(2) 多队列；低 → cache 稳定，pin 有意义 | Qwen-32K nebula ~5K/min，Qwen-8B-8K 十几 |
| **reuse_inversion_ratio** | `max(user.hit) / min(user.hit)`，≥ 2.0 触发 inversion 标志 | 用户间复用率差距。≥ 2.0 = 存在污染源 → A(1) isolation | Qwen-32K ~9.5x、DS-32K ~17x、Qwen-32B-8K ~1.1x |
| **model_params_class** ⚠️人工补 | 模型规模：`small_le_32B` / `large_200B_moe` | 大模型不能多实例物理隔离 → A(4) 暂缓 | 8B/27B/32B = small；DSK/GLM = large |
| **instance_count** ⚠️人工补 | 当前部署实例个数 | A 路由可行性：> 1 才能做物理隔离；= 1 只能 B/C/D | 生产部署值 |
| **cache_capacity_blocks** ⚠️人工补 | 单实例 cache 容量（block 数）| C 池化决策核心：< unique → 必须 C；> pin 总量 → B(2) pin 可行 | 取决于 GPU 显存 + KV layout |

### 1.2 用户层（§1 + §1.5）

| 指标 | 定义 | 决策意义 | 阈值/含义 |
|---|---|---|---|
| **trace span (s)** | 该 user 跨度 = `latest_ts - earliest_ts` | 观测窗口长度，影响 RPM 可信度。跨度 < 1h → rate 指标不稳定 | 越长越可信 |
| **dominant cov** | 主 chain（chain 0）覆盖该 user 的 block 比例 | chain 影响力。> 50% high → A(3)/B(2) 高 ROI；< 10% low → 无主 chain | < 10% 低 / 10–50% 正常 / > 50% 高 |
| **chain_length_ratio** | `dominant_chain_length / avg_blocks_per_request` | chain 占 request 的体量。> 0.3 长（A(2)/B(2) 候选）；≤ 0.3 短（A(3)/B(2) 候选）| 例: Qwen-64K S773=17/300=0.06 短；DS-8K S773=56/60=0.93 长 |
| **share_of_unique** | `user_unique / sum(Top-K user unique)` | cache 主贡献者（hit 高）或污染源（hit 低）| ≤ 5% 低 / 5–30% 正常 / ≥ 30% 高 |

---

## 2. 图表看点

### 2.1 §2 Requests per minute（柱状图）

- **趋势**：单调 / spike / 时段分布（off-hours dip）
- **平均水平 vs 峰值**：spike 是否被 §0.1 自动捕获（≥ 5× 突变）
- **空窗期**：业务断续 → pin 的 chain 可能被 idle 驱逐

**意义**：决定弹性扩容 + B-TTL 必要性。

### 2.2 §2 inter-arrival 表（gaps p50/p75/p80/p95/max）

- **p50 = 0**：大量同秒 batch 到达 → A 层 batch 聚合 ROI 高
- **p50 ≥ 60s**：单请求散开 → cache 压力小但 batch 机会少
- **max 极大**：业务断续期

**意义**：A 路由 batch 聚合优化决策。

### 2.3 §3 New unique blocks per second（柱状图）

- **平均水平**：sustained 写入压力
- **峰值**：业务突发（新 prompt 涌入）
- **0 频次**：cache 100% 命中的秒数

**意义**：cache 容量压力的瞬时强度，C 池化必要性。

### 2.4 §3 Cumulative unique blocks（working set 线图）— 最重要

- **斜率**：陡 = cache 快速膨胀；平 = 大量复用
- **是否收敛**：曲线趋平 → WS 饱和 → C 容量保障可行；持续上升 → C 收益有限
- **最终高度**：cache 容量需求下界（容量 ≥ 最终高度 才能 hold 全部 unique）

**意义**：C 池化决策的核心数据。

### 2.5 §3 new block/s quantile（p50/p95/max）

- **p95 vs p50** 差距大 → sustained 低 + spike 高（容量按 p95 算）
- **max vs p95** 差距大 → 极少数瞬时尖峰（可忽略）

### 2.6 §4 Per-request LCP histogram

→ 见 §5。

### 2.7 §5 Chain forest

- **chain 数量**：1 单业务 / 2-3 主辅 / ≥ 4 多任务（A(2) 候选）
- **chain length**：≤ 50 短 → B(2) pin 划算；≥ 500 长 → A(1) chain affinity 比 pin 更优
- **leaf_cov vs max_prefix_cov**：差距大 → chain 头几个 block 有更广共享（shadow group 信号）
- **decoded content**：判断真业务前缀 vs wrapper boilerplate（is_anomaly 触发时必看）

### 2.8 §6 算法推荐（v2 子类型）

- **3 个 badge**：A 子类 / B 子类 / C 子类
- **A(4) 黄色警告**：大模型暂缓，先补 instance_count + cache_capacity_blocks
- **B(2) 灰色说明**：淘汰打分公式待 Step 2 实测
- **反常 ⚠️**：长 chain + 低 cov + 低 hit，回 §5 看 decoded
- **实施步骤**：可直接抄成 Step 2/3 任务卡

---

## 3. 算法逻辑

### 3.1 block 切分 + prefix_path_key（hash chain）

**代码**：`scripts/verify_chain_path_closure.py:46-62`

```python
def split_blocks(raw_prompt, block_size):
    return [encoded[i:i+block_size] for i in ...]   # 默认 block_size=128 字节

def compute_prefix_path_keys(blocks):
    # K_0 = SHA256(B_0);  K_n = SHA256(K_{n-1} || B_n)
```

**核心性质**：`prefix_path_key` 是**哈希链** — 某 key 见过 ⟹ 它前面所有 key 必然见过（hash 抗碰撞）。让 LCP 计算从 O(N²) 降到 O(N) 单次扫描。

### 3.2 LCP 计算（per-request）

**代码**：`scripts/per_user_report_analyzer.py:587-594`

```python
lcp = 0
for k in keys:                  # 当前 request 的 block keys 顺序遍历
    if k in seen_keys:
        lcp += 1
    else:
        break                   # hash-chain 保证：一旦 miss，后面都不需要查
hit_blocks += lcp
per_request_lcp.append(lcp)
seen_keys.update(keys)          # 当前 request 处理完后才把新 key 加入
```

- LCP = 当前 request 从 block 0 起**连续命中**的 block 数
- `ideal_hit_rate = sum(hit_blocks) / sum(total_blocks)`

### 3.3 全局 chain（greedy max-child walk）

**代码**：`scripts/verify_chain_path_closure.py:96-148`

```
1. 把所有 request 的 keys 插入 trie（每 node 维护 count + sample_request_id）
2. 从 root 出发：
   while node.children:
       max_child = argmax(children.count)
       cov   = max_child.count / total_requests   # 全局覆盖率
       ratio = max_child.count / node.count       # 局部"赢家通吃"比例
       if cov   < coverage_threshold:  break  # 主 chain 已稀薄
       if ratio < branch_threshold:    break  # 主 chain 不再强势
       chain.append(max_child); node = max_child
```

两个停止条件：
- `coverage_threshold`（默认 0.05）：max_child 在**全模型**中占比 ≥ 5%
- `branch_threshold`（默认 0.25）：max_child 在**当前 parent**中占比 ≥ 25%

### 3.4 多 chain forest（per-user）

**代码**：`scripts/multi_chain_finder.py`

不再贪心走单条 max-child，而是 trie DFS 找**所有满足条件的叶子**：
- 沿途记录每个 chain 的 `coverage_pcts[]`
- pruning：`min_chain_length` + `min_chain_coverage` + `max_chains`
- 同一 trie 可产出多条独立 chain

---

## 4. chain_threshold_sweep 图

### 4.1 横纵轴

- **x 轴**：`branch_threshold ∈ [0.00, 1.00]`，21 个点（步进 0.05）
- **y 轴**：在该 threshold 下检测到的全局 chain length（block 数）

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
| **平台期** `[0, X]` | branch_threshold ≤ 主 chain 沿途每个 branch_ratio | 主 chain 整条通过，chain length = L |
| **陡降点** `X` | 等于主 chain 的**最弱一处** branch_ratio | 第一个 branch_ratio 不够，chain 截断 |
| **零区域** `(X, 1.0]` | 严苛到 root 处 max_child 都不够 | chain = 0 |

### 4.3 4 条可读取信息

1. **L（平台高度）= 主前缀自然深度** — 业务 system_prompt 多深
2. **X（陡降点）= 主 chain 最弱环节强度** — X 越大主 chain 越强势
3. **平台是否真"平"** — 真平 = 沿途 branch_ratio 接近；阶梯下降 = 中间有"强环节"
4. **选 threshold 时**：取平台中段，远离陡降点；默认 0.25 经 7 模型实测的安全中段（详见 model_portraits.md §3.8）

---

## 5. Per-request LCP histogram

### 5.1 数据生成

每个 request 算一个 LCP（§3.2），收集列表。

**代码**：`scripts/per_user_report_analyzer.py:145-169` `lcp_histogram()`

```python
max_lcp = max(per_request_lcp)
bucket_size = max(1, max_lcp // 30)        # 自动 ~30 个等宽 bucket
buckets[lcp // bucket_size] += 1
```

- **x 轴**：LCP 值（block 数）；~30 个等宽 bucket，每 bucket 宽度 = `max_lcp / 30`
- **y 轴**：落在该 bucket 的 request **数**（频次）

### 5.2 6 种典型形态

| 形态 | 解读 | 业务类型 |
|---|---|---|
| **单峰在 0** | 几乎全 miss | Qwen-8B-8K 邮件分类、unique 极高 user |
| **单峰在高位** | 几乎全命中 | 成熟 chain 用户、business warm-up |
| **双峰（0 + chain_len 附近）** | 部分 miss + 部分 chain 命中 | 典型 chain pattern；中间空 = chain 是"原子" |
| **三峰** | 多 chain 用户（DS-8K S773 3 chain）| 多业务 router |
| **平滑分布** | LCP 渐变，无明显聚集 | 长文档复用（Qwen-64K 长 prefix 连续命中）|
| **极长尾** | 少数 request 命中超长 prefix | outlier / boilerplate 重复 |

### 5.3 关键看点

- **0 峰的 count** = 首次见 prompt 数（cache miss 下界）
- **高位峰位置** ≈ chain_length（验证 chain 是否真在"被走完"）
- **0 峰 vs 高位峰高度比** = chain pin ROI 上界
- **p95 高 vs max 高** —— p95 高 = 普遍命中长 prefix（业务良好）；仅 max 高 = 个别 outlier
- **bucket_size 太大**（如 max_lcp=300 → bucket=10）：双峰可能被合并 → 配合 §1 表里的 LCP p50/p95/max 验证

---

## 6. block 单位换算（block → GB KV cache）

### 6.1 为何不直接显示 GB

工具产出的 `unique_blocks` 是**字节级 prompt block**（128 字节 / block），**不是** vLLM KV cache 的 token-level block。两者完全不是同一单位：

| 维度 | 我们工具的 block | vLLM KV cache block |
|---|---|---|
| 单位 | utf-8 prompt 字节切片 | tokens 维度 + 多层 attention KV tensor |
| 默认大小 | 128 字节 | 16 tokens × 模型层数 × head_dim × dtype |
| 物理意义 | prompt 内容前缀 | 显存中实际 KV tensor |
| 大小关系 | 1:1 与 prompt 字节对应 | 取决于模型架构（fp16 一个 DSK V3 token 约 KB 量级，MLA 后压缩） |

直接把"我们 block 数"乘个固定系数转 GB **不准确**：
- 字节 → token：依赖 tokenizer，中文 ~0.33 token/byte 英文 ~0.25-0.3
- token → KV bytes：依赖 `n_layers × n_kv_heads × head_dim × dtype_size`
- 量化（fp8 / int8）影响 dtype_size

**误差可能 ±50% 量级**。

### 6.2 转换公式（粗估，可选）

如果接受 ±50% 误差，转换式为：

```
GB(our_unique_blocks)
  = our_unique_blocks            # 我们工具产出
  × bytes_per_block              # 128 bytes
  × tokens_per_byte_avg          # ~0.3 (英) / ~0.4 (中) / ~0.35 (混合)
  × kv_bytes_per_token           # 模型架构常数（见下）
  / 1024³
```

### 6.3 需要补到 model_report.json 的字段

要在 HTML 上启用 GB 显示，需要人工补 2 个字段（加在现有 3 个之外）：

| 字段名 | 含义 | 取值来源 | 示例 |
|---|---|---|---|
| `tokens_per_byte_avg` | 该业务 prompt 的平均 token / byte 比例 | tokenizer 抽样 1000 条 request 跑一遍取均值 | 中文为主 ~0.4；英文为主 ~0.28；JSON tool_call 类 ~0.32 |
| `kv_bytes_per_token` | 单 token KV cache 字节数 = `2 × n_layers × n_kv_heads × head_dim × dtype_size` | 模型架构 + 部署量化策略 | Qwen 8B fp16: `2 × 32 × 32 × 128 × 2 = 524,288` ≈ 512 KB；DSK V3 (MLA fp16) 实际 ~70 KB |

> **DSK MLA 特殊**：DeepSeek V3 用 Multi-head Latent Attention，KV 被压缩到 latent space，**实际 KV / token 远小于 naive 公式**。需查具体部署文档或实测。
>
> **量化时**：fp8 → dtype_size=1（KV 减半），int8 → 同 fp8，int4 → 减 4 倍。

### 6.4 简化版（推荐）：单一标定因子

避免两个估算字段叠加误差，更稳的做法是**生产环境标定一次**得到单一系数：

| 字段名 | 含义 | 标定方式 |
|---|---|---|
| `gb_per_our_unique_block` | 我们 1 个 unique block 等价多少 GB KV cache | 在生产环境取一个 trace 段, 测实际显存增量 / 我们工具产出的 unique_blocks 增量 |

然后 HTML §0 显示：

```
unique_blocks = 1,200,000  (≈ 4.2 GB 实测标定; 单 unique block ≈ 3.7 KB)
```

### 6.5 当前实施状态

- 工具暂未实现 GB 转换 — 默认只显示 block 数
- 用户决定是否启用后，需要：
  1. 把上面 2 字段（或简化版 1 字段）加进 `model_report.json` 的 `_note` 区域
  2. 改 `compute_model_context` 接受这些字段
  3. 改 HTML §0 渲染 `≈ X.X GB`
  4. 在 HTML 上同时显示误差说明

**短期建议**：保留 block 数作为主指标，GB 只作为可选辅助显示（避免精确性 trap）。
