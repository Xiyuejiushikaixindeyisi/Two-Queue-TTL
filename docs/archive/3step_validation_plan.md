# KV Cache 淘汰算法离线分析平台 · 三步走实验大纲

> **本文档为持续维护的实验路线图**。所有阶段实施进展、参数选择、中间结论、决策点变更必须及时回写本文件。
> 上下文一旦丢失，本文档应足以让任何接手者完整理解当前位置和下一步动作。
>
> **当前焦点：** 多模型 funnel 筛选 + 选定场景出 HTML (3 阶段工作流见 [`USAGE.md`](../USAGE.md))
> **创建时间：** 2026-04-30
> **最近更新：** 2026-05-18

---

## 当前状态速览（首屏）

| Step | 状态 | 关键产出 / 阻塞点 |
|------|------|------------------|
| **1.1 全局 chain 检测** | ✅ 完工 | `scripts/verify_chain_path_closure.py` |
| **1.2 per-user chain + 阈值扫描 + HTML 渲染** | ✅ 完工 | `scripts/per_user_chain_analyzer.py` (内置 21 点 sweep) + `scripts/render_chains_html.py` |
| **1.3 跨时间稳定性** | ⏸️ 等数据落地 | 骨架就位 `scripts/chain_stability_analyzer.py`; 需 2 份独立 24h 采样 (任意模型) |
| **1.5 Per-user 深度报告 + chain forest** | ✅ 完工 | `scripts/per_user_report_analyzer.py` + `scripts/render_user_report_html.py` + `scripts/multi_chain_finder.py`; 9 模型 21 user 实测 |
| **1.6 Token-level 编码 (与 vLLM 一致)** | ✅ 完工 | `lib/prompt_encoder.HFTokenEncoder` + `lib/hf_tokenizer.py`; GLM-5 / Qwen3 tokenizer + `kv_meta.json` vendor; HTML 启用 GB/min + 当天 unique GB total (见 [`step1_6_token_level_experiment_plan.md`](step1_6_token_level_experiment_plan.md)) |
| **1.7 Funnel 工具集成 (USAGE.md 3 阶段)** | ✅ 完工 | 阶段 1 `scripts/target_users_hit_rate.py` (跨数据集 auto-top-k + GB/min) + 阶段 2 `scripts/txt_tree_to_csv.py` (txt 直通) + 阶段 3 `scripts/v2_run_pipeline.py` (per-user HTML orchestrator); 完整使用指南 [`USAGE.md`](../USAGE.md) |
| **Step 2 API 测试** | ⏸️ 阻塞 | 1.3 验证 chain 跨日 Jaccard ≥ 0.7 后启动 |
| **Step 3 算法设计** | 🚫 禁止启动 | Step 1.3 + Step 2 全部完成后才允许 |

**关键参数 / 设计决议** (按时间倒序):
- **2026-05-18 (Step 1.7)**: funnel 工具补齐 (`target_users_hit_rate` auto-top-k + GB/min 列, `txt_tree_to_csv` sequential rid, `per_user_report_analyzer` max_prompt_length, `render_user_report_html` token 级文案 + GB/min 表 + unique GB total). 旧"DS-8K 中心化"叙事弃用, 改为多模型 funnel 主线.
- **2026-05-18 (Step 1.6)**: token-level encoder 主线化. 引入 `HFTokenEncoder` (任意 HF tokenizer), vendor GLM-5 (MLA, 89,856 B/tok) + Qwen3 (GQA, 147,456 B/tok), 配套 `kv_meta.json` 启用 GB/min 真实估算. byte_v1 保留作 regression baseline. quantile bucket 从 second 改 minute, 配合 reuse P80 ≈ 60s 更直观.
- **2026-05-12**: branch_threshold default 0.45 → **0.25** (生产数据两次实证 0.45 过严: 边界 case + 7 模型跨数据集 chain 漏识).
- **2026-05-12**: Step 3 算法决策矩阵成型 (5 维 × 4 算法, 21 用户分类), 见 [`step3_algorithm_decision_matrix.md`](step3_algorithm_decision_matrix.md).
- **2026-04-30**: branch_threshold default 0.95 → 0.45.

**完整进度日志:** §7.
**历史 spec / 时间快照:** [`archive/`](archive/) (DS-8K findings / model_portraits / 各 HTML redesign spec / step1_runbook 等).

---

## 0. 平台定位与设计原则

本项目是一个 **KV cache 淘汰算法的离线分析与算法验证平台**。

三步走战略中：
- **Step 1（实验验证）和 Step 2（API 测试）必须沉淀为可复用的通用分析模块**，能够对接任意模型的 trace 与任意模型实例的 API
- **Step 3（算法设计）允许针对具体模型定制**，但消费的输入与产出的输出应符合 Step 1/2 定义的契约

