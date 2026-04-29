# Two-Queue TTL KV Cache 淘汰算法实验计划

**版本**：v1.1（基于原始实验计划修订）  
**核心修订**：TTL 消融区间锚定 F13 实测数据；新增注册表实验分组；统一关键指标定义；成功标准改为相对指标；Phase 3 收窄；新增路由因素隔离实验。

---

## 1. 实验目标

### 1.1 核心问题

在固定 KV cache 容量下，Two-Queue TTL 是否比默认 LRU 更能保住高价值 prefix blocks？

### 1.2 重点验证

1. 高价值 FREE_CACHED blocks 是否经常在下一次 potential hit 前被提前淘汰？
2. Two-Queue TTL 是否能减少 `evicted_before_next_hit`？
3. 低命中率的根本原因是 **eviction**，还是 **routing 不合理**，还是两者都有？
4. Two-Queue TTL 是否能提升 `prefix_block_hit_rate` 和 `saved_prefill_tokens`？
5. Protected Queue 是否会被污染（进入后从未再次命中）？
6. TTL / protected_capacity / promotion_threshold 的最优配置是什么？

---

## 2. 数据基础与核心指标定义

在任何实验开始前，统一以下术语，避免歧义。

### 2.1 已有离线数据（qwen_v3_5_27b_64k）

**模块一 reuse_distance_blocks（空间维度）**

| 百分位 | reuse_distance_blocks |
|---|---|
| p50 | 39,282 blocks |
| p80 | 80,303 blocks |
| p95 | 234,858 blocks |
| max | 1,582,240 blocks |

- reusable_requests / total_requests = 8,749 / 8,755 = **99.9%**（无限容量下的理论命中率上界）

**F13 reuse_time_seconds（时间维度）**

| 百分位 | reuse_time |
|---|---|
| p50 | 17 秒 |
| p80 | **46 秒**（base_ttl 锚点）|
| p95 | **255 秒**（extended_ttl 锚点）|
| max | 6,415 秒（107 分钟）|

### 2.2 关键指标的精确定义

**ideal_hit_rate（本实验的统一定义）**

```
ideal_hit_rate = Infinite Cache 仿真下的 prefix_block_hit_rate
               （与真实 trace、routing、block_size 完全一致，仅去掉容量限制）

注意：ideal_hit_rate ≤ 理论上界（99.9%）
差距来自 routing 因素（不同实例间无法共享 KV cache）
理论上界由 Module 1 离线分析给出，ideal_hit_rate 由仿真给出
```

**gap_closed_ratio（成功标准的核心度量）**

```
gap_closed_ratio =
    (Two-Queue hit_rate - LRU hit_rate)
    ÷ (Infinite Cache hit_rate - LRU hit_rate)

含义：Two-Queue TTL 缩小了 LRU 与 Infinite Cache 差距的多少比例
范围：[0, 1]，越高越好；0 = 与 LRU 相同；1 = 达到 Infinite Cache 水平
```

**protected_pollution_rate**

```
protected_pollution_rate =
    进入 Protected 后从未再次命中、最终被淘汰的 block 数
    ÷ Protected 队列总淘汰 block 数
```

---

## 3. 实验总体分层

```
Phase 0  问题诊断
  0-A：Block 生命周期观测（eviction 是否真实发生在 potential hit 前）
  0-B：路由因素隔离（routing 贡献了多少命中率损失）

Phase 1  离线 Trace Replay 仿真
  1-A：基线策略对比（LRU / Infinite Cache / Belady / Two-Queue 系列）
  1-B：注册表覆盖率验证（top-N prefix 覆盖多少 saved tokens）

Phase 2  参数消融实验
  2-A：Protected 容量比例（锚定 reuse_distance 分位数）
  2-B：TTL 参数（锚定 F13 分位数）
  2-C：晋升阈值 × TTL 交叉实验
  2-D：Priority Score 特征消融
  2-E：Hard Protected Warm-up top-N 扫描

Phase 3  在线原型验证（仅在 Phase 1/2 满足上线门槛后启动）
  3-1：小规模 A/B（LRU vs Two-Queue 最小版）
  3-2（可选）：扩展分组（需 3-1 结果支持）
```

