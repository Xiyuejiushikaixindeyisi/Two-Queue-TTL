# Phase 3 实现计划：多节点 Routing-Aware Replay / 多级缓存

**版本**：v1.0  
**前提**：Phase 2 全部交付，且满足：
- Phase 2 所有测试通过
- 至少一个真实 trace 上 `gap_closed_ratio(Hard Protected) ≥ 0.4`
- 参数消融实验已找到推荐配置（base_ttl, protected_ratio, top_n）

**目标**：
1. 将仿真从单节点扩展为多节点集群模型
2. 量化**路由策略**对命中率的影响，与淘汰策略的影响分离（实验计划 §4-B）
3. 支持两级缓存仿真（L1 hot + L2 warm）
4. 为 Phase 4 在线对接提供多节点 trace 格式和指标接口

---

## 1. Phase 3 新增文件结构

```
sim/
  routing/
    __init__.py
    router.py              — AbstractRoutingPolicy + 四种具体路由实现
    multinode_runner.py    — 多节点编排器（节点级 + 集群级指标）
  cache/
    multilevel_cache.py    — TwoLevelCache（L1/L2 级联）

scripts/
  run_multinode.py         — 多节点仿真 CLI
  run_routing_ablation.py  — 路由因素隔离实验

tests/
  unit/
    test_router.py
    test_multinode_runner.py
    test_multilevel_cache.py
  integration/
    test_routing_isolation.py   — 路由因素隔离实验正确性
    test_multinode_vs_singlenode.py
```

**修改文件**：

| 文件 | 修改内容 |
|---|---|
| `sim/core/trace.py` | 追加可选字段 `node_id: Optional[int]`（routing 结果记录）|
| `sim/metrics/collector.py` | 新增路由相关指标字段 |
| `sim/metrics/reporter.py` | 新增集群级汇总输出 |
| `sim/config.py` | 新增 `MultiNodeConfig` dataclass |

---

## 2. 任务 3.1 — 路由策略抽象与实现

### 文件：`sim/routing/router.py`

### 抽象接口

```python
class AbstractRoutingPolicy(ABC):
    """
    Determines which cache node handles a given request.
    Stateless policies (round-robin, affinity) vs stateful (least-loaded).
    """

    @abstractmethod
    def route(self, record: TraceRecord, num_nodes: int) -> int:
        """Return node index in [0, num_nodes). Pure function where possible."""

    def reset(self) -> None:
        """Reset internal state (e.g., load counters). Called before each replay."""
```

### 四种路由实现

**RoundRobinRouter**

```
route() → counter % num_nodes
无状态假设，counter 按请求顺序递增
用途：当前生产环境 least-loaded 的近似（基线）
```

**UserAffinityRouter**

```
route() → hash(user_id) % num_nodes
相同 user_id 的所有请求路由到同一节点
用途：量化 user 级 affinity 的命中率增益
```

**SystemPromptAffinityRouter**

```
route() → hash(hash_ids[0]) % num_nodes
按 prefix chain 第一个 block hash 路由（代理 system prompt）
用途：量化 system prompt 级 affinity 的命中率增益
注意：若 hash_ids 为空，fallback 到 RoundRobinRouter
```

**LeastLoadedRouter**

```
route() → 选当前 cache 使用率最低的节点
需要 runtime 状态（各节点当前 cache_size）
通过 MultiNodeRunner 在路由前注入各节点负载
用途：近似生产环境的动态路由
```

### 路由注册表

```python
ROUTER_REGISTRY = {
    "round_robin":          RoundRobinRouter,
    "user_affinity":        UserAffinityRouter,
    "system_prompt_affinity": SystemPromptAffinityRouter,
    "least_loaded":         LeastLoadedRouter,
}
```

---

## 3. 任务 3.2 — 多节点仿真编排

### 文件：`sim/routing/multinode_runner.py`

### 设计原则

