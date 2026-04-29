# Phase 1 实现计划：LRU / TTL-LRU / Two-Queue TTL

**版本**：v2.0（重新设计版）
**日期**：2026-04-28
**状态**：审阅中，待执行

---

## A. 对原始计划的审阅

### 严重缺陷（需修正）

| 编号 | 问题 | 影响 |
|---|---|---|
| S1 | 原计划遗漏 TTL-LRU 策略 | 无法隔离 TTL 贡献，Two-Queue TTL 收益无法归因 |
| S2 | 无 prefix-path key 语义设计 | hash_ids[:i+1] 的前缀累积键未定义，直接使用 hash_ids[i] 会导致误命中 |
| S3 | FutureIndex 混入 MetricsCollector | 职责不清，future_index 应独立模块，可单独测试 |

### 设计缺陷（需改进）

| 编号 | 问题 | 影响 |
|---|---|---|
| D1 | registry.py 混入 Phase 1 | Protected 直入逻辑依赖生产注册表，Phase 1 无法单独验证 |
| D2 | 无 YAML 配置层 | 超参数硬编码，无法批量实验 |
| D3 | 未规定 toy trace 5 个必要条件 | 测试可能虚过（不覆盖边界场景） |
| D4 | InfiniteCachePolicy 在 Phase 1 | gap_closed_ratio 需要 3 个策略联动计算，Phase 1 仅 2 个策略时意义不大 |
| D5 | 事件模型缺失 | PromotionEvent 等作为 side-channel 未单独抽象，污染 policy 接口 |

---

## B. 修订后的 Phase 1 范围

### 目标（必须交付）

1. **核心数据模型**：`TraceRecord`, `BlockMeta`, `BlockQueue`，prefix-path key 函数
2. **Trace IO**：CSV / JSONL 加载，FutureIndex 预计算（独立模块）
3. **PrefixCache 引擎**：prefix 语义 + pinned 机制 + 容量管理
4. **三个淘汰策略**：LRU baseline、TTL-LRU（隔离 TTL 贡献）、Two-Queue TTL MVP
5. **事件模型**：HitEvent、MissEvent、EvictionEvent、PromotionEvent
6. **指标系统**：MetricsCollector（两阶段架构）+ MetricsSummary
7. **实验配置**：YAML-driven ExperimentConfig + ExperimentRunner + CLI
8. **测试套件**：8 个单元测试文件 + 2 个集成测试文件，全部绿灯
9. **Toy Trace**：手工推导所有策略的预期值，写入测试注释

### 非目标（明确推迟到 Phase 2）

- `registry.py` / `OfflineRegistry`（Protected 直入逻辑）
- Hard Protected 单独分区
- Demotion（Protected → Probation 降级）
- Warm-up 机制
- Belady Oracle
- `InfiniteCachePolicy` 和 `gap_closed_ratio`
- 长输入 protected eviction budget
- priority_score 加权淘汰

---

## C. 文件结构（目标状态）

```
two_queue_ttl/
├── pyproject.toml
├── configs/
│   ├── phase1_lru.yaml
│   ├── phase1_ttl_lru.yaml
│   └── phase1_two_queue_ttl.yaml
├── sim/
│   ├── core/
│   │   ├── block.py          — BlockMeta, BlockQueue（无 PolicyEvents）
│   │   ├── trace.py          — TraceRecord
│   │   ├── events.py         — HitEvent, MissEvent, EvictionEvent, PromotionEvent
│   │   └── prefix_key.py     — make_prefix_path_keys(model_id, hash_ids) -> List[str]
│   ├── policies/
│   │   ├── __init__.py       — POLICY_REGISTRY
│   │   ├── base.py           — AbstractCachePolicy ABC
│   │   ├── lru.py            — LRUPolicy
│   │   ├── ttl_lru.py        — TTLLRUPolicy
│   │   └── two_queue_ttl.py  — TwoQueueTTLPolicy
│   ├── cache/
│   │   └── prefix_cache.py   — PrefixCache + RequestResult
│   ├── io/
│   │   └── trace_loader.py   — load_trace(path) -> List[TraceRecord]
│   ├── analysis/
│   │   └── future_index.py   — FutureIndex（预计算 + bisect 查询）
│   ├── metrics/
│   │   ├── collector.py      — MetricsCollector（两阶段：replay + finalize）
│   │   └── summary.py        — MetricsSummary dataclass + 格式化输出
│   ├── simulator.py          — SimulationEngine（组装所有模块）
│   ├── experiment.py         — ExperimentRunner（多配置批量运行）
│   └── config.py             — SimConfig, TwoQueueTTLConfig, ExperimentConfig
├── scripts/
│   └── run_phase1.py         — CLI 入口（argparse → ExperimentRunner）
└── tests/
    ├── fixtures/
    │   └── toy_trace.csv     — 手工设计的 7 行 toy trace
    ├── unit/
    │   ├── test_prefix_key.py
    │   ├── test_trace_loader.py
    │   ├── test_future_index.py
    │   ├── test_prefix_cache.py
    │   ├── test_lru_policy.py
    │   ├── test_ttl_lru_policy.py
    │   ├── test_two_queue_ttl_policy.py
    │   └── test_metrics_collector.py
    └── integration/
        ├── test_toy_trace_single.py   — 单策略 toy trace 手工验证
        └── test_toy_trace_compare.py  — 多策略对比 + gap_closed_ratio（Phase 2 时激活）
```

### 与原计划的文件差异

| 变化 | 说明 |
|---|---|
| 新增 `sim/core/prefix_key.py` | 集中定义 prefix-path key 逻辑，避免分散计算 |
| 新增 `sim/core/events.py` | 将 PolicyEvents 拆分为具体事件类型，便于类型检查 |
| 新增 `sim/analysis/future_index.py` | 从 MetricsCollector 中分离，独立可测 |
| 新增 `sim/simulator.py` | 组装层，隔离 PrefixCache 和 MetricsCollector 的耦合 |
| 新增 `sim/experiment.py` | 多配置批量运行，支持对比实验 |
| 新增 `configs/*.yaml` | YAML-driven 超参数管理 |
| 删除 `sim/io/registry.py` | 推迟到 Phase 2 |
| 删除 `sim/policies/infinite.py` | gap_closed_ratio 推迟到 Phase 2 |

---

## D. 七个里程碑（含验收标准）

### Milestone 1：核心数据模型 + prefix_key

**目标文件**：`sim/core/block.py`、`sim/core/trace.py`、`sim/core/events.py`、`sim/core/prefix_key.py`

