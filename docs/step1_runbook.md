# Step 1 通用实验 SOP — 拿到新生产数据集后如何分析

> **创建时间：** 2026-05-08
> **最近修订：** 2026-05-12（含 Step 1.5 / v2 prefix coverage；v3 shadow detection 尝试后撤回——portraits §3.6 说明）
> **适用范围：** 任意模型 `<model>` 的生产 trace，符合 [`docs/3step_validation_plan.md` §0.1](3step_validation_plan.md) 的 4 列 raw CSV 标准
> **上游：** [`docs/3step_validation_plan.md`](3step_validation_plan.md) §2 / [`docs/per_user_research_design.md`](per_user_research_design.md) / [`docs/model_portraits.md`](model_portraits.md)
> **数据约定：** [`data/README.md`](../data/README.md)

本文档是"操作流程视角"的端到端整合 SOP。命令在 plan / `data/README.md` / `per_user_research_design.md` 中都有零散版本，本文是唯一的"拷贝即用"完整版。

---

## 0. 适用前提（不满足时 STOP）

- **数据格式：** 4 列标准（`request_id / user_id / raw_prompt / timestamp`），UTF-8 或 UTF-8 BOM；中文别名"请求 ID / 租户 ID / 请求参数 / timestamp"自动识别
- **离线运行：** 工具链不依赖任何 LLM 官方 tokenizer，可在断网机器跑
- **`block_size` 默认 128 字节**：utf8 字节切片，不映射到真实 token；跨数据集对比要求一致
- **Step gating（plan §2.3）：** 1.3 必须在 ≥ 2 份**同 threshold** 的 1.2 输出齐备后启动；任何 Step 3 算法实现必须在 1.3 + Step 2 完成后才允许

---

## 阶段 0 · 数据落地 + 占位符

```bash
# 占位符约定
MODEL=qwen64k_24h_0510    # 改为实际数据集名（data/README.md §1 命名规则）
THR=0.45                  # 看阶段 1.1 PNG 后填入

# 落地原始 CSV + 创建输出目录
mkdir -p data/$MODEL/raw outputs/$MODEL
# 把生产 CSV 拷进 data/$MODEL/raw/
```

**命名规则**（`data/README.md` §1）：`<model_short>_<window>_<sample_size>`
- `<model_short>`：dsk8k / dsk32k / qwen64k / qwen8k 等简短代号
- `<window>`：`2h` / `24h` / `2d` / `1w` 等
- `<sample_size>`：`5k` / `10k`；按"自然日"组织时用日期标签 `mmdd`

**意义**：每个数据集独立目录与 `outputs/<dataset>/` 1:1 对应，避免与历史结果混淆。

---

## 阶段 1 · 模型级分析（4 命令）

每个数据集独立跑一次。

### 1.1 阈值扫描（必跑，先看图选 threshold）

```bash
python scripts/chain_threshold_sweep.py \
    --raw-csv data/$MODEL/raw \
    --output  outputs/$MODEL/threshold_sweep.png
```

**意义**：对 21 个 threshold（0–1，步长 0.05）一次性算出每个用户的 chain 长度。21 个阈值复用同一棵 trie，开销几秒。

**读图方法**：
- **每条折线**对应一个 user_id；粗黑线是 global
- **水平段**：chain length 对 threshold 不敏感 → system prompt 段
  - 水平段长度 ≈ 该用户 system prompt 的 block 数
- **突降点**：第一个真实分叉的 block；横坐标值 ≈ 该分叉点的 `max_child / parent` 占比
- **选 threshold**：落在水平段末尾 + 突降之前；**留 5–10% 安全边际**——紧贴突降点的话下一天 ratio 抖动就可能让 chain=0（DS-8K 5.6 实测教训）

### 1.2 全局 chain（plan Q1）

```bash
python scripts/verify_chain_path_closure.py \
    --raw-csv          data/$MODEL/raw \
    --branch-threshold $THR \
    --output           outputs/$MODEL/chain_summary.json
```

**意义**：模型是否存在所有用户共走的 system prompt？
- `chain_length=0` → 跨用户无统一前缀，直接看 1.2
- `chain_length>0` → 全局 system prompt 存在；`chain_coverage_pct` 告知是否被 heavy user 主导