- 每个节点维护**独立的 PrefixCache 实例**（不跨节点共享 KV block）
- 请求按路由策略分发，每条 TraceRecord 只进入一个节点
- 节点级指标单独收集，集群级指标在最后聚合

### 核心类

```python
class MultiNodeSimulationRunner:
    """
    Runs a multi-node simulation where each node has its own PrefixCache
    backed by the configured eviction policy.

    Parameters
    ----------
    num_nodes:
        Number of cache nodes.
    per_node_config:
        SimConfig applied to each node (capacity = per_node_capacity).
    router:
        Routing policy determining which node handles each request.
    """

    def __init__(
        self,
        num_nodes: int,
        per_node_config: SimConfig,
        router: AbstractRoutingPolicy,
        registry: Optional[OfflineRegistry] = None,
    ) -> None: ...

    def run(self, trace: List[TraceRecord]) -> MultiNodeResult: ...
```

### MultiNodeResult

```python
@dataclass
class MultiNodeResult:
    per_node_snapshots: List[MetricsSnapshot]   # 每节点独立指标
    cluster_snapshot: MetricsSnapshot           # 集群加权聚合
    routing_distribution: Dict[int, int]        # 节点 → 请求数
```

### 集群级指标聚合规则

```
cluster.total_requests           = sum(per_node.total_requests)
cluster.total_blocks_requested   = sum(per_node.total_blocks_requested)
cluster.total_blocks_hit         = sum(per_node.total_blocks_hit)
cluster.prefix_block_hit_rate    = total_blocks_hit / total_blocks_requested
cluster.eviction_count           = sum(per_node.eviction_count)
cluster.saved_prefill_tokens     = sum(per_node.saved_prefill_tokens)
```

---

## 4. 任务 3.3 — 路由因素隔离实验

### 核心公式（实验计划 §4-B）

```
total_miss = routing_miss + eviction_miss + other_miss

routing_miss = LRU(当前路由) hit_rate
             - LRU(完美 user-affinity) hit_rate

eviction_miss = LRU(完美 user-affinity) hit_rate
              - Infinite(完美 user-affinity) hit_rate
```

若 `routing_miss > eviction_miss`：路由是主要瓶颈，优先考虑 routing 优化  
若 `eviction_miss` 显著：淘汰策略优化方向成立，Two-Queue TTL 值得投入

### 实验配置（`scripts/run_routing_ablation.py`）

对同一 trace、同一 capacity，依次运行以下 4 种组合：

| 实验 ID | 路由策略 | 淘汰策略 | 用途 |
|---|---|---|---|
| R1 | round_robin | LRU | 当前生产基线 |
| R2 | user_affinity | LRU | 完美 user-affinity 基线 |
| R3 | user_affinity | Infinite Cache | 理论命中率上界 |
| R4 | user_affinity | Two-Queue TTL | 算法收益（排除 routing 干扰）|

输出：

```
routing_miss  = R1.hit_rate - R2.hit_rate
eviction_miss = R2.hit_rate - R3.hit_rate
tq_gain       = R4.hit_rate - R2.hit_rate
```

### 新增指标

| 指标 | 定义 |
|---|---|
| `per_node_hit_rate[]` | 每个节点的 `prefix_block_hit_rate` |
| `routing_miss_rate` | 因路由分散导致的命中损失（R1-R2 差值）|
| `eviction_miss_rate` | 因淘汰导致的命中损失（R2-R3 差值）|
| `node_load_imbalance` | max(per_node_requests) / mean(per_node_requests) - 1 |

---

## 5. 任务 3.4 — 两级缓存仿真

### 文件：`sim/cache/multilevel_cache.py`

### 两级缓存语义

```
L1（热缓存）：容量小，命中延迟低，采用激进淘汰策略
L2（温缓存）：容量大，命中延迟中等，采用保守淘汰策略

查找顺序：
  1. 查 L1 → hit → done（L1 命中）
  2. L1 miss → 查 L2 → hit → 将 block 提升至 L1（L2 命中，block 升温）
  3. L2 miss → 计算 block，写入 L1（缓存未命中，重新计算）

L1 淘汰时：
  被淘汰的 block 写入 L2（降温，而非直接丢弃）
L2 淘汰时：
  block 从系统完全移除
```