当前以"多模型 funnel 筛选 → 选定场景出 HTML"为工作流；任何具体模型 (GLM-V5 / Qwen-V3 / DS-V3 等) 都是应用对象，不是平台设计目标。

### 0.1 关键技术约束

- **离线运行**: 分析平台必须能在断网环境下运行 (数据私有、机器不联网). 禁止任何运行时下载 / 在线模型加载. tokenizer + kv_meta.json 通过 **git vendor 到 `models/<name>_tokenizer/`** 同步, 仍 air-gapped 友好 (`git pull` 即用).
- **编码器双轨制**:
  - `byte_v1` (regression baseline): utf8 字节切片 (128 B/block) + SHA-256 链, 不依赖 tokenizer. **仅作回归测试**, hit_rate 比 vLLM 真实数字系统性偏高 0-30pp.
  - `hf_token_v1` (主线, 与 vLLM 一致): HF AutoTokenizer + chat_template + 128-token 切片 + SHA-256 chain fallback hash. 配套 `kv_meta.json` 启用真实 GB/min KV cache 压力估算. 当前 vendor: GLM-5 (MLA, 89,856 B/tok) + Qwen3 (GQA, 147,456 B/tok).
- **Raw CSV 标准格式** (用户保证所有采样数据集遵循):
  | 列序 | 列名 | 含义 |
  |------|------|------|
  | 1 | `request_id` | 请求 ID (唯一) |
  | 2 | `user_id` | 租户 ID (实为 product_id, 每模型 ≤ 37 个) |
  | 3 | `raw_prompt` | 原始 prompt 文本 |
  | 4 | `timestamp` | 请求到达时间戳 (秒级精度); 缺失时 hit_rate/chain/LCP 正常, spike/rpm/GB-min 时序失效但 caveat 提示 |

  列名支持中文别名 (`请求ID` / `租户ID` / `请求参数`) + UTF-8 BOM, 由 `lib/` 自动适配.
- **txt 数据集 (无 timestamp 的同事散文件)**: 通过 `scripts/txt_tree_to_csv.py` 转 4 列 CSV (sequential rid + fixed user_id), 之后走标准 pipeline; 详见 [`USAGE.md`](../USAGE.md) 阶段 2.
- **Block 切分单位**: 默认 `block_size=128` (单位由 encoder 决定: byte_v1 是字节, hf_token_v1 是 tokens).

---

## 1. 三步走战略总览

```
[Step 1 实验验证]──→ 用 trace 数据验证 chain 假设、稳定性、用户分布
        ↓ 决策点
[Step 2 API 测试]──→ 用真实 API 测量缓存容量、真实命中率、生命周期
        ↓ 决策点
[Step 3 算法设计]──→ 离线策略设计、模拟器迭代、回到生产部署
```

每一步的产出为下一步的硬输入；每一步结尾有明确的决策点，决定是否进入下一步、终止或回退。

---

## 2. Step 1：实验验证（基于 Trace 的离线分析）

### 2.1 必答问题（Step 1 完成定义）

完成 Step 1 必须能回答下列三个问题：

| # | 必答问题 | 输出 artifact |
|---|---------|---------------|
| Q1 | 模型是否存在 strict prefix-path chain？长度多长？内容（LCP）是什么？ | `chain_summary.json` |
| Q2 | 模型中每个用户的 prefix-path chain 分别是什么？ | `per_user_chains.json` |
| Q3 | 每个用户的 chain 是否跨 2h / 24h / 隔天稳定？ | `chain_stability_report.json` |

### 2.2 子步骤

#### 1.1 全局 Strict path-closed chain detection（LCP 验证）

- **目标：** 严格验证 chain 是真实路径还是统计假象，并解码 chain 内容供业务方使用
- **算法：** Trie + 双阈值
  - 构建：每条请求按 `prefix_path_keys` 序列插入 trie，节点 count++
  - 查询：从 root 沿最热子节点前进，满足两个阈值才继续：
    - `branch_threshold`（默认 **0.25**，2026-05-12 修订；上一版 0.45 见下方阈值选择指南）：`max_child.count / parent.count ≥ X`
    - `coverage_threshold`（默认 **0.05**）：`node.count / total_requests ≥ X`
  - **阈值选择指南（基于 DS-8K 实测结论，详见 `dsk8k_step1_findings.md`）：**
    - `0.95`：严格闭合，几乎不会 false positive，但生产 prompt 难满足（含时间戳/任务混杂会导致 chain=0）
    - `0.25`（**当前推荐 default，2026-05-12 修订**）：识别"主流但非碾压"chain（分叉 ratio 在 0.15–0.45 间），覆盖 GLM / Qwen-32B-8K / Qwen-32K 等模型的中等强度主流路径
    - `0.45`（历史 default，2026-04-30–05-12）：识别"主流量主路径 + 容许少数派分支"；7 模型跨数据集实证表明系统性漏识 chain，仅保留作旧实验对比用
    - `0.30`：探索性，可能包含次要路径
  - 复杂度：O(total_blocks) 构建，O(chain_length) 查询
  - 性能预估：DS-8K 1M 操作 / 秒级；Agent 128K 上下文 × 10K 请求 = 10M 操作 / ~10 秒，均可接受
