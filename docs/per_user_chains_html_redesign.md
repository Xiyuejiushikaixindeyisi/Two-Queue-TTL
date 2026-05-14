# per_user_chains.html 模型级报告改造 spec

> **创建时间：** 2026-05-14
> **目标：** 把 `per_user_chains.html`（`render_chains_html.py` 输出的**模型级**报告）改造成符合 5 大决策需求 + 图上直接标注分位数
> **当前状态：** spec 已定稿（4 个决策点 + 4 个细节阈值已锁定），暂不编码
> **配套文档：**
> - [`metrics_glossary.md`](metrics_glossary.md) — per-user HTML 指标释义
> - [`step3_algorithm_decision_matrix.md`](step3_algorithm_decision_matrix.md) §9.2 — 算法决策矩阵
>
> **本文 vs 上述文档分工**：本文是**模型级** HTML（`per_user_chains.html`）改造 spec；metrics_glossary 是**用户级** HTML（`user_report.html`）的释义；decision_matrix §9.2 是算法决策规则。

---

## 1. 5 大决策需求（用户给出）

| # | 需求 | 决策用途 |
|---|---|---|
| 1 | 模型级理想 KV cache 命中率 + 虚高 caveat | 整体优化潜力 |
| 2 | 模型级用户偏斜（n_users / req_pct / hit_rate / reuse_inversion）| 路由策略：低 hit + 高流量 user 是否需要隔离 |
| 3 | 模型级请求量时序 + spike 检测 | 流量稳定性 → 弹性扩容必要性 |
| 4 | cache 压力（new block/s + cumulative WS + GB 估计）| C 池化 / offline 扩容决策 |
| 5 | chain 内容（threshold_sweep + global LCP decoded + cross-user LCP）| 共享前缀业务画像 |

---

## 2. 现状 vs gap

### 2.1 `render_chains_html.py` 现有 5 个 section

| 现有 section | 内容 | 数据源 |
|---|---|---|
| §1 Params | branch_threshold / coverage_threshold / block_size | per_user_chains.json `params` |
| §2 Stats | total_requests / total_users / total_blocks | `stats` |
| §3 User Aggregate | users_with_chain / users_matching_global_50pct_prefix | `user_aggregate` |
| §4 Global Chain | global LCP 长度 + decoded 内容 + branch_points | `global_chain` |
| §5 Per-User Chains | 每 user: req_count / chain_length / cov / decoded | `users[]` |

### 2.2 缺失字段（per_user_chains.json）

| 字段 | 用途 | 复用情况 |
|---|---|---|
| `stats.ideal_hit_rate_aggregate` | 需求 1 | 算法同 per_user_report_analyzer |
| `users[i].ideal_hit_rate` | 需求 2 | 同上 |
| `users[i].request_pct` | 需求 2 | 简单除法 |
| `reuse_inversion_ratio` + `reuse_inversion` (top-level) | 需求 2 | 复用 v2 工具 `compute_model_context` |
| `time_series.requests_per_minute` | 需求 3 | analyze_user 单 pass 算 |
| `traffic_spikes` + `spike_config` | 需求 3 | 复用 v2 工具 `detect_traffic_spikes` |
| `time_series.new_unique_blocks_per_second` | 需求 4 | 单 pass 算 |
| `time_series.cumulative_unique_blocks` | 需求 4 | 单 pass 累加 |
| `requests_per_min_q: {avg, p50, p80, p95, max}` | 需求 3 | 简单分位 |
| `new_unique_blocks_per_sec_q: {avg, p50, p80, p95, max}` | 需求 4 | 简单分位 |
| `threshold_sweep_points: [{threshold, chain_length}]` | 需求 5 | 复用 `chain_threshold_sweep.py` 内部逻辑 |

---