### 核心类

```python
class TwoLevelCache:
    """
    L1 (hot, small, aggressive eviction) →
    L2 (warm, large, conservative eviction) cascade.

    Parameters
    ----------
    l1_policy:
        Eviction policy for L1. Evicted blocks go to L2.
    l2_policy:
        Eviction policy for L2. Evicted blocks are discarded.
    """

    def __init__(
        self,
        l1_policy: AbstractCachePolicy,
        l2_policy: AbstractCachePolicy,
    ) -> None: ...

    def process_request(self, record: TraceRecord) -> TwoLevelRequestResult: ...
```

### TwoLevelRequestResult

```python
@dataclass
class TwoLevelRequestResult:
    record: TraceRecord
    l1_hit_hashes: List[str]
    l2_hit_hashes: List[str]
    miss_hashes: List[str]
    l1_evicted_to_l2: List[BlockMeta]   # L1 淘汰 → 写入 L2
    l2_evicted: List[BlockMeta]          # L2 淘汰 → 彻底丢弃
```

### 两级缓存指标

| 指标 | 定义 |
|---|---|
| `l1_hit_rate` | L1 命中 / 总请求块数 |
| `l2_hit_rate` | L2 命中 / 总请求块数 |
| `total_hit_rate` | (L1+L2 命中) / 总请求块数 |
| `l1_eviction_to_l2_count` | L1 淘汰但降温写 L2 的次数 |
| `l2_eviction_count` | L2 彻底淘汰次数 |

### 配置（追加至 `sim/config.py`）

```python
@dataclass
class MultiLevelConfig:
    l1_capacity: int
    l2_capacity: int
    l1_policy: str = "lru"
    l2_policy: str = "two_queue_ttl"
```

---

## 6. 新增配置（`sim/config.py`）

```python
@dataclass
class MultiNodeConfig:
    num_nodes: int = 1
    per_node_capacity: int = 0          # 0 表示使用 SimConfig.cache_capacity / num_nodes
    router: str = "round_robin"         # 路由策略 key
    total_capacity_mode: bool = True    # True: per_node_capacity = total / num_nodes
```

---

## 7. 单元测试要求

### `tests/unit/test_router.py`

```
□ RoundRobinRouter：连续请求均匀分布到各节点
□ UserAffinityRouter：同 user_id 的请求总到同一节点
□ SystemPromptAffinityRouter：同首个 block_hash 的请求总到同一节点
□ UserAffinityRouter：不同 user_id 在 num_nodes=1 时全到节点 0
□ 空 hash_ids 时 SystemPromptAffinityRouter 不崩溃（fallback round_robin）
□ router.reset() 后 RoundRobin 重置计数器
```

### `tests/unit/test_multinode_runner.py`

```
□ num_nodes=1 时结果与 SimulationRunner 单节点运行完全一致
□ 集群 total_requests = sum(per_node.total_requests)
□ 集群 total_blocks_hit = sum(per_node.total_blocks_hit)
□ user_affinity routing 下 cluster hit_rate ≥ round_robin（在有重复 user 的 trace 上）
□ routing_distribution 各节点请求数之和 = total_requests
```

### `tests/unit/test_multilevel_cache.py`

```
□ L1 hit → 不查 L2
□ L1 miss + L2 hit → block 提升至 L1
□ L1 满时淘汰 block 写入 L2，而非直接丢弃
□ L2 满时淘汰 block 完全丢弃
□ total_hit_rate = l1_hit_rate + l2_hit_rate（两者互斥）
□ 两级 total hit_rate ≥ 相同总容量单级 LRU hit_rate（in theory）
```

### `tests/integration/test_multinode_vs_singlenode.py`