### 1.3 per-user chain（plan Q2）

```bash
python scripts/per_user_chain_analyzer.py \
    --raw-csv          data/$MODEL/raw \
    --branch-threshold $THR \
    --output           outputs/$MODEL/per_user_chains.json
```

**意义**：每个 user_id（=product_id）独立 trie 的 chain。**这份 JSON 是 Step 1.3 跨日稳定性的输入**。

### 1.4 模型级 HTML 渲染

```bash
python scripts/render_chains_html.py \
    --input outputs/$MODEL/per_user_chains.json
```

**意义**：把模型级 chain 渲染成可读 HTML，便于业务方核对 system prompt 是不是预期的。

---

## 阶段 2 · 用户级深度分析（Step 1.5 + v2 prefix coverage）

### 2.1 Per-user 深度报告

```bash
python scripts/per_user_report_analyzer.py \
    --raw-csv          data/$MODEL/raw \
    --output-dir       outputs/$MODEL/per_user_reports
    # 可选参数（默认即可）：
    # --top-k-users         3       Top-3 用户
    # --min-request-pct     0.01    user 必须 ≥ 1% 流量
    # --block-size          128
    # --mc-branch-threshold 0.05    multi-chain 阈值
    # --mc-coverage-threshold 0.05
    # --mc-min-chain-length 10
    # --mc-min-chain-coverage 0.01
    # --mc-max-chains       50
```

**意义**：选 Top-3 满足 ≥1% 流量门槛的 user，对每个 user 计算四项指标：
1. **理想 KV cache 命中率**（vLLM block-level，user-internal）
2. **请求量时序**（间隔分位数 P50/P75/P80/P95 + requests/min 时序）
3. **new unique block/s 时序**（cache 写入压力 + 累计 WS）
4. **multi-chain forest**（含 v2 prefix coverage 字段；shadow 标注由人工 inspect 完成——见 §6）

### 2.2 HTML 渲染

```bash
python scripts/render_user_report_html.py \
    --input-dir outputs/$MODEL/per_user_reports
```

**意义**：生成每 user 一份自包含 HTML（SVG inline，离线可读）。

**HTML 含 5 个章节**：
- §1 Banner + 关键指标（reqs / hit_rate / unique_blocks）
- §2 请求时序图 + 间隔分位数表
- §3 cache 插入压力（new unique block/s + 累计 WS）
- §4 LCP 直方图
- §5 chain forest（含 v2 max_prefix_cov 字段；shadow 标注靠人工）
- caveats（timestamp 秒精度声明 / 单租户声明）

**产出目录结构**：

```
outputs/$MODEL/per_user_reports/
├── user_summary.json          # 全部候选 user 汇总（机器可读）
├── user_summary.csv           # 同上扁平版（Excel / 横向对比）
├── <APP-ID>/
│   ├── user_report.json       # 单 user 完整指标
│   ├── chain_forest.json      # chain forest（含 v2/v3 字段）
│   └── user_report.html       # 单 user HTML 报告
```

---

## 阶段 3 · 查看 chain forest（含 v2/v3 新字段）

### 3.1 单 user chain forest 详细信息

```bash
APP=com.huawei.your-app-id    # 改为目标 user_id

python3 -c "
import json
d = json.load(open('outputs/$MODEL/per_user_reports/$APP/chain_forest.json'))

# Chain 详细表（v2 max_prefix_coverage_pct 新字段）
print(f'{\"id\":>3} {\"len\":>5} {\"cov%\":>7} {\"max_pre%\":>9} {\"branch_pos\":>11} {\"branch_r\":>9}')
print(f'{\"-\"*3} {\"-\"*5} {\"-\"*7} {\"-\"*9} {\"-\"*11} {\"-\"*9}')
for c in d['chains']:
    max_pre = c.get('max_prefix_coverage_pct')
    br = c.get('branch_at_root_ratio')
    max_pre_s = f'{max_pre:.2f}' if isinstance(max_pre, (int, float)) else '-'
    br_s = f'{br:.3f}' if isinstance(br, (int, float)) else '-'
    print(f'{c[\"chain_id\"]:>3} {c[\"chain_length\"]:>5} '
          f'{c[\"coverage_pct\"]:>6.2f}% {max_pre_s:>9} '
          f'{str(c[\"branch_at_root_position\"]):>11} {br_s:>9}')
"
```