- **内容解码（不依赖 LLM tokenizer）：**
  - chain 找到后，反查走完整链的 request_id（trace 与 raw 通过 request_id 字段精确匹配）
  - 取任一 request_id，从 `data/<model>/raw/*.csv` 加载 raw_prompt 字段
  - 按 `block_size=128` 字节切片 → 每个 chain block 对应一个字节段
  - utf8 decode（`errors='replace'`）写入 `lcp_content.decoded_text`
- **待补模块：** `scripts/verify_chain_path_closure.py`
- **数据流：一步到位（不依赖 convert_raw_trace.py）**
  - 直接消费 raw CSV，内部完成切块 + 哈希链构建 + trie 分析
  - 不产出中间 trace CSV，简化 Step 1 pipeline
  - Step 2/3 如需 trace CSV，独立调用 `convert_raw_trace.py`
- **复用性：** 对任意模型 raw CSV 直接套用，输入格式严格遵循 §0.1 的 4 列标准
- **输入：** raw CSV（单文件或包含多 CSV 的目录）
- **输出 (`chain_summary.json`)：**
  - `lcp_blocks`: 真实 path-closed 链长度
  - `lcp_coverage`: 该链对应的请求覆盖率
  - `lcp_content`: 每个 chain block 的 prefix_path_key + count + 解码文本
  - `branch_points`: 路径分叉位置 + 各分支频率
- **可选模式（multi-chain）：** 在分叉点递归探索，输出多条并行 chain（覆盖多租户 / 多 prompt 场景）

#### 1.2.0 Threshold sweep（推荐先跑，辅助找阈值）

- **目标：** 通过扫描 `branch_threshold ∈ [0, 1]` 步长 0.05 共 21 个点，画图找到合适的阈值
- **算法：** Trie 只构建一次（与 1.2 一致），21 个阈值复用同一棵树跑 LCP，开销几乎为 0
- **可视化：**
  - X 轴：branch_threshold
  - Y 轴：chain length（每个用户独立一条线 + global 一条特粗黑线）
  - 头部用户（request share ≥ 20%）：三角形 + 粗线
  - 其余用户：圆点 + 细线 + 半透明
  - 推荐 default 阈值（**0.25** since 2026-05-12；旧 default 0.45）画一条红色虚线作为参考
- **待补模块：** `scripts/chain_threshold_sweep.py`
- **输入：** raw CSV（同 1.1）
- **输出：** PNG + CSV（同基名同目录）
- **典型用法：**
  ```bash
  python scripts/chain_threshold_sweep.py \
      --raw-csv data/<model>/raw \
      --output  outputs/<model>/threshold_sweep.png
  # 看图选定阈值后，跑下一步：
  python scripts/per_user_chain_analyzer.py \
      --raw-csv data/<model>/raw \
      --branch-threshold <选定阈值> \
      --output  outputs/<model>/per_user_chains.json
  ```

#### 1.2 Per-user prefix-path chain detection

- **术语澄清：** 此处 user_id 实际为 **product_id**（每个模型最多 ~37 个产品），不是终端用户
- **目标：** 解决"全局 chain 是否被某个 heavy user 假装为通用模式"的问题
- **方法：** 对每个 user_id 独立构建 trie，跑 1.1 的算法
- **覆盖范围：** 所有 user_id（包括低请求量），无 chain 的 user 输出空 chain（合法结果，反映该产品无固定 system prompt）
- **性能：** ≤37 个 user × 单 user 子集 ≤ DS-8K 全集，远低于 Agent 单 trace 压力，无须优化
- **待补模块：** `scripts/per_user_chain_analyzer.py`
- **输入：** trace CSV + raw CSV
- **输出 (`per_user_chains.json`)：** 每个用户一项
  - `user_id`
  - `request_count`
  - `chain_length`（0 表示无 chain）
  - `chain_coverage`（该用户内部覆盖率）
  - `chain_content`：解码文本（与 1.1 同格式）
  - `matches_global_chain`（与全局 LCP 是否一致）

#### 1.3 跨时间窗口稳定性分析