## 3. 4 个架构决策点（已锁定）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| 1 | 是否复用 v2 工具的 detect_traffic_spikes / compute_model_context | **A 复用** | 单点维护 |
| 2 | 是否合并模型级 + per-user HTML | **A 保持独立** | 避免大改；只在 user_report.html 顶部加 "← back to model overview" 链接 |
| 3 | GB 估计是否本次实施 | **A 暂不做** | 精度有争议，等生产标定单一因子后再补（详见 metrics_glossary §6）；本次只标 TODO |
| 4 | threshold_sweep 是 per-model 还是 per-user | **A 保持模型级** | per-user chain 信息已在 §5 解码内容里详细；sweep 模型级一张图够 |

---

## 4. Phase A 数据采集 spec（改 `per_user_chain_analyzer.py`）

per_user_chains.json 顶级新增字段（按 §2.2 表）。

**实现要点**：
- 单 pass 扫描 raw CSV 同时算所有时序 + LCP（参考 v2 工具的 `analyze_user`）
- 复用 v2 工具的 `detect_traffic_spikes` 函数（imports from per_user_report_analyzer）
- 复用 `compute_model_context` 部分逻辑（reuse_inversion / aggregate）

**新增字段位置**：

```jsonc
{
  // 现有字段保持
  "params": {...},
  "stats": {
    // 现有
    "total_requests": ...,
    "total_users": ...,
    "total_blocks": ...,
    "empty_prompts": ...,
    // 新增 (需求 1)
    "ideal_hit_rate_aggregate": float,
    "trace_duration_minutes": float
  },
  "user_aggregate": {...},
  "global_chain": {...},
  "users": [
    {
      // 现有保持 + 新增字段 (需求 2)
      "user_id": ...,
      "request_count": ...,
      "request_pct": float,             // 新
      "ideal_hit_rate": float,          // 新
      "chain_length": ...,
      ...
    }
  ],
  // 新增 top-level (需求 2)
  "reuse_inversion_ratio": float | "inf",
  "reuse_inversion": bool,
  "max_hit_user": str,
  "min_hit_user": str,
  // 新增 top-level (需求 3)
  "time_series": {
    "requests_per_minute":           [{"minute": int, "count": int}],
    "new_unique_blocks_per_second":  [{"second": int, "count": int}],
    "cumulative_unique_blocks":      [{"second": int, "total": int}]
  },
  "traffic_spikes":   [...],
  "spike_config":     {"window_minutes": 5, "threshold_multiplier": 5.0},
  // 新增 top-level (需求 3, 4 分位数)
  "requests_per_min_q":            {"avg": ..., "p50": ..., "p80": ..., "p95": ..., "max": ...},
  "new_unique_blocks_per_sec_q":   {"avg": ..., "p50": ..., "p80": ..., "p95": ..., "max": ...},
  // 新增 top-level (需求 5)
  "threshold_sweep_points": [
    {"threshold": 0.00, "chain_length": ...},
    {"threshold": 0.05, "chain_length": ...},
    // ... 21 points
  ]
}
```

---

## 5. Phase B HTML 渲染 spec（改 `render_chains_html.py`）

### 5.1 §0 模型层关键指标（新）

```
n_users / ideal_hit_rate_aggregate / rpm_avg / unique_rpm_avg / trace_duration_minutes
```

附**红底 caveat**：
> ⚠️ ideal_hit_rate_aggregate 是**字节级 LCP 上界**，相对 vLLM 实际命中率系统性偏高 0–10pp，短 prompt 业务最高可达 30pp。决策应优先看 ratio / 排序而非绝对值。详见 metrics_glossary.md §3。

### 5.2 §1 用户偏斜表（新）