输出含 v2 `max_prefix_cov` 字段（chain prefix 最大覆盖率）。shadow case 见 §6（人工标注 SOP）。

### 3.2 跨用户横向对比（v1.5 新输出）

```bash
column -t -s, outputs/$MODEL/per_user_reports/user_summary.csv | less -S
# 或直接在 Excel 里打开 user_summary.csv
```

CSV 列：`user_id / request_count / request_pct / ideal_hit_rate / chain_forest_count / dominant_chain_cov_pct / dominant_chain_length / p50_gap / p95_gap / new_block_per_sec_p95`

### 3.3 查看某条 chain 的 decoded 内容

```bash
CHAIN_ID=0    # 改为目标 chain id

python3 -c "
import json
d = json.load(open('outputs/$MODEL/per_user_reports/$APP/chain_forest.json'))
c = d['chains'][$CHAIN_ID]
print(f'chain #{c[\"chain_id\"]}: len={c[\"chain_length\"]}, '
      f'leaf_cov={c[\"coverage_pct\"]}%, max_prefix_cov={c[\"max_prefix_coverage_pct\"]}%')
print(f'sample_request_id={c[\"sample_request_id\"]}')
print(f'\nDecoded content (first 600 chars):')
text = ''.join(b.get('decoded_text') or '' for b in c['decoded_content'])
print(text[:600])
"
```

**用途**：核对 chain 是不是真的 system prompt / RAG 模板，而非 multi-chain 阈值过松产生的 noise。

---

## 阶段 4 · 跨日稳定性（Step 1.3，可选；需 ≥ 2 份数据）

```bash
DAY1=qwen64k_24h_0510
DAY2=qwen64k_24h_0511
THR=0.45

# 前置：两份数据集都必须用同一 threshold 跑过 1.2
for ds in $DAY1 $DAY2; do
  python scripts/per_user_chain_analyzer.py \
      --raw-csv          data/$ds/raw \
      --branch-threshold $THR \
      --output           outputs/$ds/per_user_chains_thr${THR}.json
done

# 跨日 Jaccard
python scripts/chain_stability_analyzer.py \
    --input  d1=outputs/$DAY1/per_user_chains_thr${THR}.json \
             d2=outputs/$DAY2/per_user_chains_thr${THR}.json \
    --top-n  20 \
    --output outputs/cross_day_thr${THR}/chain_stability_report.json
```

**意义**：plan §2.3 决策门控——chain 跨日 Jaccard ≥ 0.7 才允许进 Step 2。

**`--allow-mismatched-params`**：仅在故意比较不同 threshold 的 1.2 输出时用（极少见）。日常使用永远不要传。

---

## 阶段 5 · 决策（plan §2.3 + portraits 二维分类）

跑完阶段 1-2 后，对照 `user_summary.csv` + `chain_forest.json` 的 v2/v3 字段，按下表判定 Step 3 算法路径：

| 数据特征 | Step 3 算法方向 | portraits 对应案例 |
|---|---|---|
| 主用户 hit_rate < 25% | **放弃 prefix cache 优化** | Qwen-8B-8K（邮件分类） |
| 主用户 chain ≤ 30 block + hit_rate > 80% | user-internal 长内容复用 → **大容量 LRU** | Qwen-64K S773（员工助手） |
| 多用户 hit_rate 倒置 ≥ 3x | **按 hit_rate 排序 pin**（不按流量） | Qwen-32K（4.8x）/ DS-32K（7.8x） |
| 多条独立 chain + cov 5–30% / chain | **per-user 多 chain 队列** | DS-8K（router）/ GLM（7 chain） |
| 多 chain + 人工标注 shadow（§6） | **按 shadow group 去重后 pin** | Qwen-32B-8K S773（人工标注 14 block shadow → 78.75% cov） |
| 单 user chain forest 中 max_prefix_cov >> leaf_cov | **pin chain 前缀段**（不 pin 全 chain） | Qwen-64K chipset2（92.3% hit / leaf 41.9%） |

