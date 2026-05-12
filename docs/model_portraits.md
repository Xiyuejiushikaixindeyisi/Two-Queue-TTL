# 模型画像 — KV cache 复用情况按模型分类

> **创建时间：** 2026-05-11
> **最近修订：** 2026-05-12（Step 1.5 per-user 实测 + chain forest 反向验证）
> **数据来源：** Maas 平台 2026-04-24 9:00–11:00（Text + Agent 请求）
> **上游设计：** [`docs/3step_validation_plan.md`](3step_validation_plan.md)
> **相关单模型深度：** [`docs/dsk8k_step1_findings.md`](dsk8k_step1_findings.md)

本文档汇总 7 个生产模型的离线 trace 分析结果。核心结论：**没有任何模型适合"单一全局静态 pin"，chain 不等于 KV cache 命中率，Step 3 算法不能 one-size-fits-all**。DS-8K 不是普适基线，是 7 种画像之一。

> **2026-05-12 状态**：Step 1.5（per-user 报告 + multi-chain forest）已在全部 7 模型实测完成。每个 §1.X 节末尾追加了"2026-05-12 实测修订"子节，对 2026-05-11 的初版推断做反向验证；推断错的部分用 ❗ 标记，确认的用 ✓ 标记，新增的用 ＋ 标记。三个 cross-cutting 新发现见 §3.5–§3.7。

---

## 0. 实验背景

### 0.1 实验目的

根据 Maas 现网数据分析生产环境中不同模型的 KV cache 复用收益和规律、业务流量模式、显存压力，以及据此如何设计 request 调度、KV block 淘汰、KV cache 量化等策略。

### 0.2 实验数据

- **数据来源：** Maas 平台 2026-04-24 9:00–11:00
- **使用场景：** Text 请求、Agent 请求（显式包含 toolcall / opencode / claude code 等标识）
- **工作负载字段：**
  - `user_id`：区分不同用户
  - `request_id`：真实 request，由原始 request_id 和 turn_index 拼接
  - `timestamp`：相对采样开始时刻的时间偏移（**秒精度**——见 §4.2 已知问题）
  - `raw_prompt`：原始输入
  - `chat_id`：原始数据中的 request_id
  - `turn_index`：代表这是 chat 中的第几轮，第一轮 `turn_index == 0`

### 0.3 指标定义

| 指标 | 含义 |
|---|---|
| **ideal hit rate** | 容量无限情况下 KV cache 命中率 |
| **reuse time** | 一个 KV block 被另一个后续请求复用时，两者之间的时间间隔 |
| **reuse distance** | 2 次复用之间，有多少个不重复的 KV block 被插入了缓存 |
| **Reuse×** 总体复用倍数 | 全局每个 Block 平均被访问多少次（=1 代表没有被复用） |
| **WS (blocks)** | 唯一 Block 总数，体现 cache 容量压力 |
| **Heavy% → Req%** | Top 10% 用户所承载的请求量占全量请求比例 |
| **Heavy gap** | 重度用户相邻两次请求的时间间隔，刻画请求到达密集程度 |
| **Full WS (blocks)** | 全量所有用户产生的唯一 Block 总数，代表要拉满理论 KV cache 命中率，缓存至少需要容纳的最小规模 |
| **Insertion rate (blk/s)** | 每秒新增唯一 Block 流入量，表征缓存写入冲刷压力 |

---

## 1. 七个模型的画像

### 1.1 Qwen-V3.5-27B-64K — 单租户长文档/skills

**核心特征**
- 用户偏斜：4 个用户，Top 10% 用户承载 95.2% 请求
- 理想命中率：81.66%
- 复用偏斜：Reuse× = 5.45x；Heavy× = 8.75x
- cache 压力：Heavy WS = 1,274,380 blocks
- 复用窗口：p80 reuse time = 72s；p95 reuse time = 347s

**业务推断**
单租户长文档或代码审查工作流，一个重度用户持续提交相似的大上下文，可能是同一份长文档、同一段代码仓、同一个长任务上下文、一段 skills。Qwen-64K 的高命中率不是来自很长的 system prompt——当前观察到它的固定 chain 只有 17 blocks，说明高复用主要来自用户内容中的重复长前缀（skills）。在 17 个 block 的 system prompt 走完后，请求被很快分流到不同文档/skills 中，不存在一条可以直接 pin 住的长 system prompt，但在局部范围内存在大量可复用 block。因此 **Qwen-64K 的核心优化对象不是 system prompt，而是长文档上下文/skills**。