**核心设计：prefix-path key**

```python
# sim/core/prefix_key.py
def make_prefix_path_keys(model_id: str, hash_ids: List[str]) -> List[str]:
    """
    为 hash_ids 链生成累积前缀 key 列表。
    Ki = f"{model_id}:{':'.join(hash_ids[:i+1])}"
    长度为 len(hash_ids)，第 i 个 key 代表前 i+1 个 block 的前缀路径。

    为什么用累积前缀而非单个 hash_id：
    不同请求可能共享相同的 hash_ids[i] 值，但前缀路径不同；
    使用累积路径 key 避免跨请求的误命中（false sharing）。
    """
```

**事件模型**：

```python
# sim/core/events.py
@dataclass
class HitEvent:
    key: str
    timestamp: float
    block_pos: int
    queue: Optional[BlockQueue]   # None for LRU (no queue concept)

@dataclass
class MissEvent:
    key: str
    timestamp: float
    block_pos: int

@dataclass
class EvictionEvent:
    key: str
    timestamp: float
    hit_count: int
    queue: Optional[BlockQueue]
    reason: str = "lru"   # "lru" | "ttl_expiry" | "demotion"

@dataclass
class PromotionEvent:
    key: str
    timestamp: float
    hit_count: int
```

**验收标准**：

- `make_prefix_path_keys("m", ["a","b","c"])` == `["m:a", "m:a:b", "m:a:b:c"]`
- `TraceRecord` 可正确解析 hash_ids 为 `List[str]`
- `BlockMeta` 可从 `EvictionEvent` 中恢复完整信息

---

### Milestone 2：Trace Loader + FutureIndex

**目标文件**：`sim/io/trace_loader.py`、`sim/analysis/future_index.py`

**FutureIndex 设计**：

```python
# sim/analysis/future_index.py
class FutureIndex:
    """
    预计算每个 prefix-path key 的未来访问时间戳列表（升序排序）。
    支持 O(log N) 查询：在 after_time 之后是否还有访问。

    建立方式：
      1. 遍历 trace，对每个请求展开 make_prefix_path_keys
      2. 将 (key, timestamp) 追加到 _index[key] 列表
      3. 不需要排序（trace 按时间顺序读入）

    查询方式：
      has_future_access(key, after_time) -> bool
      使用 bisect_right(timestamps, after_time) < len(timestamps)
    """
    def build(self, trace: List[TraceRecord]) -> None: ...
    def has_future_access(self, key: str, after_time: float) -> bool: ...
    def next_access_time(self, key: str, after_time: float) -> Optional[float]: ...
```

**验收标准**：

- 空 trace → `has_future_access` 对所有 key 返回 False
- 单条记录 → 时间戳之前有未来访问，之后无
- 多条相同 key → bisect_right 正确跳过已过去的访问
- `build()` 时间复杂度 O(N × L)，N=请求数，L=平均 hash_ids 长度

---

### Milestone 3：PrefixCache 容器

**目标文件**：`sim/cache/prefix_cache.py`

**核心语义**：

```python
# sim/cache/prefix_cache.py
@dataclass
class RequestResult:
    keys: List[str]           # 本次请求生成的所有 prefix-path key
    hit_keys: List[str]       # 命中的 key（连续前缀段）
    miss_keys: List[str]      # 未命中的 key（第一个 miss 后全部算 miss）
    evicted: List[EvictionEvent]  # 本次请求触发的淘汰事件

class PrefixCache:
    """
    Prefix cache 仿真引擎。

    前缀语义：
    - 命中链在第一个 miss 处断开，后续全部算 miss（即使 cache 中存在）
    - miss block 需要分配容量（可能触发 eviction）
    - 分配时，当前请求已处理的 block（hit + 已分配 miss）加入 pinned 集合
    """
    def process_request(self, record: TraceRecord, timestamp: float) -> RequestResult: ...
```

**pinned 机制细节**：

```
pinned = set()
for key in keys:
    if policy.contains(key):
        hit → pinned.add(key)
        # 第一个 miss 后停止 hit 检查
    else:
        miss → 如果 cache 满，evict_one(pinned)
             → policy.add(key, ...)
             → pinned.add(key)  ← 防止后续 eviction 把刚分配的 block 淘汰掉
```

**验收标准**：

- 完整 prefix hit（第二次相同请求）→ `miss_keys` 为空
- 部分 prefix hit → `hit_keys` 为连续前缀，`miss_keys` 从第一个 miss 开始
- cache 满时分配触发 eviction，evicted 不在 pinned 集合中
- 空 hash_ids → `RequestResult(keys=[], hit_keys=[], miss_keys=[], evicted=[])`

---

### Milestone 4：三个淘汰策略

**目标文件**：`sim/policies/base.py`、`sim/policies/lru.py`、`sim/policies/ttl_lru.py`、`sim/policies/two_queue_ttl.py`

**AbstractCachePolicy 接口**（v2.0，使用 events.py 类型）：

```python
class AbstractCachePolicy(ABC):
    def contains(self, key: str) -> bool: ...
    def access(self, key: str, timestamp: float, block_pos: int, user_id: str) -> Optional[HitEvent]: ...
    def add(self, key: str, timestamp: float, block_pos: int, user_id: str) -> None: ...
    def evict_one(self, timestamp: float, pinned: Optional[Set[str]] = None) -> Optional[EvictionEvent]: ...
    def flush_promotions(self) -> List[PromotionEvent]: ...
    def size(self) -> int: ...
```

**LRUPolicy**：

- 使用 `OrderedDict` 维护 LRU 顺序
- `access` → `move_to_end()`，返回 `HitEvent(queue=None)`
- `evict_one` → 从头部迭代，跳过 pinned，返回 `EvictionEvent`

**TTLLRUPolicy**（TTL-LRU，隔离 TTL 贡献）：

- 继承 LRU 逻辑，每个 block 记录 `ttl_expiry`
- `access` 时：若 `timestamp > ttl_expiry` → 删除 block，视为 **miss**（返回 None，不计入 hit）
- `access` 命中（未过期）后：刷新 TTL（`ttl_expiry = timestamp + ttl`）
- `evict_one`：优先淘汰 TTL 已过期的 block（`reason="ttl_expiry"`），其次 LRU 头部（`reason="lru"`）

  ```
  evict_one 优先级：
  1. 扫描找最早过期的已过期 block（O(n)，可接受）→ reason="ttl_expiry"
  2. 若无过期 block，从 LRU 头部找第一个非 pinned   → reason="lru"
  ```

  > **TTL-LRU 语义约定（已确认）**：
  > - `timestamp > ttl_expiry` → 过期（严格大于，等于视为有效）
  > - 过期 block 被 access = **miss**（删除后重新插入），不是 hit+刷新
  > - 语义：过期 block 相当于"cache 中不存在"，TTL-LRU 将 TTL 作为"有效期"的硬边界
  >
  > **TTL 边界测试（单元测试级别，非 toy trace 级别）**：
  > 在 `test_ttl_lru_policy.py` 中专门设计 `timestamp == ttl_expiry` 的测试用例，
  > 验证边界行为是 hit（严格大于判断下，相等时不过期）。

