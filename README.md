# KV Cache Eviction 离线仿真与验证平台

> **平台目标**：通过离线 trace 分析 + 仿真器回放，对 LLM serving 中的 KV cache 淘汰策略进行可重复的算法验证。
>
> 当前已支持：多策略对比仿真、多租户 trace 画像、prefix-path chain 检测、跨模型批量分析。

---

## 平台能力概览

| 模块 | 功能 | 关键脚本 |
|------|------|---------|
| **仿真器** | LRU / TTL-LRU / TwoQueueTTL / Belady / InfiniteCache 多策略对比 | `scripts/run_simulation.py` |
| **容量扫描** | 跨容量自动扫描 + gap_closed_ratio + 图表 | `scripts/analyze_capacity_sweep.py` |
| **Trace 转换** | raw_prompt CSV → hash_ids CSV（utf8_bytes 切块） | `scripts/convert_raw_trace.py` |
| **多租户分析** | 用户分布 / 重度-轻度对比 / 复用时间分布 / 推荐容量与 TTL | `scripts/analyze_multitenant_trace.py` |
| **Chain 检测** | Trie + 双阈值找 strict path-closed chain，支持 token 解码 | `scripts/verify_chain_path_closure.py` |
| **Per-user Chain** | 每个 user_id 的独立 chain + 与全局 chain 对比 | `scripts/per_user_chain_analyzer.py` |
| **批量 Pipeline** | 跨多个模型一键跑 convert→registry→analyze→对比 | `scripts/batch_pipeline.py` |
| **算法实验** | Plan A (Adaptive TTL) + Plan B1 (threshold sweep) | `scripts/experiment_plan_ab.py` |
| **离线注册表** | 高频 chain blocks 提取，用于 TwoQueueTTL 直入 Protected | `scripts/generate_registry.py` |

---

## 快速安装

```bash
git clone <repo-url>
cd two_queue_ttl

# 核心运行无外部依赖；可选：
pip install matplotlib numpy   # 容量扫描图表
pip install -e ".[dev]"         # 安装 pytest 等开发依赖

# 验证安装
python -m pytest tests/ -q     # 181 项测试，<1 秒
```

**离线运行：** 平台不依赖任何 LLM tokenizer，纯字节级哈希实现。代码可在断网环境下完整运行。

---

## 三步走验证战略（当前主线）

针对生产模型的 KV cache 淘汰算法分析采用 **trace 验证 → API 测试 → 算法设计** 三步走流程。

```
[Step 1 实验验证]    用 trace 数据验证 chain 假设、稳定性、用户分布
        ↓ 决策点
[Step 2 API 测试]    用真实 API 测量缓存容量、真实命中率、生命周期
        ↓ 决策点
[Step 3 算法设计]    离线策略设计、模拟器迭代、回到生产部署
```

每一步独立可执行，每一步的产出为下一步的输入。Step 1 和 Step 2 设计为通用模块，可对任意模型复用。

详细路线见 [`docs/3step_validation_plan.md`](docs/3step_validation_plan.md)。

---

## 典型实验流程

### 流程 A — 对单个模型做 trace 画像

```bash
# 1. 转换 raw CSV 为标准 trace
python scripts/convert_raw_trace.py \
    --input data/<model>/raw/sample.csv \
    --output data/<model>/trace1_production.csv

# 2. 多租户 trace 分析
python scripts/analyze_multitenant_trace.py \
    --trace data/<model>/trace1_production.csv \
    --heavy-pct 10 \
    --output-dir outputs/<model>/trace_analysis
```

### 流程 B — 多模型批量分析

```bash
# 一键处理多个模型，最后输出跨模型对比表
python scripts/batch_pipeline.py \
    --models qwen_v3.5_27b_64k qwen_v3_32b_8k qwen_v3_32b_32k \
             deepseek_v3.1_8k deepseek_v3.1_32k

# 仅打印对比表（所有 trace_summary.json 已就绪时）
python scripts/batch_pipeline.py --compare-only
```

### 流程 C — Chain 检测（Step 1.1 / 1.2）

```bash
# 全局 strict path-closed chain（含 token 解码）
python scripts/verify_chain_path_closure.py \
    --raw-csv data/<model>/raw \
    --output  outputs/<model>/chain_summary.json

# Per-user chain + 与全局 chain 对比
python scripts/per_user_chain_analyzer.py \
    --raw-csv data/<model>/raw \
    --output  outputs/<model>/per_user_chains.json
```

### 流程 D — 算法 capacity sweep（Step 3 类）

```bash
# 容量扫描，自动计算 gap_closed_ratio
python scripts/analyze_capacity_sweep.py \
    --trace data/<model>/trace1_production.csv \
    --capacities 10000 50000 100000 500000 \
    --base-ttl 46 --extended-ttl 255 \
    --infinite-cache \
    --output-dir outputs/<model>/sweep \
    --plot

# Plan A (adaptive TTL) + Plan B1 (threshold sweep) 联合实验
python scripts/experiment_plan_ab.py \
    --trace data/<model>/trace1_production.csv \
    --capacities 10000 50000 100000 \
    --probe-capacities 10000 50000 100000 \
    --thresholds 2 3 4 5 \
    --alpha 0.7 --base-ttl 46 \
    --output-dir outputs/<model>/plan_ab
```

