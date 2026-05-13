# Step 3 算法决策矩阵 — 5 维评估 × 4 算法选择

> **创建时间：** 2026-05-12
> **最近修订：** 2026-05-13（A/B 子类型 framework + llm-d baseline 重新校准 + §4.6 21 user 细化表 + §9.2 v2 工具 spec）
> **上游数据：** [`docs/model_portraits.md`](model_portraits.md)（7 模型 21 用户 chain forest 实测）
> **上游设计：** [`docs/3step_validation_plan.md`](3step_validation_plan.md) §3 Step 2 / §4 Step 3
> **适用场景：** 每个 user 选择 Step 3 算法路径的入口文档

本文是 Step 3 算法选择的**入口文档**。portraits 提供"用户画像 + 实测数据"，本文给"按数据选算法"的规则。要回答 "S773 在 DS-8K 上用什么 cache 优化策略" 这种问题，直接查本文 §4 表格。

---

## 0. 适用前提

- Step 1.1–1.5 已完成，`user_report.json` + `chain_forest.json` + chain decoded content 可访问
- 人工已识别业务类型（router / RAG / Agent / 分类 / 长文档复用）
- 模型参数量、cache 容量 budget、QPS 上限等部署约束已知

---

## 1. 5 维评估输入（每个 user 的 portrait）

| 维度 | 量化指标 | 数据源 | 例 |
|---|---|---|---|
| **业务流量类型** | router / RAG / Agent / 分类 / 长文档复用 / 多模态 | chain decoded content 人工识别 | DS-8K S773 = router |
| **cache 存储压力** | `unique_blocks` / `new_block_per_sec_p95` | `user_report.json` stats | Qwen-64K S773 = 2M unique |
| **模型参数量** | 8B / 27B / 32B / GLM-5.1 | 已知部署配置 | 27B 多卡分布式 |
| **请求量** | `request_count` / avg blocks/req | `user_report.json` stats | Qwen-32B-8K nebula = 56K req |
| **理想命中率** | `ideal_hit_rate`（vLLM block-level）| `user_summary.csv` | DS-32K cloud = 0.04 |

---

## 2. 4 种 Step 3 算法（按 cache 优化层级）

### A. 路由算法（request 调度层）

**生产基线（必读）**：目标部署是华为 Maas 平台，路由层已有两种机制：
1. **负载均衡**（请求级别）
2. **长短文本分流**（按 max_tokens 分到 8K / 32K 实例，避免长文 TTFT 影响短文）

此外 llm-d 已经实现 prefix-aware routing，打分公式：

```
final_score(pod) = 2.0 × precise_prefix_cache_score
                 + 1.0 × kv_cache_utilization_score
                 + 1.0 × queue_score
```

prefix_score 权重最高（2.0），意味着**所有 user 默认享受"按共享前缀选 pod"的路由**——不需要主动推 A。本节 A 子类型的真正语义是：**在 llm-d baseline 之上是否需要额外干预**。

| A 子类型 | 何时需要在 baseline 之上加干预 | 实施手段 |
|---|---|---|
| **A0 baseline** | 所有 user 默认 | llm-d prefix + utilization + queue 已实现；**不算主动推荐** |
| **A1 chain affinity** | 固定长 chain + 稳定 user（cache 抖动时 prefix_score 路由会漂动）| 显式 user → pod 绑定 |
| **A2 prefix routing 调权** | 长文档 / skill 复用主导（动态前缀，非 chain）| llm-d 内已实现；可上调 prefix_score 权重 |
| **A3 isolation routing** | 低 hit + 高流量、高 unique blocks（污染其他 user）| 单独实例 / cache 物理隔离 |

**A0 不算主动推荐，A1/A2 影响小、A3 是真正"激进路由"**。之前主菜表把 A 当一个动作处理，把所有多租户用户推到 A，是过激进的。

### B. 淘汰算法（cache 替换层）

| B 子类型 | 适用 | 实施 |
|---|---|---|
| **B1 简单 LRU** | 长文档 / skill 复用（动态前缀，非 chain）；chain 极长 pin 不划算 | pod 内默认 LRU |
| **B2 chain pin** | **短 chain + 高 cov**（pin 成本低、覆盖高）| KV block 标记 not-evictable |
| **B3 多 chain 队列** | 单 user 内多条独立 chain | 每 chain 一个 LRU 子集 |
| **B-TTL（辅）** | 配合 B2 / B3 防止僵尸 chain 占容量 | 长时间未访问 unpin |

**B 默认不是 B2 chain pin**——长 chain 用户更应该走 B1 LRU（pin 容量过大）。B2 适用面窄：短 chain（≤ 100 block 量级）且 cov 中等以上。

### C. KV cache 池化算法（容量层）
- 容量分区：cache 按 user / chain 划分配额
- 跨实例共享：多机 cache 池化（NVLink / RDMA）
- KV 量化：fp8 / int8 压缩 trade-off 命中率换容量
- 容量扩张：物理扩容（最直接但最贵）

### D. prompt 修改（业务层）
- 减少动态字段（去掉 wrapper 中的 request_id / timestamp）
- 业务侧统一 system prompt（避免同 user 多版本 prompt 漂移）
- 拆分多轮对话（长 history 单独 cache）
- 接受"不优化"：业务上限低时承认 cache 不是杠杆

---

## 3. 5 维 → 4 算法映射规则

### 主菜规则（按优先级自上而下）