**TwoQueueTTLPolicy**（Two-Queue TTL MVP，无 demotion）：

- 双 `OrderedDict`：`_probation` + `_protected`
- 新 block 进 Probation（无 TTL）
- `access` Probation block：`hit_count += 1`，达到 `promotion_threshold` 时晋升 Protected
- 晋升时设置 `ttl_expiry`：`hit_count >= extended_hit_threshold` → `extended_ttl`，否则 `base_ttl`
- `access` Protected block（**无论是否过期**）：**仍然返回 HitEvent**，并刷新 TTL
- `evict_one` 优先级：
  1. Probation LRU 头部（非 pinned） → `reason="lru"`
  2. Protected TTL 已过期，最早过期的（非 pinned） → `reason="ttl_expiry"`
  3. Protected LRU 头部（非 pinned） → `reason="lru"`
- `flush_promotions()` 返回 `_pending_promotions` 并清空

  > **TQ-TTL 与 TTL-LRU 的关键语义差异（实验对比时必须标注）**：
  >
  > | 场景 | TTL-LRU | Two-Queue TTL |
  > |---|---|---|
  > | `access` 过期 block | **miss**（删除重新插入） | **hit**（刷新 TTL，block 继续留在 Protected） |
  > | 过期后淘汰时机 | 下次 access 时立即删除 或 evict 时优先淘汰 | 仅在 evict 压力下才被淘汰（软保护） |
  > | 过期的含义 | "有效期结束，立即失效" | "保护期结束，降低淘汰优先级" |
  >
  > 这一差异会使两策略在"TTL 过期后访问"场景的命中率出现系统性差异，
  > **不能将该差异归因于队列分层的效果**，必须在实验报告中单独标注。
  >
  > **`max_idle_after_expiry` 说明**：
  > 算法设计文档提到 Protected block 在 TTL 过期后经过 `max_idle_after_expiry`（60s）无命中
  > 才允许正常淘汰。Phase 1 MVP 暂不实现此计时器，仅以 eviction 优先级（TTL 过期的
  > Protected 优先于未过期的）作为近似。Phase 2 实现 demotion 时再引入精确计时器。
  >
  > **重要语义**：Phase 1 不实现 demotion。Protected 超容量时通过 evict 优先级
  > 保证 Probation 先于 Protected 被淘汰，而非强制 Protected 降级。

**验收标准**：

- LRU：`evict_one` 遵守 LRU 顺序，pinned 集合被跳过
- TTL-LRU：过期 block access 返回 None（miss），`timestamp == ttl_expiry` 时不过期（hit），优先淘汰过期 block
- TQ-TTL：过期 Protected block access 返回 HitEvent（非 miss），eviction 优先级正确，flush_promotions 刷新后清空

---

### Milestone 5：SimulationEngine + MetricsCollector

**目标文件**：`sim/simulator.py`、`sim/metrics/collector.py`、`sim/metrics/summary.py`

**SimulationEngine**（组装层）：

```python
class SimulationEngine:
    """
    组装 PrefixCache + MetricsCollector，执行单次 replay。
    职责：
    1. 调用 FutureIndex.build(trace)
    2. 逐条处理 TraceRecord → RequestResult
    3. 将 RequestResult 中的事件传递给 MetricsCollector
    4. replay 结束后调用 collector.finalize()
    5. 返回 MetricsSummary
    """
    def run(self, trace: List[TraceRecord]) -> MetricsSummary: ...
```

**MetricsCollector（两阶段架构）**：

```python
class MetricsCollector:
    def __init__(self, future_index: FutureIndex, block_size: int): ...

    # 阶段 2（replay 中）
    def on_request(self, result: RequestResult, timestamp: float) -> None:
        """
        记录 hit/miss 计数，记录 EvictionEvent（含 timestamp）。
        不在此处查询 FutureIndex（replay 中不应有后向信息查询）。
        """

    # 阶段 3（replay 后）
    def finalize(self) -> MetricsSummary:
        """
        对每个 EvictionEvent，用 bisect_right 查询 FutureIndex，
        确定 eviction_time 之后是否有未来访问。
        计算 evicted_before_next_hit_count 和 protected_pollution_rate。
        """
```

**MetricsSummary**（不可变 dataclass）：

```python
@dataclass(frozen=True)
class MetricsSummary:
    # 基础计数
    total_requests: int
    total_blocks_requested: int
    total_blocks_hit: int
    total_blocks_miss: int
    eviction_count: int            # 总淘汰次数 = lru + ttl_expiry + demotion
    eviction_count_lru: int        # 因 LRU 容量压力淘汰
    eviction_count_ttl_expiry: int # 因 TTL 过期淘汰（TTL-LRU 诊断用）
    eviction_count_demotion: int   # 因降级淘汰（Phase 2，Phase 1 恒为 0）
    promotion_count: int

    # 质量指标
    hot_prefix_eviction_count: int      # 被淘汰时 hit_count >= 2 的 block 数
    evicted_before_next_hit_count: int  # 被淘汰且后续还有访问的 block 数
    protected_eviction_count: int
    probation_eviction_count: int
    protected_pollution_count: int      # Protected 淘汰中入队后从未再命中的 block 数

    # 派生属性（@property）
    @property
    def prefix_block_hit_rate(self) -> float:
        return self.total_blocks_hit / self.total_blocks_requested if self.total_blocks_requested > 0 else 0.0

    @property
    def saved_prefill_tokens(self) -> int:
        return self.total_blocks_hit * self.block_size

    @property
    def protected_pollution_rate(self) -> float:
        return self.protected_pollution_count / self.protected_eviction_count if self.protected_eviction_count > 0 else 0.0
```

**验收标准**：

- `total_blocks_hit + total_blocks_miss == total_blocks_requested`（不变式）
- `evicted_before_next_hit_count > 0` 在设计好的 toy trace 上
- 零除保护：无请求时 `prefix_block_hit_rate == 0.0`，无 Protected 淘汰时 `protected_pollution_rate == 0.0`

