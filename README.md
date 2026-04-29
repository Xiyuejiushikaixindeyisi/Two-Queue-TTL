# KV Cache Eviction 离线仿真平台

> **目标**：通过离线 trace 回放，公平对比 LRU / TTL-LRU / Two-Queue TTL 等淘汰策略对 prefix cache 命中率的影响，为向 vLLM 提交创新性 KV cache 淘汰算法 PR 提供实验依据。

---

## ⚠️ Phase 1 局限性声明

本平台为**离线仿真**工具，与 vLLM runtime 存在以下已知差异：

| 维度 | 本平台 (Phase 1) | vLLM runtime |
|------|-----------------|--------------|
| 哈希函数 | SHA-256（固定 seed，可重现） | SipHash（含随机 seed） |
| 并发模型 | 单线程串行回放 | 多请求并发，真实 ref_cnt |
| Radix Tree | 无（平铺 dict） | 前缀共享结构 |
| 内存单位 | block 计数 | GPU 显存字节 |
| Decode 阶段 | 忽略（仅 prefill） | decode 步也访问 KV block |
| 驱逐时机 | 每次 miss 前同步驱逐 | 异步，ref_cnt=0 才可驱逐 |

当前结论仅反映 prefix cache 命中率的**相对排名**，不代表绝对节省量。Phase 2+ 将逐步对齐 vLLM 语义。

---

## 快速安装

```bash
# 克隆项目
git clone <repo-url>
cd two_queue_ttl

# 无外部必选依赖。可选：tiktoken（更精确分词）
pip install tiktoken       # 可选，不安装则退回 UTF-8 bytes 分词器

# 安装开发依赖（仅测试需要）
pip install pytest pytest-cov

# 或直接用 pyproject.toml
pip install -e ".[dev]"
```

---

## 最小示例（toy trace，30 秒内可复现）

```bash
# 1. 在 7 行 toy trace 上对比三种策略
python scripts/run_simulation.py \
    --trace tests/fixtures/toy_trace.csv \
    --compare \
    --capacity 4 \
    --block-size 1 \
    --base-ttl 10.0 \
    --extended-ttl 100.0

# 2. 用 JSON 配置运行（输出 summary.json + comparison.json）
python scripts/run_phase1.py --config configs/phase1_toy_trace.json
cat outputs/phase1_toy_trace/summary.json

# 3. 容量扫描 + 生成图表（需 matplotlib）
python scripts/analyze_capacity_sweep.py \
    --trace tests/fixtures/toy_trace.csv \
    --capacities 2 3 4 6 8 \
    --base-ttl 10 --extended-ttl 100 \
    --infinite-cache \
    --output-dir outputs/toy_sweep \
    --plot
```

---

## 运行测试

```bash
python -m pytest tests/ -q          # 181 项测试，< 1 秒
python -m pytest tests/ -v          # 详细模式
python -m pytest tests/integration/ # 仅集成测试
python -m pytest tests/unit/        # 仅单元测试
```

---

## 当前支持策略

| 策略 ID | 名称 | 状态 | 说明 |
|---------|------|------|------|
| S0 | `infinite_cache` | ✅ 已实现 | 永不淘汰，提供 `ideal_hit_rate` 上界和 `gap_closed_ratio` 分母 |
| S1 | `lru` | ✅ 已实现 | 生产基线，LRU 淘汰 |
| S2 | `belady` | ✅ 已实现 | 最优离线 oracle（需 FutureIndex），算法理论上界；注：对前缀缓存为近似上界 |
| S3 | `ttl_lru` | ✅ 已实现 | LRU + TTL 过期优先，隔离 TTL 贡献 |
| S5/S6/S7 | `two_queue_ttl` | ✅ 已实现 | 双队列 + 分层 TTL + 可选离线注册表，算法核心 |

### 待补充策略（Phase 2）

| 策略 ID | 名称 | 说明 |
|---------|------|------|
| S4 | `two_queue_no_ttl` | 仅分队列、无 TTL，隔离队列结构的独立贡献 |
| S8 | `two_queue_role_detection` | 运行时角色识别进入 Protected（需 message boundary） |
| S9 | `two_queue_hard_protected` | S7 基础上加 Hard Protected 常驻第三层 |

---

## 核心指标