---

## 6. Shadow group 人工标注 SOP

### 是什么

chain 之间在 trie 上 `branch_pos=0`（看似无共享前缀），但 decoded content 头部**语义上**共享同一段业务 prompt——例如三条 chain 都以 "你是一个 XX 助手，根据 ..." 开头，只是 XX 不同。

### 为什么必须人工标注

曾尝试 v3 自动检测（byte-level LCP + union-find），生产数据上**双向失败**——`{"model":...,"stream":true,"messages":[{"role":"system","content":"`这种 JSON wrapper 在所有请求中都共享 70+ byte 但**不是业务 shadow**（false positive），同时真业务 shadow 因 JSON 字段顺序 / 动态字段 / 微差异等原因被漏报（false negative）。完整失败分析见 portraits §3.6。

**根本原因**：shadow 是语义层判断（"业务模板共享" vs "boilerplate 共享"），byte-level 算法不足；语义层判断需要 LLM tokenizer 或 JSON 解析，违反 plan §0.1 "不依赖 tokenizer" 约束。所以**这件事永远靠人工**。

### 标注流程（每 user 约 5–10 秒）

1. 打开 `outputs/$MODEL/per_user_reports/$APP/user_report.html`
2. 滚到 §5 chain forest
3. 对每条 chain 点 "show decoded content" 展开
4. **跳过** JSON wrapper `{"model":...,"messages":[{"role":"system","content":"`（约前 70 byte，所有 chain 都有）
5. 看 `"你是一个..."` 开头的实际 system prompt 文本
6. 心算判断哪些 chain 是相同模板：
   - "你是一个 XX 助手" 中 XX 实质相同 + 后续任务描述高度相似 → shadow group
   - "你是 XX" vs "你扮演 YY" → 不同业务，**不是 shadow**
   - JSON wrapper 之外的 system prompt 开头部分（哪怕只共享 5–10 个字）即可判定 shadow

### 标注沉淀位置

人工标注后写进：
- 对应模型的 findings 文档（如 `dsk8k_step1_findings.md`）
- 或 portraits.md §1.X 该模型的 "实测修订" 子节
- 或 portraits.md §3.6 顶部的 case 表（如发现新 case 加一行）

### Step 3 业务意义

pin 决策时**同组 chain 应视为一个共享 pin unit**，不重复计算容量。

**例子（Qwen-32B-8K S773，人工标注）**：
- chain 0: 34 block / leaf_cov 41.45%
- chain 1: 14 block / leaf_cov 37.30%
- 人工判定 chain [0, 1] 共享前 ~14 block 业务模板
- 朴素累加：48 block 拿 78.75% cov ❌（重复算了 14 block 共享前缀）
- shadow 修正：**14 + 20 = 34 block 拿 78.75% cov ✓**

### 截至 2026-05-12 的 4 个已知 case

| 模型 | user | shadow group | 共享内容（标注） |
|---|---|---|---|
| DS-8K | S00...0773 | chains 共享 JSON wrapper + 中文 system prompt 开头 | 推测，未细查 |
| Qwen-32B-8K | S00...0773 | chain [0, 1] 前 14 block 几乎完全相同 | 用户标注 |
| Qwen-32B-8K | quality_public_sentiment | chain [0, 1] 都以 "你是一个文本分析助手" 开头 | 推测 |
| DS-32K | mdata.mdata20180908 | chain [1, 2] 内容相似度非常高 | 用户标注 |

---

## 7. 一键脚本（阶段 0–2 全跑）