---

## 4. Phase 0：问题诊断实验

### 4-A：Block 生命周期观测

**目的**：确认"高价值 prefix block 在下一次 potential hit 前被提前淘汰"这一问题是否真实显著。若不显著，Two-Queue TTL 的意义大幅下降。

**插桩位置**

| 位置 | 记录内容 |
|---|---|
| `get_computed_blocks` | 请求 prefix lookup 命中哪些 blocks |
| `allocate_slots` 前后 | 本次请求分配 block 数，触发了哪些 eviction |
| `block_pool.touch` | block 从 free queue 移除，ref_cnt 变化 |
| 请求结束 / free | 哪些 blocks 进入 FREE_CACHED |
| eviction 发生点 | 被删除 block 的 block_hash、在 FREE_CACHED 中的存活时长 |

**关键指标**

| 指标 | 含义 |
|---|---|
| `free_cached_lifetime` | block 从进入 FREE_CACHED 到被淘汰或被命中的时间 |
| `FREE_CACHED → HIT 比例` | 进入 FREE_CACHED 后成功等到下次命中的比例 |
| `FREE_CACHED → EVICTED 比例` | 进入 FREE_CACHED 后被淘汰的比例 |
| `evicted_before_next_hit_blocks` | 在下次 potential hit 前被淘汰的 block 数 |
| `hot_prefix_eviction_count` | 高价值（高频）prefix block 被淘汰次数 |
| `eviction_by_input_length_bucket` | 按请求输入长度分桶的驱逐量（判断长输入是否是主要来源）|

**判断标准**

```
若 evicted_before_next_hit_blocks 占比高（> 20%）
且 hot_prefix_eviction_count 较高
且 长输入请求触发大量高价值 block eviction
→ eviction 是主要瓶颈，Two-Queue TTL 方向成立

若 高价值 block 很少被提前淘汰（< 5%）
→ 低命中率主要来自 routing 或 prefix exactness 问题，调整实验优先级
```

**Ascend 适配说明**：若 vLLM-Ascend 上述插桩位置的 API 与标准 vLLM 存在差异，退而使用日志级别的事后分析作为代替方案（基于 block_hash 出现和消失的时间戳序列重建生命周期）。

---

### 4-B：路由因素隔离实验

**目的**：将 routing 导致的命中率损失与 eviction 导致的损失分开量化，为优化优先级提供决策依据。

**实验设计**

在 Phase 1 仿真中，对同一 trace 分别运行：

| 路由策略 | 说明 |
|---|---|
| 当前策略（least-loaded） | 生产环境现状 |
| 完美 user-affinity routing | 同一 user_id 的所有请求路由至同一实例 |
| 完美 system-prompt-affinity | 相同 system prompt 的请求路由至同一实例 |

固定：相同 trace、相同 cache 容量、相同 eviction policy（LRU）。

**分析框架**

```
total_miss = routing_miss + eviction_miss + other_miss

routing_miss = LRU（当前 routing）命中率 - LRU（完美 affinity）命中率
eviction_miss = LRU（完美 affinity）命中率 - Infinite Cache（完美 affinity）命中率
```

若 routing_miss > eviction_miss，则优先优化 routing；若 eviction_miss 显著，则 Two-Queue TTL 值得投入。

---

## 5. Phase 1：离线 Trace Replay 仿真实验

### 5.1 输入数据要求

**必须字段**

```
timestamp, model_id, user_id / tenant_id, request_type / scenario,
input_length, output_length, hash_ids / block_ids, block_size, turn
```

**可选字段**（没有时使用代理信号）

```
message role boundary    → 代理：block_pos（前 64 块视为 system prompt 代理）
system prompt block 范围 → 代理：离线注册表 block_hash 匹配
```

**当可选字段不可用时的策略**

第一轮实验完全依赖 `block_pos + hit_count + 离线注册表`，不依赖运行时角色识别。这与算法设计方案第一版实现保持一致。