| 指标 | 含义 | 越…越好 |
|------|------|---------|
| `prefix_block_hit_rate` | block 级命中率 = hits / total_blocks_requested | 高 |
| `gap_closed_ratio` | (policy - LRU) / (InfiniteCache - LRU)，衡量缩小了多少 gap | 高（目标 ≥ 0.5） |
| `saved_prefill_tokens` | 命中节省的 prefill token 数 | 高 |
| `eviction_count` | 总淘汰次数 | — |
| `hot_prefix_eviction_count` | 被淘汰时 hit_count ≥ 2 的 block 数 | 低 |
| `evicted_before_next_hit_count` | 被淘汰后仍有未来访问的 block 数 | 低 |
| `protected_eviction_count` | Protected 队列淘汰次数（TwoQueueTTL 专用） | — |
| `probation_eviction_count` | Probation 队列淘汰次数（TwoQueueTTL 专用） | — |
| `protected_pollution_rate` | Protected 淘汰中从未再命中的比例 | 低（警戒线 0.3） |
| `promotion_count` | Probation → Protected 晋升次数 | — |

### gap_closed_ratio 计算方法

```
gap_closed_ratio = (policy_hit_rate - lru_hit_rate)
                 / (infinite_cache_hit_rate - lru_hit_rate)

范围：0.0 = 与 LRU 相同；1.0 = 达到 InfiniteCache 水平
目标：gap_closed_ratio ≥ 0.5（进入在线验证的门槛）
```

运行容量扫描时加 `--infinite-cache` 参数即可自动计算此指标。

---

## Trace1 实验背景：Qwen-V3.5-27B-64K

本平台主要用于验证以下生产数据的结论：

| 分析维度 | 数据 | 来源 |
|----------|------|------|
| 理想命中率（Infinite Cache） | **82.4%** | `block_prefix_analyzer` |
| 复用时间 p50 / p80 / p95 | 17s / **46s** / 255s | F13 reuse_time 分析 |
| 可复用请求比例 | 8,749 / 8,755 = **99.9%** | reuse_distance 分析 |
| 共享前缀长度 | 2,562 analysis-blocks（327,936 chars） | common_prefix 分析 |
| 第 0 块共享覆盖率 | 68.1% | common_prefix 分析 |

**TTL 参数锚定**：`base_ttl=46s`（F13 p80），`extended_ttl=255s`（F13 p95）。

**运行 trace1 实验：**
```bash
# 如果 trace 文件是 raw CSV（有 raw_prompt 列），先转换
python scripts/convert_raw_trace.py \
    --input data/your_qwen35_trace.csv \
    --output data/trace1_qwen35_27b.csv

# 容量扫描（含 gap_closed_ratio）
python scripts/analyze_capacity_sweep.py \
    --trace data/trace1_qwen35_27b.csv \
    --capacities 1000 5000 10000 26000 53500 \
    --base-ttl 46 --extended-ttl 255 \
    --promotion-threshold 2 \
    --protected-ratio 0.30 \
    --infinite-cache \
    --output-dir outputs/trace1 \
    --plot

# 加 Belady Oracle（慢，每次淘汰 O(capacity)）
python scripts/analyze_capacity_sweep.py ... --belady

# 生成离线注册表（S7）
python scripts/generate_registry.py \
    --trace data/trace1_qwen35_27b.csv \
    --top-n 500 --min-fraction 0.10 \
    --output data/trace1_registry.txt

# 带注册表的容量扫描
python scripts/analyze_capacity_sweep.py ... \
    --registry data/trace1_registry.txt
```

实验配置文件：`configs/trace1_qwen35_27b.json`（含容量锚点、消融矩阵、成功标准）。

---

## 策略详细说明

### LRU（S1）
最近最少使用，生产基线，单 OrderedDict 管理所有 FREE_CACHED block。

### TTL-LRU（S3）
TTL 仅影响**淘汰优先级**，不影响命中判断：
- `access()` 对任何仍在 cache 中的 block 返回 True（包括 TTL 已过期）
- `evict_one()` 优先淘汰 TTL 过期 block，其次按 LRU 顺序

### Two-Queue TTL（S5/S6/S7）
双队列结构（Probation / Protected），具备抗污染能力：

