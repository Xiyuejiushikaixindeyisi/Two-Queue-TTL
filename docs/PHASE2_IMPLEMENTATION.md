# Phase 2 实现计划：Belady Oracle / Hard Protected / Demotion / Warm-up / 参数消融

**版本**：v1.0  
**前提**：Phase 1 全部交付，且满足以下门槛：
- 所有 Phase 1 测试通过
- toy trace 上 `gap_closed_ratio(Two-Queue TTL) = 0.5` 精确验证
- 至少在一个真实 trace 上 `gap_closed_ratio ≥ 0.3`（若不满足则需重新评估方向）

**目标**：
1. 实现 Belady Oracle，量化任何在线策略的理论命中率上界
2. 实现 Hard Protected 三层队列 + Demotion 机制
3. 实现 Warm-up 仿真（离线预热 Hard Protected）
4. 支持实验计划 §7 全部参数消融实验

---

## 1. Phase 2 新增文件结构

```
sim/
  policies/
    belady.py              — Belady Oracle（需要 future_access_index）
    hard_protected_ttl.py  — Three-tier: Hard Protected / Protected / Probation
  warmup.py                — apply_warmup()，预热 Hard Protected 队列

scripts/
  run_ablation.py          — 批量参数扫描 CLI，输出汇总 CSV

tests/
  unit/
    test_belady_policy.py
    test_hard_protected_ttl_policy.py
    test_warmup.py
  integration/
    test_ablation.py       — 消融实验基本正确性验证
```

**修改文件**：

| 文件 | 修改内容 |
|---|---|
| `sim/policies/__init__.py` | 注册 `"belady"`, `"hard_protected_ttl"` |
| `sim/config.py` | 新增 `HardProtectedConfig` dataclass |
| `sim/metrics/collector.py` | 新增 `demotion_count`, `hard_protected_eviction_count`, `registry_hit_rate` |
| `sim/metrics/reporter.py` | 新增指标输出 |
| `sim/runner.py` | 支持 Belady 的 future_access 注入；支持 `run_ablation()` |

---

## 2. 任务 2.1 — Belady Oracle

### 文件：`sim/policies/belady.py`

### 算法原理

Belady 最优策略：每次需要驱逐时，选择下次访问时间最晚（或永不再访问）的 block 驱逐。这是任意在线策略的理论命中率上界。

Belady 必须提前知道整个 trace 的未来访问时间，因此是离线算法，不可在线实现。在仿真平台中，future_access_index 已由 `MetricsCollector.precompute_future_access()` 构建，直接复用。

### 接口设计

```python
class BeladyPolicy(AbstractCachePolicy):
    def __init__(
        self,
        capacity: int,
        future_access_index: Dict[str, List[float]],
    ) -> None:
        """
        Parameters
        ----------
        future_access_index:
            Dict mapping block_hash to sorted list of access timestamps.
            Built by MetricsCollector.precompute_future_access(trace).
        """
```

### evict_one 实现逻辑

```
对 cache 中每个未被 pin 的 block：
  查 future_access_index[block_hash]，用 bisect_right(times, current_time) 找下次访问时间
  若无未来访问 → 该 block 下次访问时间为 +∞
  选 next_access_time 最大的 block 驱逐
```

### SimulationRunner 适配

Belady 需要在构建 Policy 前先 precompute future access index，需在 `_build_policy()` 中特殊处理：

```python
# runner.py 中 _build_policy() 新增分支
if name == "belady":
    # future_access_index 需要 trace，在 run() 中传入
    return BeladyPolicy(
        capacity=self._config.cache_capacity,
        future_access_index=self._future_access_index,  # run() 时注入
    )
```

`run()` 方法需要在 precompute 之后、构建 policy 之前，将 index 存为实例变量。

### 目标实验

```
对比：LRU / Two-Queue TTL / Belady / Infinite Cache
关键指标：
  gap_closed_ratio(Belady)   应接近 1.0（理论上界）
  gap_closed_ratio(TQ-TTL) / gap_closed_ratio(Belady)  → 量化 TQ-TTL 的效率
```