```
┌────────────────────────────────────────────────────────────────────────┐
│ User skew (n=4, reuse_inversion_ratio = 9.5x ⚠️ ≥ 2.0 触发倒置)      │  ← 红底警告
│ max hit: ai.ocr (0.38)   min hit: nebula (0.04)                       │
├──────────────────┬─────────┬──────┬──────────┬──────────┬─────────────┤
│ user_id          │ req_cnt │ pct  │ hit_rate │ chain_len│ vs global   │
├──────────────────┼─────────┼──────┼──────────┼──────────┼─────────────┤
│ nebula           │ 56,000  │ 27.9%│ 0.04 🔴 │   0      │ no_chain    │
│ ai.ocr           │ 49,000  │ 24.5%│ 0.38 🟡 │   20     │ unique      │
│ ...000022        │ 24,000  │ 12.0%│ 0.73 🟢 │   65     │ unique      │
│ S773             │ 22,000  │ 11.3%│ 0.07 🔴 │   0      │ no_chain    │
└──────────────────┴─────────┴──────┴──────────┴──────────┴─────────────┘
```

- 表头：`User skew (n=X, reuse_inversion_ratio = Y.Yx)` —— ratio ≥ 2.0 时表头红底
- 次行：`max hit: <user> (X.XX)   min hit: <user> (X.XX)`
- 表格行按 `req_count desc` 排序
- `hit_rate` 三色（复用 §9.2.2 阈值）：
  - `< 0.30` → 红 🔴 (band-low)
  - `0.30 - 0.60` → 黄 🟡 (band-normal)
  - `> 0.60` → 绿 🟢 (band-high)
- `vs global`: `no_chain` / `same_as_global` / `unique` / `partial_match (X%)` 文本说明

### 5.3 §2 请求量时序（新）

```
┌──────────────────────────────────────────────────────┐
│ Requests per minute · avg=35 /min                    │
│                                                      │
│ 120 ┌────── max=120 ──────────────────╶ ── ─        │ ← 红 dashed
│  80 ┌────── p95=80  ──────────────╶ ── ── ─         │ ← 橙 dashed
│  60 ┌────── p80=60  ─── ── ── ── ─                  │ ← 黄 dashed
│  30 ┌────── p50=30 ──┃───┃── ── ── ── ── ─          │ ← 蓝 dashed
│     │ ┃ ┃ ┃ ┃ ┃ ┃ ┃ ┃ ┃ ┃ ┃ ┃ ┃ ┃ ┃ ┃ ┃             │ (柱状图)
│   0 └──────────────────────────────────────         │
│      0    5    10   15   20   25 ...    (min)        │
└──────────────────────────────────────────────────────┘
┌─────────────┬─────────┬─────────┬─────────┬─────────┐
│ avg: 35/min │ p50: 30 │ p80: 60 │ p95: 80 │ max:120 │  ← stat-item 卡片栏
└─────────────┴─────────┴─────────┴─────────┴─────────┘
```

**视觉要素**：
- 柱状图主图
- **4 条 dashed 水平参考线**（`stroke-dasharray="4,3"`）：
  - P50 蓝 `#3182ce`
  - P80 黄 `#d69e2e`
  - P95 橙 `#dd6b20`
  - Max 红 `#c53030`
- 每条线右端标 `pXX=值`（同色小字）
- 图下方 **5 个 stat-item 卡片**，顺序：`avg → p50 → p80 → p95 → max`（升序）
- 图标题含 `· avg=X /min`

### 5.4 §2.1 流量突变时刻（新）

表格列：`window_start` / `prev_count` / `this_count` / `ratio_to_prev`

警告条：
- `N 个 ≥ 5× 突变 → ⚠️ 建议弹性扩容` 红底（任一突变触发）
- `无 ≥ 5× 突变事件` 灰底（无突变时）

### 5.5 §3 cache 压力 - new block/s（新）

同 §2 结构（柱状图 + 4 dashed 参考线 + 5 卡片）：

