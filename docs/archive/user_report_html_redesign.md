# user_report.html 改造 spec (APP 级)

> **创建时间：** 2026-05-14
> **目标：** 把 `user_report.html`（`render_user_report_html.py` 输出的 **APP 级**报告）改造成回答每 user 的**淘汰策略 + prompt 改进**
> **当前状态：** spec 已定稿（5 决策点 + 章节编号 + 列出 bug 排查），暂不编码
> **配套文档：**
> - [`per_user_chains_html_redesign.md`](per_user_chains_html_redesign.md) — **模型级** HTML 改造 spec
> - [`metrics_glossary.md`](metrics_glossary.md) — per-user 指标释义
> - [`step3_algorithm_decision_matrix.md`](step3_algorithm_decision_matrix.md) §9.2 — 算法决策矩阵

---

## 1. 模型级 vs APP 级 chain 算法差异（必读）

两份 HTML 报告**用不同算法**回答不同问题。**chain 数 / 长度看到不一致是设计如此，不是 bug**。

| 维度 | 模型级 `per_user_chains.html` | APP 级 `user_report.html`（本文） |
|---|---|---|
| 核心函数 | **`find_lcp`** (greedy max-child walk) | **`find_chain_forest`** (DFS multi-chain) |
| 每 user 产出 | **1 条 chain**（或 0）| **1-50 条 chain** |
| 算法行为 | 沿 max_child 走，遇 branch_ratio < threshold 即停 | DFS 找全部满足 length+coverage 阈值的叶子 |
| 默认 `branch_threshold` | **0.25** | **0.05** (`mc_branch_threshold`)|
| 默认 `min_chain_length` | 不过滤 | **10** |
| 默认 `max_chains` | 隐式 ≤ 1 | **50** |
| user 覆盖 | **全部** user | 仅 **Top-K** 且 `≥ 1%` 流量 |
| 主目的 | 横向比较所有 user 的**代表 chain**，回答**模型整体路由 (A)**+ **池化 (C)** + **prompt (D)** | 深入剖析 Top-K user 的**多 chain 多样性**，回答 **B 子类型淘汰策略** + **D prompt 精细改进** |
| 受众 | 平台运维 / 架构师 | 业务 / 算法 owner |

**HTML 顶部必加 banner**：

```
ℹ️  此 APP 级报告使用 DFS multi-chain 算法 (mc_branch_threshold=0.05)，
    每 user 可产出多条 chain。模型级报告 (per_user_chains.html) 使用
    greedy max-child walk (branch_threshold=0.25)，每 user 仅 1 条。
    chain 数 / 长度差异是设计如此。详见
    docs/user_report_html_redesign.md §1。
```

---

## 2. 5 个决策（已锁定 2026-05-14）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| 1 | 顶部 banner 说明算法差异 | ✅ 同意 | 防混淆 |
| 2 | 沉淀新 doc | ✅ 同意 | living docs |
| 3 | user vs model 对比可视化 | **B 水平条** | 直观对比 |
| 4 | 推荐队列数算法 | **B 仅算 cov ≥ 10% 的 chain** | 避免维护成本/cache 占用 |
| 5 | reuse time CDF x 轴 | **对数轴** | reuse_time 量级跨度大 (秒-小时) |

---

## 3. HTML 标题编号规则（重要）

**连续编号 1-9，不用 0 / 1.5 / 2.1 / 3.2 等 sub-numbering**。

最终结构：

| § | 标题 | 主要内容 |
|---|---|---|
| **1** | 模型层指标 | n_users (全模型) + ideal_hit + rpm avg/p80 + new_block avg/p80 |
| **2** | user vs model 对比 | 水平条（hit rate / avg blocks per req / unique 占比）|
| **3** | user metrics | requests + 流量占比 + ideal_hit + total/unique_blocks + 占 model unique 比例 |
| **4** | 请求量时序 | 柱状 + 4 user 参考线 + 1 model_p50 参考线 + 5 卡片 + 表格 + user 级 spike |
| **5** | cache 压力 | new_block/s 柱状 + 4 user 参考线 + 1 model_p50 参考线 + 5 卡片 + 表格 + cumulative WS + 状态 |
| **6** | reuse time CDF | SVG line chart (对数 x 轴) + avg/P50/P80/P95 表 |
| **7** | Per-request LCP | histogram + P30/P50/P80/P95/Max 表 + Top 10 LCP 表 + 反常诊断 |
| **8** | Chain forest | decoded chain cards + chain 分叉虚高诊断 |
| **9** | 算法推荐 | 子类型 + 建议队列数 |