### 单元测试要求（`tests/unit/test_belady_policy.py`）

```
□ 无未来访问的 block 优先被驱逐（永不再用 → 第一个被选）
□ 下次访问最晚的 block 优先被驱逐
□ 结果等于或优于 LRU（同 trace 同 capacity，Belady hit_rate ≥ LRU hit_rate）
□ capacity=∞ 时与 Infinite Cache 结果相同
□ evict_one 跳过 pinned 集合
□ toy trace 下 Belady hit_rate ≥ Two-Queue TTL hit_rate
```

---

## 3. 任务 2.2 — Hard Protected 三层队列

### 文件：`sim/policies/hard_protected_ttl.py`

### 三层队列结构

```
Hard Protected  (10%~20% C_total)  — 离线注册，常驻，max_idle=600s 超时降级
Protected       (20%~30% C_total)  — hit_count ≥ threshold，TTL 保护
Probation       (50%~70% C_total)  — 首次出现，主要淘汰压力
```

设计文档参考：`kv_cache_eviction_design.md` §3、§7.4、§12

### 新增配置（`sim/config.py`）

```python
@dataclass
class HardProtectedConfig:
    base_ttl: float = 46.0
    extended_ttl: float = 255.0
    promotion_threshold: int = 2
    hard_protected_ratio: float = 0.15   # 10%~20% C_total
    protected_ratio: float = 0.25        # 20%~30% C_total
    # Probation 比例 = 1 - hard_protected_ratio - protected_ratio
    max_idle_hard_protected: float = 600.0   # Hard Protected 闲置超时
    max_idle_after_expiry: float = 60.0      # Protected TTL 过期后的宽限期
```

### 淘汰优先顺序

```
第 1：Probation LRU（TTL=0，最老）
第 2：Protected TTL 已过期 + max_idle_after_expiry 超时，LRU 顺序
第 3：Protected LRU（TTL 未过期但容量不足）
第 4：Hard Protected 内部 LRU（仅当 Hard Protected 自身超容量时）
禁止：驱逐 ref_cnt > 0 或 pinned 中的 block
```

### Demotion 机制（Phase 1 未实现，Phase 2 正式引入）

**触发条件**：

```
A. Protected TTL 过期 + max_idle_after_expiry(60s) 内无新命中 → 降至 Probation
B. Hard Protected 闲置 > max_idle_hard_protected(600s) → 降至 Protected
C. Protected 容量超限 → 将 lowest-priority LRU block 降至 Probation
```

**Demotion 与 Eviction 的区别**：

- Demotion：block 仍在 cache 中，只是从高保护队列移入低保护队列
- Eviction：block 从 cache 中完全移除（释放空间）
- Demotion 不计入 `eviction_count`，单独计入 `demotion_count`

### 新增指标

| 指标 | 定义 |
|---|---|
| `demotion_count` | Protected → Probation 或 Hard Protected → Protected 降级次数 |
| `hard_protected_eviction_count` | Hard Protected 队列 block 被实际驱逐的次数（正常应为 0）|
| `registry_hit_rate` | block 进入 FREE_CACHED 时命中注册表的比例（Hard Protected 或 Protected 注册）|

### 单元测试要求（`tests/unit/test_hard_protected_ttl_policy.py`）

```
□ 注册表中的 block 直接进入 Hard Protected（不经 Probation/Protected）
□ Hard Protected block 闲置 > max_idle → 降级至 Protected（demotion）
□ Protected TTL 过期 + 宽限期过 → 降级至 Probation（demotion）
□ Protected 超容量 → 强制将最低优先级 LRU block 降级至 Probation
□ 淘汰优先级：Probation > Protected TTL 过期 > Protected > Hard Protected
□ Hard Protected block 在闲置超时前不被淘汰（即使 cache 满）
□ demotion_count 统计正确
□ flush_events() 返回 promotions 和 demotions 列表
```

---

## 4. 任务 2.3 — Warm-up 仿真

### 文件：`sim/warmup.py`

### 功能