- **目标：** 量化 chain 在不同时间尺度下的漂移
- **输入数据（用户提供，2026-05-08 命名锁定）：**
  - **`dsk8k_24h_0506`**：5.6 全天（24h 窗口）随机采样
  - **`dsk8k_24h_0507`**：5.7 全天（24h 窗口）随机采样
  - 替代原计划的 `dsk8k_2h_5k / 24h_10k / 2d_10k` 三份；两份独立 24h 采样可直接做跨日 Jaccard，比从一份 2d 数据切分更直接
- **方法：** 两份数据集分别跑 1.1 + 1.2，得到各自的全局 LCP 和 per-user chains，再两两对比
- **模块：** `scripts/chain_stability_analyzer.py`（骨架就位 2026-05-07；输入为 1.2 JSON artifact，与具体模型解耦）
- **输出 (`chain_stability_report.json`)：**
  - 每两个样本间的 chain top-N Jaccard 相似度矩阵
  - 每个用户 chain 的跨日漂移率
  - **稳定性等级判定：**
    | 7 天 Jaccard 平均 | 判定 |
    |-------------------|------|
    | ≥ 0.95 | 高度稳定（静态 pin 可行） |
    | 0.7–0.95 | 慢漂移（静态 pin + 周级更新） |
    | 0.4–0.7 | 中漂移（必须动态准入/退出） |
    | < 0.4 | 不稳定（chain 优化方向不可行） |

#### 1.4 Step 1 总览报告

- **待补模块：** `scripts/step1_summary.py`
- **输入：** 1.1–1.3 的所有输出
- **输出：** Markdown 格式报告 + Step 2 进入决策

### 2.3 决策点（Step 1 → Step 2）

| 情形 | 决策 |
|------|------|
| 全局/用户 chain 存在且跨日稳定（≥0.7 Jaccard） | ✅ 进入 Step 2 |
| chain 存在但中漂移（0.4–0.7） | ⚠️ 进入 Step 2，但 Step 3 必须设计动态准入/退出 |
| chain 不稳定（<0.4） | ❌ DS-8K 优化方向不可行；Step 3 改为通用 LRU 优化 |
| 全局 chain 仅由 1 个 heavy user 贡献 | ⚠️ 标记为"单租户优化"，业务沟通后再进入 Step 2 |

---

## 3. Step 2：API 测试（生产环境真实测量）

### 3.1 必答问题（Step 2 完成定义）

| # | 必答问题 | 输出 artifact |
|---|---------|---------------|
| Q1 | 每个模型实例的 KV cache 容量？ | `cache_capacity_estimate.json` |
| Q2 | 线上真实 KV cache 命中率？ | `online_hit_rate.json` |
| Q3 | 所有 block 的真实生命周期 CDF？ | `block_lifecycle_all.csv` + 分布图 |
| Q4 | 真实被重用的 block 的生命周期 CDF？ | `block_lifecycle_reused.csv` + 分布图 |
| Q5 | 理论可重用的 block 是否全部被重用？ | `reuse_efficiency.json` |

### 3.2 子步骤

#### 2.1 模型实例部署 + API 接入（外部依赖）

完成后需获取：API endpoint、并发限额、是否暴露 prefix cache 监控指标。

#### 2.2 Cache 容量探测

- **目标：** 通过实验性手段反推该实例的 KV cache block 容量
- **方法：**
  - 准备 N 个互不相关的请求填满 cache
  - 重发原始第 1 个请求，观察是否命中
  - 二分搜索 N，找到刚好被淘汰的临界点
- **待补模块：** `scripts/probe_cache_capacity.py`
- **输出 (`cache_capacity_estimate.json`)：**
  - `estimated_capacity_blocks`
  - `confidence_interval`
  - `probe_method_log`
- **复用性：** 对任意模型 API 直接套用，无需修改

#### 2.3 生产 trace replay + 真实 hit rate

- **目标：** 用生产 trace 重放 API，测量真实 prefix cache 命中行为
- **待补模块：** `scripts/replay_to_api.py`
- **关键设计：**
  - 按原 timestamp 调度（或按比例缩放）
  - 异步并发以保持原始流量形状
  - 采集每请求的 TTFT
  - 如果 API 暴露 `cached_tokens`/`cache_hit` 指标，直接采集；否则通过 TTFT vs prefill 长度回归推断
- **输出 (`online_hit_rate.json`)：**
  - 全局 hit rate
  - 每用户 hit rate
  - 时间窗口 hit rate（每 5 分钟一段）
  - 每请求 hit/miss 明细 CSV

#### 2.4 Block 生命周期分析

- **目标：** 离线计算每个 block 在 cache 中存活了多久（首次进入 → 被淘汰）
- **方法：** 用 2.2 探测到的容量驱动 LRU 模拟器，重放 trace，记录每个 block 的进入和淘汰 timestamp
- **待补模块：** `scripts/block_lifecycle_analyzer.py`
- **输出：**
  - `block_lifecycle_all.csv`：所有 block 的 (block_key, enter_t, evict_t, lifetime, was_reused)
  - 全部 block 生命周期 CDF 图
  - 被重用 block 子集的生命周期 CDF 图（这部分子集对优化更有意义）