---

## 仿真策略

| 策略 ID | 名称 | 状态 | 说明 |
|---------|------|------|------|
| S0 | `infinite_cache` | ✅ | 永不淘汰，给出 `ideal_hit_rate` 上界（gap_closed_ratio 分母） |
| S1 | `lru` | ✅ | 生产基线 |
| S2 | `belady` | ✅ | 最优离线 oracle（前缀缓存为近似上界） |
| S3 | `ttl_lru` | ✅ | LRU + TTL 过期优先 |
| S5/S6/S7 | `two_queue_ttl` | ✅ | 双队列 + 分层 TTL + 可选离线注册表 |

### Two-Queue TTL（核心算法）

```
Probation（~70% 容量）   ← 新 block 首次进入
   │  hit_count ≥ threshold 时晋升
   ▼
Protected（~30% 容量）   ← 已验证可复用 block，TTL 保护
```

淘汰顺序：Probation LRU → TTL 过期 Protected → Protected LRU

| 参数 | 默认 | 数据依据 |
|------|------|---------|
| `base_ttl` | 46.0 s | F13 reuse_time p80 |
| `extended_ttl` | 255.0 s | F13 reuse_time p95 |
| `promotion_threshold` | 2 | 第 3 次出现时晋升 |
| `protected_ratio` | 0.30 | Protected 容量占比 |

可选 `--registry`：注册表中的 block 直接进入 Protected。

---

## 核心指标

| 指标 | 含义 | 越…越好 |
|------|------|---------|
| `prefix_block_hit_rate` | hits / total_blocks_requested | 高 |
| `gap_closed_ratio` | (policy − LRU) / (∞Cache − LRU) | 高（目标 ≥ 0.5） |
| `saved_prefill_tokens` | 命中节省的 prefill token 数 | 高 |
| `eviction_count` | 总淘汰次数 | — |
| `hot_prefix_eviction_count` | 被淘汰时 hit_count ≥ 2 的 block | 低 |
| `evicted_before_next_hit_count` | 被淘汰后仍有未来访问 | 低 |
| `protected_pollution_rate` | Protected 中淘汰前从未再命中的比例 | 低（警戒 0.3） |

---

## Trace 格式

### 标准格式（hash_ids CSV）

| 列名 | 类型 | 说明 |
|------|------|------|
| `timestamp` | float | 请求时间戳（秒） |
| `model_id` | str | 模型标识 |
| `user_id` | str | 用户/租户标识 |
| `request_type` | str | `prefill` 等 |
| `input_length` | int | 输入 token 数 |
| `hash_ids` | str | `\|` 分隔的 block hash 列表 |

### Raw Prompt 格式（4 列标准，Step 1/2 直接消费）

| 列名 | 类型 | 说明 |
|------|------|------|
| `request_id` | str | 请求 ID（唯一） |
| `user_id` | str | 租户 ID（实为 product_id） |
| `raw_prompt` | str | 原始 prompt 文本（最长支持 128K+） |
| `timestamp` | float | 时间戳（秒） |

`convert_raw_trace.py` 将 raw 格式转为 hash_ids 格式；新模块（`verify_chain_path_closure.py` 等）直接消费 raw 格式无需转换。

---

## 项目结构

```
two_queue_ttl/
├── sim/                         # 仿真器核心
│   ├── core/                    # BlockMeta / TraceRecord / prefix_path_key / FutureIndex
│   ├── policies/                # LRU / TTL-LRU / TwoQueueTTL / Belady / InfiniteCache
│   ├── cache/                   # PrefixCache（前缀链 + ref_cnt pinning）
│   ├── io/                      # trace 加载 / 分词器 / 离线注册表
│   ├── metrics/                 # MetricsCollector / 报表 / gap_closed_ratio
│   ├── analysis/                # FutureIndex 类 API
│   ├── config.py                # SimConfig / ExperimentConfig
│   ├── runner.py                # SimulationRunner
│   └── experiment.py            # ExperimentRunner（JSON 配置入口）
│
├── scripts/                     # 命令行工具
│   ├── run_simulation.py             # 单次仿真 + 多策略对比
│   ├── run_phase1.py                 # JSON 配置批量实验
│   ├── analyze_capacity_sweep.py     # 容量扫描 + gap_closed_ratio
│   ├── convert_raw_trace.py          # raw → hash_ids
│   ├── generate_registry.py          # 离线注册表生成
│   ├── analyze_multitenant_trace.py  # 多租户画像（heavy/light/reuse/TTL 推荐）
│   ├── batch_pipeline.py             # 跨模型批量 pipeline
│   ├── experiment_plan_ab.py         # Plan A+B1 算法实验
│   ├── verify_chain_path_closure.py  # Step 1.1: strict chain detection
│   └── per_user_chain_analyzer.py    # Step 1.2: per-user chain
│
├── tests/                       # 181 项测试，<1 秒
│   ├── fixtures/                # toy_trace / sample_trace
│   ├── unit/                    # 9 个策略 / 模块单元测试
│   └── integration/             # 端到端对比测试
│
├── configs/                     # JSON 实验配置
│   ├── phase1_toy_trace.json
│   └── trace1_qwen35_27b.json
│
├── docs/                        # 设计文档
│   ├── 3step_validation_plan.md         # 当前主线：三步走验证大纲
│   ├── kv_cache_eviction_design.md      # Two-Queue TTL 算法设计
│   ├── two_queue_ttl_experiment_plan.md # Phase 0-3 实验计划
│   ├── terminology.md                   # 术语规范
│   ├── DEVELOPMENT_REQUIREMENTS.md      # 开发约定
│   ├── PHASE1-4_IMPLEMENTATION.md       # 历史实现计划
│   └── round2_plan.md
│
├── data/.gitkeep                # 数据目录占位（实际数据被 gitignore）
├── outputs/                     # 仿真输出（被 gitignore）
└── pyproject.toml
```

