# Step 1.5 实验设计 — User-Level Research + Multi-Chain Forest

> **创建时间：** 2026-05-11
> **状态：** 设计定稿（D1–D8 全部 ack，代码实现待启动）
> **上游：** [`docs/3step_validation_plan.md`](3step_validation_plan.md) §2 / [`docs/model_portraits.md`](model_portraits.md) §3.3
> **下游产物：** `scripts/per_user_report_analyzer.py` + `scripts/multi_chain_finder.py` + `scripts/render_user_report_html.py`（待实现）

本设计作为 Step 1 → Step 2 之间的最后一道实验，把分析颗粒度从"模型级"下沉到"user 级"，并升级 chain 算法为 multi-chain forest，回填 portraits §3.3 揭示的 "DS-8K / GLM / DS-32K 都是多 prompt 并行" 这一观察。

---

## 1. 实验目标

模型级画像（[`model_portraits.md`](model_portraits.md)）揭示了 trace 形状层面的共性差异，但 **Step 3 算法选择最终落到 user 颗粒度**——pin 哪些 chain、哪些用户走 LRU、cache 容量怎么分配，都要以 user 为决策单位。当前 `per_user_chains.json` 给的每 user chain length 和覆盖率不足以判断"该 user 是否值得 pin"，缺四类信息：

1. 该 user 自身的 **复用能力上界**（user-internal ideal hit rate，对齐 vLLM block-level 命中口径）
2. 该 user 的 **请求到达模式**（间隔分布 + 时序）
3. 该 user 的 **cache 压力贡献**（new unique block/s 时序）
4. 该 user 的 **chain 结构是 1 条还是 N 条**（chain forest）

第 4 点同时回应 portraits §3.3 的发现：当前贪心单一主子的 `find_lcp` 在多 prompt 并行结构下**系统性低估真实 chain 数量**，必须升级为 multi-chain。

**收益预期：**
- 每个高请求量 user 一份独立 HTML 报告，业务方可读、算法方可决策 pin 策略
- multi-chain forest 成为通用 primitive，回头可打 1.1 / 1.2 的盲点
- 为 Step 3 的目标函数 `request_count × (1 − hit_rate) × chain_length` 准备所需输入

---

## 2. 输入 / 输出契约

**Input：** 一份 raw CSV 目录（与 1.1 / 1.2 接口完全一致；不依赖任何模型特定信息、不依赖 LLM tokenizer）

**Output 目录结构：**

```
outputs/<dataset>/per_user_reports/
├── user_summary.json              # 全部候选 user 的汇总指标（机器可读）
├── user_summary.csv               # 同上的扁平版本（Excel / 跨模型横向对比）
├── <user_id_1>/
│   ├── user_report.json           # 该 user 的完整原始指标
│   ├── chain_forest.json          # 该 user 的 multi-chain 输出（独立文件，便于复用）
│   └── user_report.html           # 渲染后的人读报告
├── <user_id_2>/
│   └── ...
└── <user_id_3>/
    └── ...
```

报告数量 ≤ 3，可能少（见 §3 筛选规则）。

---

## 3. 用户筛选规则

两条门槛**同时**满足才入选：

1. 按 `request_count` 降序排序，**取前 3 名**
2. 同时要求 `request_count / total_requests ≥ 1%`（即 `--min-request-pct 0.01`）

**示例：**
- Qwen-V3-8B-8K / GLM-V5.1（单租户） → 输出 1 份
- Qwen-V3.5-27B-64K（4 用户，Top10% 占 95.2%） → 通常输出 3 份
- Qwen-V3-32B-8K（37 用户，长尾） → 仍只取 Top-3

筛选结果由 `user_summary.json` 的 `selected_users` + `excluded_users`（含排除原因）记录，可追溯。

---

## 4. 四项指标的算法

### 4.1 理想 KV cache 命中率（user-internal，对齐 vLLM block-level 口径）

**定义：** 该 user 提出的请求中**能够被重用**的所有 block 数 / 该 user 提出的请求所有 block 数。"能被重用"采用 vLLM cache 视角：cache 第一次写入是 miss，后续读到同样 `prefix_path_key` 是 hit。

**算法（与 §4.3 共享 `seen_keys` 集合，一次遍历）：**