---

### Milestone 6：YAML Config + ExperimentRunner + CLI

**目标文件**：`sim/config.py`、`sim/experiment.py`、`scripts/run_phase1.py`、`configs/*.yaml`

**Config 层次**：

```python
# sim/config.py
@dataclass
class TwoQueueTTLConfig:
    promotion_threshold: int = 2
    base_ttl: float = 3600.0
    extended_ttl: float = 86400.0
    extended_hit_threshold: int = 5

@dataclass
class TTLLRUConfig:
    ttl: float = 3600.0

@dataclass
class CacheConfig:
    capacity: int = 1000
    block_size: int = 16
    policy: str = "lru"          # "lru" | "ttl_lru" | "two_queue_ttl"
    ttl_lru: TTLLRUConfig = field(default_factory=TTLLRUConfig)
    two_queue_ttl: TwoQueueTTLConfig = field(default_factory=TwoQueueTTLConfig)

@dataclass
class ExperimentConfig:
    name: str
    trace_path: str
    output_dir: str
    policies: List[CacheConfig]
    description: str = ""
```

**YAML 示例**（`configs/phase1_two_queue_ttl.yaml`）：

```yaml
name: phase1_two_queue_ttl
trace_path: tests/fixtures/toy_trace.csv
output_dir: outputs/phase1_two_queue_ttl
description: Two-Queue TTL MVP 验证实验

policies:
  - policy: lru
    capacity: 4
    block_size: 1

  - policy: ttl_lru
    capacity: 4
    block_size: 1
    ttl_lru:
      ttl: 5.0

  - policy: two_queue_ttl
    capacity: 4
    block_size: 1
    two_queue_ttl:
      promotion_threshold: 2
      base_ttl: 10.0
      extended_ttl: 100.0
      extended_hit_threshold: 5
```

**CLI**（`scripts/run_phase1.py`）：

```
usage: run_phase1.py [-h] [--config CONFIG] [--output OUTPUT]
                     [--policy {lru,ttl_lru,two_queue_ttl}]
                     [--trace TRACE] [--capacity CAPACITY]

示例：
  # 使用 YAML 配置文件
  python scripts/run_phase1.py --config configs/phase1_two_queue_ttl.yaml

  # 直接指定参数（覆盖 YAML 中的 policy）
  python scripts/run_phase1.py --config configs/phase1_two_queue_ttl.yaml \
      --policy two_queue_ttl --capacity 100
```

**验收标准**：

- CLI 可以加载 YAML 文件并运行实验
- 输出目录包含 `summary.json` 和 `metrics_table.txt`
- 不同 policy 的 `total_blocks_requested` 相同（来自同一 trace）

---

### Milestone 7：集成测试 + Toy Trace 验证 + README

**目标文件**：`tests/fixtures/toy_trace.csv`、`tests/integration/test_toy_trace_single.py`、`tests/integration/test_toy_trace_compare.py`

（详见 §E Toy Trace 设计 + §F 测试矩阵）

---

## E. Toy Trace 设计（7 行，手工可追踪）

### 设计原则

Toy trace 必须同时满足以下 5 个条件（否则测试虚过）：

1. 至少 1 次 prefix **完整命中**（验证 hit 统计）
2. 至少 1 次 prefix **部分命中**（验证前缀语义）
3. 至少 1 次 **eviction**（需要 cache capacity < 工作集大小）
4. 至少 1 个 block 被淘汰后在**后续请求中再次出现**（触发 `evicted_before_next_hit > 0`）
5. 至少 1 个 block 在 Two-Queue TTL 下发生**晋升**（hit_count 达到 promotion_threshold）

**TTL 场景附加条件**（Phase 1 特有，缺一不可）：

6. **场景 A（TTL 内复用）**：某次访问与上次插入/刷新的时间差 < TTL → TTL-LRU 应 hit，TTL 刷新
7. **场景 B（TTL 过期后访问）**：某次访问时 block 仍在 cache 但 TTL 已过期 → TTL-LRU 视为 miss，LRU 视为 hit，两策略命中率产生系统性差异
8. **TTL 边界行为**：`timestamp == ttl_expiry` 的精确边界测试放在单元测试 `test_ttl_lru_policy.py` 中，不要求 toy trace 覆盖（toy trace 时间戳设计中有意错开精确边界，避免浮点数引起歧义）
9. 请求数 7-10 行，人工可逐步追踪

> **说明**：场景 B 是区分 TTL-LRU 与 LRU 的关键机制测试。Toy trace 需明确一个时间段，
> 使某些 block 在 LRU 下因 LRU 压力不足而"幸存"（仍在 cache），
> 但在 TTL-LRU 下因 TTL 过期被视为 miss。这要求 cache 容量相对宽松（让 block 不被 LRU 淘汰），
> 同时时间间隔超过 TTL（让 block 过期）。当前 toy trace 的 R4→R5（gap=5.1s > TTL=5s）满足此条件。

### Toy Trace 内容

**参数设定**：capacity=4, block_size=1, TTL=5.0s, promotion_threshold=2

```csv
timestamp,model_id,user_id,request_type,input_length,hash_ids,output_length,turn
1.0,m1,u1,prefill,3,A|B|C,0,0
2.0,m1,u2,prefill,2,D|E,0,0
3.0,m1,u1,prefill,3,A|B|C,0,0
4.0,m1,u1,prefill,4,A|B|C|F,0,0
9.1,m1,u3,prefill,2,A|B,0,0
10.0,m1,u2,prefill,2,D|E,0,0
11.0,m1,u1,prefill,3,A|B|C,0,0
```

**TTL 场景覆盖映射**：

| 请求 | 时间戳 | 与上次插入的间隔 | 覆盖场景 |
|---|---|---|---|
| R3 (t=3.0) | gap=2s < TTL=5s | R1 插入 A/B/C (t=1.0) | 场景 A（TTL 内复用）—— 但 LRU 已驱逐 A，所以变成 miss；体现在 R4 |
| R4→R5 (t=4.0→9.1) | gap=5.1s > TTL=5s | R4 最后命中/插入 (t=4.0) | **场景 B（TTL 过期后访问）** — LRU hit，TTL-LRU miss，两策略产生分叉 |
| R5→R7 (t=9.1→11.0) | gap=1.9s < TTL=5s | R5 刷新 TTL (t=9.1) | 场景 A（TTL 内复用）— TQ-TTL Protected 命中，TTL 刷新 |