```
Probation（~70% 容量）   ← 新 block 首次进入
   │  hit_count ≥ threshold 时晋升
   ▼
Protected（~30% 容量）   ← 已验证可复用的 block，TTL 保护
```

淘汰顺序：Probation LRU → TTL 过期 Protected → Protected LRU

| 参数 | 默认值 | 数据依据 |
|------|--------|----------|
| `base_ttl` | 46.0 s | F13 reuse_time p80 |
| `extended_ttl` | 255.0 s | F13 reuse_time p95 |
| `promotion_threshold` | 2 | 第 3 次出现时晋升 |
| `protected_ratio` | 0.30 | Protected 容量占比 |

可选：通过 `--registry` 传入离线注册表，注册表中的 block 直接进入 Protected。

### Infinite Cache（S0）
永不淘汰，给出 `ideal_hit_rate` 上界（对应 `gap_closed_ratio` 的分母）。

### Belady Oracle（S2）
最优离线算法：驱逐下一次访问时间最远的 block。注意：对前缀缓存为近似上界（前缀链级联效应导致小 trace 上可能低于 LRU；大 trace 上可靠上界）。

---

## Trace 格式

### 标准格式（hash_ids CSV）

| 列名 | 类型 | 说明 |
|------|------|------|
| `timestamp` | float | 请求时间戳（秒） |
| `model_id` | str | 模型标识 |
| `user_id` | str | 用户/会话标识 |
| `request_type` | str | `prefill` 等 |
| `input_length` | int | 输入 token 数 |
| `hash_ids` | str | `\|` 分隔的 block content hash 列表 |

### Raw Prompt 格式（支持自动转换）

| 列名 | 类型 | 说明 |
|------|------|------|
| `request_id` | str | 请求标识 |
| `user_id` | str | 用户标识 |
| `raw_prompt` | str | 原始输入文本（最长支持 128K） |
| `timestamp` | float | 时间戳（秒） |

用 `scripts/convert_raw_trace.py` 将 raw_prompt 格式转换为 hash_ids 格式；
或在仿真时直接加 `--raw` 参数（自动调用 tiktoken 分词）。

---

## 项目结构

```
two_queue_ttl/
├── sim/
│   ├── core/
│   │   ├── block.py          — BlockMeta, BlockQueue, PolicyEvents
│   │   ├── trace.py          — TraceRecord
│   │   ├── policy.py         — AbstractCachePolicy（接口）
│   │   ├── prefix_key.py     — make_prefix_path_keys（vLLM 对齐的链式哈希）
│   │   └── future_index.py   — FutureIndex 函数式 API（Belady + MetricsCollector 共享）
│   ├── analysis/
│   │   └── future_index.py   — FutureIndex 类 API（含 request-index 精确查询，测试用）
│   ├── policies/
│   │   ├── lru.py            — LRUPolicy（S1）
│   │   ├── ttl_lru.py        — TTLLRUPolicy（S3）
│   │   ├── two_queue_ttl.py  — TwoQueueTTLPolicy（S5/S6/S7）
│   │   ├── infinite_cache.py — InfiniteCachePolicy（S0）
│   │   └── belady.py         — BeladyOraclePolicy（S2）
│   ├── cache/
│   │   └── prefix_cache.py   — PrefixCache（前缀链语义 + ref_cnt pinning）
│   ├── io/
│   │   ├── trace_loader.py   — 加载标准 hash_ids CSV
│   │   ├── raw_trace_loader.py — 加载 raw_prompt CSV（自动分词）
│   │   ├── prompt_tokenizer.py — SHA-256 block hash（tiktoken / UTF-8 fallback）
│   │   └── registry.py       — OfflineRegistry（离线注册表）
│   ├── metrics/
│   │   ├── collector.py      — MetricsCollector（两阶段）+ MetricsSnapshot + compute_gap_closed_ratios
│   │   └── reporter.py       — compare_table, print_report, to_csv_row
│   ├── config.py             — SimConfig, TTLLRUConfig, TwoQueueTTLConfig, ExperimentConfig
│   ├── runner.py             — SimulationRunner（单次 + 多策略对比）
│   └── experiment.py         — ExperimentRunner（读 JSON 配置，写输出）
│
├── scripts/
│   ├── run_simulation.py       — 命令行入口（单策略 / --compare 模式）
│   ├── run_phase1.py           — JSON 配置批量实验入口
│   ├── analyze_capacity_sweep.py — 容量扫描 + gap_closed_ratio + 图表
│   ├── generate_registry.py    — 从 trace 生成离线注册表
│   └── convert_raw_trace.py    — raw_prompt CSV → hash_ids CSV
│
├── tests/
│   ├── fixtures/
│   │   ├── toy_trace.csv       — 7 行验证 trace（手工期望值已推导）
│   │   └── sample_trace.csv    — 示例 trace
│   ├── unit/                   — 策略单元测试（9 个文件）
│   └── integration/            — 端到端对比测试（3 个文件）
│
├── configs/
│   ├── phase1_toy_trace.json   — toy trace 三策略实验
│   └── trace1_qwen35_27b.json  — Qwen-V3.5-27B-64K trace1 实验配置
│
├── docs/
│   ├── terminology.md          — 关键术语与口径规范（必读）
│   ├── DEVELOPMENT_REQUIREMENTS.md — 代码开发要求
│   ├── PHASE1_IMPLEMENTATION.md    — Phase 1 实现计划
│   ├── PHASE2_IMPLEMENTATION.md    — Phase 2 实现计划
│   ├── PHASE3_IMPLEMENTATION.md    — Phase 3 实现计划（实验系统）
│   └── PHASE4_IMPLEMENTATION.md    — Phase 4 实现计划（vLLM 对接）
│
├── kv_cache_eviction_design.md     — Two-Queue TTL 算法设计方案（含生产数据依据）
├── two_queue_ttl_experiment_plan.md — 实验计划（Phase 0-3，含成功标准）
├── pyproject.toml
└── data/                           — 放置 trace 文件（不提交进 git）
    └── .gitkeep
```