| 优先级 | 触发条件 | 主菜算法 | 原因 |
|---|---|---|---|
| **1** | **multi_tenant (≥ 3 user) + reuse_inversion (max/min hit_rate ≥ 2.0) + 本 user 低 hit + 高流量** | **A（A3 isolation routing）** | 低复用用户驱逐其他用户的 chain，**单实例隔离是直接解**；不是所有多租户 user 都走 A，仅低 hit + 高流量的"污染源" user |
| 2 | `hit_rate < 25%` + chain pin uplift < 5pp | **D（业务侧重写）** | 业务上限低；可与 A3 配合（低 hit 高流量 user = D + A3） |
| 3 | `unique_blocks ≥ 1M` + `hit_rate ≥ 0.7` | **C（池化 / 容量）** | 大模型 + 高 reuse，瓶颈是容量 |
| 4 | 单 user 有长 chain（≥ 200 block）且稳定 | **A（A1 chain affinity）+ B1 LRU** | 长 chain 不 pin（pin 容量过大），改为 user → pod 绑定 + pod 内 LRU |
| 5 | 单 user 有 **短** chain（≤ 100 block）且 cov ≥ 15% | **B（B2 chain pin）** | 默认；pin ROI 高 |
| 6 | 单 user 有 ≥ 3 条独立短 chain | **B（B3 多 chain 队列）** | 默认 |
| 7 | 长文档 / skill 复用（动态前缀，无 chain）| **A2 prefix routing + B1 LRU** | 依赖 llm-d prefix_score；pod 内 LRU |
| 8 | 无 chain forest 且不触发 1–3 兜底 | **D**（+ A0 baseline 即可） | |

**重要语义修订（2026-05-13）**：

- **A 不再是"多租户必走主菜"**。llm-d baseline 已经做了 prefix-aware routing，A 的真正语义是"额外干预"——只在 A1（长 chain affinity）/ A3（污染源隔离）触发；A0/A2 不算主动推荐。
- **B 不再默认是 chain pin**。多数用户（长文档、长 chain）实际上是 **B1 LRU**；B2 chain pin 适用面窄（短 chain + 中等 cov 以上）。
- **2026-05-12 的"A 优先级 1"修订过激进**：把所有多租户 user 都推到 A 不正确。本次修订（2026-05-13）将 A1（priority 1）限定到"低 hit + 高流量的污染源"，多租户 chain 主导用户回到 B5/B6。

### 辅菜规则（通常组合）

| 主菜 | 辅菜组合 | 适用 |
|---|---|---|
| **A3 isolation** | **A3 + D** | 污染源 user（低 hit 高流量）：隔离 + 业务侧重写（Qwen-32K nebula、DS-32K S773 / cloud） |
| **A1 affinity** | **A1 + B1 LRU** | 长 chain 用户：绑定 pod + pod 内 LRU（Qwen-64K supply / chipset2） |
| **A2 prefix** | **A2 + B1 LRU** | 长文档复用：依赖 llm-d + pod 内 LRU（Qwen-64K S773） |
| **B2 pin** | **B2 + A0 baseline** | 短 chain 高 cov（默认走 A0 baseline，不需要 A 主动干预） |
| **B3 多队列** | **B3 + A0 baseline** | 多独立 chain（DS-8K 全部 user、Qwen-32B-8K ags） |
| C | **C + B1 / B2** | 大模型 + 容量保障（Qwen-64K chipset2 是 C + B1 LRU + A1 affinity 三合一） |
| **D** | **D + A0 baseline** | 单租户低 hit（Qwen-8B-8K） |
| **D** | **D + A3 isolation** | 多租户低 hit 高流量 = 污染源 |

### 例外 / 边界

- **chain forest 为空（如 Qwen-32K nebula）**：跳过 B；走 D + A3
- **shadow group 存在（人工标注）**：pin 时同 group 合并算容量
- **单租户 + 长 chain（如 GLM）**：B2 pin 全 chain（156-377 block 范围还在 pin 划算区间），C 备容量
- **单租户 + 短 chain 高 hit（如 Qwen-64K S773）**：C 主菜 + B1 LRU；不 pin 17 block chain（长文档复用靠 llm-d prefix_score + LRU）

### 工具自动化 caveat（重要）

> **§9 描述的 `per_user_report_analyzer.py` 自动推荐是"粗粒度"**：它产出 `primary=A/B/C/D` 但不区分 A0/A1/A2/A3 或 B1/B2/B3。
>
> **A/B 子类型必须人工根据 chain 形态判断**：
> - chain 数量（1 / 2-3 / 4+）
> - dom_len 长度（≤ 100 = 短；100-500 = 中；> 500 = 长）
> - cov 比例（≥ 15% = pin 划算；< 5% = LRU）
> - hit_rate + 流量占比组合（低 hit + 高流量 = 污染源 → A3）
>
> §4.6 给出 21 user 的细化映射作为参考。新模型分析时：先看工具粗推荐，再查 §4.6 同形态用户的细化判断。

---

## 4. 21 用户决策矩阵（基于 2026-04-24 trace 实测）

### 4.1 高 ROI chain pin 主菜（B 主导）