### 5.2 对比策略

| 策略 ID | 名称 | 描述 |
|---|---|---|
| S0 | Infinite Cache | 无容量限制，命中率上界（= ideal_hit_rate）|
| S1 | LRU | 当前默认策略，基线 |
| S2 | Belady Oracle | 已知未来请求的最优淘汰，算法可能达到的上界 |
| S3 | TTL-LRU | 只加 TTL，不分队列（隔离 TTL 单独贡献）|
| S4 | Two-Queue（no TTL） | 只分 Probation/Protected 队列，不加 TTL（隔离队列单独贡献）|
| S5 | Two-Queue + fixed TTL（46s） | 双队列 + base_ttl = F13 p80 = 46s |
| S6 | Two-Queue + tiered TTL | hit_count=2 → 46s，hit_count≥5 → 255s |
| S7 | Two-Queue + registry | 离线注册表直接入 Protected，hit_count 晋升（**第一版实际方案**）|
| S8 | Two-Queue + role-detection | 运行时 role 识别入 Protected（需 message boundary，作为上界参考）|
| S9 | Two-Queue + Hard Protected | S7 基础上加 top-N Hard Protected 常驻 |

**S7 是第一版实际实现方案，S8 是理论上界参考，S7 与 S8 的差距反映"运行时角色识别"的价值**。

### 5.3 缓存容量配置

容量扫描应以 Module 1 数据为锚点，使每个容量点有物理意义：

| 容量标签 | 说明 | 对应 Module 1 分位数 |
|---|---|---|
| `cap_p50` | = reuse_distance_p50 = 39,282 analysis blocks 等价 | 理论上 S0 覆盖 50% 复用 |
| `cap_p80` | = reuse_distance_p80 = 80,303 analysis blocks 等价 | 理论上 S0 覆盖 80% 复用 |
| `cap_p95` | = reuse_distance_p95 = 234,858 analysis blocks 等价 | 理论上 S0 覆盖 95% 复用 |
| `cap_real` | 实际部署的 available_cache_blocks（需从日志获取）| 真实场景 |
| `cap_5pct` | = 5% working set | 极端容量不足 |
| `cap_20pct` | = 20% working set | 中等容量 |

**重要验证点**：在 `cap_p80` 下，S0（Infinite Cache）的命中率是否接近 80%？如果不是，说明 routing 因素贡献了额外损失，gap 被 routing 而非容量主导。

### 5.4 路由配置

第一轮：**固定使用当前 routing**（避免变量混杂），待 Phase 0-B 路由隔离实验完成后，再决定是否在 Phase 1 加入 routing 变量。

### 5.5 Phase 1 核心指标

**命中收益类**

| 指标 | 优先级 |
|---|---|
| `prefix_block_hit_rate` | 最高 |
| `saved_prefill_tokens` | 最高 |
| `gap_closed_ratio`（相对 LRU vs Infinite Cache）| 最高 |
| `actual_hit / ideal_hit` | 高 |

**淘汰归因类**

| 指标 | 优先级 |
|---|---|
| `evicted_before_next_hit_blocks` | 最高 |
| `hot_prefix_eviction_count` | 高 |
| `lost_prefix_hit_tokens` | 高 |
| `protected_evictions` | 中 |
| `probation_evictions` | 中 |
| `evictions_by_input_length_bucket` | 中 |

**队列健康类**

| 指标 | 优先级 |
|---|---|
| `protected_pollution_rate` | 高（> 30% 则准入策略过宽）|
| `promotion_count` | 中 |
| `protected_queue_hit_rate` | 中 |
| `registry_hit_rate`（注册表命中比例）| 中 |

---

## 6. Phase 1 前置：离线注册表生成与验证

**在 Phase 1 仿真开始前执行**，是注册表相关策略（S7、S9）的前提。

### 6.1 生成流程

```
输入：reuse_distance_events.csv（Module 1 输出）

步骤：
  1. 统计每个 block_hash 的出现频次和 hit_count
  2. 提取 hit_count 排名前 N 的 block_hash 序列
  3. 识别公共前缀链（连续 block_hash 组）
  4. 输出 hard_protected_registry.txt（top 10%）
     和 protected_registry.txt（top 10%~30%）
```