**去除**：
- 旧 §1.5 分类标签（用户不需要）
- 旧 §0.1 model spike（已在 §1 模型层指标整合）
- 旧 §1 中的 trace span (s)（用户要求去掉）
- 旧 §0 中的 model_params_class / instance_count / cache_capacity_blocks 三个人工补字段（去除）

---

## 4. 各 section 实施 spec

### §1 模型层指标

**显示字段**（4 项 + n_users）：

| 字段 | 数据源 | 用途 |
|---|---|---|
| `n_users` (**全模型 user 数，非 selected**) | model_report.json `n_users_total`（**新字段**，原 `n_users` 是 selected 数 = 误用）| 模型规模 |
| `ideal_hit_rate_aggregate` | model_report.json `ideal_hit_rate_aggregate` | 模型整体命中率 |
| `rpm_avg` + `rpm_p80` | model_report.json `rpm_avg` + `requests_per_min_q.p80`（**新字段**）| 流量基准 |
| `unique_rpm_avg` + `new_block_p80` | model_report.json `unique_rpm_avg` + `new_unique_blocks_per_sec_q.p80`（**新字段**）| cache 压力基准 |

**去除字段**：`model_params_class` / `instance_count` / `cache_capacity_blocks` / 旧 spike 列表（spike 不在 §1 显示）。

### §1 bug 排查（用户报告）

用户反馈：实测有的模型 **`rpm_avg = 0`** / **`unique_rpm_avg = 0`**。

**可能原因**：

1. **trace timestamp 解析失败**：raw CSV 中 `timestamp` 列为非数字、`-`、空字符串 → `int(float(ts))` 报错 → fallback 0
2. **timestamp 全为 0**：所有 record 的 ts 都 fallback 0 → `earliest_ts = latest_ts = 0` → `duration = 0` → 除零 → 返回 0.0
3. **单 user trace 且单 ts**：`earliest_ts == latest_ts`（极端情况）
4. **timestamp 单位错误**：ms 而非 s，但实际不会让 duration=0，只会让数值变大

**排查 + 修复 checklist**：

- [ ] `iter_raw_records` 增加 ts 解析失败计数 + 日志（当前默默 fallback 0）
- [ ] `compute_model_context` 增加 caveat：`trace_duration_seconds == 0` 时 HTML 显示警告"trace duration = 0，RPM / unique_rpm 不可靠"
- [ ] HTML §1 渲染时如果 rpm_avg == 0：显示红字 + 链接到本节排查 checklist
- [ ] 在 model_report.json 输出 `ts_parse_failed_count`（多少 row ts 解析失败）

### §2 user vs model 对比

**3 个水平条**：

```
Hit rate
user  ████████████░░░░░░░░░░░░░░  0.27 (-0.19 vs model)
model ███████████████████░░░░░░░  0.46

Avg blocks/req
user  ██████░░░░░░░░░░░░░░░░░░░░  3.4
model ███████████████████░░░░░░░  20.0

Unique 占比
user  ████░░░░░░░░░░░░░░░░░░░░░░  10.8% (low share)
```

**文字诊断**（基于 hit + share 组合）：
- `user_hit < model_hit AND share > 30%` → 红底 "**污染源**：低复用 + 高 cache 写入，建议 A(1) isolation"
- `user_hit > model_hit AND share < 5%` → 绿底 "**良性轻量**：高复用 + 低 cache 占用"
- 其他 → 中性

### §3 user metrics

**显示**（5 项）：
- `requests` (绝对数 + **流量占比**)
- `ideal_hit_rate`
- `total_blocks`
- `unique_blocks` (绝对数 + **占模型 unique 比例**)
- `hit_blocks`

**去除**：`trace span (s)`、`chains found`（移到 §8）

### §4 请求量时序

新增和修改：

| 项 | 内容 |
|---|---|
| 柱状图 | req/min（保留 v2）|
| 4 dashed 参考线 | user 自己的 P50（蓝）/ P80（黄）/ P95（橙）/ Max（红）|
| **+1 dashed 参考线** | **model 的 rpm_p50**（绿色 #2f855a，区分 user 红色系参考线，标注 `model p50=X`）|
| 5 stat-item 卡片 | avg → p50 → p80 → p95 → max（升序）|
| **+ 表格** | P50 / P80 / P95 / Max 数字纵向显示 |
| **+ user 级 spike 时刻表** | 复用 `detect_traffic_spikes(user_req_per_window, ...)`；现状 §0.1 只有 model 级，现在每 user 独立检测 |

### §5 cache 压力

同 §4 结构（new_block/s 替代 req/min）：