> **注**：R4→R5 的 gap=5.1s 有意设计为刚好超过 TTL=5s，使 R4 结束时 cache 中的 {A,B,C,F}
> 全部过期（TTL_expiry=4+5=9.0 < 9.1），同时这些 block 未被 LRU 驱逐（capacity=4 刚好放得下）。
> 这是 TTL-LRU 与 LRU 产生行为分叉的关键时刻。
>
> **TTL 边界精确测试**：`timestamp == ttl_expiry` 的边界验证（gap 恰好=5.0s 时是 hit 还是 miss）
> 放在 `test_ttl_lru_policy.py` 单元测试中单独覆盖，不通过 toy trace 实现，
> 以避免 CSV 浮点精度问题引入歧义。

**prefix-path key 映射**（model_id=m1）：

```
hash_ids [A,B,C]    → keys [m1:A, m1:A:B, m1:A:B:C]
hash_ids [D,E]      → keys [m1:D, m1:D:E]
hash_ids [A,B,C]    → keys [m1:A, m1:A:B, m1:A:B:C]
hash_ids [A,B,C,F]  → keys [m1:A, m1:A:B, m1:A:B:C, m1:A:B:C:F]
hash_ids [A,B]      → keys [m1:A, m1:A:B]
hash_ids [D,E]      → keys [m1:D, m1:D:E]
hash_ids [A,B,C]    → keys [m1:A, m1:A:B, m1:A:B:C]
```

---

### LRU 策略手工推导（capacity=4）

**R1 (t=1.0)** `[A,B,C]` → 全 miss，add A,B,C → cache: {A,B,C}

**R2 (t=2.0)** `[D,E]` → D miss, add D → cache: {A,B,C,D}（满）
→ E miss，evict LRU=A，add E → cache: {B,C,D,E}

**R3 (t=3.0)** `[A,B,C]` → A miss（stop），add A（evict LRU=B）→ cache: {C,D,E,A}
→ B,C 算 miss（第一个 miss 后停止 hit 检查）

  > 注：R3 命中 0 个，miss 3 个（prefix 语义：第一个 miss 后全算 miss）

  更正：R3 时 cache 为 {B,C,D,E}（LRU 顺序 B→C→D→E，B 最老）
  - key m1:A → miss（B 最老，evict B）→ add A → cache: {C,D,E,A}
  - key m1:A:B → miss（第一个 miss 后 stop）→ 不再检查 m1:A:B:C
  - hit_count: 0，miss_count: 3

**R4 (t=4.0)** `[A,B,C,F]` → cache: {C,D,E,A}（LRU 顺序 C→D→E→A）
  - m1:A → **hit**（A 在 cache，move_to_end）→ pinned={A}
  - m1:A:B → miss（B 不在 cache）→ evict LRU=C → add B → cache: {D,E,A,B}
  - m1:A:B:C → miss → evict LRU=D → add C → cache: {E,A,B,C}
  - m1:A:B:C:F → miss → evict LRU=E → add F → cache: {A,B,C,F}
  - hits: 1，miss: 3

**R5 (t=9.1)** `[A,B]` → cache: {A,B,C,F}（LRU 顺序 A→B→C→F）
  - m1:A → **hit** → pinned={A}
  - m1:A:B → **hit** → pinned={A,B}
  - hits: 2，miss: 0

**R6 (t=10.0)** `[D,E]` → cache: {A,B,C,F}（LRU 顺序 C→F→A→B）
  - m1:D → miss → evict LRU=C → add D → cache: {F,A,B,D}
  - m1:D:E → miss → evict LRU=F → add E → cache: {A,B,D,E}
  - hits: 0，miss: 2

**R7 (t=11.0)** `[A,B,C]` → cache: {A,B,D,E}（LRU 顺序 A→B→D→E）
  - m1:A → **hit** → pinned={A}
  - m1:A:B → **hit** → pinned={A,B}
  - m1:A:B:C → miss → evict LRU=D → add C → cache: {E,A,B,C}
  - hits: 2，miss: 1

**LRU 汇总**：

```python
expected_lru = {
    "total_requests": 7,
    "total_blocks_requested": 3+2+3+4+2+2+3,  # = 19
    "total_blocks_hit": 0+0+0+1+2+0+2,         # = 5
    "total_blocks_miss": 19 - 5,                # = 14
    "prefix_block_hit_rate": 5/19,              # ≈ 0.2632
    "eviction_count": 0+1+1+3+0+2+1,           # = 8
    # evicted_before_next_hit: 需要追踪被淘汰的 block 是否后续再现
    # R2 evict A: A 在 R3,R4,R5,R7 再现 → +1
    # R3 evict B: B 在 R4,R7 再现 → +1
    # R4 evict C: C 在 R7 再现 → +1
    # R4 evict D: D 在 R6 再现 → +1
    # R4 evict E: E 在 R6 再现 → +1
    # R6 evict C: C 在 R7 再现 → +1
    # R6 evict F: F 无后续 → 0
    # R7 evict D: D 无后续 → 0（R7 是最后一条）
    "evicted_before_next_hit_count": 6,
}
```

---

### TTL-LRU 策略手工推导（capacity=4, TTL=5.0s）

TTL-LRU 与 LRU 的主要区别：
- 过期 block 被 access 时视为 miss（删除，重新插入）
- evict_one 优先选过期 block

**R1-R4** 与 LRU 相同（TTL 尚未过期）

进入 R5 (t=9.1) 时，R1 中插入的 block 的 TTL 情况：
- A 在 R4(t=4) 被 hit，TTL = 4.0 + 5.0 = 9.0 < 9.1 → **A 已过期**
- B 在 R4(t=4) 被 add，entry_time=4, TTL=9.0 < 9.1 → **B 已过期**
- C 在 R4(t=4) 被 add，entry_time=4, TTL=9.0 < 9.1 → **C 已过期**
- F 在 R4(t=4) 被 add，entry_time=4, TTL=9.0 < 9.1 → **F 已过期**

  > cache: {A,B,C,F} 全部过期

**R5 (t=9.1)** `[A,B]`：
  - m1:A → 在 cache 但已过期 → **miss**（删除 A，重新插入）
    → evict_one 找过期 block（B 过期）→ evict B → add A（TTL=14.1）
  - m1:A:B → B 不在 cache → **miss** → evict_one 找过期（C 过期）→ evict C → add B（TTL=14.1）
  - hits: 0，miss: 2