**总结**
Qwen-64K 是理论收益最高的模型，但不是最容易落地的模型。它的高复用来自长文档上下文而不是固定 system prompt。这个模型首先要解决容量问题——如果 cache 容量远小于工作集，任何淘汰算法都很难接近 81.7% 的理论上限。

**2026-05-12 实测修订**

Top-3 用户的 chain forest 实测后，"单租户长文档/skills" 框架不够精确——**Qwen-64K 实际是三种用户模式并存**：

| 用户 | 流量 | hit_rate | chains | branch_pos | 几何 / 业务 |
|---|---|---|---|---|---|
| S773（员工助手） | 95.16% | 0.817 | **1** | 0 | 17 block 短 chain；高 hit 来自 user-internal 长文档（与原推断一致）|
| supply.ioc.rock（供应链多模态） | 3.06% | 0.739 | **10** | **51（全部）** | **51 block 共享前缀 + 10 业务分支**；前缀解码为 Claude Code 风格的 JSON tool schema（`read` / `edit` / `path` / ...）|
| chipset2（芯片开发） | 1.63% | 0.923 | **7** | **0（全部）** | root 即分叉成 7 条独立长 chain（1172–1802 block）；每条解码为独立的 Claude Code agent persona（`Agent` / `subagent_type` / `worktree`）|

**＋新发现：两个轻度用户都是 Claude Code-style AI coding assistant**（system prompt 是 JSON tool 定义），不是泛"长文档"。

**＋新发现：supply.ioc.rock 是 pin ROI 最高的单一候选**——51 block 共享前缀 pin 后可覆盖 53.9% 该用户流量，ROI = 0.95 block / % cov（仅次于 S773 的 17 block / 56.7%）。原推断完全没识别出此候选。

**＋发现：chipset2 的 92.3% hit rate 远高于 chain leaf cov 累计（41.9% = 8.39% + 6 × 5.59%）**——差值 50+ 个百分点来自 chain prefix 上的非 leaf 命中。这是 `multi_chain_finder` 当前 leaf-only 输出的语义局限（详见 §3.7）。

**✓ 确认原推断**：主用户 S773 chain 仅 17 block 但 hit_rate 81.7%——chain 完全解释不了命中率，必然来自 user-internal 长文档复用。原推断对该用户精确。

**❗修正**：原推断"Qwen-64K 核心优化对象不是 system prompt"仅对主用户成立；supply / chipset2 的核心 ROI 都在 chain pin（特别是 supply 的 51 block 共享前缀）。Step 3 算法需要**按用户类型差异化**：
- 主用户走 user-LRU + 容量扩张
- supply.ioc.rock pin 51 block 共享前缀（最高 ROI）
- chipset2 LRU 或选择性 pin 1–2 条 chain（看业务价值，长 chain 单条 cov 5.6–8.4%）

---

### 1.2 Qwen-V3-8B-8K — 高并发单租户总结概括任务

**核心特征**
- 用户偏斜：1 个用户
- 理想命中率：18.05%

**业务推断**
只有一个用户 `S0...0773`，平均每分钟接收 250 条请求。单轮请求占 51%，多轮请求占 49%，每个 session 中最多提出 19 轮请求，平均每个 request 提出 1.54 轮请求。单轮请求 80% 的复用发生在 2s 内；多轮请求 80% 的复用发生在 1s 内。system prompt 长度短、覆盖率低。目的是对用户或助手的提问/回答进行语言改写，最终输出 100 字内的总结。

**总结**
Qwen-8B-8K 理想 KV cache 命中率只有 18.05%，这说明哪怕 cache 无限大也只有约 18% 的 block 能够复用——prefix cache 的**业务上限低**。且 Qwen-8B-8K 的主要任务是"总结概括"，prompt 中以用户历史请求/LLM 历史回复为主，也无法通过 prompt 改写提升 KV cache 命中率。

---

### 1.3 Qwen-V3-32B-8K — 高并发多租户批处理流