| 模型 | user | hit | chains | dom_len / cov | 推荐 | 实施要点 |
|---|---|---|---|---|---|---|
| **DS-8K** | S773 | 0.55 | 3 | 56 / 46.6% | **B 多队列** | 3 条独立 chain（问题分类器/会议纪要/多轮问答），每 chain 独立 LRU |
| **DS-8K** | ebg.ioc.efc | 0.71 | 6 | 20 / 10.8% | **B 多队列** | 6 chain 累计 cov 47.5%，pin 总成本 219 block |
| **Qwen-32B-8K** | quality_public_sentiment | 0.75 | 2 | 14 / 61.8% | **B 极短 pin** | **全平台单 pin ROI 第一**（14 block / 61.8% cov），shadow 后实际 cov 可能更高 |
| **Qwen-32B-8K** | ags（隐藏 Top-4） | **0.80** | 6 | 100 / 17.5% | **B + 业务侧验证** | hit 全模型最高，6 chain pin 总容量 < 600 block |
| **GLM-V5.1** | tianzhou.ai | 0.94 | 7 | 156 / 14.7% | **B（pin 全 7 chain）** | 7 chain 总 1463 block，仅占 unique 0.74%；C 备用看 cache 是否能 hold 197K unique |
| **DS-32K** | mdata.mdata20180908 | 0.70 | 4 | 588 / 22.1% | **B（含 shadow 修正）** | chain 1+2 经人工标注是 shadow，pin 时按 14 block 算共享前缀 |
| **Qwen-32K** | ...000022 | 0.73 | 1 | 65 / 28.9% | **B 单 chain pin** | 单 chain 但 cov 较高，pin 65 block 拿 28.9% 该 user cov |

### 4.2 池化 / 容量主菜（C 主导）

| 模型 | user | hit | unique_blocks | 推荐 | 实施要点 |
|---|---|---|---|---|---|
| **Qwen-64K** | S773 | 0.82 | ~2M Heavy WS | **C 大容量 LRU** | hit 0.82 来自 user-internal 长文档复用；chain pin 仅 17 block 几乎无用；需大容量 + 可能 KV 量化 |
| **Qwen-64K** | chipset2 | 0.92 | 17,292 | **C + B 组合** | chain 1172-1802 block × 7 条 = ~11K pin 容量，需 C 保障 + B 实施 |
| **Qwen-64K** | supply.ioc.rock | 0.74 | 87,170 | **C + B 组合** | 10 chain × 平均 700 block，supply 的 51-block 共享前缀（shadow 修正）pin 单价极高 |

### 4.3 路由 / batch 聚合主菜（A 主导）

| 模型 | user | reqs | 推荐 | 实施要点 |
|---|---|---|---|---|
| **Qwen-32B-8K** | 全部 4 user（aggregate） | 119K req / 2h | **A + B 组合** | reuse p50=0s 暗示同秒大 batch；A 层 batch 聚合后再 B 层 chain pin。**待 Step 2 实测同 batch 内 cache 命中行为** |
| **Qwen-32B-8K** | nebula.venussearch | 56,694 | **A 主菜** | 47.6% 流量 + insertion_rate 105/s，需 A 路由分流；B chain pin 23 block 是辅助 |

### 4.4 放弃 prefix cache（D 主导）

| 模型 | user | hit | 失败原因 | 推荐 |
|---|---|---|---|---|
| **Qwen-8B-8K** | S773 | 0.17 | 邮件分类，每邮件独立内容；chain 14 block / cov 5.9% | **D 业务侧**（如能改提示模板减少噪音可能微提升） |
| **Qwen-32K** | nebula.venussearch | 0.15 | 2.6M unique blocks，**chain forest 为空** | **D + A**（路由分流减少单实例压力） |
| **DS-32K** | S773 | 0.09 | 60.8% 流量但 chain 仅 10 block / cov 12.3% | **D + 轻 B** |
| **DS-32K** | cloud.ioc.global | 0.04 | 14.7% 流量但 hit 4%，AI 答案质量评估业务每次输入不同 | **D**（业务上限太低） |
| **Qwen-32K** | S773（隐藏 Top-4） | 0.07 | 11.3% 流量同 DS-32K S773 模式 | **D + 轻 B** |

### 4.5 中间灰色地带（需 Step 2 进一步实测）

| 模型 | user | hit | 推荐 | 备注 |
|---|---|---|---|---|
| **Qwen-32K** | ai.ocr | 0.38 | **B 探索** | 4 chain 短（20 block dom_len），pin 收益不确定，需 Step 2 实测真实命中率 |
| **DS-8K** | mdata.mdata20180908 | 0.25 | **B 多队列** | 5 chain 短（14-95 block dom_len），与同名 DS-32K 用户业务相同但 chain 短 8 倍 |

### 4.6 A/B 子类型细化（21 user 人工判断，2026-05-13）

§4.1–§4.5 给的是工具粗推荐（A/B/C/D 主菜）。本表用 §2 引入的 A0/A1/A2/A3 + B1/B2/B3 子类型重新分类，结合 llm-d baseline 真实语义。**新模型分析时优先查本表同形态用户**。