```python
seen_keys: set = ∅
hit_blocks = 0
total_blocks = 0
new_per_sec: dict[int, int] = defaultdict(int)        # §4.3 共用

# 按 timestamp 升序遍历该 user 的请求
for request in user_requests_sorted_by_ts:
    blocks = split_blocks(request.raw_prompt, block_size=128)
    keys   = compute_prefix_path_keys(blocks)
    total_blocks += len(keys)
    
    for k in keys:
        if k in seen_keys:
            hit_blocks += 1                            # vLLM hit
        else:
            seen_keys.add(k)
            new_per_sec[request.timestamp] += 1        # §4.3 时序

ideal_hit_rate = hit_blocks / total_blocks
```

**两个等价性需要在报告 caveat 标注：**

1. `prefix_path_key = hash(parent_key + block_content)`——哈希链性质保证 **block i 命中 ⟹ block 1..i 全部命中**，即 set 查表语义与 trie LCP 语义完全等价
2. hit pattern 等同于"每个 request 的 LCP 长度"。报告 HTML 可额外画 LCP 长度直方图，便于业务方观察"哪些请求几乎全 hit、哪些请求几乎全 miss"

### 4.2 请求量时序

**请求间隔分位数：**

```
对该 user 的请求按 timestamp 升序排序
gaps[i] = timestamp[i+1] - timestamp[i]
输出: p50, p75, p80, p95 (秒)
```

**requests/min 随时间变化图：**

```
bucket_size = 60 秒
buckets[ts // 60] += 1
画图: x = bucket_index (相对采样开始的分钟数), y = bucket count
```

**Caveat（必须在 HTML 报告顶部 + 图表底部双层标注）：**

timestamp 是整数秒精度，无法升级到 ms 或 µs。同一秒内多请求 gap=0，会让 P50/P75 偏小、batch 内请求被误算成"密集到达"。1s 边界附近的统计**不可信**。

### 4.3 New unique block/s 随时间变化

**算法（与 §4.1 共用 `seen_keys`，无需第二次遍历）：**

```python
# 在 §4.1 循环中已经累计 new_per_sec
画图: x = timestamp_sec, y = new_per_sec[ts]

# 同时输出累计曲线
cumulative[i] = sum(new_per_sec[ts] for ts <= i)
画图: x = timestamp_sec, y = cumulative
```

**两个图同时输出：**
- 瞬时 new block/s（柱状/散点）：观察 burst 模式
- 累计 unique block 总数（折线）：与 portraits §0.3 的 `WS` 概念对齐

**Caveat：** 同 4.2，秒精度。

### 4.4 Chain forest（multi-chain）

详见 §5。在该 user 自己的 trie 上跑 chain forest，输出该 user 的全部"独立 chain"。

---

## 5. Multi-Chain Forest 算法详细设计

### 5.1 设计原则

当前 `find_lcp` 是 `root → max_child → max_child → ... → STOP` 的**单一贪心路径**。multi-chain 改为**递归探索所有强势分支**：

- 在每个 trie 节点，**不是只选最重子节点**，而是把**所有满足阈值的子节点**都视为独立分支起点
- 对每个分支独立递归 LCP
- 收集所有 leaf（即 chain 终点）的路径作为 chain forest

**核心权衡：** 递归会产生指数级 chain，必须用**阈值 + 输出剪枝**控制。

### 5.2 递归算法伪代码

```python
def multi_chain(node, mc_branch_thr, mc_cov_thr,
                total_requests, path_keys=[], path_counts=[]):
    """递归收集所有满足阈值的 chain。"""
    chains_here = []
    
    # 当前节点本身可作为一条 chain 终点
    if len(path_keys) > 0:
        chains_here.append({
            "keys":   path_keys.copy(),
            "counts": path_counts.copy(),
        })
    
    # 找所有满足阈值的子节点
    eligible = []
    for k, child in node.children.items():
        cov   = child.count / total_requests
        ratio = child.count / node.count
        if cov >= mc_cov_thr and ratio >= mc_branch_thr:
            eligible.append((k, child))
    
    if not eligible:
        return chains_here    # 当前节点是 leaf chain
    
    # 对每个 eligible child 递归
    for k, child in eligible:
        chains_here.extend(
            multi_chain(child, mc_branch_thr, mc_cov_thr,
                        total_requests,
                        path_keys + [k], path_counts + [child.count])
        )
    
    return chains_here
```

**与单 chain `find_lcp` 的关键差异：**

| 维度 | 单 chain (1.1) | multi-chain |
|---|---|---|
| 节点处选择 | 只走 `max_child` | 走**所有** `ratio ≥ branch_thr ∧ cov ≥ cov_thr` 的 child |
| 输出 | 单条 chain（一条 leaf 路径） | chain 集合（多条 leaf 路径） |
| 终止 | 阈值不过 STOP | 阈值不过则不递归该 child；node 本身仍是合法 chain 终点 |
| 复杂度 | O(chain_length) | O(chain_count × avg_chain_length) — 受阈值控制 |