**核心特征**
- 用户偏斜：37 个用户，119,208 请求，Top 10% 用户承载 80.9% 请求
- 理想命中率：72.25%；Reuse× = 3.60x
- cache 压力：Heavy WS = 364,005 blocks；Full WS = 608,003 blocks
- 复用窗口：p50 reuse time = 0s；p80 reuse time = 3s

**业务推断**
Qwen-8K reuse p50=0s 很可能存在大规模并发——同一秒内发出数十条请求，这些请求携带相同的大前缀（相同 system prompt 或相同文档上下文），然后在末尾附加不同用户问题。但同一 batch 内无法进行 KV cache 复用，**理想 KV cache 命中率可能虚高**——需要进一步实验。

**总结**
Qwen-8K 是业务体量最大的模型。reuse_time=0s 一方面说明存在高并发共享前缀的机会，另一方面也提示我们当前数据精度（timestamp 秒级）还不足。下一阶段的关键是补 batch 级证据，确认这 72.25% 里有多少是真实线上可利用的跨请求复用。

**2026-05-12 实测修订**

| 用户 | 流量 | hit_rate | chains | dom_cov |
|---|---|---|---|---|
| nebula.venussearch | 47.56% | 0.710 | 2 | 24.9% |
| S773 | 21.40% | 0.728 | 2 | 41.4% |
| quality_public_sentiment | 11.97% | 0.753 | 2 | 61.8% |

**＋新发现：三用户 hit_rate 高度一致（0.71–0.75）**，**无复用倒置**（Heavy/Light 比 ≈ 1.06）。这是 7 模型中**最均匀**的画像，与 DS-32K / Qwen-32K 的极端分裂模式形成鲜明对比。

**＋新发现：dom_cov 与流量反向相关**（流量小 → 单条 chain 覆盖率高）——重度用户业务多样、单条 chain 占比低；轻度用户业务单一、单条 chain 主导。

**✓ 确认**：reuse p50=0s 数据精度问题保留，Step 2 实测前无法分辨"真复用" vs "batch 内"。

---

### 1.4 Qwen-V3-32B-32K — 高并发多租户多样内容

**核心特征**
- 用户偏斜：33 个用户，25,919 请求，Top 10% 用户承载 64.3% 请求
- 理想命中率：21.98%
- 复用偏斜：Reuse× = 1.28x；Heavy× = 1.62x；Light× = 2.11x
- cache 压力：Heavy WS = 3,051,718 blocks；Full WS = 4,301,389 blocks

**业务推断**
Qwen-32K 更像企业级文档批量处理或自动化流水线。重度用户请求很多，但每次输入内容不同，出现了**复用倒置**现象。

**关键现象：轻度用户复用高于重度用户** — `Light× = 2.11x  vs Heavy× = 1.62x`。

**总结**
Qwen-32K 理想 KV cache 命中率只有 21.98%——prefix cache 的业务上限低。Qwen-32K 更应该从**多租户隔离 / request 路由**入手；租户中有些用户存在长且稳定的 system prompt（如 `S0...0773`），针对这部分用户做 prefix cache reuse 提升工作有助于降低总体 TTFT。

**2026-05-12 实测修订**

| 用户 | 流量 | hit_rate | chains | unique_blocks | 备注 |
|---|---|---|---|---|---|
| nebula.venussearch | 27.87% | **0.153** | **0** | **2,625,152** | chain forest 为空；2.6M unique（极端多样化）|
| ai.ocr | 24.52% | 0.379 | 4 | 1,071,846 | 中度复用 |
| ...000022 | 11.96% | **0.732** | 1 | 29,852 | **chain 主导业务** |

**＋实测倒置幅度 4.79x**（0.153 vs 0.732），远超原推断的 "Light×=2.11 vs Heavy×=1.62" 比例。

**＋新发现：原推断"重度用户内容不同"得到极端确认**——nebula.venussearch 单 user 内 2.6M unique blocks 且 chain forest 为空（在 0.05/0.05 阈值下找不到任何合格 chain）。**这部分 27.87% 流量几乎 0 cache 价值**，Step 3 应当把它彻底排除在 chain 优化外。

