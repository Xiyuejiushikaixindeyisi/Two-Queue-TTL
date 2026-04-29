# KV Cache Eviction 离线仿真实验平台 — 代码开发要求 v1.1

## 1. 项目定位

本项目是一个 KV cache eviction 离线仿真实验平台，在真实 trace 上评估
不同淘汰策略对 prefix cache 命中率的影响。

评估策略路线图（按实现阶段）：

- Phase 1（当前）: LRU, TTL-LRU, Two-Queue TTL, Infinite Cache
- Phase 2（后续）: Belady Oracle, Hard Protected + Warm-up
- Phase 3（后续）: 多节点 routing-aware replay, 多级缓存
- Phase 4（后续）: vLLM/vLLM-Ascend 在线 instrumentation 对接

---

## 2. 工程原则

### 2.1 正确性优先

优先级：**正确性 > 可测试性 > 可扩展性 > 性能优化**

不要为了性能牺牲统计口径的正确性和可解释性。
如某处实现暂时低效，在注释中标明，不要静默留坑。

### 2.2 模块结构（与现有骨架对齐）

不写单个大脚本。每个模块只负责一类事情：

```
sim/
  core/
    block.py         — BlockMeta, BlockQueue, PolicyEvents 数据类
    policy.py        — AbstractCachePolicy 抽象基类（扩展点）
    trace.py         — TraceRecord 数据类
  policies/
    __init__.py      — POLICY_REGISTRY（策略名 → 类）
    lru.py           — LRU baseline
    ttl_lru.py       — TTL-LRU（隔离 TTL 单独贡献）
    two_queue_ttl.py — Two-Queue TTL MVP
    infinite.py      — Infinite Cache（无容量限制，ideal_hit_rate 基准）
    # 后续: belady.py, hard_protected.py
  cache/
    prefix_cache.py  — PrefixCache 仿真引擎（prefix 语义 + 分配循环）
  io/
    trace_loader.py  — 读取 CSV/JSONL，返回 List[TraceRecord]
    registry.py      — OfflineRegistry（block_hash 离线注册表查找）
  metrics/
    collector.py     — MetricsCollector（两阶段：在线累积 + 离线 finalize）
    reporter.py      — 输出格式（表格、JSON、CSV、compare_table）
  runner.py          — SimulationRunner（单次运行 + 多策略对比）
  config.py          — SimConfig, TwoQueueTTLConfig 配置 dataclass

scripts/
  run_simulation.py    — CLI 入口（argparse，调用 runner）
  generate_registry.py — 从 trace 生成离线注册表文件
```

**说明：**

- 不需要单独的 `prefix_key.py`。trace 的 `hash_ids` 字段是预计算的
  block hash 链，仿真层直接使用，不在仿真时重新哈希内容。
- 不需要单独的 `experiment.py`。`runner.py` 承担实验编排职责。
- 不引入 `pyyaml`，config 使用 dataclass，避免额外依赖。

### 2.3 策略插件化

所有 eviction policy 通过统一接口实现：

```python
class AbstractCachePolicy(ABC):
    def contains(block_hash: str) -> bool
    def access(block_hash, timestamp, block_pos, user_id) -> bool
    def add(block_hash, timestamp, block_pos, user_id) -> None
    def evict_one(timestamp, pinned=None) -> Optional[BlockMeta]
    def flush_events() -> PolicyEvents   # 晋升/降级事件 side-channel
```

新增策略只需：

1. 创建 `sim/policies/<name>.py` 并继承 `AbstractCachePolicy`
2. 在 `sim/policies/__init__.py::POLICY_REGISTRY` 加一行

其余代码（`PrefixCache`、`MetricsCollector`、`SimulationRunner`）不需修改。

---

## 3. 测试要求

使用 pytest。测试与实现同步进行，不允许"先实现后补测试"。

### 3.1 单元测试覆盖项

**LRU:**

- [ ] 空 cache 返回 miss
- [ ] `add` → `access` 返回 hit
- [ ] eviction 遵守 LRU 顺序（最老先出）
- [ ] `access` 刷新 LRU 顺序
- [ ] `evict_one` 跳过 `pinned` 集合中的 block
- [ ] 全部 pinned 时 `evict_one` 返回 `None`
- [ ] 空 `hash_ids` 请求不崩溃（PrefixCache 层）

**TTL-LRU:**

- [ ] TTL 过期的 block 被视作 miss（即使在 cache 中）
- [ ] 淘汰时优先选 TTL 过期的 block，而非 LRU 最老
- [ ] hit 刷新 TTL