### 6.2 注册表覆盖率验证实验

| top-N | 覆盖的 prefix block 数 | 覆盖的 saved_prefill_tokens | 占总 saved tokens 比例 |
|---|---|---|---|
| top-10 | ? | ? | ? |
| top-50 | ? | ? | ? |
| top-100 | ? | ? | ? |
| top-500 | ? | ? | ? |

**判断标准**：若 top-50 prefix 覆盖 ≥ 50% 的 saved_prefill_tokens，则 warm-up + Hard Protected 策略有高价值，值得在 S9 中验证。若覆盖率低（< 20%），则前缀分布过于分散，注册表方案效果有限。

---

## 7. Phase 2：参数消融实验

### 7-A：Protected 容量比例实验

**扫描值**：`protected_capacity_ratio = 10%, 20%, 30%, 40%, 50%`

与 Module 1 数据对照输出：

```
对每个 protected_capacity_ratio：
  计算 Protected 绝对容量（块数）
  标注该容量对应 Module 1 的哪个百分位（reuse_distance_pX）
  观察 hit_rate 和 protected_pollution_rate
```

**预期形态**：

```
Protected 太小（< cap_p50）：保护不足，hit_rate 接近 LRU
Protected 适中（≈ cap_p80）：hit_rate 明显提升，pollution 可控
Protected 太大（> cap_p95）：pollution 上升，低价值 block 占据保护区
```

---

### 7-B：TTL 参数实验（锚定 F13 分位数）

**TTL 扫描区间必须覆盖 F13 关键分位数**，不基于工作负载经验估算。

对 qwen_v3_5_27b_64k（已有 F13 数据）：

| 参数 | 扫描值 | F13 锚点说明 |
|---|---|---|
| base_ttl（Protected 首次晋升）| **10s, 17s, 46s, 120s, 255s, 600s** | 10s=低于p50；17s=p50；46s=p80；255s=p95 |
| extended_ttl（hit_count≥5）| **46s, 120s, 255s, 600s** | 以 p80 为下限 |
| Probation TTL | **0s, 10s, 30s** | 默认 0，测试短暂保护的影响 |

> **对其他模型（Qwen-32B-8K、DeepSeek、Agent 等）**：各模型的 TTL 扫描区间必须在该模型的 F13 分析完成后，以其 p80 为 base_ttl 锚点重新制定，不得直接套用 qwen_v3_5_27b_64k 的数值。

**观察内容**：

```
base_ttl < F13_p50（17s）：TTL 过短，退化为 LRU
base_ttl ≈ F13_p80（46s）：覆盖 80% 复用，预期最优区间
base_ttl ≫ F13_p80：Protected 被过期块占满，污染率上升
```

---

### 7-C：晋升阈值 × TTL 交叉实验

晋升阈值与 TTL 存在参数间相互影响，必须做交叉实验，不能分别做单因素实验。

**实验矩阵（2 × 3）**：

| | base_ttl = 17s（p50）| base_ttl = 46s（p80）| base_ttl = 255s（p95）|
|---|---|---|---|
| threshold = 1（激进）| A11 | A12 | A13 |
| threshold = 2（平衡）| A21 | A22 | A23 |
| threshold = 3（保守）| A31 | A32 | A33 |

**关注组合**：

```
A11（threshold=1, ttl=17s）：激进晋升 + 短 TTL → 高周转，污染风险低但保护时间短
A22（threshold=2, ttl=46s）：算法设计方案推荐配置，预期最佳平衡
A13（threshold=1, ttl=255s）：激进晋升 + 长 TTL → 污染风险高
A33（threshold=3, ttl=255s）：保守晋升 + 长 TTL → 错过短窗口复用
```

**核心观察**：pollution_rate 在哪个组合开始失控？gap_closed_ratio 在哪个组合最高？

---

### 7-D：Priority Score 特征消融

逐步加入特征，验证每一项的实际增益：