**＋新发现：...000022 是 Qwen-32K 内唯一适合 chain pin 的用户**（hit_rate 0.73 + 单条 chain dom_cov 28.9%）；尽管原推断已提"S773 这类长且稳定 prompt 用户"，但实测让 ROI 排序具体化——纳入 pin 候选的应是 ...000022（11.96% 流量、单 chain）而非按流量排序。

---

### 1.5 GLM-V5.1 — 单租户超长 system prompt

**核心特征**
- 用户偏斜：1 个用户
- 理想命中率：93.89%
- system prompt：chain blocks = 249（约 31,872 tokens）

**业务推断**
GLM-V5.1 拥有超长 system prompt，只有一个用户 `com.huawei.ipd.tianzhou.ai`，只有单轮请求，平均每分钟接收 40 条请求，80% 的复用发生在 58s 内。

**核心问题**
GLM-V5.1 的问题是虽然 KV cache 命中率高，但 system prompt 覆盖率低——**只有 11% 的请求包含这个 system prompt（可能存在多条 system prompt）**，不能通过 pin 住单一 system prompt 来提高 KV cache 理想命中率。

此外随着 branch_threshold 上升，chain_length 出现**断崖式下降**（在 0.15 处从 249 直接掉到 0，而非阶梯式下降）。这个形状说明 trie 上的"主干"是：root 处分裂出多支，每支占比约 15%，每支内部又是一条几乎确定性的 ~249-block 长 chain——所以阈值越过 0.15 就在 root 第一步被卡死，越过则后续 ratio≈1.0 一路畅通。

**这说明 GLM-V5.1 很可能存在多条 system prompt，每条都很长，但单条覆盖率低，覆盖率最高的也仅为 15%。**

**下一步**
筛查所有 system prompt，如果 system prompt 总量 ≤ 10，则可以根据 system prompt 做多条 KV block 淘汰队列，根据请求到达情况来动态淘汰 block。对 GLM-V5.1 来说**最重要的提升仍然是增加 cache 容量**。

---

### 1.6 DeepSeek-V3.1-8K — 固定系统提示 + 短用户输入

**核心特征**
- 用户偏斜：14 个用户，17,312 请求，Top 10% 用户承载 88.5% 请求
- 理想命中率：55.48%
- system prompt：chain blocks = 56；前 56 个 block 在 42% 的请求中以完全相同的顺序出现，可认为是 system prompt
- cache 压力：Heavy WS = 269,025 blocks；Full WS = 309,403 blocks

**业务推断**
DS-8K 很可能是 Agent 或工具调用场景，system prompt 中包含大量工具定义、few-shot 示例、固定约束或 RAG 背景。用户每次输入较短，主要变化在请求末尾。

**总结**
- **收益**：固定前缀长、chain block 数少、cache 压力低，理论上可跳过 87.5% 的固定前缀 prefill 计算，降低 50% 以上的 TTFT 延时（200ms 以上）。实际 TTFT 降幅还需要结合 decode、排队、host overhead 和线上压测量化。
- **风险**：
  - DS-8K 的单条 system prompt 覆盖率低，存在 10 条以上的长 system prompt，无论 pin 住哪条都会导致其他用户的 TTFT 上升，需要针对用户做单独的 KV cache 淘汰队列
  - 存在 prompt 版本漂移风险，system prompt 更新后旧 chain 可能失效，需要监控 chain hash 生命周期；需要将数据采集范围扩大到 24h / 隔天等，观察 system prompt 是否稳定

**2026-05-12 实测修订**

**❗推断撤回："Agent + 工具调用 + few-shot" 业务模式描述错了**。DS-8K 的实际业务是 **multi-task router / 分类器**：system prompt 是中文 "你是 XX 助手，根据 X 判断 Y" 的短 routing template（10–95 block），**不是 Agent 工具定义 JSON schema**（那是 Qwen-64K 轻度用户 supply / chipset2 的画像，详见 §3.5）。

**Top-3 用户的 chain forest 实测**：

