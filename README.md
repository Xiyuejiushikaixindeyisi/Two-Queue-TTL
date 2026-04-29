# KV Cache Eviction 离线仿真平台

对 LRU、TTL-LRU、Two-Queue TTL 三种 prefix cache 淘汰策略进行离线回放评估。

> **⚠️ Phase 1 局限性声明**
>
> 本平台是**离线仿真**工具，与 vLLM runtime 存在以下已知差异，Phase 1 结果不能直接等价于线上部署效果：
>
> | 维度 | 本平台（Phase 1） | vLLM runtime |
> |------|------------------|--------------|
> | 哈希函数 | SHA-256（固定 seed，离线可重现） | `hash_block_tokens`（SipHash / CBOR，含随机 seed） |
> | 并发模型 | 单线程串行回放 | 多请求并发，ref_cnt 真实递增 |
> | Radix Tree | 无（平铺 dict） | 前缀共享结构，内存更紧凑 |
> | 内存单位 | block 计数 | GPU 显存字节 |
> | Decode 阶段 | 忽略（仅 prefill） | decode 步也访问 KV block |
> | 驱逐时机 | 每次 miss 前同步驱逐 | 异步，ref_cnt=0 才可驱逐 |
>
> Phase 2+ 将逐步对齐 vLLM 语义。当前结论仅反映 **prefix cache 命中率的相对排名**，不代表绝对节省量。

---

## 快速开始

```bash
# 安装依赖（无外部依赖，标准库即可运行）
pip install pytest   # 仅测试需要

# 运行全套测试
python -m pytest tests/ -q
```

---

## 使用方法

### 方式一：JSON 配置文件（推荐）

```bash
# 在 toy trace 上同时跑三种策略，输出到 outputs/phase1_toy_trace/
python scripts/run_phase1.py --config configs/phase1_toy_trace.json

# 自定义输出目录
python scripts/run_phase1.py --config configs/phase1_toy_trace.json --output-dir /tmp/my_run
```

输出文件：
- `outputs/<name>/summary.json` — 每种策略的完整指标
- `outputs/<name>/comparison.json` — 多策略横向对比表

### 方式二：命令行参数

```bash
# 单策略运行
python scripts/run_simulation.py \
    --trace tests/fixtures/toy_trace.csv \
    --policy lru \
    --capacity 4 \
    --block-size 1

# 三策略对比（--compare 模式）
python scripts/run_simulation.py \
    --trace tests/fixtures/toy_trace.csv \
    --compare \
    --capacity 4 \
    --block-size 1 \
    --base-ttl 10.0 \
    --extended-ttl 100.0

# 使用真实 trace
python scripts/run_simulation.py \
    --trace data/sample_trace.csv \
    --policy two_queue_ttl \
    --capacity 10000 \
    --base-ttl 46 \
    --extended-ttl 255
```

---

## 配置文件格式（JSON）

```json
{
  "name": "my_experiment",
  "trace_path": "tests/fixtures/toy_trace.csv",
  "output_dir": "outputs/my_experiment",
  "description": "自由描述",
  "policies": [
    {"policy": "lru", "capacity": 4, "block_size": 1},
    {
      "policy": "ttl_lru",
      "capacity": 4,
      "block_size": 1,
      "ttl_lru": {"ttl": 5.0}
    },
    {
      "policy": "two_queue_ttl",
      "capacity": 4,
      "block_size": 1,
      "two_queue_ttl": {
        "promotion_threshold": 2,
        "base_ttl": 10.0,
        "extended_ttl": 100.0,
        "protected_ratio": 0.5
      }
    }
  ]
}
```

---

## Trace 格式

CSV，必须包含以下列：

| 列名 | 类型 | 说明 |
|------|------|------|
| `timestamp` | float | 请求时间戳（秒） |
| `model_id` | str | 模型标识 |
| `user_id` | str | 用户/会话标识 |
| `request_type` | str | `prefill` 或 `decode` |
| `input_length` | int | 输入 token 数 |
| `hash_ids` | str | `\|` 分隔的 block content hash 列表 |
| `output_length` | int | 输出 token 数 |
| `turn` | int | 对话轮次 |