---

## 关键设计原则

### 1. 离线运行 / 不依赖 LLM tokenizer
所有切块均使用 utf8_bytes（128 字节/块），SHA-256 链式哈希。chain 复用率分析只需"切块方法一致"即可保证正确性。代码可在断网机器上完整运行。

### 2. 模块复用性
Step 1（trace 分析）和 Step 2（API 测试）模块设计为通用工具——输入 raw CSV / API 配置 + 必要参数即可对任意模型使用，不与具体模型绑定。

### 3. 仿真与生产语义差异
本平台为离线仿真工具，与 vLLM runtime 存在差异：

| 维度 | 本平台 | vLLM runtime |
|------|--------|--------------|
| 哈希函数 | SHA-256（固定） | SipHash（含随机 seed） |
| 并发模型 | 单线程串行回放 | 多请求并发，真实 ref_cnt |
| Decode 阶段 | 忽略（仅 prefill） | decode 步也访问 KV block |
| 驱逐时机 | miss 前同步驱逐 | 异步，ref_cnt=0 才可驱逐 |

仿真结论反映 prefix cache 命中率的**相对排名**，不代表绝对节省量。Step 2 的 API 测试是闭环验证手段。

---

## 配置文件格式（JSON）

```json
{
  "name": "my_experiment",
  "trace_path": "data/my_trace.csv",
  "output_dir": "outputs/my_experiment",
  "policies": [
    {"policy": "lru", "capacity": 10000, "block_size": 128},
    {"policy": "ttl_lru", "capacity": 10000, "block_size": 128,
     "ttl_lru": {"ttl": 46.0}},
    {"policy": "two_queue_ttl", "capacity": 10000, "block_size": 128,
     "two_queue_ttl": {
       "base_ttl": 46.0, "extended_ttl": 255.0,
       "promotion_threshold": 2, "protected_ratio": 0.30
     }}
  ]
}
```

```bash
python scripts/run_phase1.py --config configs/phase1_toy_trace.json
```

---

## Toy Trace 验证

7 行 toy trace（capacity=4，block_size=1）期望值：

| 指标 | LRU | TTL-LRU | TwoQueueTTL |
|------|-----|---------|-------------|
| `total_blocks_hit` | 7 | 7 | 7 |
| `prefix_block_hit_rate` | 0.3684 | 0.3684 | 0.3684 |
| `eviction_count` | 8 | 8 | 8 |
| `promotion_count` | — | — | **2** |

```bash
python -m pytest tests/integration/test_toy_trace.py -v
```

---

## 已分析的生产模型

| 模型 | 请求数 | 重度用户 | ∞ hit rate | Reuse | 主要特征 |
|------|--------|---------|-----------|-------|---------|
| Qwen-V3.5-27B-64K | 8,782 | 1人/95% | 81.7% | 5.45x | 单租户长文档 |
| Qwen-V3-32B-8K | 119,208 | 3人/81% | 72.2% | 3.60x | 高频批处理流 |
| Qwen-V3-32B-32K | 25,919 | 3人/64% | 22.0% | 1.28x | 多租户高多样性 |
| DeepSeek-V3.1-8K | 17,312 | 1人/89% | 55.5% | 2.25x | 固定大 system prompt |
| DeepSeek-V3.1-32K | 7,126 | 1人/61% | 21.9% | 1.28x | 多租户多 chain |

横向规律：
- **上下文越长 → reuse 越高**（除非内容多样性极高）
- **chain 比例决定 8K 模型收益**（DS-8K chain 占 87.5% 上下文）
- **高频批处理 → TTL 退化为 LRU**（reuse p80 ≤ 3s）
- **22% 的 hit rate 是内容多样性硬上限**（算法选择无关）

---

## 许可证

MIT