| 用户 | 流量 | hit_rate | chains | branch_pos | 业务 |
|---|---|---|---|---|---|
| S773（员工助手） | 88.46% | 0.552 | **3** | 0（全部） | 问题分类器 / 会议纪要 / 多轮问答 router |
| mdata.mdata20180908 | 3.62% | 0.250 | **5** | **13（全部）** | 5 种 JSON → Markdown 模板（RAG 文档生成）|
| ebg.ioc.efc（企业 IOC） | 3.48% | 0.707 | **6** | 0（全部） | 财务多任务 router（意图分类 / 指标识别 / ...）|

**S773 的 3 条 chain decoded 内容**（验证 router 业务模式）：
- chain 0（56 block, cov 46.6%）："你是智能助手 Agent 的【问题分类器】" — 主流 routing entry-point
- chain 1（18 block, cov 9.4%）："你是一个会议纪要总结助手"
- chain 2（10 block, cov 5.1%）："你是一个专注于多轮问答场景的智能助手"

**❗推断撤回："存在 10 条以上长 system prompt"在单 user 内不成立**——S773 只有 3 条短 chain。**Top-3 用户合计 14 条 chain**（3+5+6），分散在不同用户内；预期全 14 用户总和 30+ chain。修订后的表述：**"跨用户共有 30+ 短 chain，但单 user 内 3-6 条"**。

**＋新发现：S773 chain 0 是 DS-8K 整体最高 ROI 的 pin 候选**——56 block pin 拿 46.6% cov（ROI = 1.20 block / % cov）。pin 它等于把员工助手系统的"问题分类"步骤 prefill 免费。

**＋新发现：mdata 几何与 Qwen-64K 的 supply.ioc.rock 同形**（共享 wrapper + 多业务分叉），但业务完全不同——supply 是 Claude Code，mdata 是 RAG 文档生成（详见 §3.5）。

**＋新发现：S773 是跨模型出现的 product_id 但承载不同业务**——DS-8K 上是 router/分类器（chain 56 block）；Qwen-64K 上是长文档/skills（chain 17 block + user-internal 文档）。**product_id 不等于业务模式**——选模型时业务方按容量 / 任务长度切换。

**✓ 确认**：chain 覆盖率 41–46% / Heavy WS 269K 等 trace-level 数字与原推断一致；TTFT 降低 50%+ 的理论收益仍然成立，但 pin 对象从"56 block 共享 system prompt"改为"S773 chain 0（中文 routing prompt）"。

---

### 1.7 DeepSeek-V3.1-32K — 多租户超长 system prompt + 内容多样

**核心特征**
- 用户偏斜：12 个用户，Top 10% 用户承载 60.8% 请求
- 理想命中率：21.89%
- 复用偏斜：Heavy× = 1.24x；Light× = 2.46x
- system prompt：chain blocks = 211（约 27,008 tokens，占 32K 上下文约 82.6%）

**业务推断**
DS-32K 和 DS-8K 表面上都存在长 system prompt，但本质完全不同：DS-8K 更像一个共享 chain；DS-32K 多个租户各自拥有独立的超长 system prompt，且**重度用户的 system prompt 只有 2 block**。

**核心问题**
DS-32K 的问题是长前缀属于不同租户，且每个租户的 chain 都很长。如果所有租户共享同一个全局 LRU cache，不同租户的 system prompt chain 会**互相驱逐**。此外 DS-32K 也存在和 Qwen-32K 同样的**复用倒置**问题——总体理想 KV cache 命中率低，复用主要来自轻度用户，重度用户复用率低，复用价值集中在小流量用户中。

**总结**
DS-32K 内多个租户都有自己的超长前缀（尤其是轻度租户）。全局 LRU 下，211 blocks × N 租户会互相驱逐，所以它的主要方向是**租户隔离和 cache 分区**。

**2026-05-12 实测修订**

| 用户 | 流量 | hit_rate | chains | dom_cov |
|---|---|---|---|---|
| S773 | 60.79% | **0.089** | 1 | 12.3% |
| mdata.mdata20180908 | 14.90% | **0.697** | 4 | 22.1% |
| cloud.ioc.global | 14.65% | **0.041** | 1 | 8.8% |

**＋实测倒置幅度 7.83x**（0.089 vs 0.697），是 7 模型中**最极端的复用倒置**。

