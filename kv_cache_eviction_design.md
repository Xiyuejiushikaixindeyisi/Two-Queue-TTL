# Two-Queue TTL KV Cache 淘汰算法设计方案

**版本**：v1.0  
**数据基础**：qwen_v3_5_27b_64k 生产数据集离线分析（模块一 reuse_distance + F13 reuse_time）

---

## 1. 背景与动机

### 1.1 当前问题

vLLM 默认 LRU 对所有 `ref_cnt=0` 的 FREE_CACHED blocks 一视同仁，不区分 block 的未来复用价值。在高并发、长输入场景下，单条长请求会一次性分配大量 KV blocks，将高价值的 system prompt、Agent skills、tool description 等 prefix blocks 提前驱逐，导致后续请求无法命中 prefix cache。

### 1.2 数据证据

以下数据来自 qwen_v3_5_27b_64k 生产数据集，block_size=128 chars（analysis_block_size，CharTokenizer）：

**模块一：reuse_distance_blocks（空间维度）**

| 百分位 | reuse_distance_blocks | 含义 |
|---|---|---|
| p50 | 39,282 blocks | 覆盖 50% 复用需在窗口内保住的 unique block 数 |
| p80 | 80,303 blocks | 覆盖 80% 复用需保住的 unique block 数 |
| p95 | 234,858 blocks | 覆盖 95% 复用需保住的 unique block 数 |
| max | 1,582,240 blocks | 极端长尾 |

- **99.9%** 的请求（8,749 / 8,755）存在前缀复用机会，复用潜力极高
- 瓶颈不在复用行为本身，而在高并发流量对 cache 容量的竞争压力

**F13：reuse_time_seconds（时间维度）**

| 百分位 | reuse_time | 含义 |
|---|---|---|
| p50 | 17 秒 | 一半的复用事件在 17 秒内发生 |
| p80 | 46 秒 | 80% 的复用事件在 46 秒内发生 |
| p95 | 255 秒（4.2 分钟） | 95% 的复用事件在 4 分钟内发生 |
| max | 6,415 秒（107 分钟）| 全部复用事件在 107 分钟内完成 |

- F13 曲线陡峭：80% 的复用集中在 **2 秒 ~ 46 秒** 的极短窗口内
- KV block 只需存活 46 秒即可服务 80% 的复用机会，**时间压力并不大**
- 真正的压力来自这 46 秒内涌入了 **80,303 个竞争 block**（估算：~1,746 analysis blocks/s）

### 1.3 设计目标

在固定 KV cache 容量下，通过分层队列管理，使高价值 prefix blocks 在下一次复用前更少被提前淘汰，使实际 prefix cache 命中率更接近离线理想命中率。

---

## 2. 容量天花板分析

在进行算法设计之前，必须明确一个硬约束：**驱逐策略无法突破物理容量上限**。

```
available_cache_blocks（vLLM 实际 KV block 数）
    vs.
覆盖 X% 复用所需的 reuse_distance_blocks（同时在缓存中保留的 unique block 数）
```

换算关系（analysis_block_size=128 chars，chars_per_token≈1.0~1.5）：

```
vLLM 等价 block 数 = reuse_distance_analysis_blocks
                     × chars_per_token
                     ÷ vllm_block_size_tokens
```

| 场景 | vllm_block_size=128 tokens | vllm_block_size=2048 tokens |
|---|---|---|
| 覆盖 p80（80,303 blocks）| ~80,000–120,000 vLLM blocks | ~5,000–7,500 vLLM blocks |
| 覆盖 p50（39,282 blocks）| ~39,000–59,000 vLLM blocks | ~2,500–3,700 vLLM blocks |

**算法预期收益的理论上界**：

```
若 available_cache_blocks ≫ reuse_distance_p80：
    默认 LRU 已足够，Two-Queue 增益有限

若 available_cache_blocks ≈ reuse_distance_p80：
    Two-Queue 可将命中率从约 50% 提升至接近 80%（边际收益最大区间）

若 available_cache_blocks ≪ reuse_distance_p80：
    Two-Queue 可减少高价值 block 的无效驱逐，但命中率天花板仍受容量限制
```

**行动建议**：在算法上线前，从 vLLM 启动日志获取 `num_gpu_blocks`，代入上表判断算法处于哪个收益区间，作为是否值得投入实现的决策依据。

---

## 3. 算法总体思想

将 `ref_cnt=0` 且仍保留 `block_hash` 的 **FREE_CACHED blocks** 按未来复用价值分为三层队列：