---

## 策略说明

### LRU
最近最少使用淘汰。基线策略。

### TTL-LRU
TTL 仅影响淘汰优先级，不影响命中判断：
- `access()` 对任何仍在 cache 中的 block 返回 True（包括 TTL 已过期）
- `evict_one()` 优先淘汰 TTL 过期的 block，其次按 LRU 顺序

### Two-Queue TTL
双队列结构（Probation / Protected），具备抗污染能力：
- 新 block 进入 Probation 队列
- 命中次数达到 `promotion_threshold` 后晋升至 Protected
- Protected block 拥有更长 TTL 保护
- 淘汰顺序：Probation 优先 → TTL 过期的 Protected → 有效 Protected（LRU）

关键参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `promotion_threshold` | 2 | 晋升所需命中次数（第 N+1 次出现时晋升） |
| `base_ttl` | 46.0 s | Probation 和新晋升 block 的 TTL |
| `extended_ttl` | 255.0 s | hit_count ≥ 5 后使用的扩展 TTL |
| `protected_ratio` | 0.30 | Protected 队列容量占比 |

---

## 输出指标说明

| 指标 | 说明 |
|------|------|
| `prefix_block_hit_rate` | block 级命中率 = hits / total_blocks_requested |
| `saved_prefill_tokens` | 节省的 prefill token 数 = hits × block_size |
| `eviction_count` | 总淘汰次数 |
| `hot_prefix_eviction_count` | 被淘汰时 hit_count ≥ 2 的 block 数（越低越好） |
| `evicted_before_next_hit_count` | 被淘汰后还有未来访问的 block 数（越低越好） |
| `protected_eviction_count` | Protected 队列淘汰次数（TwoQueueTTL 专用） |
| `protected_pollution_rate` | Protected 淘汰中从未再命中的比例（越低越好） |
| `promotion_count` | Probation → Protected 晋升次数 |

---

## 项目结构

```
sim/
  core/          — BlockMeta, AbstractCachePolicy, TraceRecord, prefix_key
  policies/      — LRUPolicy, TTLLRUPolicy, TwoQueueTTLPolicy
  cache/         — PrefixCache（前缀链语义 + pinning）
  io/            — trace_loader, OfflineRegistry
  metrics/       — MetricsCollector（两阶段）, reporter
  config.py      — SimConfig, TTLLRUConfig, TwoQueueTTLConfig, ExperimentConfig
  runner.py      — SimulationRunner（单次 + 多策略对比）
  experiment.py  — ExperimentRunner（读 JSON 配置，写输出文件）

scripts/
  run_phase1.py    — 多策略 JSON 配置入口
  run_simulation.py — 命令行参数入口
  generate_registry.py — 生成离线注册表

tests/
  fixtures/
    toy_trace.csv    — 7 行验证 trace
    sample_trace.csv — 示例 trace
  unit/              — 策略单元测试
  integration/       — 端到端对比测试

configs/
  phase1_toy_trace.json — toy trace 三策略实验配置

docs/
  terminology.md     — key 口径规范（必读）
```

---

## 运行 toy trace 验证

```bash
# 一键验证所有期望值
python -m pytest tests/integration/test_toy_trace.py -v

# 手动运行并查看输出
python scripts/run_phase1.py --config configs/phase1_toy_trace.json
cat outputs/phase1_toy_trace/summary.json
cat outputs/phase1_toy_trace/comparison.json
```

Toy trace 期望结果（capacity=4, block_size=1）：

| 指标 | LRU | TTL-LRU | TwoQueueTTL |
|------|-----|---------|-------------|
| total_blocks_hit | 7 | 7 | 7 |
| prefix_block_hit_rate | 0.3684 | 0.3684 | 0.3684 |
| eviction_count | 8 | 8 | 8 |
| promotion_count | 0 | 0 | **2** |
| protected_eviction_count | — | — | 0 |