### 5.3 阈值设计（与单 chain 完全解耦）

**为什么不能沿用单 chain default：** 单 chain `branch_threshold=0.45` 在 multi-chain 模式下**几乎肯定误杀**少数派 system prompt。验证：

| 模型 | root 处某 system prompt 的 ratio | 单 chain default (0.45) | multi-chain default (0.05) |
|---|---|---|---|
| GLM-V5.1（8 条 prompt） | 0.15 | 全部拦截 | 全部通过 |
| DS-8K（10+ 条 prompt） | 主流 0.42 / 其他 0.05–0.10 | 只剩 1 条 | 全部通过 |
| DS-32K（多租户长 prompt） | 各租户 5–15% | 大部分拦截 | 全部通过 |

**因此 multi-chain 模式使用独立 namespace `--mc-*`，default 显著放松：**

| 参数 | multi-chain default | 对应单 chain default | 理由 |
|---|---|---|---|
| `--mc-branch-threshold` | **0.05** | 0.45 | 不误杀 cov ≥ 5% 的少数派分支；深层节点用它筛 noise |
| `--mc-coverage-threshold` | **0.05** | 0.05 | 与单 chain 一致；保留"全局 ≥ 5% 才有 ROI"语义 |

注意：在 root 节点上，`branch_threshold` 与 `coverage_threshold` 因 `root.count = total_requests` 而**几乎等价**（前者退化为后者）。`branch_threshold` 的差异化作用主要在深层节点——避免某 child 的 `cov` 通过但 `ratio` 极小（仅占父节点 1‰）这种 noise child 被递归进去。

### 5.4 输出剪枝（防 chain forest 爆炸）

光靠双阈值不够——比如某节点分叉成 5 个都满足 `ratio ≥ 0.05`，下面又各分 5 个 → 指数爆炸。**额外三层剪枝：**

| 参数 | default | 含义 |
|---|---|---|
| `--mc-min-chain-length` | **10** | chain 长度 < 10 不输出（避免 1–9 block 的 stub） |
| `--mc-min-chain-coverage` | **0.01** | chain 末节点 cov < 1% 不输出（防长尾 chain 爆炸） |
| `--mc-max-chains` | **50** | 总输出 chain 数上界；按 cov 降序取 top-N，溢出在 HTML 标注 "additional N chains pruned" |

**剪枝顺序：** 递归阶段已经被双阈值过滤；递归完成后再过 `min_chain_length` + `min_chain_coverage`，最后按 cov 排序取 `max_chains`。

### 5.5 输出 schema（`chain_forest.json`）

```json
{
  "params": {
    "mc_branch_threshold": 0.05,
    "mc_coverage_threshold": 0.05,
    "mc_min_chain_length": 10,
    "mc_min_chain_coverage": 0.01,
    "mc_max_chains": 50,
    "block_size": 128
  },
  "stats": {
    "total_chains_before_pruning": 87,
    "total_chains_after_length_pruning": 41,
    "total_chains_after_coverage_pruning": 23,
    "total_chains_after_max_cap": 23,
    "trie_total_requests": 5421,
    "trie_total_nodes": 124803
  },
  "chains": [
    {
      "chain_id": 0,
      "chain_length": 56,
      "coverage_count": 2278,
      "coverage_pct": 42.0,
      "max_prefix_coverage_pct": 80.0,
      "coverage_pcts": [80.0, 80.0, 75.5, 60.1, 42.0, ..., 42.0],
      "branch_at_root_position": 0,
      "branch_at_root_ratio": 0.80,
      "decoded_content": [
        {"position": 0, "prefix_path_key": "...", "count": 5421, "decoded_text": "..."},
        ...
      ]
    },
    {
      "chain_id": 1,
      "chain_length": 47,
      "coverage_count": 943,
      "coverage_pct": 17.4,
      "max_prefix_coverage_pct": 23.5,
      "coverage_pcts": [23.5, ..., 17.4],
      "branch_at_root_position": 0,
      "branch_at_root_ratio": 0.24,
      ...
    },
    ...
  ]
}
```

`branch_at_root_position` + `branch_at_root_ratio` 帮助回答"chain 1 和 chain 2 在 trie 上是从第几个 block 起分叉的"——对应"system prompt 是否共享前缀片段"。