| 模型 | user | 流量 | hit | chain 形态 | 工具粗推荐 | **细化推荐** | 关键论据 |
|---|---|---|---|---|---|---|---|
| **Qwen-64K** | S773 | 95.2% | 0.82 | 短 chain 17 + 长文档复用 | C + A | **A2 prefix + B1 LRU + C 容量** | 17 block pin 价值低；长文档靠 llm-d prefix_score |
| **Qwen-64K** | supply.ioc.rock | 3.1% | 0.74 | 10 chains × 380-1374 | C + B | **A1 affinity + B1 LRU** | 10 chain 平均 700 block，pin 容量过大 |
| **Qwen-64K** | chipset2 | 1.6% | 0.92 | 7 chains × 1172-1802 | C + B | **A1 affinity + B1 LRU + C 容量** | 同理，长 chain 不 pin |
| **Qwen-32K** | nebula | 27.9% | 0.15 | 0 chains, 2.6M unique | D + A | **A3 isolation + D** | 2.6M unique 必须单独实例防污染 |
| **Qwen-32K** | ai.ocr | 24.5% | 0.38 | 4 chains 20 block dom_len | B 探索 | **A0 baseline + B1 LRU** | chain 短 cov 低，pin ROI 不高 |
| **Qwen-32K** | ...000022 | 12.0% | 0.73 | 1 chain 65 / cov 28.9% | B 单 chain | **A0 + B2 chain pin** | 短 chain 中 cov，pin 划算 |
| **Qwen-32K** | S773 | 11.3% | 0.07 | 1 chain 10 | D + 轻 B | **A3 isolation + D** | 流量小但 hit 极低，仍是污染源 |
| **Qwen-32B-8K** | nebula | 47.6% | 0.71 | 2 chains 23 / 24.9% | B + A | **A0 + B2 chain pin** | 短 chain，pin 划算 |
| **Qwen-32B-8K** | S773 | 21.4% | 0.73 | 2 chains 34 / 41.4% | B + A | **A0 + B2 chain pin + shadow 合并** | 短 chain 高 cov；§6.4 shadow 修正 |
| **Qwen-32B-8K** | quality_public_sentiment | 12.0% | 0.75 | 2 chains 14 / 61.8% | B + A | **A0 + B2 chain pin** | 全平台 pin ROI 第一 |
| **Qwen-32B-8K** | ags | 11.0% | 0.80 | 6 chains 100 / 17.5% | B + A | **A0 + B3 多队列** | 6 chain 各自独立 |
| **GLM-V5.1** | tianzhou.ai | 100% | 0.94 | 7 chains 156-377 | B pin 全 | **A0 + B2 pin 全 7 chain + C 备容量** | 单租户，1463 block 总 pin 容量在划算区间 |
| **DS-8K** | S773 | 88.5% | 0.55 | 3 chains 56 / 46.6% | B 多队列 | **A0 + B3 多队列** | 3 chain 各自独立 |
| **DS-8K** | mdata | 3.6% | 0.25 | 5 chains 14-95 | B 多队列 | **A0 + B3 多队列** | 同上 |
| **DS-8K** | ebg.ioc.efc | 3.5% | 0.71 | 6 chains 20-58 / cov 47.5% | B 多队列 | **A0 + B3 多队列** | 6 chain 累计 cov 高 |
| **DS-32K** | S773 | 60.8% | 0.09 | 1 chain 10 | D + 轻 B | **A3 isolation + D** | 60.8% 流量 + hit 9%，最强污染源 |
| **DS-32K** | mdata | 14.9% | 0.70 | 4 chains 588 / 14 / 14 / 815 | B + shadow | **A0 + B2 chain pin（含 shadow 合并）+ A1 affinity** | chain 1+2 shadow 后 14 block pin 24% cov；588/815 长 chain 用 affinity |
| **DS-32K** | cloud.ioc.global | 14.7% | 0.04 | 1 chain 60 | D | **A3 isolation + D** | hit 4% 是污染源 |
| **Qwen-8B-8K** | S773 | 100% | 0.17 | 1 chain 14 | D | **A0 + D 业务侧** | 单租户无路由可做 |

**汇总（子类型主菜分布）**：

| 子类型主菜 | user 数 | 流量占比 | 典型代表 |
|---|---|---|---|
| A3 isolation routing | 4 | 27.9 + 11.3 + 60.8 + 14.7 = ~115%（跨模型）| Qwen-32K nebula、DS-32K S773 / cloud、Qwen-32K S773 |
| A1 chain affinity | 3 | ~5% | Qwen-64K supply / chipset2、DS-32K mdata 长 chain 部分 |
| A2 prefix routing | 1 | Qwen-64K 95% | Qwen-64K S773 |
| A0 baseline only | 13+ | 大部分 | 所有其他 user |
| B1 LRU only | 4 | ~30% | Qwen-64K 全 3 user + Qwen-32K ai.ocr |
| B2 chain pin | 6 | ~25% | DS-8K S773 chain 0、Qwen-32B-8K 4 user、Qwen-32K 000022、DS-32K mdata |
| B3 多队列 | 4 | ~10% | DS-8K 全 3 user、Qwen-32B-8K ags |

**关键观察**：
1. 真正需要工程层 A 干预的（A1 + A3 = 7 user），不到一半；多数走 A0 baseline + B1/B2/B3
2. B1 LRU（4 user）+ B2 chain pin（6 user）+ B3 多队列（4 user）≈ 14 user，**多于工具粗推荐 B 的覆盖**——因为长文档用户在工具里被推 C，实际淘汰侧仍是 LRU
3. D 主菜（5 user）中 **4 个** 配 A3 isolation（不是单独走 D），印证 §3 主菜规则 priority 1 + 2 的组合

---

## 5. 模型级最优策略汇总