| 项 | 内容 |
|---|---|
| 柱状图 | new_block/s（保留 v2）|
| 4 dashed 参考线 | user 自己 P50/P80/P95/Max |
| **+1 dashed 参考线** | **model 的 new_block_p50**（绿色） |
| 5 stat-item 卡片 | 同 §4 |
| **+ 表格** | P50/P80/P95/Max 数字 |
| cumulative WS + 状态判断 | 保留 v2 |
| GB 估计 | TODO（Phase D 等生产标定） |

### §6 reuse time CDF（新）

**定义**：一个 prefix_path_key 被复用时，前后两次访问之间的时间间隔（秒）。block 维度，不是 request 维度。

**算法**：

```python
last_seen_ts: dict[bytes, int] = {}
reuse_times: list[int] = []

for rid, ts, prompt in sorted_records:
    blocks = split_blocks(prompt, block_size)
    keys = compute_prefix_path_keys(blocks)
    for k in keys:
        if k in last_seen_ts:
            gap = ts - last_seen_ts[k]
            reuse_times.append(gap)        # 相邻两次访问的间隔
        last_seen_ts[k] = ts                # 更新 last_seen
```

**数据字段**（新增到 user_report.json）：

```jsonc
{
  "reuse_time_quantiles": {
    "avg": float, "p50": int, "p80": int, "p95": int, "max": int,
    "count": int   // 复用事件总数 (block 被复用次数)
  },
  "reuse_time_cdf_points": [
    {"t_seconds": int, "cumulative_pct": float}
    // ~50 个采样点，按对数轴间距取
  ]
}
```

**HTML**：

```
┌──────────────────────────────────────────────────────┐
│ Reuse time CDF (block-level reuse interval)         │
│                                                      │
│ 100%┌─────────────────────╶ ── ─                    │
│  95%┌── p95=180s ─────╶ ── ─                        │
│  80%┌── p80=60s ──╶ ── ─                            │
│  50%┌── p50=15s ──╶ ── ─                            │
│   0%└─────────────────────────                       │
│      1     10    100   1000  10000  (log s)         │
└──────────────────────────────────────────────────────┘
┌─────────┬─────────┬─────────┬──────────┬───────────┐
│ avg: 32s│ p50: 15s│ p80: 60s│ p95: 180s│ count: 12K│
└─────────┴─────────┴─────────┴──────────┴───────────┘
```

x 轴对数（决策 5）；y 轴 0-100% 累积概率。

**解读说明**（HTML 提示）：
- `reuse_time p50 >> inter_arrival p50`：block 复用比相邻 request 慢，**LRU 不够，需要 chain pin 或 B(2) 多队列**
- `reuse_time p50 ≈ inter_arrival p50`：复用紧跟请求，**LRU 友好**，B(1) 即可

### §7 Per-request LCP（修订）

**数据字段新增**：

```jsonc
{
  "lcp_distribution": {
    "quantiles": {
      "p30": int,    // 新
      "p50": int,
      "p80": int,    // 新
      "p95": int,
      "max": int
    },
    "top10_lcp_values": [
      {"lcp_value": int, "request_count": int}
      // 取 lcp_counter.most_common(10)
    ]
  }
}
```

**HTML 增加**：
- P30/P50/P80/P95/Max 表（现有只有 p50/p95）
- Top 10 LCP 值表（LCP block 数 + 命中 request 数）
- **反常诊断说明**（基于 hit_rate vs chain_length 组合）：

| 现象 | 解读 | 业务暗示 |
|---|---|---|
| Top-10 集中在 `0` 且数量大 | hit_rate 低；大量 request 冷启动 | 单租户 / unique 极高 user |
| Top-10 集中在 `chain_length` 附近 | hit_rate 高；chain 真的"被走完" | 成熟 chain 业务 |
| Top-10 分散 + chain_length 短 | **hit_rate 高但 chain 短**：长文档复用（非 chain 部分共享） | Qwen-64K 长文档 user |
| Top-10 集中在 0 但 chain_length 长 | **hit_rate 低但 chain 长**：chain 是 death chain（已弃用的旧 system prompt） | 业务方需检查 prompt 漂移 |

### §8 Chain forest（修订）

现有 chain decoded cards 保持。新增 **chain 分叉虚高诊断**。

**算法**（chain 间前缀重合检测）：

