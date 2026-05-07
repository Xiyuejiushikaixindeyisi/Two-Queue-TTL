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
- `<sample_size>`：采样规模（`5k` = 5,000 条，`10k` = 10,000 条）

---

## 2. 当前数据集

### 三步走战略 Step 1.3 用数据集（用户提供）

| 子目录 | 描述 | 用途 | 状态 |
|--------|------|------|------|
| `dsk8k_2h_5k/raw/` | DS-8K 2h 内随机 5,000 条 | Step 1.1/1.2 主样本 + 1.3 短窗口 | ⏳ 待提供 |
| `dsk8k_24h_10k/raw/` | DS-8K 24h 内随机 10,000 条 | Step 1.3 中窗口稳定性 | ⏳ 待提供 |
| `dsk8k_2d_10k/raw/` | DS-8K 2 天内随机 10,000 条 | Step 1.3 跨日稳定性 | ⏳ 待提供 |

**这三份数据是 Step 1.3 的硬依赖。** 1.3 必须在三份齐备后启动；任何 Step 3 算法实现必须在 1.3 验证完成后才允许。

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
# 三份数据集各跑一遍 1.1 + 1.2 + 1.2.0 + HTML
for ds in dsk8k_2h_5k dsk8k_24h_10k dsk8k_2d_10k; do
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
```

---

## 5. 隐私

- `raw_prompt` 字段含**生产环境真实业务请求体**，包含企业 system prompt 与用户输入
- 严禁将任何 `raw/*.csv` 提交到 git（`.gitignore` 已强制屏蔽 `data/**`）
- 分析平台必须在断网机器上运行（详见 `docs/3step_validation_plan.md` §0.1）