**v2 新增字段（2026-05-12）—— prefix coverage**：

`coverage_pcts[]` 给出 chain 上**每个 position** 的覆盖率（counts[i] / total × 100）。由于 trie 节点 count 沿 chain 单调非增，`coverage_pcts` 同样单调非增。

- `max_prefix_coverage_pct = coverage_pcts[0]` —— chain 第一个 block 的覆盖率（即 chain root 处最高 cov）
- `coverage_pct = coverage_pcts[-1]` —— chain leaf 处覆盖率（原字段，向后兼容）
- `max_prefix - leaf` 揭示 chain 在 trie 上的**衰减幅度**：差值越大，说明这条 chain 内部分叉越多，能 pin 的"高 cov 前缀段"越短

这是 portraits §3.7 提出的局限的修复——解决 chipset2 这种 "leaf cov 5–8% 但 max prefix cov >> leaf" 的现象，让 Step 3 算法能选择 pin 到 prefix 上 cov 衰减前的最佳位置。

`coverage_count` 仍然指 leaf count（未改语义，保持向后兼容）。

**v3 prefix shadow detection — 尝试 + 撤回（2026-05-12）**：

曾尝试给 `chain_forest.json` 加 `semantic_prefix_overlap` 字段，对 chain pairwise 算 byte-level LCP + union-find 自动识别 shadow group。生产数据上**双向失败**：

- **False positive**：JSON wrapper（`{"model":...,"stream":true,"messages":[{"role":"system","content":"`）几乎在所有请求里都存在，70+ byte 共享被算成 shadow，但它不是真业务 shadow
- **False negative**：人工标注的 4 个真实 shadow case 全部漏报。可能原因：JSON 字段顺序不固定 / 动态字段（request_id / seed / timestamp）插入到 wrapper 中 / chain 间有 1–2 byte 微差

两个 failure mode 互斥——调高阈值减少 false positive 必然加重 false negative，反之亦然。**byte-level LCP 单一信号无法区分 wrapper boilerplate 和业务 prompt 共享**——这需要语义层信息（JSON 解析 + token 级 fuzzy match），违反 plan §0.1 "不依赖 LLM tokenizer" 约束。

**结论：shadow detection 不能自动化**。`chain_forest.json` 不再输出 `semantic_prefix_overlap` 字段，HTML 不再有 shadow 紫色标注，CLI 参数 `--shadow-min-bytes` 已删除。**shadow case 由人工 inspect HTML 的 chain decoded content 头部标注**，沉淀进对应模型的 findings 文档。

**保留**：v2 `coverage_pcts` / `max_prefix_coverage_pct` 字段——这是单 chain 内部信号，没有 wrapper vs 业务的歧义。

---

## 6. 工程拆解

**新建 3 个模块（互相解耦）：**

| 模块 | 职责 | 输入 | 输出 | 复用价值 |
|---|---|---|---|---|
| `scripts/multi_chain_finder.py` | 纯算法 primitive | `TrieNode` 实例 + 阈值（程序内调用，不直接 CLI） | `List[chain dict]` | **可回头给 1.1 / 1.2 加 `--multi-chain` flag** |
| `scripts/per_user_report_analyzer.py` | 编排器 | raw CSV + 全部 CLI 参数 | `user_summary.json` + `user_summary.csv` + 每 user 的 `user_report.json` + `chain_forest.json` | per-user 流程入口 |
| `scripts/render_user_report_html.py` | JSON → HTML | `user_report.json` + `chain_forest.json` | `user_report.html` | per-user HTML 渲染 |

**复用现有 primitives：** `verify_chain_path_closure.py` 的 `TrieNode` / `trie_insert` / `compute_prefix_path_keys` / `split_blocks` / `iter_raw_records`。

**HTML 渲染技术栈：** 用 inline SVG（matplotlib `savefig(fmt='svg')` 或纯 Python 写 SVG）画时序图，HTML 自包含、离线可读、不依赖 JS。

**这次实验不动 1.1 / 1.2：** multi-chain primitive 先单独验证，避免改动面太大。1.1 / 1.2 加 `--multi-chain` flag 是 follow-up 工作。

---

## 7. 参数解释表（CLI 全集）

### 7.1 `per_user_report_analyzer.py`

#### 必填参数

| 参数 | 类型 | 含义 | 备注 |
|---|---|---|---|
| `--raw-csv` | Path | raw CSV 文件或目录 | 同 1.1 / 1.2 接口；UTF-8 / UTF-8 BOM 自动识别 |
| `--output-dir` | Path | 输出根目录 | 推荐 `outputs/<dataset>/per_user_reports` |