```python
chain_shadow_pairs = []
for i in range(len(chains)):
    for j in range(i + 1, len(chains)):
        ci_keys = [b["prefix_path_key"] for b in chains[i]["decoded_content"]]
        cj_keys = [b["prefix_path_key"] for b in chains[j]["decoded_content"]]
        common = 0
        for k1, k2 in zip(ci_keys, cj_keys):
            if k1 == k2:
                common += 1
            else:
                break
        if common > 0:
            chain_shadow_pairs.append({
                "chain_a": i, "chain_b": j,
                "shared_prefix_blocks": common,
                "chain_a_length": len(ci_keys),
                "chain_b_length": len(cj_keys),
                "ratio_a": common / len(ci_keys),
                "ratio_b": common / len(cj_keys),
            })
```

**数据字段**：

```jsonc
{
  "chain_shadow_pairs": [
    {"chain_a": 0, "chain_b": 1,
     "shared_prefix_blocks": 100,
     "chain_a_length": 150, "chain_b_length": 130,
     "ratio_a": 0.67, "ratio_b": 0.77}
  ]
}
```

**HTML**：

如果 `chain_shadow_pairs` 非空 → 顶部加红底警告：

```
⚠️ chain 数量可能虚高：
  - chain 0 (150 block) ↔ chain 1 (130 block): 前 100 block 完全一致
    (chain_a 67% / chain_b 77% 重合)
  
可能原因：system prompt 输入不全 (前 100 block 是共享前缀,
后续被 trie 视为不同 branch 因为 prompt 末段不同)。
建议：人工检查 decoded content，必要时合并 chain 或调整
mc_branch_threshold 让 trie 在更早位置合并。
```

### §9 算法推荐（修订）

现有子类型保持。**新增建议队列数**。

**算法**（决策 4B）：

```python
effective_chains = [c for c in chains if c["coverage_pct"] >= 10.0]
queue_count_suggestion = len(effective_chains)
```

**HTML 显示**（在 §6 推荐 §6 → §9 后 B(2) 子类型 badge 下方）：

```
B(2) 多队列 LRU 推荐队列数: 3 (基于 chain forest, 仅算 cov ≥ 10% 的 chain)
  Chain 0: cov=46.6% → 独立队列
  Chain 1: cov=15.0% → 独立队列  
  Chain 2: cov=10.8% → 独立队列
  Chain 3: cov=5.0%  → 合并到 LRU 共享 (cov 不足 10%, 维护成本不划算)
  Chain 4: cov=3.2%  → 合并到 LRU 共享

避免队列数过多导致维护成本高 / cache 占用大。
```

---

## 5. 数据采集层 spec (Phase C — per_user_report_analyzer.py 改造)

### 5.1 新增字段

| 字段 | 写入文件 | 用途 |
|---|---|---|
| `n_users_total` | model_report.json | §1 显示全模型 user 数（不只 selected）|
| `requests_per_min_q.p80` | model_report.json | §1 显示 + §4 model_p50 参考线 |
| `new_unique_blocks_per_sec_q.p80` | model_report.json | §1 显示 + §5 model_p50 参考线 |
| `ts_parse_failed_count` | model_report.json | bug 排查日志 |
| `reuse_time_quantiles` | user_report.json | §6 |
| `reuse_time_cdf_points` | user_report.json | §6 SVG |
| `lcp_distribution.quantiles.p30` + `.p80` | user_report.json | §7 |
| `lcp_distribution.top10_lcp_values` | user_report.json | §7 |
| `chain_shadow_pairs` | user_report.json | §8 |
| `user_traffic_spikes` | user_report.json | §4 user 级 spike |
| `recommended_queue_count` | user_report.json `step3_recommendation` | §9 |

### 5.2 修改字段

| 字段 | 当前 | 改后 |
|---|---|---|
| `n_users` | selected user 数 | **改为字面意思: selected user 数；新增 `n_users_total` = 全模型** |
| `rpm_avg = 0` 容错 | 默认返回 0.0 | 增加 `trace_duration_caveat` 字段标记不可靠 |

### 5.3 bug 修复

`compute_model_context` 加：
```python
if trace_duration_seconds == 0:
    model_context["trace_duration_caveat"] = (
        "trace_duration = 0 (earliest_ts == latest_ts); "
        "rpm_avg / unique_rpm_avg 不可靠"
    )
```

`iter_raw_records` 加：
```python
ts_parse_failed = 0
try:
    ts_int = int(float(ts))
except (ValueError, TypeError):
    ts_int = 0
    ts_parse_failed += 1
# 累计到 model_report.json
```

---

## 6. HTML 渲染层 spec (Phase D — render_user_report_html.py 改造)

### 6.1 整体重构

**顶部 banner**（§1 算法差异说明）：