```
┌─────────────────────────────────────────────────────────────┐
│  Hard Protected（10%~20% C_total）                          │
│  离线注册的已知高频公共前缀，常驻保护，零淘汰压力           │
├─────────────────────────────────────────────────────────────┤
│  Protected（20%~30% C_total）                               │
│  已被验证可复用（hit_count ≥ 2）的 blocks，TTL 保护        │
├─────────────────────────────────────────────────────────────┤
│  Probation（50%~70% C_total）                               │
│  首次出现、低频、低置信度 blocks，承担主要淘汰压力          │
└─────────────────────────────────────────────────────────────┘
```

**淘汰时优先从 Probation 取块，Hard Protected 最后被动员。**

---

## 4. Block 状态定义

### 4.1 算法管理范围

算法**仅管理** FREE_CACHED 状态的 blocks：

| 状态 | 定义 | 是否可淘汰 |
|---|---|---|
| FREE_CACHED | ref_cnt=0，block_hash 已登记，无请求在用 | **是**（算法管理对象）|
| ACTIVE_PARTIAL | 正在写入，block 未满 | 否 |
| ACTIVE_FULL | 当前请求使用中，ref_cnt > 0 | 否 |
| ACTIVE_SHARED_HIT | prefix hit 命中并已 touch | 否 |

### 4.2 不变量（任何时刻必须成立）

- 绝不淘汰 `ref_cnt > 0` 的 block
- 绝不淘汰当前请求已 touch 的 computed block
- 绝不破坏 `block_hash` 的链式语义（淘汰必须从链尾开始）

---

## 5. Block 价值分类

### 5.1 高价值 block（优先保护）

| 类型 | 识别方式 | 备注 |
|---|---|---|
| 离线注册的公共 system prompt | block_hash 在离线注册表中 | 最可靠，无运行时识别负担 |
| 离线注册的 Agent skills / tool description | 同上 | |
| hit_count ≥ 2 的 block | 运行时计数 | 已被验证为可复用 |
| 被多请求共享的 block | 运行时引用追踪 | |
| prefix 靠前位置（block_pos < 64）的 block | 运行时位置 | system prompt 的代理信号 |

### 5.2 低价值 block（默认 Probation）

- 首次出现（hit_count = 0）的用户内容
- 用户个性化问题、随机长输入
- 长文件 / 合同尾部（block_pos 靠后且无命中历史）
- 长尾用户低频内容

### 5.3 运行时角色识别的限制

> **重要约束**：vLLM block 粒度（token 序列哈希）不携带消息角色元数据。  
> 运行时无法直接判断某个 block 是否属于 system prompt，除非由 serving 层透传 `prompt_type` 标签（需修改调用链，实现复杂度高）。

**第一版实现策略**：完全依赖**离线注册表**识别高价值 block，不实现运行时角色识别。未在注册表中的 block 一律从 Probation 出发，通过 hit_count 晋升。

---

## 6. Priority Score

每个进入 FREE_CACHED 的 block 计算保护分数，用于决定进入哪个队列：

```
priority_score =
    registry_score        // 离线注册表匹配得分
  + hit_count_score       // 历史命中次数得分
  + position_score        // block 在 prefix 中的位置得分
  + shared_score          // 多请求共享得分
```

### 6.1 各项评分规则（第一版）

| 评分项 | 条件 | 分值 |
|---|---|---|
| registry_score | block_hash 在 Hard Protected 注册表中 | +100 |
| registry_score | block_hash 在 Protected 注册表中 | +80 |
| hit_count_score | hit_count ≥ 5 | +60 |
| hit_count_score | hit_count ≥ 2 | +40 |
| hit_count_score | hit_count = 1 | +15 |
| position_score | block_pos < 64 | +30 |
| position_score | block_pos < 256 | +15 |
| shared_score | 被 ≥ 3 个不同 request_id 引用过 | +25 |
| shared_score | 被 2 个不同 request_id 引用过 | +10 |

### 6.2 队列划分阈值

| 分数 | 目标队列 |
|---|---|
| ≥ 100 | Hard Protected 候选（需在注册表中）|
| ≥ 60 | Protected Queue |
| < 60 | Probation Queue |

---

## 7. TTL 设计

TTL 表示**保护租约**，不是强制删除时间。TTL 内 block 不优先参与淘汰；TTL 过期后 block 可被降级或进入低优先级淘汰候选。

### 7.1 TTL 基准值（锚定 F13 实测数据）