#### 通用参数

| 参数 | Default | 类型 | 含义 | 调参建议 |
|---|---|---|---|---|
| `--block-size` | 128 | int | utf8 字节切块大小 | **不要随便改**——跨数据集对比要求一致 |
| `--top-k-users` | 3 | int | 每个数据集挑选的 user 数上限 | 想横向对比更多用户可调大；但要满足 `--min-request-pct` |
| `--min-request-pct` | 0.01 | float | user 必须承载的最小请求量占比（0–1） | 默认 1%；只关心 heavy user 时可调大到 0.10 |

#### Multi-chain 算法参数（与单 chain 完全解耦）

| 参数 | Default | 类型 | 含义 | 调参建议 |
|---|---|---|---|---|
| `--mc-branch-threshold` | 0.05 | float | 递归时 child 必须满足 `count / parent.count ≥ X`；递归进入门槛 | **default 0.05 适配 multi-chain 模式**；探索性分析可降到 0.02（更松）；偏保守可升至 0.10 |
| `--mc-coverage-threshold` | 0.05 | float | 递归时 child 必须满足 `count / total ≥ X`；全局 ROI 下限 | 默认 0.05（5% 覆盖率以下 cache hit 不值得做）；探索性分析可降到 0.02 |
| `--mc-min-chain-length` | 10 | int | 输出 chain 最短长度（不达不输出） | 短于 10 block 通常是 noise，不构成 system prompt；某些短 chain 模型可调到 5 |
| `--mc-min-chain-coverage` | 0.01 | float | 输出 chain 末节点最小覆盖率 | cov < 1% 的 chain 没有 cache hit ROI；探索性可降到 0.005 |
| `--mc-max-chains` | 50 | int | 单 user 输出 chain 总数上界 | 按 cov 降序取 top-N；溢出时 HTML 标注 "additional N chains pruned" |

### 7.2 `render_user_report_html.py`

| 参数 | Default | 类型 | 含义 |
|---|---|---|---|
| `--input-dir` | 必填 | Path | `per_user_report_analyzer.py` 的 `--output-dir` 输出位置 |
| `--block-size` | 128 | int | 解码用，需要与分析阶段一致（自动从 JSON 读，无须命令行传） |

### 7.3 参数选择的设计依据速查

| 参数 | 为什么是这个 default | 改动影响 |
|---|---|---|
| `--top-k-users=3` | portraits §1 显示 Top-3 通常已覆盖 60%–95% 流量；多看意义递减 | 调大：报告膨胀；调小：丢小 heavy user |
| `--min-request-pct=0.01` | 1% 是"有意义"用户的最低门槛；以下用户的 chain 即使存在也无业务影响 | 调大：丢轻度 user；调小：报告膨胀 |
| `--mc-branch-threshold=0.05` | GLM/DS-8K/DS-32K 的少数派 prompt ratio 集中在 5%–15%；0.05 是不误杀的下限 | 调大（如 0.10）：再次误杀 GLM；调小（如 0.02）：递归进入更深、可能产生伪 chain |
| `--mc-coverage-threshold=0.05` | "全局 5% 是 cache hit ROI 下限"——单 chain 1.1 沿用此值 | 调大：少数派 prompt 被砍；调小：长尾 chain 输出爆炸 |
| `--mc-min-chain-length=10` | 短于 10 block 的 chain 不构成 system prompt（系统消息通常 ≥ 50 block） | 调大：可能丢失短 chain 模型；调小：噪声增加 |
| `--mc-min-chain-coverage=0.01` | 与 `min_request_pct` 对齐；< 1% 的 chain 无业务价值 | 调小：长尾爆炸；调大：丢部分有意义的次主流 chain |
| `--mc-max-chains=50` | portraits 推断 GLM ≥ 6 / DS-8K 10+；50 足够；HTML 渲染上限考虑 | 调大：HTML 加载慢；调小：可能截断 |

---

## 8. 验证 / 预期产出

跑完后**用 portraits §1 的推断对账**——如果数据符合推断，证明 multi-chain 算法 + 报告维度选对了：