在 trace replay 开始前，将注册表中的 top-N block hash 预先注入 Hard Protected 队列，模拟 vLLM 服务启动时对公共 system prompt 的 KV 预热。

### 接口

```python
def apply_warmup(
    policy: AbstractCachePolicy,
    registry: OfflineRegistry,
    top_n: int,
    warmup_timestamp: float = 0.0,
) -> int:
    """
    Pre-load top_n blocks from registry into the policy's Hard Protected queue
    before trace replay begins.

    Parameters
    ----------
    policy:
        Must be HardProtectedTTLPolicy or compatible. If policy does not
        support Hard Protected, blocks are loaded into Protected.
    registry:
        Source of block hashes. Uses hard_protected registry first,
        then protected registry.
    top_n:
        Maximum number of blocks to pre-load.
    warmup_timestamp:
        Simulation timestamp for the warmup event (default 0.0, before trace start).

    Returns
    -------
    Number of blocks successfully loaded.
    """
```

### Warm-up 数据来源

```
生产使用：reuse_distance_events.csv（Module 1 输出）→ 提取高频 prefix chain
仿真使用：OfflineRegistry 的 hard_protected 集合，按出现频次取 top-N
```

### 目标实验（实验计划 §7-E）

扫描 `top_n = [0, 10, 50, 100, 500, 1000]`，观察：

| top_n | Hard Protected 命中率 | saved_prefill_tokens 增量 | 污染率 |
|---|---|---|---|
| 0 | baseline | 0 | - |
| 10 | ? | ? | ? |
| ... | | | |

找到边际收益拐点（继续增加 top_n 但 saved_tokens 增量趋于平缓的 N 值）。

### 单元测试要求（`tests/unit/test_warmup.py`）

```
□ warmup 后 registry block 出现在 Hard Protected 队列中
□ top_n=0 不加载任何 block
□ top_n > registry 大小时加载全部可用 block，不报错
□ warmup 后的 block 在 replay 开始时有正确的 hit_count=0
□ warmup_timestamp 在 trace 最早时间戳之前
```

---

## 5. 任务 2.4 — 参数消融实验支持

### 文件：`scripts/run_ablation.py`

### 支持的消融维度（实验计划 §7）

**7-A Protected 容量比例扫描**

```
protected_ratio ∈ [0.10, 0.20, 0.30, 0.40, 0.50]
固定：base_ttl=46s, promotion_threshold=2
输出：每个比例下的 hit_rate, pollution_rate, gap_closed_ratio
```

**7-B TTL 参数扫描（锚定 F13 分位数）**

```
base_ttl ∈ [10s, 17s(p50), 46s(p80), 120s, 255s(p95), 600s]
extended_ttl ∈ [46s, 120s, 255s(p95), 600s]
固定：promotion_threshold=2, protected_ratio=0.30
```

**7-C 晋升阈值 × TTL 交叉实验（2×3 矩阵）**

```
threshold ∈ [1, 2, 3]
base_ttl  ∈ [17s(p50), 46s(p80), 255s(p95)]
全 9 组合，找到 pollution_rate < 30% 且 gap_closed_ratio 最高的组合
```

**7-D Priority Score 特征消融**

```
D1: 仅 hit_count
D2: hit_count + block_pos（前 64 块视为高价值代理信号）
D3: hit_count + block_pos + 离线注册表（第一版标准方案）
D4: D3 + Hard Protected Warm-up top-50
```

**7-E Hard Protected Warm-up top-N 扫描**

```
top_n ∈ [0, 10, 50, 100, 500, 1000]
策略：hard_protected_ttl + warmup
输出：warmup_blocks_count, hard_protected_hit_rate, saved_tokens_delta
```

### CLI 设计

```
python scripts/run_ablation.py \
    --trace data/trace.csv \
    --experiment 7-B \
    --capacity 10000 \
    --output results/ablation_7B.csv
```

输出 CSV 格式：每行一组参数配置 + 对应所有指标。

---

## 6. Phase 2 新增指标汇总

在 Phase 1 指标基础上追加：

