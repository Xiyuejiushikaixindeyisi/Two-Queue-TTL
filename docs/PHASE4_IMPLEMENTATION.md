# Phase 4 实现计划：vLLM/vLLM-Ascend 在线 Instrumentation 对接

**版本**：v1.0  
**前提**：Phase 3 全部交付，且满足以下在线上线门槛：
- Phase 1 toy trace 验证通过
- 真实 trace 上 `gap_closed_ratio ≥ 0.3`（Phase 3-1 最低上线门槛）
- Phase 3 路由因素隔离实验完成，eviction_miss 是主要瓶颈（而非 routing_miss）

**目标**：
1. 从真实 vLLM/vLLM-Ascend 实例无侵入地导出标准 trace 格式
2. 验证离线仿真对在线结果的预测精度（误差 < 10%）
3. 提供 A/B 实验监控框架，支持 Phase 3-1 在线原型（实验计划 §8.2）
4. 建立在线指标到离线 MetricsSnapshot 的映射桥接

---

## 1. Phase 4 核心约束

```
✗ 不修改 vLLM/vLLM-Ascend 任何核心源文件（block_manager.py、scheduler.py 等）
✗ 不实现在线服务
✗ 不引入多进程、异步框架、数据库
✓ 通过日志解析、Prometheus 指标采集、monkey-patch（仅测试环境）三种方式采集数据
✓ 输出格式与离线仿真完全兼容（TraceRecord CSV/JSONL）
```

---

## 2. Phase 4 新增文件结构

```
instrumentation/
  __init__.py
  vllm_trace_exporter.py    — 从 vLLM 日志/指标导出 TraceRecord
  vllm_log_parser.py        — 解析 vLLM 结构化日志的具体实现
  metric_bridge.py          — vLLM Prometheus 指标 → MetricsSnapshot 字段映射
  ab_monitor.py             — A/B 实验监控：实时对比两组在线指标
  simulation_validator.py   — 离线仿真 vs 在线实测的误差分析

scripts/
  export_trace.py           — CLI：从 vLLM 日志文件导出 trace
  run_ab_monitor.py         — CLI：启动 A/B 监控，持续对比两组指标
  validate_simulation.py    — CLI：对比离线仿真预测与在线实测

tests/
  unit/
    test_vllm_log_parser.py
    test_metric_bridge.py
    test_ab_monitor.py
    test_simulation_validator.py
  integration/
    test_trace_export_roundtrip.py   — 导出 trace → 仿真 → 验证格式完整性
```

---

## 3. 任务 4.1 — vLLM Trace 导出器

### 文件：`instrumentation/vllm_trace_exporter.py`、`instrumentation/vllm_log_parser.py`

### 采集原理

vLLM 不直接输出 TraceRecord 格式，需要通过以下**三种非侵入方式**之一或组合采集：

**方式 A：结构化日志解析（生产优先推荐）**

vLLM 在 INFO 级别日志中记录 `prompt_tokens_cached`、`prefix_cache_hit_rate` 等信息。通过解析这些日志，可以重建近似的 TraceRecord。

精度限制：日志通常为 batch 级别聚合，block 级别的 `hash_ids` 无法直接获取。

**方式 B：Prometheus 指标采集**

vLLM 暴露的关键 Prometheus 指标：

| 指标名 | 含义 | 对应 TraceRecord 字段 |
|---|---|---|
| `vllm:gpu_prefix_cache_hit_rate` | prefix cache 命中率 | 用于验证仿真预测 |
| `vllm:prompt_tokens_total` | 总 prompt token 数 | `input_length` 累计 |
| `vllm:generation_tokens_total` | 总生成 token 数 | `output_length` 累计 |
| `vllm:request_latency_seconds` | 请求延迟分布 | TTFT 关联 |
| `vllm:num_requests_running` | 并发请求数 | 负载监控 |

**方式 C：monkey-patch 插桩（仅测试/staging 环境）**

在 vLLM 进程启动时，通过 Python 的 `unittest.mock` 或直接属性替换，在以下位置插入 hook：

```python
# 采集位置（不修改源文件，在外部注入）
BlockSpaceManager.get_computed_blocks    → 记录 prefix lookup 结果
BlockSpaceManager.allocate_slots         → 记录 block 分配和 eviction
SequenceGroup.__init__                   → 记录请求元数据（model_id, user_id）
```

### TraceRecord 字段对应关系