```
┌──────────────────────────────────────────────────────┐
│ New unique blocks per second · avg=80 /s             │
│                                                      │
│ 500 ┌────── max=500 ──────────────╶ ─                │
│ 200 ┌────── p95=200 ──────── ── ── ─                │
│ 120 ┌────── p80=120 ── ── ─                          │
│  50 ┌────── p50=50  ── ── ── ── ─                    │
│   0 └────────────────────────────                    │
│      0   100  200  300  400 ...   (sec)             │
└──────────────────────────────────────────────────────┘
┌─────────────┬─────────┬──────────┬──────────┬────────┐
│ avg: 80 /s  │ p50: 50 │ p80: 120 │ p95: 200 │ max:500│
└─────────────┴─────────┴──────────┴──────────┴────────┘

[GB 估计 — TODO 待 model_report.json 补 kv_bytes_per_token 字段后启用]
```

GB 估计在本期暂不实施（4-3A 决策），显示 TODO 提示。

### 5.6 §3.1 累计 unique 线图 - working set（新）

```
┌──────────────────────────────────────────────────────┐
│ Cumulative unique blocks · final = 1.2M              │
│                                                      │
│ 1.2M┌──────────── final = 1,200,000 ──── ─           │ ← 灰 dashed
│     │                                  ╱ ─ ── ─      │
│     │                            ╱╱                  │
│  0.6M┌─── half = 600K ─── ╱╱                         │ ← 灰 dashed (半饱和)
│     │                ╱╱  ↑ T_half = 1240 sec         │
│     │           ╱╱                                   │
│   0 └────────╱╱─────────────────────────             │
│      0   600  1200  1800  2400  ...  (sec)           │
└──────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────┐
│ WS 状态: ✅ 已收敛 (最后 5min 斜率 < 5% 平均斜率) │  ← 绿底
│   C 池化结论: 容量 ≥ 1.2M block 可 hold 全部 unique │
└────────────────────────────────────────────────────┘
```

**视觉要素**：
- 线图：cumulative unique blocks over time
- **2 条 horizontal dashed 灰线**：
  - `final = X` 在 y_max 处
  - `half = X/2` 在 y_max/2 处
- **T_half 标注**：达到 max/2 的时刻（小字垂直标注在 x 轴上方）
- **WS 状态自动判断**（最后 5min 斜率 vs 平均斜率比值）：
  - 比值 `< 5%` → **绿底** "已收敛 → C 池化容量保障可行（≥ final 即可 hold 全部 unique）"
  - 比值 `≥ 5%` → **红底** "持续上升 → WS 无上限，C 收益有限，建议优先 A 路由/B 淘汰"

### 5.7 §4 chain 内容

**§4.1 全局 chain（保留现状 + 加 threshold_sweep）**

```
┌──────────────────────────────────────────────────────┐
│ chain_length vs branch_threshold sweep               │
│                                                      │
│  L ─────────────────┐                                │
│                     │                                │
│                     │                                │
│  0 ─────────────────┴──────────────  → branch_threshold│
│   0.0  0.2  0.4  0.6  0.8  1.0                       │
└──────────────────────────────────────────────────────┘
平台高度 L = 主前缀自然深度
陡降点 X = 主 chain 最弱环节强度
(详见 metrics_glossary.md §4)
```

- 嵌入 threshold_sweep SVG line chart（model-level）
- 复用现有 §4 Global Chain decoded content
- 注明 "跨用户 LCP = global chain" 语义（避免概念混淆）

**§4.2 Global Chain decoded（现有保持）**

### 5.8 §5 Per-User Chains（现有保持 + 加 hit_rate 列）

每 user card header 加 `hit_rate=X.XX` 显示（三色 band）。

### 5.9 通用样式（CSS）

新增 / 复用：
- `.dashed-ref-line`: `stroke-dasharray="4,3" stroke-width="1"` 
- 4 参考线色：P50 `#3182ce` / P80 `#d69e2e` / P95 `#dd6b20` / Max `#c53030`
- `.ws-converged` 绿底 / `.ws-not-converged` 红底
- `.inversion-warning` 红底（reuse_inversion_ratio ≥ 2.0）
- `.spike-warning` 红底 / `.no-spike` 灰底
- 复用 v2 工具：`.band-low/normal/high`、`.missing-field`、`.caveat`