**Two-Queue TTL:**

- [ ] 新 block 进入 Probation
- [ ] registry 中的 block 跳过 Probation 直接进 Protected
- [ ] `hit_count` 达到 `promotion_threshold` 时从 Probation 晋升 Protected
- [ ] 晋升时设置 `base_ttl`，`hit_count ≥ 5` 时设置 `extended_ttl`
- [ ] Protected hit 刷新 TTL
- [ ] `flush_events()` 正确返回晋升列表，且刷新后清空
- [ ] 淘汰优先级：Probation LRU > Protected TTL 过期 > Protected LRU
- [ ] Protected TTL 过期后可被正常淘汰（软保护语义，非强制删除）
- [ ] `evict_one` 跳过 pinned 集合

> **重要语义说明：** "Protected 超容量"的正确处理是将 Protected 中优先级
> 最低的 block **降级至 Probation**（demotion），而非直接淘汰出 cache。
> 该功能属于 **Phase 2**，Phase 1 MVP 不实现 demotion，仅通过 eviction
> 优先级保证 Probation 先于 Protected 被淘汰。

**Prefix Cache 引擎:**

- [ ] 前缀语义：hit 链在第一个 miss 处断开，后续全部算 miss
- [ ] 完整 prefix hit（第二次相同请求）
- [ ] 部分 prefix hit（前段命中，后段缺失）
- [ ] 分配 miss block 时 hit block 不被驱逐（pinned 机制）
- [ ] 分配多个 miss block 时，已分配的不被后续驱逐（pinned 增长）
- [ ] cache 满时触发 eviction

**Metrics:**

- [ ] `evicted_before_next_hit_count > 0`（需要设计能触发该情况的 trace）
- [ ] `protected_pollution_rate` 在无 Protected eviction 时不除零
- [ ] `prefix_block_hit_rate` 在无请求时不除零
- [ ] `saved_prefill_tokens = total_blocks_hit × block_size`
- [ ] `total_blocks_hit + total_blocks_miss = total_blocks_requested`

### 3.2 集成测试 toy trace 最小充分条件

toy trace 必须同时满足以下 5 条，否则测试会虚过：

1. 至少 1 次 prefix **完整命中**（验证 hit 统计）
2. 至少 1 次 prefix **部分命中**（验证前缀语义）
3. 至少 1 次 **eviction**（需要 cache capacity < 工作集大小）
4. 至少 1 个 block 被淘汰后在**后续请求中再次出现**（触发 `evicted_before_next_hit > 0`）
5. 至少 1 个 block 在 Two-Queue TTL 下发生**晋升**

必须手工计算 expected result 并写成注释，包括：

```python
# 手工推导过程写在测试文件注释中
expected = {
    "total_requests": N,
    "total_blocks_requested": M,
    "total_blocks_hit": K,
    "prefix_block_hit_rate": K / M,
    "eviction_count": E,
    "evicted_before_next_hit_count": B,
}
```

toy trace 行数建议在 **8～15 行**之间，人工可逐步追踪。

### 3.3 两策略对比集成测试

对同一 toy trace，在相同 capacity 下跑 LRU 和 Two-Queue TTL，验证：

- [ ] 两者 `total_requests` 相同
- [ ] 两者 `total_blocks_requested` 相同
- [ ] `gap_closed_ratio ∈ [0, 1]`（不要求 Two-Queue 一定优于 LRU）

---

## 4. 指标定义（统一口径，不得随意修改字段名）

### 4.1 必须实现的基础指标

| 指标名 | 定义 |
|---|---|
| `prefix_block_hit_rate` | `total_blocks_hit / total_blocks_requested` |
| `saved_prefill_tokens` | `total_blocks_hit × block_size` |
| `eviction_count` | 总淘汰次数 |
| `hot_prefix_eviction_count` | 被淘汰时 `hit_count ≥ 2` 的 block 数 |
| `evicted_before_next_hit_count` | 被淘汰且在 trace 中还有后续访问的 block 数 |
| `protected_eviction_count` | 从 Protected 队列被淘汰的次数 |
| `probation_eviction_count` | 从 Probation 队列被淘汰的次数 |
| `protected_pollution_rate` | Protected 淘汰中"入队后从未再命中"的比例 |
| `promotion_count` | Probation → Protected 晋升次数 |

### 4.2 派生指标（需要 Infinite Cache baseline）