| TTL 等级 | 时长 | 数据依据 | 适用场景 |
|---|---|---|---|
| base_ttl | **46 秒** | F13 p80 = 46s | Protected 首次晋升时的默认 TTL |
| extended_ttl | **255 秒** | F13 p95 = 255s | hit_count ≥ 5 的高频 block |
| permanent_ttl | **600 秒 + 闲置失效** | F13 max 的十分之一作为闲置阈值 | Warm-up 预热的已知公共前缀 |

> **设计原则**：TTL 以 F13 百分位为锚点，而非以工作负载类型为锚点。  
> 历史设计中的"Qwen 32K → 600~1200s"与 F13 p80=46s 相差 13~26 倍，会导致 Protected 队列被过期块长期占用。

### 7.2 TTL 与 hit_count 的联动规则

```
block 进入 Protected 时（hit_count = 2）：ttl = base_ttl（46s）
prefix hit 刷新时（hit_count = 3~4）    ：ttl 重置为 base_ttl（46s）
prefix hit 刷新时（hit_count ≥ 5）      ：ttl 重置为 extended_ttl（255s）
warm-up 预热 block                      ：ttl = permanent_ttl（600s，闲置后失效）
```

### 7.3 TTL 过期后的行为

- TTL 过期的 Protected block → 降为 Probation 低优先级淘汰候选，**不立即删除**
- 下次 prefix hit 仍可重新激活并刷新 TTL
- 若在降级后 `max_idle_after_expiry = 60s` 内无命中 → 允许正常淘汰

### 7.4 Hard Protected 的"常驻失效"机制

常驻不等于永久：若 Hard Protected block 在 `max_idle = 600s` 内零命中，自动降级至 Protected，防止因 system prompt 版本更新导致旧块长期占用容量。

---

## 8. 队列容量规划

| 队列 | 容量占比 | 备注 |
|---|---|---|
| Hard Protected | 10% ~ 20% C_total | 应 ≥ top-N 公共前缀 block 总量，启动时自动计算 |
| Protected | 20% ~ 30% C_total | Agent-heavy 节点可提升至 40% |
| Probation | 50% ~ 70% C_total | 承担主要淘汰压力 |

**Agent-heavy 节点调整建议**：若节点承载大量 Agent 工作负载（64K+ system prompt），Protected 比例可提升至 35%~45%，对应缩减 Probation 比例。

**容量约束检查**（启动时执行）：

```
if hard_protected_capacity < top_n_prefix_blocks:
    自动扩大 Hard Protected 比例，等比缩减 Probation
    记录 WARNING 日志提示配置不足
```

---

## 9. 离线注册机制

取代运行时角色识别，是第一版的核心高价值 block 识别手段。

### 9.1 注册表生成流程

```
数据来源：reuse_distance_events.csv（模块一输出）

离线分析步骤：
  1. 统计每个 block_hash 的出现频次和命中次数
  2. 提取 hit_count 排名前 N 的 block_hash 序列
  3. 识别公共前缀链（同一 prefix chain 中连续出现的 block_hash 组）
  4. 输出 hard_protected_registry.txt 和 protected_registry.txt
```

### 9.2 注册表格式

```
# hard_protected_registry.txt
# block_hash  hit_count  description
a3f2c8...     1842       top-1 system prompt prefix chain
b7d1e9...     1205       top-2 system prompt prefix chain
...

# protected_registry.txt
c4a9f2...     312        high-freq tool description
...
```

### 9.3 运行时查找

block 进入 FREE_CACHED 时，O(1) 查找注册表（hash set）：

```
if block_hash in hard_protected_registry → registry_score = +100
elif block_hash in protected_registry   → registry_score = +80
else                                    → registry_score = 0，走 hit_count 晋升路径
```

### 9.4 注册表更新策略

- 不支持运行时热更新（避免并发问题）
- 通过重启服务生效，适合每天或每周离线更新一次
- 更新时清除旧注册表对应的 Hard Protected 标记，重新执行 warm-up

---

## 10. 准入策略（block 进入 FREE_CACHED 时）

```
触发条件：
  1. block 已 full（token 数 = block_size）
  2. block_hash 已生成
  3. 已登记到 prefix cache table
  4. ref_cnt 归零（请求结束）

准入流程：
  1. 查离线注册表 → 计算 registry_score
  2. 读取 hit_count、block_pos、shared_count → 计算完整 priority_score
  3. 按阈值分配队列：≥100 → Hard Protected，≥60 → Protected，<60 → Probation
  4. 设置对应 TTL：Hard Protected → permanent_ttl，Protected → base_ttl，Probation → 0

特殊规则：
  - 未知 block（不在注册表中）首次出现 → 一律进入 Probation，命中后再晋升
  - 避免未经验证的 block 污染保护区
```