**＋新发现：DS-32K 不是简单的"重度 vs 轻度"，是三类分裂**：
- **类 A（低复用重度）**：S773（60.8% 流量、hit 8.9%）
- **类 B（高复用中度）**：mdata（14.9% 流量、hit 69.7%）
- **类 C（低复用中度）**：cloud.ioc.global（14.65% 流量、hit 4.1%！）

cloud.ioc.global 颠覆了"按流量分类"——它和 S773 流量天差地别但同属"低复用"。**类 A 和 类 C 都不适合 chain pin，类 B 是核心 ROI**。

**＋pin ROI 计算（按 §3.1 目标函数）**：
- mdata = 1062 × (1 − 0.697) × ~200 block ≈ **64K block-miss 可挽回**
- S773 = 4332 × (1 − 0.089) × ~2 block ≈ 8K block-miss（即使按原推断的 2 block chain 算，ROI 远低于 mdata）
- cloud.ioc.global = 1044 × (1 − 0.041) × ? ≈ 不值得 pin

**❗修正**：原推断"重度用户 system prompt 只有 2 block" 在实测中没直接验证（实测只暴露 dom_cov + hit_rate，未暴露 chain_length；S773 dom_cov=12.3% + hit_rate=8.9% 暗示其 chain 也短）。**Step 3 应按 hit_rate 排序 pin 候选，不按流量**——mdata 是 DS-32K 内唯一值得 pin 的用户。

---

## 2. 二维分类（综合归纳）

按 **用户偏斜 × chain 形状** 把 7 个模型分组，每组对应不同的 Step 3 算法方向：

|  | 全局 chain 主导<br>（≥ 1 条 cov ≥ 30%） | 多 prompt 并行<br>（无单一主导） | 业务上限低<br>（hit rate ≤ 25%） |
|---|---|---|---|
| **单租户** | — | **GLM-V5.1**（多 prompt 队列 + 加容量）<br>**Qwen-64K**（长文档复用，不靠 chain） | **Qwen-8B-8K**（放弃 prefix cache 方向） |
| **多租户** | **DS-8K**（per-prompt 淘汰队列） | **DS-32K**（租户隔离 + cache 分区）<br>**Qwen-32B-8K**（待 batch 精度补证） | **Qwen-32B-32K**（租户隔离 + request 路由） |

**几个直读结论：**

1. **没有任何模型适合"单一全局静态 pin"**——DS-8K 看起来像，但 10+ 独立 prompt 让全局 pin 反而会驱逐其他 prompt。
2. **Qwen-64K 是异类**：它的高命中率与 chain 无关，**任何基于 chain 的优化都漏掉它的 81.7% ceiling**；它需要的是大容量 LRU + 文档级 reuse 识别。
3. **三个模型不在 chain 优化的 ROI 区间**：Qwen-8B-8K（18%）、Qwen-32B-32K（22%）、可能还有一半的 Qwen-32B-8K（不确定）。Step 3 不应该花大力气在它们上。
4. **GLM / DS-8K / DS-32K** 共同特征：**多条独立长 system prompt 并行**。当前 `verify_chain_path_closure.py` 只走 trie 最重一支，对这种结构系统性低估真实 chain 数量；plan §2.2/1.1 末尾埋的 **multi-chain mode 必须实现**，不再是"可选"。

---

## 3. 关键发现

### 3.1 复用倒置（Heavy× < Light×）

DS-32K（1.24 vs 2.46）和 Qwen-32K（1.62 vs 2.11）都出现。**算法层面的含义**：

- 当前所有 prefix cache 策略默认"重度用户 = 高复用用户"，按 user 维度做 pin 时优先 pin 重度用户的 chain
- 复用倒置数据说明这个默认假设错了——重度用户内容多样化、低复用；轻度用户的 prompt 高度重复
- 但重度用户的"未命中代价 × 请求量"仍然是大头，单纯按 Reuse× 排序 pin 会错把容量给轻度用户
- 正确的目标函数：**`pin 该 chain 后能挽回的 cache miss token 数 = request_count × (1 − hit_rate_per_user) × chain_length`**——不是 Reuse× 也不是 request_count 单独使用

### 3.2 chain ≠ KV cache 命中率