**R6 (t=10.0)** `[D,E]` → cache: {F,A,B}（F 过期，A/B TTL=14.1>10）
  - m1:D → miss → evict_one 优先过期（F 过期）→ evict F → add D（TTL=15.0）
  - m1:D:E → miss → evict_one（无过期，LRU=A）→ evict A → add E（TTL=15.0）
  - hits: 0，miss: 2

**R7 (t=11.0)** `[A,B,C]` → cache: {B,D,E}（TTL 均>11）
  - m1:A → miss → evict LRU=B → add A → cache: {D,E,A}
  - m1:A:B → miss（stop）
  - hits: 0，miss: 3

**TTL-LRU 汇总**：

```python
expected_ttl_lru = {
    "total_requests": 7,
    "total_blocks_requested": 19,   # 与 LRU 相同
    "total_blocks_hit": 0+0+0+1+0+0+0,  # = 1（仅 R4 的 m1:A）
    "total_blocks_miss": 18,
    "prefix_block_hit_rate": 1/19,  # ≈ 0.0526（远低于 LRU 的 5/19，体现 TTL 过期的代价）
    # 以下为 eviction 子计数（场景 B 关键验证）：
    # R5: evict B（TTL 过期）→ ttl_expiry，evict C（TTL 过期）→ ttl_expiry
    # R6: evict F（TTL 过期）→ ttl_expiry，evict A（LRU 压力）→ lru
    # R2,R3: LRU 压力各 1 次；R4: LRU 压力 3 次；R7: LRU 压力 1 次
    "eviction_count_total": 0+1+1+3+2+2+1,   # = 10
    "eviction_count_lru": 0+1+1+3+0+1+1,     # = 7（容量压力驱逐）
    "eviction_count_ttl_expiry": 0+0+0+0+2+1+0,  # = 3（TTL 过期驱逐）
    # 诊断问题：ttl_expiry 占比 = 3/10 = 30%，说明 TTL 是较大瓶颈
}
```

> **场景 B 验证点**：对比 LRU 和 TTL-LRU 在 R5（t=9.1）的行为：
> - LRU：A 在 cache 且未过期（LRU 不关心 TTL）→ hit
> - TTL-LRU：A 在 cache 但 TTL_expiry=9.0 < 9.1 → **miss**
> 这一分叉使 TTL-LRU 的 total_blocks_hit 从 5 降到 1，降幅显著。

---

### Two-Queue TTL 策略手工推导（capacity=4, TTL base=10s, ext=100s, threshold=2）

**R1 (t=1.0)** `[A,B,C]` → 全 miss，add 到 Probation
→ Probation: {A(h=0),B(h=0),C(h=0)}，Protected: {}

**R2 (t=2.0)** `[D,E]` → D miss，add → Probation: {A,B,C,D}（满）
→ E miss，evict Probation LRU=A → add E
→ Probation: {B,C,D,E}，Protected: {}

**R3 (t=3.0)** `[A,B,C]` → A miss（stop），add A（evict Probation LRU=B）
→ Probation: {C,D,E,A}，Protected: {}
→ hits: 0，miss: 3

**R4 (t=4.0)** `[A,B,C,F]` → cache: Prob={C,D,E,A}
  - m1:A → **hit Probation**（A.hit_count 1→2 = threshold）→ **晋升 Protected**！
    → A 进 Protected（TTL=4+10=14），Prob={C,D,E}
    → PromotionEvent: key=m1:A, t=4.0, hit_count=2
  - m1:A:B → miss，evict Probation LRU=C → add B
    → Prob={D,E,B}，Protected={A}
  - m1:A:B:C → miss，evict Probation LRU=D → add C
    → Prob={E,B,C}，Protected={A}
  - m1:A:B:C:F → miss，evict Probation LRU=E → add F
    → Prob={B,C,F}，Protected={A}
  - hits: 1，miss: 3，promotions: 1

**R5 (t=9.1)** `[A,B]` → Prob={B,C,F}，Protected={A(TTL=14)}
  - m1:A → **hit Protected**（TTL 14>9.1，刷新 TTL=9.1+10=19.1）
    → pinned={m1:A}
  - m1:A:B → **hit Probation**（B.hit_count 0→1）→ 未达 threshold，不晋升
    → pinned={m1:A,m1:A:B}
  - hits: 2，miss: 0

**R6 (t=10.0)** `[D,E]` → Prob={C,F,B}（B 被 access 更新 LRU），Protected={A(TTL=19.1)}
  - m1:D → miss，evict Probation LRU=C → add D
    → Prob={F,B,D}，Protected={A}
  - m1:D:E → miss，evict Probation LRU=F → add E
    → Prob={B,D,E}，Protected={A}
  - hits: 0，miss: 2

**R7 (t=11.0)** `[A,B,C]` → Prob={B,D,E}，Protected={A(TTL=19.1)}
  - m1:A → **hit Protected**（TTL 19.1>11，刷新 TTL=11+10=21）
    → pinned={m1:A}
  - m1:A:B → **hit Probation**（B.hit_count 1→2 = threshold）→ **晋升 Protected**！
    → B 进 Protected（hit_count=2，TTL=11+10=21），Prob={D,E}
    → Protected={A,B}，total size=4（2 Prob + 2 Protected = 4，仍在 capacity 内）
    → PromotionEvent: key=m1:A:B, t=11.0, hit_count=2
  - m1:A:B:C → miss，evict Probation LRU=D → add C
    → Prob={E,C}，Protected={A,B}
  - hits: 2，miss: 1，promotions: 1

**Two-Queue TTL 汇总**：

```python
expected_two_queue_ttl = {
    "total_requests": 7,
    "total_blocks_requested": 19,
    "total_blocks_hit": 0+0+0+1+2+0+2,    # = 5（与 LRU 相同）
    "total_blocks_miss": 14,
    "prefix_block_hit_rate": 5/19,          # ≈ 0.2632
    "eviction_count": 0+1+1+3+0+2+1,       # = 8
    "promotion_count": 2,                   # R4 和 R7 各 1 次
    "probation_eviction_count": 8,
    "protected_eviction_count": 0,
    # evicted_before_next_hit 类似分析，略
}
```

> 注：本 toy trace 中 Two-Queue TTL 的 prefix_block_hit_rate 与 LRU 相同（5/19），
> 这是正常的——Two-Queue TTL 的优势在于**保护热点 block 不被淘汰**（A 在 Protected），
> 在更大 trace 上才会体现出 hit rate 差异。

---

## F. 测试矩阵

### 单元测试文件

