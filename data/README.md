# `data/` 目录约定

本目录存放用于离线分析的 trace 数据。**实际 CSV 文件不进 git**（被 `.gitignore` 忽略），仅本说明随仓库追踪。

---

## 1. 命名约定

每个数据集对应一个独立子目录，子目录下必须包含 `raw/` 子文件夹，原始 CSV 放在 `raw/` 内。

```
data/<dataset_name>/raw/<any-name>.csv
```

**`<dataset_name>` 命名规则：**

```
<model_short>_<window>_<sample_size>
```

- `<model_short>`：dsk8k / dsk32k / qwen64k / qwen8k 等简短模型代号
- `<window>`：采集时间窗口（`2h` / `24h` / `2d` / `1w` 等）
- `<sample_size>`：采样规模（`5k` = 5,000 条，`10k` = 10,000 条）；当采样按"自然日"组织、条数不固定时可用日期标签代替（如 `0506` / `0507`）

---

## 2. 当前数据集

### 三步走战略 Step 1.3 用数据集（用户提供）

| 子目录 | 描述 | 用途 | 状态 |
|--------|------|------|------|
| `dsk8k_24h_0506/raw/` | DS-8K 5.6 全天（24h 窗口）随机采样 | Step 1.3 跨日稳定性 day1 | ⏳ 等数据落地 |
| `dsk8k_24h_0507/raw/` | DS-8K 5.7 全天（24h 窗口）随机采样 | Step 1.3 跨日稳定性 day2 | ⏳ 等数据落地 |

**这两份数据是 Step 1.3 的硬依赖。** 1.3 必须在两份齐备后启动；任何 Step 3 算法实现必须在 1.3 验证完成后才允许。

> **命名变更（2026-05-08）：** 原计划 `dsk8k_2h_5k / 24h_10k / 2d_10k` 三份替换为 `dsk8k_24h_0506` + `dsk8k_24h_0507` 两份独立 24h 采样；等价覆盖跨日稳定性需求，2h / 中窗口槽位放弃。

### 旧数据集（兼容保留）

| 子目录 | 来源 | 状态 |
|--------|------|------|
| `deepseek_v3.1_8k/` | 早期采集（2 小时窗口） | 已用完 1.1/1.2 实测验证，留作对照 |
| `deepseek_v3.1_32k/` | 同上 | 留作 trace 画像参考 |
| `qwen_v3.5_27b_64k/` 等 | 5 个生产模型 | 已跑过 batch_pipeline 横向对比 |

**注意：** 新分析任务请使用上面的三步走战略命名（`dsk8k_*`），不要把新数据放到旧子目录里——会与 1.1/1.2 历史结论混淆。

---

## 3. CSV 文件格式（强制要求）

`raw/` 下的 CSV 必须遵循 4 列标准格式（任意文件名）：

| 列序 | 列名（标准） | 列名（中文别名） | 含义 |
|------|------------|----------------|------|
| 1 | `request_id` | `请求ID` | 请求 ID（唯一） |
| 2 | `user_id` | `租户ID` | 租户 ID（实为 product_id） |
| 3 | `raw_prompt` | `请求参数` | 原始 prompt 文本（含 JSON 外壳） |
| 4 | `timestamp` | `timestamp` | 请求时间戳（秒级） |

**支持的编码：**
- UTF-8（推荐）
- UTF-8 with BOM（生产 CSV 常见，所有 Step 1 工具透明剥离 BOM）

**支持的列名语言：** 英文标准名 + 中文别名（自动识别），一份文件内不混用。

详细约束见主文档 [`docs/3step_validation_plan.md` §0.1](../docs/3step_validation_plan.md)。

---

## 4. 跑分析（数据到位后）

```bash
# 两份数据集各跑一遍 1.2.0 + 1.1 + 1.2 + HTML
for ds in dsk8k_24h_0506 dsk8k_24h_0507; do
    # 1.2.0 阈值扫描（先看图找阈值）
    python scripts/chain_threshold_sweep.py \
        --raw-csv data/$ds/raw \
        --output  outputs/$ds/threshold_sweep.png

    # 1.1 全局 chain
    python scripts/verify_chain_path_closure.py \
        --raw-csv data/$ds/raw \
        --output  outputs/$ds/chain_summary.json

    # 1.2 per-user chain
    python scripts/per_user_chain_analyzer.py \
        --raw-csv data/$ds/raw \
        --output  outputs/$ds/per_user_chains.json

    # HTML 报告
    python scripts/render_chains_html.py \
        --input outputs/$ds/per_user_chains.json
done

# 1.3 跨日稳定性（两份 1.2 JSON artifact 作为输入）
python scripts/chain_stability_analyzer.py \
    --input 0506=outputs/dsk8k_24h_0506/per_user_chains.json \
            0507=outputs/dsk8k_24h_0507/per_user_chains.json \
    --top-n 20 \
    --output outputs/dsk8k_step1_3/chain_stability_report.json
```

---

## 5. 隐私

- `raw_prompt` 字段含**生产环境真实业务请求体**，包含企业 system prompt 与用户输入
- 严禁将任何 `raw/*.csv` 提交到 git（`.gitignore` 已强制屏蔽 `data/**`）
- 分析平台必须在断网机器上运行（详见 `docs/3step_validation_plan.md` §0.1）