```bash
#!/usr/bin/env bash
set -euo pipefail
MODEL=${1:?usage: $0 <model_dataset_name> [threshold]}
THR=${2:-0.45}

mkdir -p outputs/$MODEL

# 阶段 1: 模型级
python scripts/chain_threshold_sweep.py     --raw-csv data/$MODEL/raw --output outputs/$MODEL/threshold_sweep.png
python scripts/verify_chain_path_closure.py --raw-csv data/$MODEL/raw --branch-threshold $THR --output outputs/$MODEL/chain_summary.json
python scripts/per_user_chain_analyzer.py   --raw-csv data/$MODEL/raw --branch-threshold $THR --output outputs/$MODEL/per_user_chains.json
python scripts/render_chains_html.py        --input   outputs/$MODEL/per_user_chains.json

# 阶段 2: 用户级（v2/v3 自动跑）
python scripts/per_user_report_analyzer.py  --raw-csv data/$MODEL/raw --output-dir outputs/$MODEL/per_user_reports
python scripts/render_user_report_html.py   --input-dir outputs/$MODEL/per_user_reports

echo "Done. Open outputs/$MODEL/per_user_reports/*/user_report.html"
```

---

## 8. 注意事项

1. **threshold 边界敏感性**（DS-8K 5.6 实测教训）：1.2.0 PNG 上找到突降点后，threshold 要离突降点 ≥ 5–10% 安全距离。否则下一天某分叉的 ratio 稍微抖动就可能让 chain=0。
2. **Step gating**：没有 Step 1.3 不许进 Step 2；没有 Step 2 不许写 Step 3 算法。
3. **`block_size` 一致性**：默认 128，不要随便改；跨数据集对比要求同 block_size。
4. **timestamp 秒精度**：生产 trace 是整数秒；阶段 2 输出的间隔分位数 / new block/s 在 1s 边界附近**不可信**（HTML 自动标注此 caveat）。
5. **隐私**：`raw_prompt` 含生产业务体（企业 system prompt + 用户输入），**`raw/*.csv` 永远不进 git**（`.gitignore` 已强制屏蔽 `data/**`）。
6. **离线**：所有工具不依赖在线 tokenizer / 模型加载，可在断网机器运行。
7. **HTML 中文边界**：decoded_text 按 utf8 字节切片后 utf8 decode，跨 block 边界的中文字符可能显示 `�`——属于正常现象，不影响 chain 检测正确性。
8. **shadow group 由人工标注**：曾尝试自动检测（v3 byte-level LCP），生产数据上双向失败（false positive：JSON wrapper 误报；false negative：fuzzy 业务 shadow 漏报）。流程见 §6。

---

## 9. 工具速查

| 阶段 | 工具 | 输入 | 输出 | 备注 |
|---|---|---|---|---|
| 1.1 | `scripts/chain_threshold_sweep.py` | raw CSV 目录 | `threshold_sweep.png` | 看图选 threshold |
| 1.2 | `scripts/verify_chain_path_closure.py` | raw CSV 目录 | `chain_summary.json` | 全局 chain |
| 1.3 | `scripts/per_user_chain_analyzer.py` | raw CSV 目录 | `per_user_chains.json` | 各 user chain |
| 1.4 | `scripts/render_chains_html.py` | `per_user_chains.json` | `per_user_chains.html` | 模型级 HTML |
| 1.5a | `scripts/per_user_report_analyzer.py` | raw CSV 目录 | `user_summary.{json,csv}` + 每 user JSON | v2 prefix coverage + v3 shadow detection |
| 1.5b | `scripts/render_user_report_html.py` | per_user_reports 目录 | 每 user 一份 HTML | 含 v2 max_prefix_cov；shadow 标注靠人工 |
| 1.5c | `scripts/multi_chain_finder.py`（CLI debug） | raw CSV 目录 | `chain_forest_global.json` | 全局 multi-chain 探索（不必要日常用） |
| 1.3 | `scripts/chain_stability_analyzer.py` | ≥ 2 份 `per_user_chains.json` | `chain_stability_report.json` | 跨日 Jaccard + tier |

---

## 10. 参考资料

- 三步走战略大纲：[`docs/3step_validation_plan.md`](3step_validation_plan.md)
- Step 1.5 实验设计（D1–D8 决策点存档）：[`docs/per_user_research_design.md`](per_user_research_design.md)
- 7 模型反向验证 + 修订：[`docs/model_portraits.md`](model_portraits.md)
- DS-8K 单模型深度发现：[`docs/dsk8k_step1_findings.md`](dsk8k_step1_findings.md)
- 数据集命名 / CSV 格式：[`data/README.md`](../data/README.md)