| 模型 | 主菜组合 | 单 user 维度处理 | Step 2 验证重点 |
|---|---|---|---|
| **Qwen-V3.5-27B-64K** | **C 池化** | S773 LRU + 轻度 user (supply/chipset2) chain pin | 测 cache 容量上限 + KV 量化对 hit 的影响 |
| **Qwen-V3-8B-8K** | **D prompt 修改** | 工具层无空间 | 验证业务侧 prompt 改写后 hit 提升幅度 |
| **Qwen-V3-32B-8K** | **A + B 组合** | 路由 batch + 4 user chain pin（含 ags） | 测同 batch 内 cache 命中行为，区分真复用 vs batch 内 |
| **Qwen-V3-32B-32K** | **A + D 混合** | nebula 走 A+D；000022 走 B；S773 走 D | 测 nebula 极端多样 → 路由分流降压 |
| **GLM-V5.1** | **B 主菜（pin 全 7 chain）** | 单租户简单 | 测 cache 容量 197K 是否能 hold |
| **DeepSeek-V3.1-8K** | **B 多 chain 队列** | 3 user 各自 router 业务，每 user 独立队列 | 测 chain hash 生命周期（prompt 版本漂移监控） |
| **DeepSeek-V3.1-32K** | **B（集中在 mdata）** | S773 / cloud 放弃，mdata 唯一 chain pin 候选 | 测 mdata 14 block shadow pin 真实命中收益 |

---

## 6. 关键交叉规则（与 §3 主菜规则的优先级判断）

### 6.1 D 优先级最高（放弃 prefix cache 时其他算法都收益递减）

如果 `ideal_hit_rate < 25%` **且** chain pin 后预计 hit 提升 < 5pp，**直接 D**，不要在 A/B/C 上花工程时间。**21 user 中至少 5 个走 D**（占总流量 ~30%）。

### 6.2 C 早于 B（大模型 + WS 巨大时）

如果模型 ≥ 27B **且** `unique_blocks` ≥ 1M，**先解决容量再讨论 pin**。Qwen-64K S773 案例：chain pin 17 block 几乎无用，因为真正瓶颈是 2M unique 装不下。

### 6.3 A 早于 B（多租户共享前缀 + 高 QPS）

如果某个 system prompt 被多 user 共走（即模型 1.1 的 global chain cov 高）**且** QPS ≥ 1K，**先路由聚合再考虑 pin**。否则 cache 被不同 user 互相驱逐，pin 也保不住。

**llm-d baseline 修订（2026-05-13）**：在 llm-d 部署下，"多 user 共走 prefix 路由聚合"由 prefix_cache_score（权重 2.0）自动完成，**不需要工程层 A 干预**。本规则的真正语义变为：
- 共享 prefix 且 QPS ≥ 1K → A0 baseline 就够（llm-d 自动 batch 聚合同 prefix）
- 仅当 cache 抖动让 prefix_score 路由飘动时 → 升级到 A1 chain affinity
- 仅当低 hit + 高流量 user 污染其他 user → 升级到 A3 isolation

### 6.4 shadow group（人工标注）的修正

pin 决策必须查 portraits §3.6 / 模型 findings 文档的 shadow 标注：

```
原始 pin 估算：chain X (a block) + chain Y (b block) = a+b
shadow 修正：if X, Y 在同 shadow group with N byte 共享：
            实际 pin 容量 = N // block_size + (a − N/block_size) + (b − N/block_size)
```

例：Qwen-32B-8K S773 chain 0 (34) + chain 1 (14)，shadow 标注共享 14 block，实际 pin = 14 + 20 + 0 = **34 block 拿 78.75% cov**（而非 48 block）。

---

## 7. 隐藏的发现（决策矩阵解锁的）

### 7.1 算法不应单选

21 user 中 **15+** 需要主+辅算法组合（B+C / A+B / D+轻 B）。portraits §2 之前给"每模型一个主算法"过于简化。

### 7.2 D 流量占比 ~30% 比直觉高

放弃 prefix cache 的 5 个 user 加起来覆盖 **27% 总流量**（Qwen-8B-8K S773 100% × 该模型流量 + DS-32K S773/cloud + Qwen-32K nebula/S773）。工程投入应该重新分配：

| 投入方向 | 当前 portraits §2 暗示 | 决策矩阵新发现 |
|---|---|---|
| chain pin 算法实施 | 主要投入 | 仅约 50% 流量受益 |
| KV 量化 / 容量扩张 | 次要 | Qwen-64K 是真正瓶颈 |
| 业务侧 prompt 改写 | 几乎没提 | **30% 流量需要业务方协作**，工具层无法救 |

### 7.3 隐藏 Top-K 用户的价值

Qwen-32B-8K ags 是 Top-4 才能看到的用户，hit 0.80 全模型最高。**建议常规分析把 `--top-k-users` 从 3 调到 4 或更高**，避免漏识高价值用户。或加 "Top-K 后 ≥ 5% 流量也分析" 规则。

### 7.4 C 池化优先级被低估

portraits §2 把 Qwen-64K 归到"多 prompt 并行"（multi-chain 队列），但实测表明**主用户的瓶颈是容量不是 chain**。Step 3 算法设计 should start with "how much cache budget" rather than "what to pin"——这是决策矩阵相对二维分类的核心差异。

### 7.5 llm-d baseline 重新校准 A 的语义（2026-05-13）

回看 §2 / §4 之前的 "A 主菜" 分类，多数 user 其实只需要 A0 baseline（llm-d 已实现），不需要工程层干预。真正"需要做事" 的 A 子类型仅 7 user（A1 × 3 + A2 × 1 + A3 × 4）：