| TraceRecord 字段 | vLLM 数据来源 | 精度 |
|---|---|---|
| `timestamp` | 请求到达时间（access log）| 秒级 |
| `model_id` | vLLM 启动参数 `--model`| 精确 |
| `user_id` | HTTP header `X-User-ID`（需 serving 层透传）| 依赖配置 |
| `request_type` | HTTP header `X-Request-Type` | 依赖配置 |
| `input_length` | `prompt_tokens` 字段 | 精确 |
| `output_length` | `generation_tokens` 字段 | 精确 |
| `hash_ids` | `get_computed_blocks` 返回的 block hash 列表 | 仅方式 C |

### 导出接口

```python
class VLLMTraceExporter:
    """
    Exports TraceRecord-compatible data from vLLM logs or metrics.

    Supports three collection modes:
      'log'       : Parse structured vLLM log files (production-safe)
      'prometheus': Scrape Prometheus metrics endpoint (production-safe)
      'hook'      : monkey-patch instrumentation (test/staging only)
    """

    def __init__(self, mode: str = "log", source: str = "") -> None: ...

    def export(
        self,
        output_path: str,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
    ) -> int:
        """
        Export trace records to output_path (CSV or JSONL).
        Returns number of records exported.
        """
```

### 精度声明

日志解析模式（方式 A/B）无法获取 block 级别的 `hash_ids`，此时：
- 导出的 TraceRecord 的 `hash_ids` 为空列表或基于 `input_length / block_size` 的占位估计
- 这类 trace 只能用于 TTFT / throughput 趋势分析，不能用于 prefix cache 命中率仿真
- 精确的 prefix cache 仿真必须使用方式 C（hook），或使用已有的生产 trace 数据集（如 qwen_v3_5_27b_64k）

---

## 4. 任务 4.2 — 在线与离线指标桥接

### 文件：`instrumentation/metric_bridge.py`

### 功能

将 vLLM 的 Prometheus 指标映射到 `MetricsSnapshot` 的对应字段，使离线仿真结果与在线实测结果可以直接比较。

### 字段映射表

| vLLM Prometheus 指标 | MetricsSnapshot 字段 | 换算关系 |
|---|---|---|
| `vllm:gpu_prefix_cache_hit_rate` | `prefix_block_hit_rate` | 直接对应（rate 值）|
| `vllm:prompt_tokens_total` × hit_rate | `saved_prefill_tokens` | 近似（batch 级）|
| `vllm:cache_evictions_total` | `eviction_count` | 直接对应（若有此指标）|
| `vllm:num_preemptions_total` | 与 `eviction_count` 区分 | 抢占 ≠ 缓存淘汰 |

### 误差来源分析

```
仿真 vs 在线的误差来源：

1. Routing 简化：仿真使用固定路由策略，在线路由是动态的
2. 并发效应：仿真顺序处理，在线多请求并发，ref_cnt 行为不同
3. Block 填充顺序：vLLM 的 partial block 处理与仿真的简化有差异
4. 缓存预热状态：在线服务已运行一段时间，仿真从冷启动开始
5. hash_ids 精度：方式 A/B 无法获取精确 block hash，只能近似
```

### 误差验证目标

```
目标：仿真预测 prefix_block_hit_rate 与在线实测误差 < 10%

若误差 ≥ 10%：
  1. 检查是否使用了精确的 hash_ids（方式 C）
  2. 检查 block_size 参数是否与在线部署一致
  3. 检查是否考虑了 routing 因素（单节点 vs 多节点）
  4. 若误差来自并发，记录为已知局限，在报告中说明
```

---

## 5. 任务 4.3 — A/B 实验监控框架

### 文件：`instrumentation/ab_monitor.py`

### 实验配置（对应实验计划 §8.2）

```
A 组（对照）：当前 LRU，生产流量 95%
B 组（实验）：Two-Queue TTL 最优配置，流量 5%，持续 7 天
```

### 硬指标（任何一项非零立即触发回滚）

```python
SAFETY_METRICS = {
    "ref_cnt_positive_eviction_count": 0,   # 有请求在用的 block 被淘汰
    "active_block_eviction_count": 0,       # 活跃 block 被淘汰
    "prefix_hash_inconsistency": 0,         # hash 链完整性破坏
}
```

### 收益指标（持续对比，7 天内观察稳定性）

```python
BENEFIT_METRICS = [
    "vllm:gpu_prefix_cache_hit_rate",       # 核心命中率
    "prompt_tokens_cached",                 # 节省的 prefill tokens
    "vllm:request_latency_seconds_p50",     # TTFT P50
    "vllm:request_latency_seconds_p95",     # TTFT P95
    "vllm:request_latency_seconds_p99",     # TTFT P99
    "vllm:gpu_kv_cache_usage_perc",         # KV cache 使用率
    "vllm:num_requests_running",            # 吞吐量代理指标
]
```