---

## 11. 晋升策略（Probation → Protected）

**晋升触发条件（满足以下任意一条）**：

```
条件 A：发生 prefix hit，且 hit_count 达到 2（由本次 hit 触发）
条件 B：block_hash 出现在离线注册表中（准入时直接进入对应队列）
条件 C：被 ≥ 3 个不同 request_id 命中（多用户共享信号）
```

**晋升后的处理**：

```
1. 从 Probation 队列移除
2. 重新计算 priority_score
3. 进入 Protected 队列，ttl = base_ttl（46s）
4. 记录晋升事件（用于 promotion_count 指标统计）
```

**注意**：晋升仅发生在 block 为 FREE_CACHED 状态时。prefix hit 发生时 block 会变为 ACTIVE_SHARED_HIT（ref_cnt += 1），不在淘汰候选池中；请求结束 ref_cnt 归零后，block 重新进入 FREE_CACHED，此时触发重新计算和队列分配。

---

## 12. 降级策略（Protected → Probation）

**降级触发条件**：

```
条件 A：Protected block 的 TTL 过期，且在 max_idle_after_expiry（60s）内无新命中
条件 B：Hard Protected block 闲置超过 max_idle（600s）
条件 C：Protected 队列容量超限（> 30% C_total），强制将最低分 LRU block 降级
```

**降级后**：

```
1. priority_score 重新计算
2. 进入 Probation 队列，TTL = 0
3. 记录降级事件（demotion_count 指标）
```

---

## 13. 淘汰策略

### 13.1 淘汰优先顺序

```
第 1 优先：Probation 中 TTL 过期（TTL=0）且 LRU 最老的 block
第 2 优先：Probation 中 LRU 最老的 block（未命中过的优先）
第 3 优先：Protected 中 TTL 已过期 + priority_score 最低的 block
第 4 优先：Protected 中 priority_score 最低的 LRU block
第 5 优先：Hard Protected 中仅当自身超容量时，内部按 LRU 淘汰最旧的 block
```

### 13.2 绝对禁止项

```
- 绝不淘汰 ref_cnt > 0 的 block
- 绝不淘汰当前请求已 touch 的 computed block
- 绝不在 prefix chain 中间淘汰（只能从链尾开始）
- 绝不破坏 block_hash 的链式完整性
```

---

## 14. 长输入防护预算

### 14.1 问题背景

reuse_distance p80 = 80,303 analysis blocks，其中相当一部分来自单条 64K 长请求一次性分配大量 block，对 Protected 队列形成冲击波式压力。

### 14.2 防护机制

对每条长请求设置 Protected 淘汰预算：

```
max_protected_evictions_per_request =
    min(absolute_limit, eviction_ratio × requested_blocks)

参数建议：
    absolute_limit = 128 blocks
    eviction_ratio = 5%

示例：
    64K 请求（≈500 vLLM blocks）：min(128, 25) = 25 blocks
    8K  请求（≈ 64 vLLM blocks）：min(128,  3) = 3 blocks
```

### 14.3 超预算后的处理

```
超出 max_protected_evictions_per_request 时，可选策略：
  策略 A（推荐）：延迟调度该长请求，等待 Probation 有空余容量
  策略 B：降低当前 batch 的 max_num_batched_tokens
  策略 C：触发 backpressure，向上层返回排队信号
```

### 14.4 动态收紧规则

当系统处于复用密集期（每秒命中的 prefix_cache_hit_rate 处于上升阶段）时，收紧预算至 `eviction_ratio = 2%`；低频期可放宽至 `8%`。

---

## 15. 服务启动 Warm-up

### 15.1 适用范围

仅适合**静态公共前缀**（system prompt、Agent skills、tool description）。不适合动态用户内容或文档全文。

### 15.2 Warm-up 流程

```
1. 读取离线注册表（hard_protected_registry.txt）
2. 对 top-N 公共前缀构造 warm-up 请求（max_tokens=1，只要求 prefill）
3. 发送请求，触发 KV block 生成
4. 请求结束后 block 进入 FREE_CACHED
5. 准入时命中注册表 → 直接进入 Hard Protected，TTL = permanent_ttl
6. 记录 warm-up 完成时间和已预热 block 数
```

### 15.3 Warm-up 数据来源