| 之前误判 | 修订认知 |
|---|---|
| "多租户必走 A 主菜" | 多租户在 llm-d 下默认有 prefix-aware routing；只有低 hit 高流量污染源才升级到 A3 isolation |
| "A + B 组合是主流" | 多数 user 是 A0 + B（B1/B2/B3）；A 没动作只是 baseline 兜底 |
| "B 主菜 = chain pin" | B 默认是 B1 LRU；B2 pin 适用面窄（短 chain + cov ≥ 15%）；长 chain 用户反而是 B1 LRU 更优 |

**对 portraits / Step 3 算法设计的影响**：
- 工程优先级排序应该是 **A3 isolation > B2/B3 chain pin > C 容量 > D 业务侧 > A1/A2 微调**
- A1/A2 在 llm-d 部署下投入产出比低，除非生产观察到 prefix_score 路由抖动
- A3 isolation 是最值得投入的 A 子类型（4 user 占跨模型流量大头），且实施门槛低（DevOps 层即可，不需要改算法）

---

## 8. Open Questions（需 Step 2 实测才能决定）

1. **Top-K 上限调到多少？**`--top-k-users` 3 / 4 / 5 / 全部 ≥1%？建议 4 起步 + 灰色地带继续往后看
2. **路由层 batch 聚合归属**：vLLM 内置 prefix matching 已经做 batch 内复用？还是需要外置 router 层？需 Step 2 测 vLLM 真实行为
3. **prompt 修改的业务沟通流程**：D 主菜需要业务方配合改 prompt 模板，谁主导？怎么衡量改 prompt 后的 hit rate 提升？
4. **KV 量化对 hit rate 的影响**：fp8 / int8 量化是无损还是有损？大模型上（Qwen-64K / 32B-8K）是否值得？
5. **跨实例 cache 池化**：理论收益巨大但工程复杂；与本地 LRU 哪个 ROI 更高？

---

## 9. 工具自动化

### 9.1 v1 实施（2026-05-12）

§3 的映射规则现在已落地到 `per_user_report_analyzer.py`，每次跑 Step 1.5 自动产出推荐：

- `chain_forest.json` 同级的 `user_report.json` 新增顶层字段 `step3_recommendation`，含 `primary_algorithm` / `companion_algorithm` / `business_type`（启发式推断）/ `reasons` / `difficulty` / `estimated_uplift` / `implementation_steps`
- `user_summary.csv` 加 3 列：`rec_primary` / `rec_companion` / `rec_difficulty`
- `user_report.html` §6 渲染推荐板块（4 种主菜算法用不同颜色：A 蓝 / B 绿 / C 紫 / D 橙）

**业务类型启发式**（chain decoded 内容自动识别）：
- agent_tools（JSON tool schema 含 `"tools"` / `"function"`）
- router（中文"你是 XX 助手" + ≥ 3 条独立 chain）
- rag（RAG / 文档 / markdown 关键词 + 2-6 chain）
- classification（短 chain + 分类关键词）
- short_chain_unknown / unknown / none（标 unknown 时人工 inspect HTML §5）

业务类型识别仅是启发式（参考 portraits §3.6 / 上文 v3 shadow detection 撤回教训——精确业务识别本质是语义层任务，工具只给候选）；HTML §6 同时显示 evidence_snippet，人工核对成本极低。

提升估计的算法逻辑：
- B：`top_cov × (1 − hit_rate)` 作为 chain pin 后命中率提升的上界
- C：命中率上限 = `ideal_hit_rate`（cache 容量充足时）
- A：依赖 vLLM batch 行为，Step 2 实测前置信度低
- D：完全业务依赖，无法量化

**重要 caveat**：所有数字都是 Step 1 信号 → 启发式推断，**Step 2 实测前不是承诺**。HTML §6 末尾显式标注此点。

v1 caveat：仅产出粗类型 A/B/C/D 主菜，**不区分 A0/A1/A2/A3 子类型 或 B1/B2/B3**；细类需人工查 §4.6。v2（§9.2）将解决此限制。

---

### 9.2 v2 spec（2026-05-13，待编码）

v1 缺失维度（5 维评估实际只用了 3 维）：
- ❌ 未抓"每分钟请求数 RPM"、"每分钟写入 unique block 数"
- ❌ 未做流量突变检测
- ❌ 未抓"实例个数 / cache 容量 / 模型参数量"（必须人工补，HTML 应红色标注缺失）
- ❌ 推荐输出粗到 A/B/C/D 主菜，没到子类型

v2 spec 修正上述全部 gap，但**不固定 B(2) 多队列淘汰的打分公式**（留 Step 2 实测调参）。

#### 9.2.1 数据采集新增字段

**模型层**（新建 `model_report.json` 或扩展 `per_user_reports/_aggregate.json`）：

```jsonc
{
  // 自动采集
  "n_users": int,                         // Top-K 后再 ≥3% 流量的 user 数
  "ideal_hit_rate_aggregate": float,      // 模型 total_hit / total_blocks
  "rpm_avg": float,                       // total_requests / trace_minutes
  "unique_rpm_avg": float,                // total_unique_blocks / trace_minutes
  "traffic_spikes": [                     // 5min req 数窗口 × 5× 突变
    {"window_start": "...", "window_end": "...", "ratio_to_prev": 7.2}
  ],
  "spike_config": {"window_minutes": 5, "threshold_multiplier": 5.0},

  // 人工补字段（HTML 红色标注 "缺失，需人工补"）
  "model_params_class": null,             // "small_le_32B" | "large_200B_moe"
  "instance_count": null,                 // 当前部署实例个数
  "cache_capacity_blocks": null           // 单实例 cache 容量（block 数）
}
```

