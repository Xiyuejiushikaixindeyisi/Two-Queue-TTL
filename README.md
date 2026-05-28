# KV Cache 离线分析平台

> 针对 vLLM prefix cache 场景的**离线 trace 分析平台**. 跨数据集筛选高研究价值场景 → 出 HTML 详细报告 → 决策 KV cache 优化算法 (路由 / 池化 / offloading / 量化 / 淘汰).

平台**完全离线** (air-gapped 友好), tokenizer 通过 git vendor, 不依赖任何 LLM 后端服务. 支持 byte-level (regression baseline) 与 token-level (与 vLLM 一致) 两种编码; 当前 vendor 了 GLM-5 (MLA) 与 Qwen3 (GQA) 两个 tokenizer + KV 配置.

3 阶段 funnel 工作流: **筛选 → txt 直通 / 选定场景 → 详细 HTML**. 每个阶段的 CLI 命令、数据集格式、输出位置、指标解读, 全部见 [**USAGE.md**](USAGE.md).

## ⚡ 跨数据集理想 KV cache 命中率对比 (核心)

平台**最重要**的能力: 给一个放了一堆 `.csv` 的文件夹, 一条命令算出每个数据集的
token 级理想 KV cache 命中率 (无淘汰上限, 与 vLLM 对齐). 工具: `scripts/dataset_hit_rate.py`
(扁平文件夹, 不需 `data/<model>/raw/` 嵌套).

**模式 1 — 整库池化统计** (每个 CSV 输出一个结果值; CSV 内所有请求共享一个 prefix
cache, 跨 app/租户的前缀复用都计入, 最贴近线上单实例 vLLM):

```bash
PYTHONPATH=. .venv_glm5/bin/python3 scripts/dataset_hit_rate.py \
  --dir /mnt/esfs/zhangxiyue/GLM-V5/ \
  --encoder glm5_token --tokenizer-path models/glm5_tokenizer --chat-mode wrap_user
```

**模式 2 — 指定应用 ID** (按 CSV 筛选, 每个数据集仅统计单个租户 ID = `--app-id`):

```bash
PYTHONPATH=. .venv_glm5/bin/python3 scripts/dataset_hit_rate.py \
  --dir /mnt/esfs/zhangxiyue/GLM-V5/ --app-id <租户ID值> \
  --encoder glm5_token --tokenizer-path models/glm5_tokenizer --chat-mode wrap_user
```

- 输出: 每行一个数据集 (`reqs / apps / total_blk / uniq_blk / hit_rate / GB/min / dur`), `--csv-out` 落盘.
- 口径: `ideal_hit_rate = (total_blocks − unique_blocks) / total_blocks`; chain hash 位置敏感 → **与请求顺序无关**, 不用按 timestamp 排序.
- 切 Qwen3: `--encoder hf_token --tokenizer-path models/qwen_v3_tokenizer`; app-id 在别的列加 `--app-col <列名>`.
- 与 `target_users_hit_rate.py` 的区别: 后者按 user 选 top-K + 需 `data/<model>/raw/` 布局; 本脚本扁平文件夹 + 数据集级整库口径. 详解见 [USAGE.md §⚡ 数据集级理想命中率](USAGE.md).

## 🚀 入口

| 想做什么 | 看这里 |
|---|---|
| 完整使用流程 + 3 个具体案例的 CLI | [**USAGE.md**](USAGE.md) ← 主入口 |
| vendor 新 tokenizer / 加新模型 | [`models/README.md`](models/README.md) |
| 数据集摆放约定 | [`data/README.md`](data/README.md) |
| token-level 编码设计原理 | [`docs/step1_6_token_level_experiment_plan.md`](docs/step1_6_token_level_experiment_plan.md) |
| 指标释义 (token 模式) | [`docs/metrics_glossary.md`](docs/metrics_glossary.md) |
| 术语规范 (block / key / chain) | [`docs/terminology.md`](docs/terminology.md) |
| 历史 spec / 路线图 / 算法决策矩阵 (已归档) | [`docs/archive/`](docs/archive/) |

## 该用哪个脚本?

| 我想… | 用 | 输入 |
|---|---|---|
| 一个文件夹里每个数据集的理想命中率对比 | `scripts/dataset_hit_rate.py` | 一堆 `.csv` |
| 看单个 CSV 的全局 (模型级 4 块 HTML) | `scripts/model_report.py` | 单 CSV |
| 深入单个 app (APP 级 5 块 HTML, 含 4 变体 + chain forest) | `scripts/app_report.py` | CSV+app-id 或 txt 文件夹 |

这 3 个是**主线入口** (Step-1 现网分析)。其余脚本 (旧三阶段 funnel / per-user 7 节 pipeline /
`sim/` 仿真) 属 **legacy / advanced**, 见下文与 [USAGE.md](USAGE.md) 的标注, 新人不必先看。

## 工具速查

**主线 (Step-1 现网分析)**

| 工具 | 文件 |
|---|---|
| **跨数据集理想命中率对比 (核心)** | `scripts/dataset_hit_rate.py` |
| 模型级 HTML 报告 (模型/APP 指标 + reuse_time + LCP) | `scripts/model_report.py` |
| APP 级 HTML 报告 (4 变体 + chain forest) | `scripts/app_report.py` |
| raw prompt → token block 编码 | `lib/prompt_encoder.py` |
| txt 树 → CSV 转换 | `scripts/txt_tree_to_csv.py` |
| 从本地权重 vendor tokenizer | `scripts/vendor_tokenizer_from_weights.py` |

**legacy / advanced** (旧主线, 仍可用, 但不在新人首选路径)

| 工具 | 文件 |
|---|---|
| 跨数据集筛选 (按 user top-K) | `scripts/target_users_hit_rate.py` |
| per-user 7 节 pipeline (analyzer + HTML) | `scripts/v2_run_pipeline.py` |
| model chains + 21 点阈值扫描 | `scripts/per_user_chain_analyzer.py` + `scripts/render_chains_html.py` |
| KV cache 淘汰仿真 (Step 2/3, out-of-scope) | `sim/` + `scripts/run_simulation.py` 等 |

> `scripts/quick_hit_rate.py` 是**故意保持 stdlib-only 的单文件可移植工具** (能 `scp` 到裸机跑字节级数字),
> 因此刻意不依赖 `lib/`、也不算主线。正经分析用上面 3 个主线入口 (token 级)。

详细 CLI + 案例见 [USAGE.md](USAGE.md); key 体系/输入形态对照见 [docs/terminology.md §6.5](docs/terminology.md).

## 快速开始

```bash
git clone <repo-url> && cd two_queue_ttl
python3 -m venv .venv_glm5
.venv_glm5/bin/pip install -r requirements.txt   # transformers>=4.45 + tokenizers + jinja2

# 主线: 一个文件夹 → 每个数据集的理想命中率
PYTHONPATH=. .venv_glm5/bin/python3 scripts/dataset_hit_rate.py \
  --dir /mnt/esfs/zhangxiyue/GLM-5/ \
  --encoder glm5_token --tokenizer-path models/glm5_tokenizer --chat-mode wrap_user
```

完整命令 + 模型级/APP 级报告 + legacy 三阶段见 [USAGE.md](USAGE.md).

## 离线运行

Ascend / 实验室机断网场景: `git pull` 拉取 vendored tokenizer + 代码后即可运行, 不需要任何运行时下载. 见 [`models/README.md`](models/README.md) "Why vendor in git" 章节.