---

## 6. 4 个细节阈值（已锁定 2026-05-14）

| 细节 | 选定值 | 备注 |
|---|---|---|
| 参考线样式 | **dashed**（`stroke-dasharray="4,3"`）| 不抢柱状图视觉 |
| 卡片栏顺序 | **avg → p50 → p80 → p95 → max**（升序）| 与图上从下到上对应 |
| WS 收敛阈值 | **最后 5min 斜率 / 平均斜率 < 5%** | 5% 时判定为收敛（绿底）|
| hit_rate 色标 | **复用 §9.2.2 阈值** `< 0.30 红 / 0.30-0.60 黄 / > 0.60 绿` | 跨工具一致 |

---

## 7. 不在本期 spec 内（后续单独研究）

| 项 | 说明 |
|---|---|
| **GB 估计** (Phase D) | 等生产标定单一因子（详见 metrics_glossary.md §6.4），届时把 `kv_bytes_per_token` 或 `gb_per_our_unique_block` 加进 model_report.json，§3 启用 GB 显示 |
| **per-user threshold_sweep** | per-user chain 信息已在 §5 解码内容里详细，sweep 模型级一张图够 |
| **合并 HTML** | 保持 model-level + per-user 独立；只在 user_report.html 顶部加 "← back to model overview" 链接 |
| **虚高系数标定** | 抽样若干 user 用 tokenizer 验证 token-level hit_rate vs 字节级 hit_rate，得每模型校正系数。需求 (1) caveat 之后可单独做 |

---

## 8. 实施 checklist（编码时勾选）

### Phase A — 数据采集（per_user_chain_analyzer.py）

- [ ] 加 `ideal_hit_rate_aggregate` 字段（model 聚合）
- [ ] 加 `users[i].ideal_hit_rate` + `request_pct`
- [ ] 加 `reuse_inversion_ratio` + `max_hit_user` / `min_hit_user`
- [ ] 加 `time_series.requests_per_minute`
- [ ] 加 `time_series.new_unique_blocks_per_second` + `cumulative_unique_blocks`
- [ ] 加 `requests_per_min_q` + `new_unique_blocks_per_sec_q` 分位数（含 p80）
- [ ] 复用 `detect_traffic_spikes`（import from per_user_report_analyzer）
- [ ] 加 `threshold_sweep_points`（21 阈值点 × chain_length）

### Phase B — HTML 渲染（render_chains_html.py）

- [ ] §0 模型层指标 + 红底 caveat
- [ ] §1 用户偏斜表（reuse_inversion 警告 + max/min user）
- [ ] §1 行 hit_rate 三色
- [ ] §2 请求量时序：柱状 + 4 dashed 参考线 + 5 卡片
- [ ] §2.1 流量突变时刻表 + 警告条
- [ ] §3 cache 压力 - new block/s：柱状 + 4 dashed + 5 卡片 + GB TODO 提示
- [ ] §3.1 累计 unique 线图 + final/half horizontal 灰线 + T_half 标注 + WS 状态自动判断
- [ ] §4.1 threshold_sweep SVG 嵌入
- [ ] §4.2 global chain decoded（现有保持）
- [ ] §5 per-user chains 加 hit_rate 列（三色 band）
- [ ] CSS 加参考线 4 色 / WS 状态 / inversion / spike 样式

### 验证

- [ ] Smoke test：用 v2 smoke test 数据跑 per_user_chain_analyzer → 验证新字段全部产出
- [ ] 跑 render_chains_html → grep 验证 8 个新 section + 4 个 caveat / warning 全部生成
- [ ] 7 模型实测：对比 v2 工具的 user_report.html 数据一致性（model-level hit_rate 应当 ≈ user 加权平均）

---

## 9. 偏差日志（编码后填）

> 实施时把跑出来的实际数据与本 spec 预期不符的地方记在这里。