---

## toy trace 验证期望值

Toy trace（7 条请求，capacity=4，block_size=1）所有策略期望结果：

| 指标 | LRU | TTL-LRU | TwoQueueTTL |
|------|-----|---------|-------------|
| `total_blocks_hit` | 7 | 7 | 7 |
| `prefix_block_hit_rate` | 0.3684 | 0.3684 | 0.3684 |
| `eviction_count` | 8 | 8 | 8 |
| `promotion_count` | — | — | **2** |
| `protected_eviction_count` | — | — | 0 |

```bash
# 一键验证所有期望值
python -m pytest tests/integration/test_toy_trace.py -v
```

---

## 配置文件格式（JSON）

```json
{
  "name": "my_experiment",
  "trace_path": "data/my_trace.csv",
  "output_dir": "outputs/my_experiment",
  "description": "实验描述",
  "policies": [
    {"policy": "lru",    "capacity": 10000, "block_size": 128},
    {"policy": "ttl_lru","capacity": 10000, "block_size": 128,
     "ttl_lru": {"ttl": 46.0}},
    {"policy": "two_queue_ttl", "capacity": 10000, "block_size": 128,
     "two_queue_ttl": {
       "base_ttl": 46.0, "extended_ttl": 255.0,
       "promotion_threshold": 2, "protected_ratio": 0.30
     }}
  ]
}
```

---

## 与 vLLM PR 的关系

本平台用于在提 vLLM PR 之前完成以下工作：

1. **问题诊断（Phase 0）**：验证 `evicted_before_next_hit` 占比 > 20%，确认 eviction 是低命中率的主要原因。
2. **离线仿真（Phase 1）**：在真实 trace 上对比 S0-S7 策略，获得 `gap_closed_ratio ≥ 0.5` 的实验证据。
3. **参数消融（Phase 2）**：TTL / protected_ratio / promotion_threshold 交叉实验，确定最优配置。
4. **在线原型（Phase 3）**：vLLM 代码层面实现最小版本，小流量 A/B 验证 TTFT 和吞吐改善。

PR 成功标准（来自 `two_queue_ttl_experiment_plan.md §11`）：
- 最低：`gap_closed_ratio ≥ 0.3`，`evicted_before_next_hit` 下降 ≥ 20%，`protected_pollution_rate < 0.3`
- 有价值：`gap_closed_ratio ≥ 0.5`，`saved_prefill_tokens` 提升 ≥ 10%
- 目标：`gap_closed_ratio ≥ 0.7`

---

## 许可证

MIT