| 文件 | 测试点 |
|---|---|
| `test_prefix_key.py` | 空 hash_ids、单个 hash_id、多个 hash_ids、不同 model_id 隔离 |
| `test_trace_loader.py` | CSV 加载、JSONL 加载、pipe-separated hash_ids、错误格式报错 |
| `test_future_index.py` | 空 trace、单条记录、多条相同 key、has_future_access 边界 |
| `test_prefix_cache.py` | 完整 hit、部分 hit、第一个 miss 后全算 miss、pinned 机制、空 hash_ids |
| `test_lru_policy.py` | 空 cache miss、add→access hit、LRU 顺序、access 刷新顺序、pinned 跳过、全 pinned 返回 None |
| `test_ttl_lru_policy.py` | 过期 block access 返回 None、淘汰优先过期、hit 刷新 TTL、未过期正常 hit |
| `test_two_queue_ttl_policy.py` | 新 block 进 Probation、promotion 时机、promotion 设置 TTL、Protected hit 刷新 TTL、flush_promotions 清空、evict 优先级 |
| `test_metrics_collector.py` | 基础计数不变式、零除保护、evicted_before_next_hit（需设计 mini trace）|

### 集成测试文件

| 文件 | 测试点 |
|---|---|
| `test_toy_trace_single.py` | 对 toy_trace.csv 运行 LRU/TTL-LRU/TQ-TTL，验证手工推导的 expected 值 |
| `test_toy_trace_compare.py` | 同一 trace 运行多策略，验证 `total_blocks_requested` 相同；预留 gap_closed_ratio 位置（Phase 2 激活）|

### 集成测试手工 expected 值（写入测试注释）

```python
# tests/integration/test_toy_trace_single.py
#
# Toy trace 参数：capacity=4, block_size=1, TTL=5.0s
# 手工推导过程见 PHASE1_IMPLEMENTATION.md §E
#
# LRU expected:
expected_lru = {
    "total_requests": 7,
    "total_blocks_requested": 19,
    "total_blocks_hit": 5,
    "prefix_block_hit_rate": 5/19,
    "eviction_count": 8,
    "evicted_before_next_hit_count": 6,
}
#
# Two-Queue TTL expected:
expected_tq = {
    "total_requests": 7,
    "total_blocks_requested": 19,
    "total_blocks_hit": 5,
    "prefix_block_hit_rate": 5/19,
    "promotion_count": 2,
    "probation_eviction_count": 8,
    "protected_eviction_count": 0,
}
```

---

## G. 指标定义（统一口径）

### 基础计数指标

| 指标名 | 定义 | 来源 |
|---|---|---|
| `total_requests` | trace 中的请求总数 | 每条 TraceRecord +1 |
| `total_blocks_requested` | 所有请求的 `len(hash_ids)` 之和 | 每次 process_request |
| `total_blocks_hit` | 命中的 prefix-path key 总数（连续前缀段） | HitEvent 计数 |
| `total_blocks_miss` | 未命中的 prefix-path key 总数 | MissEvent 计数 |
| `eviction_count` | 总淘汰次数（= lru + ttl_expiry + demotion 之和） | EvictionEvent 计数 |
| `eviction_count_lru` | 因 LRU 容量压力被淘汰的 block 数 | `EvictionEvent.reason == "lru"` |
| `eviction_count_ttl_expiry` | 因 TTL 过期被移除的 block 数（TTL-LRU 专用诊断）| `EvictionEvent.reason == "ttl_expiry"` |
| `eviction_count_demotion` | 因降级被淘汰的 block 数（Phase 2 实现，Phase 1 恒为 0）| `EvictionEvent.reason == "demotion"` |
| `promotion_count` | Probation→Protected 晋升次数 | PromotionEvent 计数 |

> **eviction 子计数的用途**：
> - `eviction_count_ttl_expiry / eviction_count` 高（>30%）→ TTL 设置过短，TTL 成为瓶颈
> - `eviction_count_ttl_expiry / eviction_count` 低（<10%）→ LRU 容量压力是主因，TTL 设置合理
> - 跨策略比较时，`eviction_count_total` 口径统一（TTL-LRU 的过期移除和 LRU 的容量驱逐
>   最终效果相同——block 消失，后续访问变 miss），因此计入总数是正确的

### 质量指标

| 指标名 | 定义 | 计算时机 |
|---|---|---|
| `hot_prefix_eviction_count` | 被淘汰时 `hit_count >= 2` 的 block 数 | finalize() |
| `evicted_before_next_hit_count` | 被淘汰且淘汰后 trace 中还有该 key 出现的 block 数 | finalize() + FutureIndex |
| `protected_eviction_count` | 从 Protected 队列被淘汰的次数 | EvictionEvent.queue == PROTECTED |
| `probation_eviction_count` | 从 Probation 队列被淘汰的次数 | EvictionEvent.queue == PROBATION |
| `protected_pollution_count` | Protected 淘汰中"入队后从未再命中"的 block 数 | finalize() + FutureIndex |

### 派生指标

| 指标名 | 公式 | 零除处理 |
|---|---|---|
| `prefix_block_hit_rate` | `total_blocks_hit / total_blocks_requested` | 分母=0 时返回 0.0 |
| `saved_prefill_tokens` | `total_blocks_hit × block_size` | 无零除 |
| `protected_pollution_rate` | `protected_pollution_count / protected_eviction_count` | 分母=0 时返回 0.0 |

### 跨策略指标（Phase 2 实现）

```
ideal_hit_rate   = InfiniteCachePolicy 在同一 trace 上的 prefix_block_hit_rate
gap_closed_ratio = (policy_hit_rate - lru_hit_rate) / (ideal_hit_rate - lru_hit_rate)
                   分母为 0 时返回 0.0
```

> **重要警告**：不要用 `request_hit_rate` 作为唯一结论指标。
> `request_hit_rate = 完整命中的请求数 / 总请求数`，忽略了部分命中的贡献，
> 会低估缓存效果。必须同时报告 `prefix_block_hit_rate` 和 `gap_closed_ratio`。

---

## H. 风险分析