```
□ num_nodes=1 + round_robin = 单节点 SimulationRunner（结果完全一致）
□ user_affinity routing 在有重复 user 的 toy trace 上 hit_rate > round_robin
□ routing_miss + eviction_miss 之和 ≈ total_miss（数学验证）
```

### `tests/integration/test_routing_isolation.py`

```
□ R1.hit_rate ≤ R2.hit_rate（完美 affinity ≥ round_robin）
□ R2.hit_rate ≤ R3.hit_rate（Infinite ≥ LRU）
□ routing_miss ≥ 0，eviction_miss ≥ 0
□ 在有大量同 user 重复请求的 trace 上：routing_miss > 0
```

---

## 8. CLI 接口

### `scripts/run_multinode.py`

```bash
# 基本多节点运行
python scripts/run_multinode.py \
    --trace data/trace.csv \
    --num-nodes 4 \
    --capacity-per-node 2500 \
    --policy two_queue_ttl \
    --router user_affinity \
    --output results/multinode.csv

# 路由因素隔离实验
python scripts/run_routing_ablation.py \
    --trace data/trace.csv \
    --num-nodes 4 \
    --total-capacity 10000 \
    --output results/routing_isolation.csv
```

---

## 9. Phase 3 验收标准

### 功能验收

```
□ python -m pytest tests/ → 全部通过（预计 150+ tests）
□ num_nodes=1 时 MultiNodeRunner 结果与 SimulationRunner 完全一致（浮点精确）
□ UserAffinityRouter 在有重复 user 的 trace 上 cluster hit_rate > RoundRobin
□ routing_miss 和 eviction_miss 可被独立量化（数学一致性验证）
□ 两级缓存 total hit_rate ≥ 单级等容量 LRU（在合理的 L1:L2 比例下）
□ run_routing_ablation.py 输出合法 CSV
```

### 实验验收

```
□ 路由因素隔离实验在真实 trace 上完成，输出 routing_miss vs eviction_miss 分解
□ 若 routing_miss > eviction_miss：记录结论，调整 Phase 4 优先级
□ 若 eviction_miss 显著：Two-Queue TTL 价值得到多节点维度确认
□ 多节点 gap_closed_ratio 计算结果合理（可与单节点对比）
```

---

## 10. 与 Phase 2 / Phase 4 的接口约定

### 依赖 Phase 2 的接口（不得修改）

```
HardProtectedTTLPolicy   — Phase 3 多节点实验可直接使用
run_ablation() CSV 格式  — Phase 3 在此基础上追加 num_nodes、router 列
MetricsSnapshot 字段     — Phase 3 只追加，不修改现有字段
```

### 为 Phase 4 预留的接口

```
TraceRecord.node_id        — Phase 4 在线 trace 导出时携带节点信息
MultiNodeResult            — Phase 4 A/B 实验框架复用此数据结构
routing_miss / eviction_miss 指标分解
  — Phase 4 在线 A/B 实验中用于归因分析
```

---

## 11. 重要约束与注意事项

**不实现的功能（本阶段边界）**：

```
✗ 跨节点 KV block 共享（P2P cache）
✗ 真实网络延迟模拟
✗ 动态扩缩容（节点数固定）
✗ 路由策略的在线自适应
```

**关于 LeastLoadedRouter 的实现注意事项**：

LeastLoadedRouter 需要知道各节点当前的 cache 使用率，这要求在每次 `route()` 调用前由 `MultiNodeRunner` 注入当前状态。实现方式：通过 `router.update_load(node_loads: Dict[int, float])` 在分发每个请求前更新，而不是在 `route()` 内部查询（避免耦合）。

**关于 SystemPromptAffinityRouter 的限制**：

本路由使用 `hash_ids[0]` 作为 system prompt 的代理信号，这是一个近似——实际上不同 system prompt 可能共享前几个 block（分词对齐问题）。仿真中接受这个近似，在报告中注明。