| 模型 | 预期 chain forest 结构 | 验证 portraits 推断 |
|---|---|---|
| **DS-8K** | 10+ 条 chain，每条独立 system prompt，coverage 各异（主流 ~42%、其他 5–15%） | "存在 10 条以上长 system prompt" |
| **GLM-V5.1** | ≥ 6 条 chain，每条 ~250 block，单条 cov ~15% | "可能存在多条 system prompt，覆盖率最高也仅 15%" |
| **Qwen-64K** | 主用户的 chain forest **极短**（chain 总长 ≤ 30 block），但 ideal hit rate 仍高 | **关键反向验证**：如果 chain forest 拿不到长 chain 但 hit rate 高，恰好坐实"高复用不来自 chain，来自长文档/skills"，否证通过 chain 优化的方向 |
| **DS-32K** | 多个用户各自有独立长 chain；**轻度用户 chain 比重度用户长** | "重度用户 system prompt 只有 2 block，长 chain 在轻度用户上"（复用倒置） |

**如果在 DS-8K 跑 multi-chain 输出只有 1–2 条**：说明 portraits §1.6 的"10+ 独立 system prompt"推断错了，需要回头修正 portraits。**这是一个反向验证机会**，不只是工具升级。

---

## 9. 已 ack 的决策点（D1–D8）

| ID | 决策 | 拍板内容 |
|---|---|---|
| **D1** | ideal hit rate 语义 | 选 A（user-internal trie）；对齐 vLLM block-level 命中口径；用 `seen_keys` 集合实现，与 §4.3 共享 |
| **D2** | block 去重单位 | `prefix_path_key`（位置敏感，与 chain 算法一致） |
| **D3** | multi-chain 阈值 | **与单 chain 完全解耦**；`--mc-branch-threshold=0.05` / `--mc-coverage-threshold=0.05` |
| **D4** | max_chains | 50 |
| **D5** | timestamp 精度 | 仅 caveat 标注（HTML 顶部 + 图表底部），不阻塞实验；timestamp 是整数秒，无法升精度 |
| **D6** | 单租户处理 | 不简化，完整流程；HTML 顶部加"单租户：全模型指标 = 该 user 指标"声明 |
| **D7** | summary 格式 | JSON + CSV 双输出；CSV 列：`user_id / request_count / request_pct / ideal_hit_rate / chain_forest_count / dominant_chain_cov / p50_gap / p95_gap / new_block_per_sec_p95` |
| **D8** | 输出路径 | `outputs/<dataset>/per_user_reports/`（如 §2 所示） |

---

## 10. 已知问题与 Caveat

1. **timestamp 秒精度限制**（D5）：同一秒内多请求 gap=0；P50/P75 与 batch 内复用统计可能偏小；HTML 顶部必须标注
2. **prefix_path_key 哈希链等价性**（D1 §4.1）：set 查表 == trie LCP，无歧义；但若未来改 hash 函数需重新评估
3. **multi-chain 递归深度**：极端情况下可能栈溢出（如单 chain 数千 block）；实现时改成迭代或显式栈
4. **HTML 渲染规模**：50 条 chain × 250 block × 1 个解码段 ≈ 12500 段文字；某些浏览器加载慢；考虑 fold/expand 控件
5. **复用 portraits §3.3 提到的 plan 修订**：本设计未代表 plan 决策表更新；那是独立工作
6. **跨用户 chain 对齐**：本设计**未做**——chain forest 只在单 user trie 内做；若两个 user 有相同 system prompt，他们的 chain_id 不可直接对比。跨用户对齐留给未来 follow-up

---

## 11. 落地路径

1. ✅ 本文档定稿（2026-05-11）
2. 编码三个模块（顺序）：
   - `scripts/multi_chain_finder.py`（算法 primitive）
   - `scripts/per_user_report_analyzer.py`（编排器 + 4 项指标）
   - `scripts/render_user_report_html.py`（HTML 渲染）
3. 在 DS-8K 上首次验证（与 portraits §1.6 推断对账）
4. 在其他 6 个模型上跑（验证 portraits 全表）
5. 根据 §8 反向验证结果回头修正 portraits / plan
6. （follow-up）给 1.1 / 1.2 加 `--multi-chain` flag，统一接口

---

## 12. 参考资料

- 三步走战略大纲：[`docs/3step_validation_plan.md`](3step_validation_plan.md)
- 7 模型画像（本实验的上游观察）：[`docs/model_portraits.md`](model_portraits.md)
- DS-8K 单模型深度发现：[`docs/dsk8k_step1_findings.md`](dsk8k_step1_findings.md)
- 通用 Step 1 操作 SOP：[`docs/step1_runbook.md`](step1_runbook.md)
- 数据集命名 / CSV 格式：[`data/README.md`](../data/README.md)