| 编号 | 风险 | 可能性 | 影响 | 缓解方案 |
|---|---|---|---|---|
| R1 | prefix-path key 计算错误（用单个 hash_id 而非累积） | 高 | 严重（统计全部错误） | 在 `prefix_key.py` 中集中定义，单元测试第一个验证 |
| R2 | FutureIndex 使用 O(N²) 暴力扫描 | 中 | 大 trace 上性能崩溃 | 强制要求 bisect_right，单元测试用 10k 条 trace 验证性能 |
| R3 | MetricsCollector 在 replay 中查询 FutureIndex | 低（设计文档已禁止） | 破坏两阶段架构 | SimulationEngine 在 replay 后调用 finalize()，测试中验证时序 |
| R4 | TTL 边界语义歧义（`>` 还是 `>=`）| 高 | 单元测试无法区分两种实现 | 明确约定：`timestamp > ttl_expiry` 时过期（严格大于，等于时不过期）；在 `test_ttl_lru_policy.py` 中增加 `timestamp == ttl_expiry` 的精确边界测试用例 |
| R5 | TQ-TTL 与 TTL-LRU 过期语义混淆（都用 `ttl_expiry` 字段，但行为不同）| 高 | 实验对比结论归因错误 | 在代码注释、实验报告中明确标注：TTL-LRU 过期=miss，TQ-TTL 过期=降低淘汰优先级但仍可 hit |
| R6 | Phase 1 无 demotion → Protected 效果偏乐观 | 中 | gap_closed_ratio 高估，上线后效果不如仿真 | 在报告中注明"乐观估计"；可通过适当收紧 Protected 配额（25% 而非 30%）缓解偏差 |
| R7 | Protected "污染"计算错误 | 中 | 导致 protected_pollution_rate 不可信 | 在 finalize() 中只检查 Protected 淘汰事件，用 FutureIndex 查是否有后续访问 |
| R8 | pinned 集合与 evict 优先级不一致 | 低 | 导致 block 被双重使用 | PrefixCache 测试：分配多个 miss block 时，已分配的不被后续 eviction 淘汰 |
| R9 | toy trace 手工推导计算错误 | 中 | 测试写了错误的 expected，虚假绿灯 | 至少两个人独立推导，或用代码辅助验证；eviction 子计数要分别核对 |
| R10 | YAML schema 变化导致向后不兼容 | 低（Phase 1 只有 3 个 yaml 文件） | 重新生成 YAML | dataclass 提供合理默认值，YAML 只需覆盖非默认值 |

---

## I. 推迟到 Phase 2 的功能（明确边界）

以下功能在 Phase 1 中**不实现**，有明确推迟原因：

| 功能 | 推迟原因 |
|---|---|
| `sim/io/registry.py` / `OfflineRegistry` | Protected 直入需要真实生产注册表，Phase 1 无此数据 |
| Hard Protected 单独分区 | 需要 demotion 机制支撑，Phase 1 MVP 不引入 |
| Demotion（Protected→Probation） | 降级逻辑复杂，Phase 1 通过 eviction 优先级模拟效果 |
| Warm-up 机制 | 需要 registry 支持 |
| Belady Oracle | 需要 FutureIndex 完整集成到 policy 层，Phase 1 仅在 metrics 层使用 |
| `InfiniteCachePolicy` | 需要 gap_closed_ratio 框架，Phase 1 只做单策略和两策略对比 |
| `gap_closed_ratio` 计算 | 需要 LRU + Infinite + Target Policy 三个结果，Phase 1 先验证 LRU vs TQ-TTL |
| priority_score 加权淘汰 | 实验设计文档 §6.3 的功能，Phase 2 实验时引入 |
| 长输入 protected eviction budget | 需要 input_length 统计分析，Phase 2 引入 |

---

## J. 开始建议

### 推荐执行顺序

**Week 1**（Milestone 1-3）：

```
Day 1-2: Milestone 1（core data model + prefix_key + events）
         → 全部单元测试通过再进入 M2
Day 3-4: Milestone 2（trace_loader + future_index）
         → 确认 FutureIndex 用 bisect_right，性能测试通过
Day 5:   Milestone 3（prefix_cache）
         → 重点验证 pinned 机制和 first-miss-stop 语义
```

**Week 2**（Milestone 4-5）：

```
Day 1-2: Milestone 4（三个淘汰策略）
         → 先 LRU，再 TTL-LRU，最后 TQ-TTL
         → 每个策略的单元测试全部绿灯后才进入下一个
Day 3-4: Milestone 5（SimulationEngine + MetricsCollector）
         → 在 toy trace 上验证两阶段架构的 evicted_before_next_hit 计算
Day 5:   Milestone 6（YAML config + CLI）
```

**Week 3**（Milestone 7）：

```
Day 1-2: 编写集成测试，对照手工推导的 expected 值
Day 3:   Debug（预计 FutureIndex 查询和 TTL 边界会有 bug）
Day 4:   README + 清理
Day 5:   缓冲
```

### 编码前已确认的设计决策（v2.0 更新后状态）

| 编号 | 决策 | 结论 | 依据 |
|---|---|---|---|
| D1 | TTL 过期边界判断（`>` 还是 `>=`）| **严格大于**（`timestamp > ttl_expiry`），等于时不过期 | 问题 1 讨论 |
| D2 | TTL-LRU 过期 block access 语义 | **视为 miss**，删除后重新插入，不计为 hit | 问题 2 讨论 |
| D3 | TQ-TTL 过期 Protected block access 语义 | **仍然 hit**，刷新 TTL；TTL 过期仅影响 eviction 优先级 | 问题 2 讨论 |
| D4 | eviction_count 是否包含 TTL 过期移除 | **包含**，同时拆分子计数（lru / ttl_expiry / demotion）| 问题 4 讨论 |
| D5 | Phase 1 无 demotion 的影响 | **可接受**，结果偏乐观，报告中明确标注 | 问题 3 讨论 |
| D6 | prefix-path key 格式 | `f"{model_id}:{':'.join(hash_ids[:i+1])}"` 累积路径，冒号分隔 | 原始设计 |
| D7 | FutureIndex 作用域 | 仅在 `MetricsCollector.finalize()` 中使用，不传递给 policy 层 | 两阶段架构要求 |
| D8 | YAML vs dataclass 配置 | Phase 1 使用 **Python dataclass + 直接实例化**，不引入 pyyaml | DEVELOPMENT_REQUIREMENTS.md §6 |

> **D5 补充说明（无 demotion 的乐观偏差）**：
> Phase 1 中 Protected 超容量时不主动降级，而是依赖 eviction 优先级（Probation LRU 先淘汰）。
> 这导致 Protected 队列可能积压过期低价值 block，实际效果略低于完整 demotion 实现。
> 建议实验时将 Protected 配额设置得比完整算法更保守（如 25% 而非 30%），
> 并在报告中注明："本结果为 Phase 1 乐观估计，Phase 2 实现 demotion 后数据可能有所下调。"