#### 2.5 Reuse 效率分析

- **目标：** 找出"理论上可重用但被淘汰前未重用"的 block——这是 LRU 损失最大的部分
- **待补模块：** `scripts/reuse_efficiency_analyzer.py`
- **方法：**
  - 理论可重用次数 = 该 block 在 trace 中出现的总次数 - 1
  - 实际重用次数 = 在 cache 容量约束下的实际命中次数
  - 失败重用（即未重用的可重用 block） = 理论 - 实际
- **输出 (`reuse_efficiency.json`)：**
  - 全局重用效率比
  - 失败重用的 block 列表（按损失次数排序）
  - 失败原因分类：cache 容量挤压 / 淘汰过早 / 跨用户竞争

### 3.3 决策点（Step 2 → Step 3）

| 情形 | 决策 |
|------|------|
| 真实 hit rate 与 sim 预测吻合（差距 < 5pp） | ✅ 进入 Step 3，sim 中迭代算法可信 |
| 真实 hit rate 远低于 sim 预测 | ⚠️ 先修正 sim 假设（如 batch sharing 模型），再进入 Step 3 |
| 重用失败率极高（< 30% 实际重用率） | Step 3 直接针对该根因设计 |
| Cache 容量远小于工作集（< 10%） | Step 3 改为容量规划建议，淘汰算法收益有限 |

---

## 4. Step 3：算法设计（DS-8K 焦点，待 Step 1/2 完成后细化）

### 4.1 候选优化方向（待 Step 1/2 输出后筛选）

- **A：Pinned-LRU**（静态 pin chain blocks）
- **B：动态 chain 检测 + 滑动 pin**（应对 chain 漂移）
- **C：Tenant-aware 缓存分区**（如果 per-user chain 差异大）
- **D：基础 LRU + 容量扩容**（如果 chain 不稳定，淘汰算法收益有限）

### 4.2 子步骤（占位，Step 1/2 完成后详化）

- 3.1 候选策略实现
- 3.2 模拟器中的 capacity sweep
- 3.3 路径深度敏感性扫描
- 3.4 vs 现有 LRU/two_queue_ttl baseline 对比
- 3.5 生产部署可行性评估

---

## 5. 模块复用性矩阵

| 模块 | Step | 跨模型复用 | 说明 |
|------|------|-----------|------|
| `lib/prompt_encoder.py` (`HFTokenEncoder` / `ByteLevelEncoder` / `build_encoder_from_args`) | 1.6 | ✓ | encoder 抽象, 任何 HF tokenizer + byte 双轨 |
| `lib/hf_tokenizer.py` (`load_tokenizer` / `apply_template` / `load_kv_meta`) | 1.6 | ✓ | AutoTokenizer + chat_template + kv_meta.json |
| `scripts/target_users_hit_rate.py` (阶段 1) | 1.7 | ✓ | 跨数据集 auto-top-k + GB/min long 表 |
| `scripts/txt_tree_to_csv.py` (阶段 2) | 1.7 | ✓ | txt 树 → 4 列 CSV, sequential rid + max prompt 统计 |
| `scripts/v2_run_pipeline.py` (阶段 2+3 orchestrator) | 1.5 + 1.6 | ✓ | per_user_report_analyzer + render_user_report_html 两遍 |
| `scripts/per_user_report_analyzer.py` (内部) | 1.5 + 1.6 | ✓ | 单数据集 → user_summary.json + per-user user_report.json |
| `scripts/render_user_report_html.py` (内部) | 1.5 + 1.6 | ✓ | per-user user_report.html (7 节) + cross_user_summary.html |
| `scripts/per_user_chain_analyzer.py` (阶段 3 model-level) | 1.2 | ✓ | model-level chain + 内置 21 点 threshold sweep (`--no-threshold-sweep` 关闭) |
| `scripts/render_chains_html.py` (阶段 3 model-level) | 1.2 | ✓ | per_user_chains.html (含 threshold sweep 图) |
| `scripts/multi_chain_finder.py` | 1.5 | ✓ | chain forest primitive |
| `scripts/verify_chain_path_closure.py` | 1.1 | ✓ | LCP 验证 (1.2 替代后较少单独使用) |
| `scripts/chain_threshold_sweep.py` | 1.2.0 | ✓ | standalone sweep (已被 chain_analyzer 内置, 保留向后兼容) |
| `scripts/chain_stability_analyzer.py` | 1.3 | ✓ | 跨时间稳定性 (等数据) |
| `scripts/probe_cache_capacity.py` | 2 | ✓ | (Step 2 待启动) |
| `scripts/replay_to_api.py` | 2 | ✓ | (Step 2 待启动, 需各家 API 适配层) |
| `scripts/block_lifecycle_analyzer.py` | 2 + 3 | ✓ | (Step 2 待启动) |
| `scripts/reuse_efficiency_analyzer.py` | 2 + 3 | ✓ | (Step 2 待启动) |