### ABMonitor 类接口

```python
class ABMonitor:
    """
    Monitors two groups of vLLM instances (control A, treatment B)
    and computes real-time metric diffs.

    Does NOT run the A/B traffic split — that is handled by the load balancer.
    ABMonitor only reads metrics and raises alerts.
    """

    def __init__(
        self,
        group_a_endpoints: List[str],   # Prometheus 地址列表
        group_b_endpoints: List[str],
        poll_interval: float = 60.0,    # 采集间隔（秒）
        alert_callback: Optional[Callable] = None,
    ) -> None: ...

    def check_safety(self) -> bool:
        """Return False and trigger alert if any SAFETY_METRIC is nonzero."""

    def compute_diff(self) -> Dict[str, float]:
        """
        Compute B-A difference for each BENEFIT_METRIC.
        Positive values indicate B outperforms A.
        """

    def rollback_recommendation(self) -> bool:
        """Return True if safety check fails or TTFT P99 degrades > 20%."""
```

### 回滚触发条件

```
立即回滚（硬触发）：
  ref_cnt_positive_eviction_count > 0（任意一个 B 组实例）
  active_block_eviction_count > 0
  TTFT P99 相比 A 组恶化 > 20%

观察阶段（软触发，需人工确认）：
  TTFT P95 相比 A 组恶化 > 10%
  hit_rate 相比 A 组下降（B 应 ≥ A）
  KV cache 使用率相比 A 组显著升高（内存压力）
```

---

## 6. 任务 4.4 — 仿真预测精度验证

### 文件：`instrumentation/simulation_validator.py`

### 功能

将同一时间窗口的在线实测指标与离线仿真预测进行误差分析，输出误差报告。

### 验证流程

```
Step 1：收集在线数据
  - 从 Prometheus 采集目标时间窗口的 prefix_cache_hit_rate
  - 从日志/hook 导出同期 TraceRecord（若方式 C 可用）

Step 2：运行离线仿真
  - 使用导出的 trace 运行 SimulationRunner
  - 配置参数与在线部署一致（capacity、block_size、policy）

Step 3：误差计算
  relative_error = |sim_hit_rate - online_hit_rate| / online_hit_rate

Step 4：误差归因
  若误差 > 10%，检查：
    routing_factor: 仿真是否考虑了多节点路由
    warmup_factor: 在线服务是否已有预热状态
    concurrency_factor: 在线并发与仿真单线程的差异
```

### 验证报告格式

```
仿真精度验证报告
================

时间窗口：[start_time, end_time]
在线配置：capacity=X, block_size=Y, policy=two_queue_ttl

在线实测：
  prefix_cache_hit_rate: 0.432
  saved_prefill_tokens:  1,243,000
  TTFT P50: 0.82s

离线仿真：
  prefix_block_hit_rate: 0.447   (误差: +3.5%, 在目标范围内)
  saved_prefill_tokens:  1,187,000 (误差: -4.5%)
  TTFT: N/A（仿真不测量 TTFT）

结论：
  仿真预测精度: ✅ 误差 < 10%，仿真可信
  推荐：可用仿真结果指导参数调优
```

---

## 7. Phase 4 新增指标汇总

| 指标 | 来源 | 用途 |
|---|---|---|
| `online_prefix_cache_hit_rate` | Prometheus | A/B 对比基线 |
| `simulation_prediction_error` | simulation_validator | 仿真精度评估 |
| `ab_hit_rate_diff` | ab_monitor | B-A 命中率差值 |
| `ab_ttft_p50_diff` | ab_monitor | B-A TTFT P50 差值 |
| `ab_ttft_p99_diff` | ab_monitor | B-A TTFT P99 差值（回滚判断）|
| `rollback_triggered` | ab_monitor | 是否触发回滚告警 |

---

## 8. 单元测试要求

### `tests/unit/test_vllm_log_parser.py`

```
□ 解析标准 vLLM INFO 日志，正确提取 timestamp 和 prompt_tokens
□ 格式不合规的日志行被跳过（不崩溃）
□ 空日志文件返回空 TraceRecord 列表
□ 时间范围过滤（start_time, end_time 参数）正确生效
□ 导出的 TraceRecord 格式能被 load_trace() 直接加载
```

### `tests/unit/test_metric_bridge.py`

```
□ Prometheus 指标正确映射到 MetricsSnapshot 字段
□ 缺失指标时返回合理默认值（不崩溃）
□ relative_error 计算公式正确（含零值保护）
□ 误差 < 0.1 时精度判定为通过
```

