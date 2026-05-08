# Step 1 通用实验 SOP — 拿到新生产数据集后如何分析

> **创建时间：** 2026-05-08
> **适用范围：** 任意模型 `<model>` 的生产 trace，符合 [`docs/3step_validation_plan.md` §0.1](3step_validation_plan.md) 的 4 列 raw CSV 标准
> **上游设计文档：** [`docs/3step_validation_plan.md` §2](3step_validation_plan.md)（算法定义 + 决策门控）
> **数据约定：** [`data/README.md`](../data/README.md)（命名 + 格式）

本文档是"操作流程视角"的整合 SOP；命令在 plan 与 `data/README.md` 中都有零散版本，本文是唯一的"端到端可拷贝执行版"。

---

## 0. 适用前提（不满足时 STOP）

- **数据格式：** 4 列标准（`request_id / user_id / raw_prompt / timestamp`），UTF-8 或 UTF-8 BOM；中文别名"请求ID/租户ID/请求参数/timestamp"自动识别
- **离线运行：** 工具链不依赖任何 LLM 官方 tokenizer，可在断网机器跑
- **`block_size` 默认 128 字节**：utf8 字节切片，不映射到真实 token；跨数据集对比必须同 block_size
- **Step gating：** 1.3 必须在 ≥ 2 份同 threshold 的 1.2 输出齐备后启动；任何 Step 3 算法实现必须在 1.3 验证完成后才允许（详见 plan §2.3）

---

## 阶段 0 · 数据落地 + 命名

```bash
mkdir -p data/<model>_<window>_<size>/raw
# 把生产 CSV 拷进 raw/
```

**命名规则**（`data/README.md` §1）：`<model_short>_<window>_<sample_size>`
- `<model_short>`：`dsk8k` / `dsk32k` / `qwen64k` / `qwen8k` 等
- `<window>`：`2h` / `24h` / `2d` / `1w`
- `<sample_size>`：`5k` / `10k`；按"自然日"组织、条数不固定时用日期标签 `mmdd`

**示例**：`data/qwen64k_24h_0510/raw/sample.csv`

**意义**：每个数据集独立目录与 `outputs/<dataset>/` 1:1 对应，避免与历史结果混淆。**新数据集不要放进 `deepseek_v3.1_8k/` 等旧目录。**

---

## 阶段 1 · 单数据集分析（每份数据跑一次）

设占位符 `DS=<dataset_name>`，例如 `DS=qwen64k_24h_0510`。

### 步骤 A · 1.2.0 阈值扫描（必跑，先看图）

```bash
python scripts/chain_threshold_sweep.py \
    --raw-csv data/$DS/raw \
    --output  outputs/$DS/threshold_sweep.png
```

**意义**：对 21 个 threshold（0–1，步长 0.05）一次性算出每个用户的 chain 长度。**这一步不消费 trie 计算时间**——同一棵 trie 复用 21 遍，几秒级。

**读图方法**：
- **每条折线**对应一个 user_id；粗黑线是 global
- **水平段** = 该用户 system prompt 的 block 数（这些 block 的 `max_child / parent` 占比都接近 100%，threshold 怎么动 chain 长度都不变）
- **突降点的横坐标值** ≈ 该用户第一个分叉点的主子节点占比；threshold 跨过这个值，chain 立刻断在该点

**如何选 threshold**：
- 选在"水平段尾端 + 突降之前"
- **必须留 5–10% 安全边际**——threshold 紧贴突降点的话，下一天分叉占比稍微抖动就可能让 chain=0（DS-8K 5.6 vs 5.7 真实踩坑案例，详见 `dsk8k_step1_findings.md`）

### 步骤 B · 1.1 全局 chain

```bash
python scripts/verify_chain_path_closure.py \
    --raw-csv          data/$DS/raw \
    --branch-threshold <选定值> \
    --output           outputs/$DS/chain_summary.json
```

**意义**：回答 "整个数据集是否存在所有用户共走的 system prompt"。
- `chain_length=0` → 跨用户没有统一前缀，直接看 1.2 的 per-user 结果
- `chain_length>0` → 全局 system prompt 存在；`chain_coverage_pct` 揭示是否被某个 heavy user 主导（覆盖率明显低于 100% 即"主流路径但非全员"）