所有 Step 1/2 模块设计为通用工具: 输入 trace CSV / API 配置 + 必要参数即可对任意模型使用. Step 3 模块允许针对具体模型定制.

---

## 6. Trace 数据采集

通用约定: 所有 trace CSV 放 `data/<model_dir>/raw/*.csv`, 多个 csv 视为同一数据集 (字典序拼接). 详见 [`data/README.md`](../data/README.md).

| 类型 | 来源 | 用途 |
|---|---|---|
| 生产 trace (CSV) | 各模型生产采样 (4 列含 timestamp) | 阶段 1 跨数据集筛选 + 阶段 3 完整 HTML 分析 |
| txt 散文件 (无 timestamp) | 同事手工导出 (1 txt = 1 请求, 文件名任意含中文) | 阶段 2 直接进 HTML, 走 `txt_tree_to_csv.py` 转 CSV |
| 跨时间双采样 (Step 1.3 阻塞) | 任意模型 2 份独立 24h 采样 | 跨日 Jaccard 稳定性验证 |

历史已有数据集列表见 [`archive/`](archive/) 中的快照文档 (DS-8K / Qwen-V3.5 / GLM-V5.1 等).

---

## 7. 进度追踪

| 日期 | 步骤 | 状态 | 备注 |
|------|------|------|------|
| 2026-04-30 | 文档初版 | ✅ | 三步走战略大纲建立 |
| 2026-04-30 | Step 1 子步骤设计 | ✅ | Q1.1–Q1.4 全部对齐：双阈值 trie 算法 + 全 user 覆盖 + 三采样数据集 + token 解码 |
| 2026-04-30 | 技术约束确定 | ✅ | 离线运行 / 不依赖 LLM tokenizer / Raw CSV 4 列标准格式 |
| 2026-04-30 | Step 1 数据集到位预期 | ⏳ | 当晚提供 dsk8k_2h_5k / 24h_10k / 2d_10k |
| 2026-04-30 | Step 1.1 数据流决策 | ✅ | 一步到位（raw CSV → trie），不依赖 convert_raw_trace.py |
| 2026-04-30 | Step 1.1 算法骨架 | ✅ | `scripts/verify_chain_path_closure.py` 已写完 |
| 2026-04-30 | Step 1.2 算法骨架 | ✅ | `scripts/per_user_chain_analyzer.py` 已写完，复用 1.1 的 trie/decode 模块 |
| 2026-04-30 | Step 1.1 + 1.2 生产数据验证 | ✅ | DS-8K 17K trace 跑通，发现 `dsk8k_step1_findings.md` 详记 |
| 2026-04-30 | Step 1.1 阈值 default 修订 | ✅ | branch_threshold 0.95 → 0.45（DS-8K 实测结论） |
| 2026-04-30 | Step 1.2.0 阈值扫描可视化 | ✅ | `scripts/chain_threshold_sweep.py`：21 点扫描 + per-user 折线图，trie 单次构建 |
| 2026-04-30 | Step 1.2 报告 HTML 渲染 | ✅ | `scripts/render_chains_html.py`：JSON → 静态 HTML，5 模块（params/stats/aggregate/global/per-user），合成 lcp_content 显示 |
| 2026-04-30 | **Step 1.1 + 1.2 正式完工** | ✅ | 含 1.1 / 1.2 / 1.2.0 阈值扫描 / HTML 渲染四个模块；DS-8K 实测验证完成 |
| 2026-04-30 | Step 1.3 启动条件 | ⏸️ | 等待 dsk8k_2h_5k / 24h_10k / 2d_10k 三份采样到位 |
| 2026-04-30 | Step 1.3 数据采集 | ⏳ | 待 dsk8k_2h_5k / 24h_10k / 2d_10k 到位 |
| 2026-04-30 | Step 1.3 算法 + 验证 | ⏳ | 待数据到位后启动 |
| 2026-05-07 | Step 1.3 算法骨架 | ✅ | `scripts/chain_stability_analyzer.py` 写完；输入为 1.2 JSON artifact，与具体模型解耦 |
| 2026-05-08 | Step 1.3 数据集命名变更 | ✅ | 用户改用 `dsk8k_24h_0506` + `dsk8k_24h_0507` 两份独立 24h 采样替代原 `2h_5k / 24h_10k / 2d_10k` 三份方案；2h / 中窗口槽位放弃 |
| 2026-05-08 | Step 1.3 数据落地 | ⏳ | 等 5.6 / 5.7 两份 CSV 拷入 `data/dsk8k_24h_0506/raw/` 与 `data/dsk8k_24h_0507/raw/` |
| 2026-05-08 | Step 1.3 接口契约最终确定 | ✅ | 短暂尝试过 raw-csv 输入 + CLI threshold（commit b8e8e95）后 revert。最终契约：1.3 只接受 ≥2 份 1.2 JSON 作为输入，threshold 由用户在 1.2 阶段统一保证，1.3 用 params consistency guard 校验一致性后做对比。理由：1.3 不应重做 trie，违背 step 单一职责（1.2 = chain 提取，1.3 = chain 比较） |
| 2026-05-11 | 7 模型画像 + Step 1.5 设计 | ✅ | `model_portraits.md` + `per_user_research_design.md` 沉淀；D1–D8 八个决策点全部 ack；multi-chain 阈值与单 chain 解耦（mc-* namespace，default 0.05/0.05） |
| 2026-05-11 | Step 1.5 三个模块编码完成 | ✅ | `multi_chain_finder.py`（primitive） + `per_user_report_analyzer.py`（编排器） + `render_user_report_html.py`（SVG inline HTML 渲染）；本地合成 trace 上 end-to-end smoke test 通过；待生产数据集验证 |
| 2026-05-12 | Step 1.5 七模型生产数据反向验证 | ✅ | 全部 7 模型跑通 per-user 报告 + chain forest；GLM-V5.1 推断完美命中（7 chain / dom_cov 14.7%）；DS-8K 业务推断从"Agent+工具"撤回为"中文 routing/分类器"；Qwen-64K 三用户模式拆分（主用户长文档、supply 51-block 共享 Claude Code、chipset2 root 分叉）；新发现"几何同源 ≠ 业务同源" / block_size shadow / multi_chain_finder leaf-only 局限 |
| 2026-05-12 | portraits.md 反向验证修订 | ✅ | 每个 §1.X 加"2026-05-12 实测修订"子节（保留原推断作为历史，新增 ❗✓＋ 三类标记）；新增 §3.5–§3.7 三条 cross-cutting 发现 |
| 2026-05-12 | v3 prefix-shadow 自动检测尝试 + 撤回 | ❌→✅ | 曾给 `per_user_report_analyzer.py` 加 byte-level LCP + union-find shadow grouping；生产数据上 false positive（JSON wrapper 共享误报）+ false negative（业务 shadow 漏报）双向失败。完整撤回代码 + 文档；钉死教训：shadow detection 是语义层任务，违反"不依赖 tokenizer"约束，**必须人工标注**。runbook §6 改为人工 SOP；v2 prefix coverage 保留（无歧义） |
| 2026-05-12 | branch_threshold default 第二次修订 0.45 → 0.25 | ✅ | 生产数据两次实证 0.45 过严：DS-8K 5.6 边界 case（5.6 chain=0、5.7 chain=56）+ 7 模型跨数据集发现 GLM (root ratio 0.15) / Qwen-32B-8K / Qwen-32K 等模型上 chain 分叉 ratio 集中在 0.15–0.45 被系统性漏识。改 default 为 0.25 同时保留 0.45 作为历史对比值；portraits §3.8 钉死实测依据 |
| 2026-05-12 | Step 3 算法决策矩阵成型 | ✅ | 新建 `step3_algorithm_decision_matrix.md`：5 维评估（业务类型 / cache 压力 / 模型参数 / 请求量 / 命中率）× 4 算法（A 路由 / B 淘汰 / C 池化 / D prompt 修改），21 用户全部分类。新发现：D 主导用户占 ~30% 流量、C 池化优先级被低估、Qwen-32B-8K ags 隐藏 Top-4 候选 hit 0.80。portraits §2 二维分类简化为指针，决策入口移到本文档 |
| 2026-05-12 | Step 3 推荐自动化（HTML §6） | ✅ | `per_user_report_analyzer.py` 加 `compute_step3_recommendation`，每 user 自动输出主菜 + 辅菜 + 业务类型（启发式）+ 难度 + 提升估计 + 实施步骤。`user_report.json` 顶层加 `step3_recommendation`；`user_summary.csv` 加 3 列 (`rec_primary` / `rec_companion` / `rec_difficulty`)；HTML §6 渲染推荐板块（A/B/C/D 四色区分）。3 user profile smoke test 全部通过 |
| 2026-05-12 | Step 3 决策规则修订（A 路由优先级提升） | ✅ | 用户反馈"复用倒置 + 多租户必走 A 路由"——之前 A 只看高 QPS 触发，漏掉了 cache 隔离这一核心场景。新增 `compute_model_context`：跨用户聚合 hit_rate 检测复用倒置；A 优先级提到决策树顶（早于 D），实现"多租户 + 倒置 → A 主菜（含低 hit user 走 A+D，路由隔离保护其他用户）"。decision_matrix §3 同步修订规则表。3 场景 smoke test 通过：复用倒置 → A+B/A+D；单租户 chain → B；多租户均衡 → B+A |
| 2026-05-15 | Step 1.6 启动 + GLM-5 调研 | ✅ | `docs/step1_6_token_level_experiment_plan.md` 立项: byte_v1 hit_rate 系统性偏高 0-30pp, 引入 token-level encoder. P1 调研 GLM-5 tokenizer (vocab 154820, wrap_user 5 token overhead). |
| 2026-05-15 | Step 1.6 实现 (`HFTokenEncoder` + tokenizer vendor) | ✅ | `lib/prompt_encoder.HFTokenEncoder` (通用) + `GLM5TokenEncoder` (alias); GLM-5 / Qwen3 tokenizer vendor 到 `models/<name>_tokenizer/`; CLI `--encoder hf_token --tokenizer-path ... --chat-mode wrap_user`; 19 单元测试通过. |
| 2026-05-15 | txt_tree_to_csv + flat 布局支持 | ✅ | 同事数据集 (txt 散文件) 转 4 列 CSV; flat/nested 双布局自动检测; 默认 sequential rid (中文文件名不污染); 输出 max/avg prompt 长度统计. |
| 2026-05-18 | Step 1.6 HTML 反馈整合 | ✅ | (1) "字节级上界"文案条件化 (token 模式改 "vllm 一致"); (2) cache 压力 block/s → block/min bucket (空 minute 比空 second 少, quantile 不再被 padding 拉平), 配合 reuse P80 ≈ 60s; (3) 引入 GB/min: vendor `models/<name>_tokenizer/kv_meta.json` (GLM-5 MLA 89,856 B/tok, Qwen3 GQA 147,456 B/tok), HTML §5 quantile 表追加 GB/min 行 + §1 panel 加 GB/min p80 + §3 user 卡片自动算当天 unique GB total. |
| 2026-05-18 | Step 1.7 funnel 工具补齐 (Gap 1-4) | ✅ | `target_users_hit_rate.py`: auto-top-k 模式 (默认 4) + GB/min 列 + per-user duration; `txt_tree_to_csv.py`: --request-id-mode sequential 默认; `per_user_report_analyzer.py`: max_prompt_chars/bytes/avg → user_report.stats; `render_user_report_html.py` §3 加 max prompt length 卡片. 完整使用指南落 `USAGE.md` (3 阶段 + 3 案例). |
| 2026-05-18 | 文档整理 | ✅ | 删除 8 篇仿真器旧路线 doc (PHASE1-4, round2, two_queue_ttl_plan, kv_cache_eviction_design, DEVELOPMENT_REQUIREMENTS); 归档 9 篇已实施 spec / 时间快照到 `docs/archive/`; README 重写指向 USAGE; metrics_glossary 重写以 token 为主. |