| 实验 | 使用特征 | 目的 |
|---|---|---|
| D1 | 仅 hit_count | 最小特征集基线 |
| D2 | hit_count + block_pos | 加位置代理信号 |
| D3 | hit_count + block_pos + 离线注册表 | 加注册表信号（**第一版方案**）|
| D4 | hit_count + block_pos + 注册表 + role | 加运行时角色信号（需 message boundary）|
| D5 | D3 + top-N Hard Protected 常驻 | 加 warm-up 常驻保护 |

**决策规则**：

```
若 D3 与 D4 的 gap_closed_ratio 差距 < 5pp：
  → 运行时角色识别增益有限，第一版不需要实现，维持注册表方案

若 D5 相比 D3 有显著提升（gap_closed_ratio 提升 ≥ 10pp）：
  → warm-up + Hard Protected 价值高，优先实现
```

---

### 7-E：Hard Protected Warm-up top-N 扫描

| top-N | Hard Protected 命中率 | 占总 saved_prefill_tokens 比例 | 污染率 |
|---|---|---|---|
| N=0（无 warm-up）| baseline | baseline | baseline |
| N=10 | ? | ? | ? |
| N=50 | ? | ? | ? |
| N=100 | ? | ? | ? |
| N=500 | ? | ? | ? |
| N=1000 | ? | ? | ? |

**边际收益拐点**：找到 top-N 继续增加但 saved tokens 增量趋于平缓的 N 值，作为实际 warm-up 的推荐规模。

---

## 8. Phase 3：在线原型实验

**启动条件**：Phase 1 和 Phase 2 必须同时满足以下门槛，才可进入在线原型。

### 8.1 Phase 1/2 → Phase 3 上线门槛

```
必须满足：
  gap_closed_ratio ≥ 0.3（Two-Queue 相比 LRU 缩小了 30% 的 gap）
  protected_pollution_rate < 30%
  evicted_before_next_hit 相比 LRU 下降 ≥ 20%
  无正确性问题（ref_cnt > 0 的 block 未被淘汰）

推荐满足：
  gap_closed_ratio ≥ 0.5
  saved_prefill_tokens 提升 ≥ 10%
```

### 8.2 Phase 3-1：小规模 A/B 实验（第一版上线）

**仅做两组**，其他分组需要 3-1 结果支持才能解锁。

| 组别 | 策略 | 说明 |
|---|---|---|
| A（对照）| 当前 LRU | 生产基线 |
| B（实验）| Two-Queue + 离线注册表 + base_ttl=46s + threshold=2 | Phase 2 最优配置 |

流量分配建议：5% 实验组，95% 对照组，持续 7 天观察稳定性。

### 8.3 Phase 3-2（可选，需 3-1 数据支持）

| 组别 | 策略 | 解锁条件 |
|---|---|---|
| C | Two-Queue + tiered TTL（hit≥5 → 255s）| B 组 gap_closed_ratio > 0.5 |
| D | Two-Queue + Hard Protected warm-up top-50 | 注册表覆盖率 ≥ 50% |
| E | Two-Queue + Protected eviction budget | 长输入压力实验（第 9 节）结果支持 |

### 8.4 在线监控指标

**正确性硬指标（任何一项非零立即回滚）**

```
ref_cnt_positive_eviction_count = 0
active_block_eviction_count = 0
current_request_hit_block_evicted_count = 0
prefix_hash_table_inconsistency = 0
```

**收益指标**

```
vllm_prefix_cache_hit_rate
prompt_tokens_cached
saved_prefill_tokens
TTFT P50 / P95 / P99
throughput（requests/s）
kv_cache_usage_perc
```

**算法开销指标**

```
scheduler_latency（队列操作引入的调度延迟）
CPU_overhead_per_request
memory_metadata_overhead（双队列元数据的内存消耗）
```

---

## 9. Phase 3 配套：长输入防护预算压力实验

**目的**：验证长输入请求是否会冲击 Protected Queue，以及 Protected eviction budget 是否有效。

**压力流构造**

从生产 trace 中混合：
- 短 prefix-friendly 请求（< 4K）
- 32K / 64K 长输入请求