### 步骤 C · 1.2 per-user chain

```bash
python scripts/per_user_chain_analyzer.py \
    --raw-csv          data/$DS/raw \
    --branch-threshold <选定值> \
    --output           outputs/$DS/per_user_chains.json
```

**意义**：每个 `user_id`（=product_id，每模型 ≤ 37 个）独立 trie，回答 "每个产品的 system prompt 是什么 + 与全局一致与否"。**这份 JSON 是 Step 1.3 的唯一输入。**

输出每用户：`request_count` / `chain_length` / `chain_coverage_pct` / `lcp_content`（解码文本） / `same_as_global` / `prefix_match_with_global`。

### 步骤 D · HTML 渲染

```bash
python scripts/render_chains_html.py \
    --input outputs/$DS/per_user_chains.json
```

**意义**：把 chain 解码内容渲染成可读 HTML，便于业务方核对 system prompt 是不是预期的——验证算法没把 prompt 噪音误判成 chain。

### 单数据集一键脚本

```bash
DS=qwen64k_24h_0510   # 改成实际数据集名
THR=0.45              # 看 1.2.0 PNG 后填入

python scripts/chain_threshold_sweep.py     --raw-csv data/$DS/raw --output outputs/$DS/threshold_sweep.png
python scripts/verify_chain_path_closure.py --raw-csv data/$DS/raw --branch-threshold $THR --output outputs/$DS/chain_summary.json
python scripts/per_user_chain_analyzer.py   --raw-csv data/$DS/raw --branch-threshold $THR --output outputs/$DS/per_user_chains.json
python scripts/render_chains_html.py        --input outputs/$DS/per_user_chains.json
```

---

## 阶段 2 · 跨数据集稳定性（≥ 2 份数据集后）

### 前置硬约束

所有要对比的数据集必须：
1. 已跑完阶段 1 全部步骤
2. **`--branch-threshold` 完全一致**（threshold 是 1.2 阶段的事，1.3 不再调）
3. **`coverage_threshold` / `block_size` 也一致**（默认值就行，但跨次实验不要改）

`chain_stability_analyzer.py` 启动时会自动校验这三个 params 一致性，不一致默认 abort。

### 标准跑法（单 threshold）

```bash
python scripts/chain_stability_analyzer.py \
    --input d1=outputs/<dataset_1>/per_user_chains.json \
            d2=outputs/<dataset_2>/per_user_chains.json \
    --top-n 20 \
    --output outputs/<model>_step1_3/chain_stability_report.json
```

**意义**：跨数据集做 chain Jaccard，量化漂移：
- **global chain top-N Jaccard**：全局 system prompt 是否跨日一致
- **per-user chain Jaccard**：每个产品的 system prompt 跨日漂移
- **stability tier**：依据 plan §2.2/1.3 的四档（high / slow_drift / medium_drift / unstable）

输出 `chain_stability_report.json` 含 pairwise 对比矩阵 + per-user 漂移列表 + tier 判定。

### 多 threshold 扫描工作流

要看不同 threshold 下的稳定性如何变化：

```bash
DATASETS=(qwen64k_24h_0510 qwen64k_24h_0511)

for THR in 0.40 0.45 0.50; do
  # 每个 threshold 重跑所有数据集的 1.2
  for ds in "${DATASETS[@]}"; do
    python scripts/per_user_chain_analyzer.py \
        --raw-csv data/$ds/raw \
        --branch-threshold $THR \
        --output  outputs/$ds/per_user_chains_thr${THR}.json
  done
  # 同 threshold 的 N 份输出再做 1.3
  python scripts/chain_stability_analyzer.py \
      --input d1=outputs/${DATASETS[0]}/per_user_chains_thr${THR}.json \
              d2=outputs/${DATASETS[1]}/per_user_chains_thr${THR}.json \
      --top-n 20 \
      --output outputs/<model>_step1_3/report_thr${THR}.json
done
```