**用户层**（扩展 `user_report.json`）：

```jsonc
{
  // 已有字段不变
  // 新增字段
  "rpm_avg": float,
  "unique_rpm_avg": float,
  "avg_blocks_per_request": float,        // total_blocks / total_requests
  "chain_length_ratio": float,            // dom_len / avg_blocks_per_request
  "share_of_model_unique": float,         // user_unique / model_total_unique
  "classifications": {
    "hit_band": "low" | "normal" | "high",
    "cov_band": "low" | "normal" | "high",
    "chain_len_band": "short" | "long",
    "unique_share_band": "low" | "normal" | "high",
    "chain_count_band": "few" | "many"   // ≥ 3 = many
  },
  "is_anomaly": bool                      // long + low_cov + low_hit
}
```

#### 9.2.2 分类阈值

| 维度 | 低 | 正常 | 高 |
|---|---|---|---|
| hit_band | `< 0.30` | `0.30 – 0.60` | `> 0.60` |
| cov_band | `< 0.10` | `0.10 – 0.50` | `> 0.50` |
| chain_len_band | — | ratio ≤ 0.3 → short | ratio > 0.3 → long |
| unique_share_band | `≤ 5%` | `5% – 30%` | `≥ 30%` |
| chain_count_band | — | `< 3` few | `≥ 3` many |
| 流量突变 | — | — | `bucket[i+1]/bucket[i] ≥ 5×` (req 数, 5min 窗口) |

所有阈值在 spike_config / classifications config 中可调（默认值写死，CLI flag 可覆盖）。

#### 9.2.3 推荐决策伪代码

**A 子类（路由）**：

```python
if model.params_class == "large_200B_moe":   # DSK / GLM
    A = "A(4) 暂缓"
    note = "待人工补 instance_count + cache_capacity_blocks"
elif model.n_users >= 3 and reuse_inversion \
     and user.hit_band == "low" and user.unique_share_band == "high":
    A = "A(1) isolation"
elif model.n_users <= 3 and user.hit_band == "high" and user.unique_share_band == "high" \
     and user.chain_count_band == "many" and user.chain_len_band == "long" \
     and user.cov_band in ("normal", "low"):
    A = "A(2) 多 chain 实例化 + 实例内多队列 LRU"
elif model.n_users <= 3 and user.hit_band == "high" and user.unique_share_band == "high" \
     and user.chain_count_band == "few" and user.chain_len_band == "short" \
     and user.cov_band == "high":
    A = "A(3) skill/文档 prefix routing"
else:
    A = "A0 baseline (llm-d)"
```

**B 子类（淘汰）**——模型层开关 + user 层配置：

```python
# 模型层开关
if (model.n_users >= 3 and any(u.unique_share_band == "high" for u in users)) \
   or (model.n_users == 1 and model.total_chain_count >= 3):
    B_model = "B(2) 多队列 LRU（按 user-chain-hash; 淘汰打分 TBD）"
else:
    B_model = "B(1) 默认 LRU"

# user 层配置（仅 B(2) 触发时有意义）
if B_model.startswith("B(2)") and user.chain_count >= 1:
    B_user_config = f"为 user 的 {user.chain_count} 条 chain 各分配 1 个独立队列"
```

**C 子类（池化）**：

```python
if user.unique_share_band == "high" and B_model == "B(1) 默认 LRU":
    # 路由/淘汰边际收益低 → 池化是杠杆
    C = "C(1) 强池化"
elif user.chain_count_band == "many" and user.chain_len_band == "long":
    C = "C(2) 弱池化（容量保障）"
else:
    C = None
```

**反常 user 高亮**：

```python
is_anomaly = (user.chain_len_band == "long"
              and user.cov_band == "low"
              and user.hit_band == "low")
# HTML 整行黄底 + 文字提示 "chain 长但 cov/hit 双低，建议人工检查 chain decoded 内容
#                          判断是否 wrapper boilerplate / 业务噪声"
```

#### 9.2.4 HTML 改动

新增 / 修改板块：

| 板块 | 改动 |
|---|---|
| **顶部 §0 模型层指标**（新增） | 表格列：n_users / ideal_hit_rate / rpm_avg / unique_rpm_avg / spike_count / model_params_class / instance_count / cache_capacity_blocks；**后 3 项空时红底**"⚠️ 缺失，需人工补到 `model_report.json`" |
| **§0.1 流量突变时刻**（新增） | 列 spike 时间窗 + 突变倍数；无 spike 时显示 "无 ≥ 5× 突变" |
| §1-§5 现有 | 保留 |
| **§5.1 chain 表格** | 新增列 `chain_length_ratio` + 色标：hit/cov 高（绿）/ 低（红）/ 正常（灰）；is_anomaly 行整行黄底 |
| **§6 推荐板块** | 改为显示子类型：A(0/1/2/3/4) + B(1/2) + C(1/2)；A(4) 暂缓时显式"待人工补字段"；B(2) 时显式"淘汰打分公式 TBD（Step 2 实测）" |

#### 9.2.5 21 user 终极归类（参考基准）