| 指标 | 定义 | 关注阈值 |
|---|---|---|
| `demotion_count` | 降级（队列下移）总次数 | 监控异常波动 |
| `hard_protected_eviction_count` | Hard Protected 被实际驱逐次数 | 正常应为 0 |
| `registry_hit_rate` | 准入时命中注册表的比例 | 越高说明注册表覆盖率越好 |
| `warmup_blocks_count` | Warm-up 预热成功的 block 数 | 对应 top-N 实际加载量 |
| `hard_protected_hit_rate` | Hard Protected block 的命中率 | 衡量 Hard Protected 质量 |
| `belady_gap_closed_ratio` | Belady 的 gap_closed_ratio（上界参考）| 越接近 1.0 越好 |
| `tq_efficiency` | gap_closed_ratio(TQ-TTL) / gap_closed_ratio(Belady) | 衡量 TQ-TTL 相对效率 |

---

## 7. Phase 2 实验报告模板

每次消融实验结束后，填写以下报告框架：

```
实验名称：Two-Queue TTL + Hard Protected — [模型名] trace，[容量配置]

数据集：
  模型、时间范围、请求数、block 数
  reuse_distance p50/p80/p95（Module 1）
  reuse_time p50/p80/p95（F13）

配置：
  cache_capacity（标注对应 Module 1 分位数）
  block_size
  hard_protected_ratio / protected_ratio
  base_ttl（标注对应 F13 分位数）/ extended_ttl
  promotion_threshold
  registry_size（top-N）
  warmup_top_n

结果：
  策略           | hit_rate | gap_closed_ratio | pollution_rate | eviction_count
  LRU            |          | 0.0              |                |
  Two-Queue TTL  |          |                  |                |
  Hard Protected |          |                  |                |
  Belady Oracle  |          |                  |                |
  Infinite Cache |          | 1.0              |                |

结论：
  1. Hard Protected 相比 Two-Queue TTL 的 gap_closed_ratio 提升是否 ≥ 5pp？
  2. Warm-up top-N 的边际收益拐点在哪里？
  3. Demotion 是否导致 protected_pollution_rate 下降？
  4. 推荐最优参数组合是什么？
  5. 是否建议进入 Phase 3？
```

---

## 8. Phase 2 验收标准

### 功能验收

```
□ python -m pytest tests/ → 全部通过（预计 110+ tests）
□ Belady 在 toy trace 下 hit_rate ≥ Two-Queue TTL hit_rate
□ Hard Protected block 在 max_idle 超时前不被淘汰
□ Demotion 后 block 仍在 cache 中（只是队列变化）
□ demotion_count 统计正确，不计入 eviction_count
□ Warm-up 后 Hard Protected 在 trace 开始前已有 block
□ run_ablation.py 输出合法 CSV（每行一个参数配置）
```

### 实验验收

```
□ gap_closed_ratio(Hard Protected + Warmup) > gap_closed_ratio(Two-Queue TTL)（在有注册表覆盖的 trace 上）
□ 7-B TTL 扫描结果：base_ttl ≈ F13_p80 时 gap_closed_ratio 最高区间出现
□ 7-C 交叉实验：找到 pollution_rate < 30% 且 gap_closed_ratio ≥ 0.5 的参数组合
□ 7-E Warm-up：找到边际收益拐点（top-N 继续增加但 saved_tokens_delta < 1%）
```

---

## 9. 与 Phase 1 / Phase 3 的接口约定

### 依赖 Phase 1 的接口（不得修改）

```
AbstractCachePolicy  — 所有新策略继承，接口不变
POLICY_REGISTRY      — 追加新 key，不修改已有 key
MetricsCollector     — 追加新字段，不修改已有字段
compute_gap_closed_ratio() — 直接调用，不修改签名
toy trace expected values  — 不修改 Phase 1 集成测试的断言值
```

### 为 Phase 3 预留的接口

```
HardProtectedTTLPolicy.queue_sizes() → Dict[str, int]
  — 返回各队列当前 block 数，供多节点汇总使用

run_ablation() 输出 CSV 格式
  — Phase 3 多节点实验复用相同 CSV 格式，追加 routing_policy、num_nodes 列
```