按不同混合比例：

| 长输入占比 | 并发强度 | 测试目的 |
|---|---|---|
| 10% | low | 基础稳定性 |
| 30% | medium | 常规生产场景 |
| 50% | high | 压力边界 |
| 70% | high | 极端场景 |

**对比策略**

| 策略 | 说明 |
|---|---|
| LRU | 基线 |
| Two-Queue TTL（无 budget）| 验证长输入冲击是否真实存在 |
| Two-Queue TTL + budget（5%, limit=128）| 验证 budget 是否有效 |
| Two-Queue TTL + backpressure | 验证 backpressure 对 TTFT 的影响 |

**判断标准**：

```
若无 budget 时 hot_prefix_evicted_by_long_request 显著高于有 budget 时：
  → budget 机制有效，值得上线

若 budget 导致 P99 TTFT 恶化超过 20%：
  → 需要调整 ratio 或 absolute_limit 参数，再次测试
```

---

## 10. 分模型实验设计

> **重要约束**：每个模型的 TTL 参数配置必须在该模型的 F13 分析完成后，以其 p80 为 base_ttl 锚点制定，**不得使用经验估算值**。以下框架仅为模型特征描述和重点验证项，具体 TTL 值留空待填。

### 10.1 Qwen-V3-32B-8K

**数据特征（假设）**：system prompt 稳定，用户偏斜高，复用窗口预计偏短。

**前置条件**：完成该模型 F13 分析，获取 p50/p80/p95 reuse_time。

**重点验证**：
- Protected Queue 是否能保护 system prompt blocks
- base_ttl = F13_p80（待填）是否足够
- 高频用户 prefix 是否被保住

**预期**：Two-Queue TTL 对 system prompt 保护有效；但若工作集过大，容量仍是主要瓶颈。

---

### 10.2 Qwen-V3-32B-32K

**数据特征（假设）**：长上下文，文件/合同处理多，prefix hit 可能低于 8K。

**前置条件**：完成该模型 F13 分析。

**重点验证**：
- base_ttl = F13_p80（待填）是否有收益
- 长输入是否频繁淘汰高价值 prefix
- 是否需要 FP8 量化（容量扩展）配合

---

### 10.3 DeepSeek 8K / 32K

**数据特征（假设）**：分类/问答场景，system prompt 种类少，复用窗口预计短到中等。

**前置条件**：完成该模型 F13 分析。

**重点验证**：
- 少量 Hard Protected（top-N system prompt）是否能覆盖大比例 saved tokens
- 注册表覆盖率是否高于其他模型（因 system prompt 种类少）

---

### 10.4 Agent 64K / 128K

**数据特征**：长 system prompt + skills + tools，prefix hit 已相对较高，prefix 靠前位置 block 价值极高。

**前置条件**：完成该模型 F13 分析。

**重点验证**：
- Hard Protected 比例是否应更大（建议测试 20%~40%）
- 用户尾部内容是否污染 Protected
- Skills/tools block 是否适合常驻（需要闲置失效机制防止旧版本占用）

---

## 11. 成功标准

以 `gap_closed_ratio` 为核心度量，避免依赖绝对命中率的假设。

```
gap_closed_ratio = (Two-Queue hit_rate - LRU hit_rate)
                 ÷ (Infinite Cache hit_rate - LRU hit_rate)
```

### 11.1 最低成功标准（问题存在且算法有效）

```
evicted_before_next_hit_blocks（vs LRU）下降 ≥ 20%
hot_prefix_eviction_count（vs LRU）下降 ≥ 30%
gap_closed_ratio ≥ 0.3
protected_pollution_rate < 30%
无正确性问题
```

### 11.2 有价值标准（值得进入在线原型）

```
gap_closed_ratio ≥ 0.5
saved_prefill_tokens（vs LRU）提升 ≥ 10%
TTFT P50/P95 有可观下降
protected_pollution_rate < 20%
```

### 11.3 目标标准（算法显著有效）