| 模型 | chain 长度 | chain 覆盖率 | 理想命中率 | 命中率主导来源 |
|---|---|---|---|---|
| Qwen-64K | 17 | 53.96% | 81.66% | **长文档/skills**，与 chain 无关 |
| GLM-V5.1 | 249 | 11–15% | 93.89% | **多条独立长 prompt**，单条 cov 低 |
| DS-8K | 56 | 42% | 55.48% | 主流 system prompt + 其他 9+ 条 prompt 各自小 chain |
| DS-32K | 211 | 低 | 21.89% | **轻度用户的独立长 prompt**（复用倒置） |

**chain 长度只是命中率的结构上界**；实际命中率取决于：(a) 用户请求频率与时间分布，(b) cache 容量与淘汰策略，(c) 跨用户共享程度，(d) 用户内 prompt 模板复用（chain 之外的部分）。

### 3.3 plan 几处需要修订

按这份画像，plan 的几个固化假设站不住了：

| plan 位置 | 问题 | 建议修订 |
|---|---|---|
| §2.1 Q1 措辞 | "模型是否存在 strict prefix-path chain" 默认"一条 chain" | 改为"模型存在多少条独立 chain，每条覆盖率多少" |
| §2.2/1.1 multi-chain mode | 标为"可选模式" | 升级为"必选模式"——GLM/DS-8K/DS-32K 都需要 |
| §2.3 决策表 | 只覆盖 4–5 种情形 | 扩展为"按画像 → 算法"，覆盖二维分类的 6 个非空格子 |
| §3.1 Q5 reuse efficiency | 全局指标，不分 heavy/light | 新增 Q6 "重度 vs 轻度用户的复用结构差异" |
| 顶部"DS-8K 实证"那行措辞 | 默认 DS-8K 为基线 | 改为"DS-8K 是 7 种画像之一" |

### 3.4 数据精度限制

**Qwen-32B-8K p50 reuse time = 0s** 暴露了 timestamp 秒精度的硬伤——batch 内复用和 batch 间复用无法区分，72.25% 命中率里可能有大块是 batch 内（生产 vLLM 不会真复用同 batch 的请求 KV）。

**下一步**：
- Step 2 实测时直接拿 vLLM 的 `prefix_cache_hit_tokens` 指标，绕过秒精度
- 或下次采集要求 timestamp 精度到 ms 或 µs

### 3.5 几何同源 ≠ 业务同源（2026-05-12 实测发现）

`multi_chain_finder` 在两个模型上找到了**几何上极相似**的 chain forest 结构，但业务模式完全不同：

| 几何形状 | 模型 A | 模型 B |
|---|---|---|
| 共享前缀 + 多业务分叉 | Qwen-64K **supply.ioc.rock**：51 block 共享 + 10 业务分支 | DS-8K **mdata**：13 block 共享 + 5 业务分支 |
| root 即分叉、多独立 chain | Qwen-64K **chipset2**：7 条独立 chain（root 分叉，每条 1172–1802 block）| DS-8K **S773 / ebg**：3–6 条独立 chain（root 分叉，每条 10–95 block）|

**业务对比**：
- Qwen-64K supply / chipset2 = **Claude Code AI coding assistant**（system prompt 是 JSON tool schema，`read` / `edit` / `Agent` / `subagent_type` / `worktree`）
- DS-8K mdata / S773 / ebg = **中文 routing / 分类 / RAG 生成**（"你是 XX 助手，根据 X 判断 Y"风格）

**教训**：**chain forest 的几何形状是工具产物，不能直接读成业务画像**。Step 3 算法选择不能只看 trie 几何——必须解码 chain 内容、看 system prompt 才能判断业务模式。上次 (2026-05-12 早些) 我把"Qwen-64K 轻度用户 = DS-8K 业务模式"作为推断，实测后撤回——此条作为教训钉死。

### 3.6 block_size=128 切块的 prefix-shadow 现象（2026-05-12 实测发现）

DS-8K S773 的 3 条 chain 都以 `{"model": "DeepSeek-V3.1-Terminus-NoThinking-8K", "stream": true, "messages": [{"role": "system", "content": "你是…` 开头——理论上前 100+ byte 应该完全相同。**但实测 branch_pos=0**（root 即分叉）。