```html
<div class="algo-diff-banner">
  ℹ️ 此 APP 级报告使用 DFS multi-chain (mc_branch_threshold=0.05)，每 user
  可产出多条 chain。模型级报告 per_user_chains.html 使用 greedy max-child
  walk (branch_threshold=0.25)，每 user 仅 1 条。差异是设计如此。
  详见 docs/user_report_html_redesign.md §1。
</div>
```

### 6.2 9 个 section 按 §3 编号实施

每个 section 实施细节见 §4 子节。

### 6.3 删除现有

- §0 模型层 → 改为 §1
- §0.1 流量突变 → 删除（model spike 在 §1 显示）
- §1 Key metrics → 拆分到 §2 对比 + §3 user metrics
- §1.5 分类标签 → 删除
- §2 → §4
- §3 → §5
- §3.1 cumulative WS → 合并到 §5
- §4 → §7
- §5 → §8
- §6 → §9

### 6.4 新增 SVG 函数

- `svg_horizontal_bar_compare` (§2 用户 vs 模型水平条对比)
- `svg_cdf_log_x` (§6 reuse time CDF, 对数 x 轴)

---

## 7. 不在本期 spec 内（后续单独研究）

| 项 | 原因 |
|---|---|
| GB 估计 | 等生产标定单一因子（metrics_glossary.md §6.4）|
| 反常诊断自动业务标签 | 现在仅文本提示，业务自动识别复杂 |
| user 间 shadow group 检测 | 现在仅 chain 间（同 user 内），跨 user shadow 是另一议题 |
| reuse time vs 实际 cache 存活时间对比 | 需要 vLLM 实测数据接入，超出 offline 工具范围 |
| 多 chain 自动合并建议 | chain_shadow_pairs 仅给警告，不自动合并 |

---

## 8. 实施 checklist

### Phase C — 数据采集（per_user_report_analyzer.py）

- [ ] 修 `rpm_avg / unique_rpm_avg = 0` bug：trace_duration_caveat + ts_parse_failed_count
- [ ] `n_users_total` (全模型 user 数, 来自 collect_user_counts 字典长度)
- [ ] model_report.json 加 `requests_per_min_q` + `new_unique_blocks_per_sec_q` 5 分位
- [ ] user 级 spike 检测 (复用 detect_traffic_spikes, 用户 req_per_window)
- [ ] `reuse_time_quantiles` 单 pass 算 (last_seen_ts dict + reuse_times list)
- [ ] `reuse_time_cdf_points` 50 个对数采样点
- [ ] `lcp_distribution.quantiles.p30` + `p80`
- [ ] `lcp_distribution.top10_lcp_values` (Counter.most_common)
- [ ] `chain_shadow_pairs` (chain 间前缀重合)
- [ ] `step3_recommendation.recommended_queue_count` (仅 cov ≥ 10% 的 chain)

### Phase D — HTML 渲染（render_user_report_html.py）

- [ ] 顶部 banner 算法差异说明
- [ ] §1 模型层指标 (去人工补字段, 加 rpm_p80 / new_block_p80, n_users 含义修正)
- [ ] §2 user vs model 3 水平条 + 文字诊断（污染源 / 良性轻量）
- [ ] §3 user metrics (流量占比 + unique 占比, 去 trace_span / chains_found)
- [ ] §4 请求量时序 (4 user 参考线 + 1 model_p50 绿参考线 + 5 卡片 + P50/P80/P95/Max 表 + user spike)
- [ ] §5 cache 压力 (同 §4 结构 + cumulative WS 状态保留)
- [ ] §6 reuse time CDF (对数 x 轴 + avg/P50/P80/P95 表 + LRU 解读说明)
- [ ] §7 LCP (histogram + P30/P50/P80/P95/Max 表 + Top 10 表 + 反常诊断 4 类)
- [ ] §8 chain forest (现有 + chain_shadow_pairs 警告)
- [ ] §9 算法推荐 (现有 + 建议队列数)
- [ ] 删除旧 §0/§0.1/§1.5
- [ ] 重命名 SVG 函数 (svg_horizontal_bar_compare + svg_cdf_log_x)

### 验证

- [ ] Smoke test (3-user synthetic) 验证所有 9 section + banner 渲染
- [ ] rpm/unique_rpm bug 修复后实测某些模型 (用户报告的)
- [ ] reuse time CDF 数值合理性 (与 inter_arrival 对比)
- [ ] chain_shadow_pairs 算法验证 (人工核对 GLM tianzhou 7 chain 是否触发)

---

## 9. 偏差日志（编码后填）

> 实施时把跑出来的实际数据与本 spec 预期不符的地方记在这里。