```
离线分析输出：reuse_distance_events.csv → 提取高频 prefix chain
当前数据（qwen_v3_5_27b_64k）：
    8,749 个复用事件中，高频公共前缀对应的 block 序列
    可按 hit_count 排序后取 top-50 ~ top-200 条 prefix chain
```

---

## 16. 核心监控指标

### 16.1 命中率类（最关键）

| 指标 | 含义 |
|---|---|
| `prefix_block_hit_rate` | 实际 prefix cache 命中率 |
| `actual_hit / ideal_hit` | 实测命中率 ÷ 离线理想命中率（越接近 1 越好）|
| `protected_queue_hit_rate` | Protected 队列 block 的命中率（衡量 Protected 质量）|
| `saved_prefill_tokens` | 因 prefix hit 节省的 prefill token 数 |

### 16.2 驱逐质量类

| 指标 | 含义 |
|---|---|
| `hot_prefix_eviction_count` | 高价值 block 被驱逐的次数（越低越好）|
| `evicted_before_next_hit_count` | 在下次 prefix hit 前被驱逐的 block 数 |
| `protected_evictions` | Protected 队列驱逐次数 |
| `probation_evictions` | Probation 队列驱逐次数 |

### 16.3 晋升降级类

| 指标 | 含义 |
|---|---|
| `promotion_count` | Probation → Protected 晋升次数 |
| `demotion_count` | Protected → Probation 降级次数 |
| `warmup_blocks_count` | Warm-up 预热成功的 block 数 |
| `registry_hit_rate` | 准入时命中离线注册表的比例 |

### 16.4 队列健康类

| 指标 | 含义 |
|---|---|
| `protected_queue_utilization` | Protected 队列当前使用率 |
| `probation_queue_utilization` | Probation 队列当前使用率 |
| `hard_protected_stale_blocks` | 超过 max_idle 的 Hard Protected block 数 |
| `budget_exceeded_count` | 长输入触发防护预算的次数 |

---

## 17. 最小可行版本（MVP）

第一版仅实现以下功能，验证核心假设：

```
必须实现：
  1. Probation / Protected 两队列（暂不实现 Hard Protected，用 Protected 代替）
  2. hit_count ≥ 2 时触发从 Probation 晋升至 Protected
  3. 离线注册表匹配：注册表中的 block_hash 直接进入 Protected
  4. Protected 容量上限（30% C_total）
  5. TTL 基准值：Protected 入队时 ttl = 46s（F13 p80）
  6. hit_count ≥ 5 时 ttl 延长至 255s（F13 p95）
  7. prefix hit 刷新 TTL 并更新 hit_count
  8. Probation 优先淘汰（TTL=0 LRU 顺序）
  9. Protected TTL 过期后可被正常淘汰（软保护）
  10. 基础指标：prefix_block_hit_rate、promotion_count、protected_evictions

暂不实现：
  - Hard Protected 队列（单独分区）
  - 长输入防护预算（budget 机制）
  - 动态 TTL 收紧
  - priority_score 多维度加权
  - 运行时 role 识别
  - 跨节点共享
  - 降级（demotion）机制
```

---

## 18. 与现有数据的验证计划

| 验证项 | 方法 | 期望结果 |
|---|---|---|
| TTL=46s 是否合适 | 模块二实测：记录每批次的 prefix hit 间隔，与 F13 p80 对比 | 实测 hit interval ≈ F13 p80 = 46s |
| Protected 容量是否足够 | 计算 available_cache_blocks × 30% 与 reuse_distance_p80 的差距 | 差距 < 2× 时算法有显著收益 |
| 高价值 block 驱逐是否减少 | 对比 hot_prefix_eviction_count（Two-Queue vs 默认 LRU） | Two-Queue 该指标 < 默认 LRU |
| 实测命中率 vs 离线理想值 | actual_hit / ideal_hit | 比值接近（理想为 > 0.7）|

---

## 19. 一句话总结

Two-Queue TTL 的本质是：把 FREE_CACHED blocks 按**未来复用价值**分层管理。用 **Protected Queue + TTL（锚定 F13 p80 = 46s）** 保护离线注册的公共前缀和 hit_count ≥ 2 的已验证可复用 block；用 **Probation Queue** 承担主要淘汰压力；在高并发、长输入场景下，通过**长输入防护预算**避免单条请求冲击保护区，从而降低高价值 KV block 在下一次 prefix hit 前被提前淘汰的概率，使实际命中率更接近离线分析给出的理论上界。