### `tests/unit/test_ab_monitor.py`

```
□ SAFETY_METRIC 非零时 check_safety() 返回 False
□ rollback_recommendation() 在 TTFT P99 恶化 > 20% 时返回 True
□ compute_diff() 正确计算 B-A 差值
□ 空 endpoint 列表时不崩溃（返回空 dict）
□ alert_callback 在安全检查失败时被调用
```

### `tests/integration/test_trace_export_roundtrip.py`

```
□ 生成 mock vLLM 日志 → export_trace.py 导出 → load_trace() 加载 → 格式完整
□ 导出的 TraceRecord 字段类型正确（timestamp:float, hash_ids:list）
□ SimulationRunner 可以直接使用导出的 trace 运行，不报错
```

---

## 9. CLI 接口

### `scripts/export_trace.py`

```bash
# 从 vLLM 日志导出 trace
python scripts/export_trace.py \
    --log-file /var/log/vllm/vllm.log \
    --mode log \
    --start-time "2024-01-15 10:00:00" \
    --end-time "2024-01-15 11:00:00" \
    --output data/exported_trace.csv

# 使用 Prometheus 采集
python scripts/export_trace.py \
    --prometheus-url http://localhost:9090 \
    --mode prometheus \
    --window 3600 \
    --output data/metrics_snapshot.csv
```

### `scripts/validate_simulation.py`

```bash
python scripts/validate_simulation.py \
    --trace data/exported_trace.csv \
    --policy two_queue_ttl \
    --capacity 10000 \
    --online-hit-rate 0.432 \
    --output reports/validation_report.txt
```

### `scripts/run_ab_monitor.py`

```bash
python scripts/run_ab_monitor.py \
    --group-a http://node1:9090,http://node2:9090 \
    --group-b http://node3:9090 \
    --poll-interval 60 \
    --duration 604800 \
    --output results/ab_7day.csv
```

---

## 10. Phase 4 验收标准

### 功能验收

```
□ python -m pytest tests/ → 全部通过（预计 180+ tests）
□ export_trace.py 能从 mock vLLM 日志正确导出 TraceRecord
□ 导出的 trace 能被 SimulationRunner 直接使用，结果合理
□ ab_monitor.py 在 SAFETY_METRIC 非零时正确触发告警
□ simulation_validator.py 能计算并输出 relative_error
```

### 精度验收

```
□ 在持有精确 hash_ids 的 trace 上，仿真预测误差 < 10%
□ 在仅有日志解析 trace（无 hash_ids）的情况下，明确报告"精度不足，无法用于 prefix cache 仿真"
```

### 实验验收（Phase 3-1 在线原型）

```
□ A/B 监控框架已在 staging 环境验证（7 天无 SAFETY_METRIC 触发）
□ B 组（Two-Queue TTL）prefix_cache_hit_rate ≥ A 组（LRU）
□ B 组 TTFT P99 未恶化超过 20%
□ gap_closed_ratio 在线估算（基于 A/B 差值）∈ [0.3, 1.0]
□ 若满足 Phase 3-2 上线条件（gap ≥ 0.5），输出扩量建议报告
```

---

## 11. 重要约束再次确认

```
✗ 不修改 vLLM/vLLM-Ascend 任何核心源文件
✗ 不实现在线服务（监控框架只读指标，不干预调度）
✗ monkey-patch 方式仅允许在测试/staging 环境使用
✗ 不把仿真结果直接用于生产决策（必须配合在线验证）

✓ 仿真精度不足时，诚实报告误差来源，不过度声明
✓ 回滚机制优先于性能收益
✓ A/B 实验严格遵循实验计划 §8.1 的门槛条件
```

---

## 12. 四阶段完成后的里程碑回顾

| 阶段 | 核心交付 | 关键验证点 |
|---|---|---|
| **Phase 1** | LRU / TTL-LRU / Two-Queue TTL / Infinite Cache + gap_closed_ratio | toy trace gap_closed_ratio = 0.5（精确断言）|
| **Phase 2** | Belady Oracle + Hard Protected + Demotion + Warm-up + 参数消融 | gap_closed_ratio 提升可测量；Belady hit_rate ≥ Two-Queue TTL |
| **Phase 3** | 多节点路由仿真 + routing/eviction miss 分解 + 两级缓存 | routing_miss vs eviction_miss 可独立量化 |
| **Phase 4** | vLLM trace 导出 + 仿真精度验证 + A/B 实验监控 | 在线误差 < 10%；A/B 框架 7 天无安全告警 |