每个 threshold 对应一组 1.2 输出 + 一份 1.3 报告，文件名带 threshold 后缀，事后可追溯每份比较所用的 threshold。

### `--allow-mismatched-params` 何时用

仅在你**故意**对比不同 threshold 跑出的 1.2 输出时（极少见，通常是诊断用）。日常使用永远不要传这个 flag——它会让结果失去 apples-to-apples 性质。

---

## 阶段 3 · 决策（plan §2.3）

把 1.1 + 1.2 + 1.3 输出对照下表，得出 Step 2 / Step 3 路径：

| 1.1 全局 chain | 1.2 per-user | 1.3 跨日 Jaccard | 决策 |
|---|---|---|---|
| 长度 0 | 全部 user 也 0 | — | **chain 优化方向不可行**；Step 3 改通用 LRU |
| 长度 0 | 部分 user 有 chain | — | **单租户优化**：不能做全局 pin，按 user 二分 |
| > 0 + heavy user 主导（cov 显著 < 100%） | — | — | ⚠️ 标记单租户优化；业务沟通后再 Step 2 |
| > 0 | — | global Jaccard ≥ 0.7 | ✅ 进 Step 2，全局静态 pin 可行 |
| > 0 | — | per-user Jaccard 双峰（部分 ≥ 0.7、部分 < 0.4） | ⚠️ 进 Step 2，Step 3 设计 user 二分（稳 user pin / 漂 user LRU） |
| > 0 | — | per-user mean Jaccard 0.4–0.7 | ⚠️ 进 Step 2，Step 3 必须设计动态准入 / 退出 |
| > 0 | — | < 0.4 | ❌ 终止 chain 方向；Step 3 改通用 LRU |

**重要**：跨日 Jaccard 的 tier 阈值在 plan 中是按 7 天稳定性定的；2h / 24h / 跨 1 日的判定要在 `summary.note` 里看输入实际跨度，不要让 tier 字符串误导。

---

## 注意事项

1. **threshold 边界敏感性**（DS-8K 5.6 实测教训）：1.2.0 PNG 上找到突降点后，threshold 要离突降点 ≥ 5–10% 安全距离。否则下一天某个分叉的 ratio 稍微抖动就可能让 chain=0。
2. **Step gating**（plan §2.3）：没有 Step 1.3 不许进 Step 2；没有 Step 2 不许写 Step 3 算法。
3. **`block_size` 一致性**：默认 128，不要随便改；跨数据集对比要求同 `block_size`。
4. **隐私**：`raw_prompt` 含生产业务体（含企业 system prompt + 用户输入），**`raw/*.csv` 永远不进 git**（`.gitignore` 已强制屏蔽 `data/**`）。
5. **离线**：所有工具不依赖在线 tokenizer / 模型加载，可在断网机器运行。
6. **HTML 渲染审计**：HTML 报告中的 `decoded_text` 是按 utf8 字节切片解码的，可能在中文边界产生 `�` 替换字符——属于正常现象，不影响 chain 检测正确性，仅影响阅读。

---

## 工具速查

| 阶段 | 工具 | 输入 | 输出 |
|---|---|---|---|
| 1.2.0 | `scripts/chain_threshold_sweep.py` | raw CSV 目录 | `threshold_sweep.png` |
| 1.1   | `scripts/verify_chain_path_closure.py` | raw CSV 目录 | `chain_summary.json` |
| 1.2   | `scripts/per_user_chain_analyzer.py` | raw CSV 目录 | `per_user_chains.json` |
| HTML  | `scripts/render_chains_html.py` | `per_user_chains.json` | `per_user_chains.html` |
| 1.3   | `scripts/chain_stability_analyzer.py` | ≥ 2 份 `per_user_chains.json` | `chain_stability_report.json` |

---

## 参考资料

- 算法 / 阈值含义 / 决策门控的完整设计：[`docs/3step_validation_plan.md`](3step_validation_plan.md)
- 数据集命名 / CSV 格式 / 隐私约束：[`data/README.md`](../data/README.md)
- DS-8K 实测发现（含 5.6 临界 case study）：[`docs/dsk8k_step1_findings.md`](dsk8k_step1_findings.md)