```
gap_closed_ratio ≥ 0.7
实际 prefix_block_hit_rate 接近 ideal_hit_rate 的 70%
LRU 与 Belady Oracle 的差距被显著缩小
高价值 prefix blocks 大部分能存活到下一次复用
```

---

## 12. 实验报告模板

```
实验名称：
  Two-Queue TTL vs LRU — [模型名] trace，[容量配置]

实验目的：
  [本实验回答哪个具体问题]

数据集：
  模型、时间范围、请求数、block 数、用户数、平均输入长度
  reuse_distance p50/p80/p95（Module 1）
  reuse_time p50/p80/p95（F13）

配置：
  cache_capacity（绝对值 + 对应 Module 1 分位数标注）
  block_size
  routing_policy
  protected_ratio
  base_ttl（标注对应 F13 分位数）
  extended_ttl
  promotion_threshold
  registry_size（top-N）

对比策略：
  LRU / Infinite Cache / Belady / Two-Queue 系列（按 5.2 节 S0~S9）

结果：
  prefix_block_hit_rate（各策略）
  gap_closed_ratio（各策略 vs LRU）
  saved_prefill_tokens
  evicted_before_next_hit（绝对值 + vs LRU 变化率）
  hot_prefix_eviction_count
  protected_pollution_rate
  TTFT 估算收益（若可用）

结论：
  1. eviction 是否是低命中率的主要原因？
  2. Two-Queue TTL 是否有效缩小 gap？
  3. 最优参数组合是什么？
  4. 是否建议进入在线原型？
  5. 后续实验建议
```

---

## 13. 推荐实验顺序

```
Step 0：数据准备
  确认 hash_ids / block_ids 字段完整
  确认 timestamp 精度（秒级）

Step 1：Phase 0-B 路由因素隔离
  量化 routing_miss vs eviction_miss
  → 若 routing_miss 主导（> 60%），优先考虑 routing 优化
  → 若 eviction_miss 显著，继续 Two-Queue TTL

Step 2：Phase 0-A Block 生命周期观测
  确认 evicted_before_next_hit 是否显著
  → 问题不显著则重新评估优先级

Step 3（Phase 1 前置）：生成离线注册表
  从 reuse_distance_events.csv 提取 top-N prefix block_hash
  验证注册表覆盖率（Phase 1 前置 6.2 节）
  → 确定注册表规模（top-50 / top-100 / top-500）

Step 4：Phase 1 基线 + 策略矩阵仿真
  在 cap_real / cap_p80 / cap_p50 三个容量下
  对比 S0~S9 各策略
  重点关注 S7（注册表方案）的 gap_closed_ratio

Step 5：Phase 2-A 容量比例 + Phase 2-B TTL 消融
  Protected 容量比例扫描（锚定 reuse_distance 分位数）
  TTL 扫描（锚定 F13 分位数）

Step 6：Phase 2-C TTL × threshold 交叉实验
  找到最优（base_ttl, promotion_threshold）组合

Step 7：Phase 2-D Priority Score 特征消融
  确认注册表方案是否足够，是否需要运行时角色识别

Step 8：Phase 2-E Hard Protected warm-up top-N 扫描
  找到 warm-up 规模的边际收益拐点

Step 9：Phase 3 长输入压力实验（若 Step 4~8 满足门槛）
  验证 protected eviction budget 有效性

Step 10：Phase 3-1 在线原型 A/B
  仅 LRU vs Two-Queue 最优配置
  持续 7 天，观察正确性和收益稳定性

Step 11（可选）：Phase 3-2 扩展在线分组
  基于 Step 10 结果决定是否解锁 C/D/E 分组
```

---

## 14. 一句话总结

Two-Queue TTL 实验的核心不是证明命中率变高，而是证明两件事：**第一，高价值 prefix blocks 确实在下一次复用前被提前淘汰（Phase 0）；第二，Two-Queue TTL 通过将 F13 p80（46s）锚定为 base_ttl、用离线注册表识别高价值 block，显著减少了这种 premature eviction，并最终将实际命中率推近 Infinite Cache 的理论上界（gap_closed_ratio 量化）。**