**原因**：block_size=128 byte 切块让 JSON wrapper 与 system prompt 开头同处于 block 0：
- block 0 装下 JSON header **+ system prompt 开头几个字符**（"你是智能助手 Agent" vs "你是一个会议纪要" vs "你是一个专注于多轮问答"）
- block 0 整体内容立刻不同 → `prefix_path_key` 立刻不同 → trie 在 root 就分叉

**这不是 bug，是 KV cache 实际物理行为的反映**。vLLM 生产 cache 也按 block 切，相同的 JSON wrapper 因为与不同 system prompt 同处一个 block 而被算作不同 KV state。换 block_size=32 byte 切块会让前几 block 真正共享，但生产 KV block 也得是同样大小才有 cache reuse。

**结论**：portraits / step1_runbook 提到 "branch_pos=0" 不要直接读成 "完全无共享前缀"——可能只是"共享前缀长度 < block_size"。要看真共享，需要更细粒度的切块（diagnostic-only，不影响 Step 3 决策）。

### 3.7 multi_chain_finder 的 leaf-only 局限（2026-05-12 实测发现）

实测发现 chipset2 (Qwen-64K) 的 hit rate **92.3%** 远高于 chain leaf coverage 累计 **41.9%**（7 条 chain 的 cov 加起来 = 8.39 + 6 × 5.59）。差值 50+ 个百分点来自 **chain prefix 上的非 leaf 命中**：

- 一个请求走 chain 0 的前 1000 block 但没走完 1802 block → 这 1000 block 都算 hit，但它**不算在 chain 0 的 leaf cov（8.39%）里**
- `multi_chain_finder` 当前**只输出 leaf chain**（[`per_user_research_design.md`](per_user_research_design.md) §5.2 的明确决定，避免 chain forest 包含 `[A] / [A,B] / [A,B,C]` 嵌套冗余），把"部分前缀命中"完全隐藏

**Step 3 算法决策需要的实际数据是 prefix coverage，不是 leaf coverage**。要解决，两个候选方案：
1. 给 chain dict 加 `path_coverage_pcts[]` 数组（每个 position 一个 cov 值）
2. 或输出 root → leaf 全路径的 trie 节点 count，让下游自行算累积分布

**这是 `multi_chain_finder` 的 v2 改进项**，目前不阻塞 Step 1.5 / Step 2 推进，但 Step 3 设计阶段必须解决。

**v2 已实施（2026-05-12）**：选择上面的方案 1。`chain_forest.json` 的每条 chain 现在带 `coverage_pcts: list[float]`（chain 上每 position 的覆盖率）+ `max_prefix_coverage_pct`（便利字段，= `coverage_pcts[0]`）。HTML chain card 在 leaf cov 旁边显示 `max_prefix_cov`，chipset2-like 现象（max prefix >> leaf）一眼可见。`coverage_count` / `coverage_pct` 字段语义不变（仍指 leaf），向后兼容。详见 [`per_user_research_design.md` §5.5](per_user_research_design.md)。

---

## 4. Open Questions

1. **multi-chain mode 实现时机**：是先在 1.1/1.2 里加可选参数，还是新建独立工具 `multi_chain_explorer.py`？
2. **复用倒置的算法响应**：Step 3 算法选择 pin 候选时，目标函数是否应包含 `(1 − hit_rate_per_user)` 项？需要 Step 2 实测 per-user hit rate。
3. **Qwen-64K 长文档识别**：chain 算法无法捕捉，是否需要新的"长 prompt 段相似度"分析模块？该模块不是 prefix-trie，是 substring-LCS 或 MinHash。
4. **批 batch 精度问题**：是否对 Qwen-32B-8K 单独重新采集 ms 精度 trace？还是直接走 Step 2 API 实测？
5. **plan 改写的优先级**：上面 §3.3 列的 5 处修订要不要现在做？还是等 Step 2 / multi-chain 实测后再统一改？

---

## 5. 参考资料

- 三步走战略大纲：[`docs/3step_validation_plan.md`](3step_validation_plan.md)
- DS-8K 单模型深度发现：[`docs/dsk8k_step1_findings.md`](dsk8k_step1_findings.md)
- 通用操作 SOP：[`docs/step1_runbook.md`](step1_runbook.md)
- 数据集命名 / CSV 格式：[`data/README.md`](../data/README.md)