---

## 8. 待讨论事项（Open Questions）

### Step 1 相关（已对齐 2026-04-30）
- **Q1.1：✅ 已决定** 使用双阈值机制：`branch_threshold=0.95` + `coverage_threshold=0.05`，主子节点占比与统计代表性独立判定。Trie 算法保证性能可扩展到 Agent 128K 场景
- **Q1.2：✅ 已决定** 全部 user_id 纳入分析（≤37 个），无 chain 用户输出空 chain 作为合法结果。术语澄清：user_id 实际为 product_id
- **Q1.3：✅ 已决定（2026-05-08 修订）** 用户改为提供两个独立 24h 采样数据集（`dsk8k_24h_0506` + `dsk8k_24h_0507`）替代原 2h_5k / 24h_10k / 2d_10k 三份方案；跨日稳定性由两份直接两两 Jaccard 即可量化
- **Q1.4：✅ 已决定** 解码回原始 token + 文本，便于业务方做 prompt 模板化与算法设计

### Step 2 相关
- **Q2.1：** API 是否暴露 `cached_tokens` 字段？决定 hit rate 测量方式
- **Q2.2：** Cache 容量探测时，并发限额是否够用？
- **Q2.3：** trace replay 是否能严格按原 timestamp 重放，还是必须按 rate limit 节流？

### Step 3 相关
- **Q3.1：** Pinned 策略是否需要支持运行时 reload registry？
- **Q3.2：** 是否需要在 sim 中模拟"batch sharing"语义（同时间戳请求共享 prefill）？

---

**本文档应在每个子步骤完成后立即更新对应章节、进度表和决策点。任何偏离原计划的实施都必须在本文档留下记录。**