工具实现后，跑 7 模型应得出与本表一致的子类型；不一致需排查规则。

| user | A | B (模型层) | C | 反常 |
|---|---|---|---|---|
| Qwen-64K S773 | A(3) | B(2) | C(1) | — |
| Qwen-64K supply | A(2) | B(2) | C(2) | — |
| Qwen-64K chipset2 | A(2) | B(2) | C(2) | — |
| Qwen-32K nebula | A(1) | B(2) | — | — |
| Qwen-32K ai.ocr | A0 | B(2) | — | — |
| Qwen-32K ...000022 | A0 | B(2) | — | — |
| Qwen-32K S773 | A(1)（边界，流量 11%） | B(2) | — | — |
| Qwen-32B-8K nebula | A0 | B(2) | — | — |
| Qwen-32B-8K S773 | A0 | B(2) | — | — |
| Qwen-32B-8K quality | A0 | B(2) | — | — |
| Qwen-32B-8K ags | A0 | B(2) | — | — |
| GLM tianzhou | A(4) 暂缓 | B(2)（单租户 7 chain）| C(2) | — |
| DS-8K S773 | A(4) 暂缓 | B(2) | — | — |
| DS-8K mdata | A(4) 暂缓 | B(2) | — | — |
| DS-8K ebg | A(4) 暂缓 | B(2) | — | — |
| DS-32K S773 | A(4) 暂缓 | B(2) | — | — |
| DS-32K mdata | A(4) 暂缓 | B(2) | — | — |
| **DS-32K cloud** | A(4) 暂缓 | B(2) | — | **✅ 高亮** |
| Qwen-8B-8K S773 | A0（单租户） | B(1) | — | 待 chain_ratio 实测确认 |

#### 9.2.6 TBD（编码时需注意，留 Step 2 实测）

| 项 | 当前处理 | 后续 |
|---|---|---|
| **B(2) 多队列淘汰打分公式** | 工具只推荐 "走 B(2)"，不出公式 | Step 2 实测调参 |
| **A(1) 流量"高"边界** | 默认用 unique_share_band == "high"（≥ 30%）作为代理；Qwen-32K S773 unique_share 不到 30% 但 hit 极低，规则可能漏 | Step 2 验证后调整 |
| **chain_length_ratio 实测 vs 估算** | 凡是 avg_blocks_per_request 未确认的 user（如 Qwen-8B-8K S773），先按工具实际算结果分类，可能与 §9.2.5 推断有出入 | 工具跑完后核对，必要时调阈值 |
| **流量突变响应策略** | 仅检测 + 报告，不进推荐 | Step 2 设计弹性扩容触发 |

#### 9.2.7 编码 checklist（v2 落地前的 spec 检查点）

- [ ] 模型层指标采集（`compute_model_context()` 扩展或新建函数）
- [ ] 5 min spike 检测器（独立函数 `detect_traffic_spikes(records, window_min, threshold)`）
- [ ] user 层新增字段（`compute_user_stats()` 加 4 个字段）
- [ ] 6 个 classification band 计算
- [ ] is_anomaly 计算
- [ ] 推荐规则重写为子类型版本（`compute_step3_recommendation()` 大改）
- [ ] HTML §0 模型层 + §0.1 spike + §5.1 色标 + §6 子类型显示（`render_user_report_html.py` 大改）
- [ ] 人工补字段 placeholder 红色标注
- [ ] CLI flag 暴露 spike_threshold / 阈值参数（可调）
- [ ] 跑 7 模型验证：归类结果与 §9.2.5 一致；不一致项写进 §9.2.8 偏差日志

#### 9.2.8 偏差日志（v2 实施时填）

> 工具跑完 7 模型后，记录与 §9.2.5 不一致的 user + 原因。**预期至少 Qwen-32K S773 / Qwen-8B-8K S773 因边界判断会有偏差**。

---

## 10. 沉淀决策时的工具链

把上述 5 维评估自动产出的 cookbook：

```bash
# 拉每个 user 的 5 维 portrait
python3 -c "
import json
m = 'Qwen-V3-32B-8K'
u = 'com.huawei.meta.crm.analyticscloud.ags'
d = json.load(open(f'outputs/{m}/per_user_reports/{u}/user_report.json'))
s = d['stats']
print(f'business     : (人工 from chain decoded)')
print(f'cache pressure: unique={s[\"unique_blocks\"]:,} blocks, '
      f'new_block/s p95={d[\"new_unique_blocks_per_sec_q\"][\"p95\"]}')
print(f'model params : (已知部署配置)')
print(f'request load : {s[\"total_requests\"]:,} reqs, '
      f'avg {s[\"total_blocks\"]/s[\"total_requests\"]:.0f} block/req')
print(f'hit rate     : {s[\"ideal_hit_rate\"]:.3f}')
"
```

填到 §3 的映射规则表里得算法。

---

## 11. 参考资料

- 模型画像 + chain forest 实测：[`docs/model_portraits.md`](model_portraits.md)
- 实验设计（Step 1.5 D1–D8 决策点）：[`docs/per_user_research_design.md`](per_user_research_design.md)
- 三步走战略大纲：[`docs/3step_validation_plan.md`](3step_validation_plan.md)
- DS-8K 单模型深度发现：[`docs/dsk8k_step1_findings.md`](dsk8k_step1_findings.md)
- 操作 SOP：[`docs/step1_runbook.md`](step1_runbook.md)
