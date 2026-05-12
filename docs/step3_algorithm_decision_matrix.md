# Step 3 算法决策矩阵 — 5 维评估 × 4 算法选择

> **创建时间：** 2026-05-12
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
- 跨实例分配：把相同 system prompt 的 request 调度到同一实例
- 同前缀聚合：把同秒到达的 batch 内同前缀请求合并预填
- 按 user 路由：避免 cross-user cache 互相驱逐

### B. 淘汰算法（cache 替换层）
- per-user LRU：每 user 独立淘汰队列
- chain pin：把识别出的 chain block 标记为 不淘汰
- 多 chain 队列：每个 chain 一个独立 LRU 子集
- TTL：长时间未访问的 chain 自动 unpin

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
| **1** | **multi_tenant (≥ 3 user) + reuse_inversion (max/min hit_rate ≥ 2.0)** | **A（路由 / cache 隔离）** | **重度低复用用户驱逐其他用户的 chain，路由分区是首选**；即使本 user hit < 25%（D 适用），路由仍是关键以保护其他 user |
| 2 | `hit_rate < 25%` + chain pin uplift < 5pp | **D（业务侧重写）** | 单租户 / 非倒置场景下业务上限低 |
| 3 | `unique_blocks ≥ 1M` + `hit_rate ≥ 0.7` | **C（池化 / 容量）** | 大模型 + 高 reuse 已知，瓶颈是容量 |
| 4 | multi_tenant + `unique ≥ 200K` 或 `new_block_per_sec_p95 ≥ 200` | **A（路由 / batch 聚合）** | 多租户 + 中度压力（即使无倒置）路由分区仍能减少 cross-user 驱逐 |
| 5 | `chains > 0`（chain 主导业务） | **B（淘汰 / chain pin）** | 默认 |
| 6 | 无 chain forest 兜底 | **D**（multi 加 A 辅） | |

**2026-05-12 关键修订**：A 优先级从最后一名（高 QPS 触发）提到第一名（reuse_inversion 触发）。原因：复用倒置的核心是"低复用用户驱逐高复用用户的 chain"，**路由按 user 隔离 cache 是这个问题的唯一直接解**，比 chain pin 更根本。Qwen-32K / DS-32K 等复用倒置 4–8x 的模型，所有用户都应该走 A 主菜。

### 辅菜规则（通常组合）

| 主菜 | 辅菜组合 | 适用 |
|---|---|---|
| A | **A + B** | 多租户 / 倒置 + 该 user 有 chain（路由隔离 + chain pin，如 Qwen-32K 大部分用户、DS-32K mdata） |
| A | **A + D** | 多租户 / 倒置 + 该 user hit < 25%（路由隔离保护其他 user，业务侧改写作补充，如 Qwen-32K nebula） |
| A | **A + C** | 多租户 / 倒置 + 该 user 无 chain 但 cache 压力大 |
| B | **B + C** | 单租户长 chain（pin + 容量保障，如 Qwen-64K chipset2） |
| B | **B + A** | 多租户 chain 主导（pin + 路由保护，如 Qwen-32B-8K 多数 user） |
| C | **C + B** | 大模型 + 部分 user 有强 chain（容量扩张 + 轻 pin） |
| C | **C + A** | 大模型 + 多租户（容量扩张 + 分区路由） |
| D | **D + 轻 B** | 单租户 hit 低但 chain 仍有点价值 |

### 例外 / 边界

- **chain forest 为空（如 Qwen-32K nebula）**：跳过 B；只能 D 或 A
- **shadow group 存在（人工标注）**：pin 时同 group 合并算容量
- **单租户 + 长 chain（如 GLM）**：B 主菜，C 看 cache 容量是否够 unique
- **单租户 + 短 chain 高 hit（如 Qwen-64K S773）**：C 主菜（容量保 user-internal 复用），B 弱化

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

---

## 8. Open Questions（需 Step 2 实测才能决定）

1. **Top-K 上限调到多少？**`--top-k-users` 3 / 4 / 5 / 全部 ≥1%？建议 4 起步 + 灰色地带继续往后看
2. **路由层 batch 聚合归属**：vLLM 内置 prefix matching 已经做 batch 内复用？还是需要外置 router 层？需 Step 2 测 vLLM 真实行为
3. **prompt 修改的业务沟通流程**：D 主菜需要业务方配合改 prompt 模板，谁主导？怎么衡量改 prompt 后的 hit rate 提升？
4. **KV 量化对 hit rate 的影响**：fp8 / int8 量化是无损还是有损？大模型上（Qwen-64K / 32B-8K）是否值得？
5. **跨实例 cache 池化**：理论收益巨大但工程复杂；与本地 LRU 哪个 ROI 更高？

---

## 9. 工具自动化（2026-05-12 实施）

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