```
ideal_hit_rate   = Infinite Cache 策略在同一 trace、同一 capacity 设置下的
                   prefix_block_hit_rate（Infinite Cache 忽略 capacity 上限）

gap_closed_ratio = (policy_hit_rate - lru_hit_rate)
                   ÷ (ideal_hit_rate - lru_hit_rate)
                   分母为 0 时返回 0.0
```

`gap_closed_ratio` 是实验计划的**核心成功度量**。
Phase 3 在线上线门槛：`gap_closed_ratio ≥ 0.3`（实验计划 §8.1）。

### 4.3 指标计算的两阶段架构（不可压缩为单次 replay）

`evicted_before_next_hit_count` 和 `protected_pollution_rate` 需要后向信息
（被淘汰的 block 在 trace 未来是否还有访问）。

**实现方式（强制）：**

```
阶段 1（replay 前）：
  调用 precompute_future_access(trace)
  建立 block_hash → 排序访问时间戳列表 的索引（bisect 支持 O(log n) 查找）

阶段 2（replay 中）：
  on_request() 记录每次 eviction 的 (block_hash, eviction_time, hit_count, queue)

阶段 3（replay 后）：
  finalize() 对每个 eviction 事件查索引，确定是否有后续访问
```

不能把阶段 1 和阶段 2 压缩成单次 replay，否则无法知道"未来是否有访问"。

---

## 5. 性能要求

不追求极致性能，但不能明显低效。

- **允许**的 O(n) 扫描：单次 eviction 扫描（n = cache size，通常 ≤ 10⁵）
- **不允许**：每次 access 重建整个索引

如某处暂时低效，在注释中标明后续优化方向（不要静默留坑）。

---

## 6. 代码风格

- [ ] Python 3.9+
- [ ] 函数职责单一
- [ ] 避免全局状态
- [ ] 不写魔法常量，使用 `SimConfig` / `TwoQueueTTLConfig`
- [ ] 不吞异常，错误要有清晰信息
- [ ] `sim/` 下的 library 代码不直接 `print`，用返回值和异常
- [ ] `scripts/` 层可以 `print` 摘要
- [ ] 所有 public function 有简洁 docstring
- [ ] 依赖原则：标准库优先；`pandas` 仅用于大 trace 的 IO 加速（可选依赖）；不引入 `pyyaml`

---

## 7. 可扩展性要求

- **policy 不与 simulator 强耦合**：`PrefixCache` 只依赖 `AbstractCachePolicy` 接口
- **metrics 不服务于某一 policy**：`MetricsCollector` 对所有策略统一
- `TraceRecord` / `BlockMeta` 预留可选扩展字段（`output_length`, `turn` 等）
- config schema 通过新增 dataclass 字段扩展新策略参数，不改现有字段
- 不在核心逻辑里写死模型名（`model_id` 只作为分组 key，不做条件分支）

---

## 8. 禁止事项

- ✗ 不修改 vLLM/vLLM-Ascend 源码
- ✗ 不实现在线服务
- ✗ 不引入多进程/异步/数据库
- ✗ 不把 content hash 当成 prefix cache key（`hash_ids` 是已预计算的 block hash 链，仿真器直接使用）
- ✗ 不用 `request_hit_rate` 作为唯一结论（必须同时报告 `gap_closed_ratio`）
- ✗ 不写不可测试的大脚本
- ✗ 不跳过测试
- ✗ 不擅自扩展到 non-prefix cache（Phase 1 仅处理 prefix cache）
- ✗ 不在 Phase 1 实现 Hard Protected 单独分区、demotion 机制、长输入防护预算

---

## 9. Phase 1 MVP 交付范围

### 必须交付

- [ ] `LRUPolicy`
- [ ] `TTLLRUPolicy`
- [ ] `TwoQueueTTLPolicy`（Probation + Protected，无 demotion）
- [ ] `InfiniteCachePolicy`（`ideal_hit_rate` 基准，忽略 capacity 上限）
- [ ] `OfflineRegistry`（支持 Two-Queue TTL 的 Protected 直入）
- [ ] 所有 §4.1 基础指标
- [ ] `gap_closed_ratio`（§4.2）
- [ ] 两阶段指标架构（`precompute_future_access` + `finalize`）
- [ ] §3.1 + §3.2 + §3.3 测试全部通过
- [ ] CLI（`scripts/run_simulation.py`）支持单策略运行和多策略对比输出

### Phase 2 功能（本阶段不实现）

- Hard Protected 单独分区（当前 Protected 兼任）
- Demotion（Protected → Probation 降级）
- 长输入 protected eviction budget
- Belady Oracle
- Warm-up 机制
- 多节点 routing-aware replay
