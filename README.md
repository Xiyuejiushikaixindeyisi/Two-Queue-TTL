# KV Cache 离线分析平台

> 针对 vLLM prefix cache 场景的**离线 trace 分析平台**. 跨数据集筛选高研究价值场景 → 出 HTML 详细报告 → 决策 KV cache 优化算法 (路由 / 池化 / offloading / 量化 / 淘汰).

平台**完全离线** (air-gapped 友好), tokenizer 通过 git vendor, 不依赖任何 LLM 后端服务. 支持 byte-level (regression baseline) 与 token-level (与 vLLM 一致) 两种编码; 当前 vendor 了 GLM-5 (MLA) 与 Qwen3 (GQA) 两个 tokenizer + KV 配置.

3 阶段 funnel 工作流: **筛选 → txt 直通 / 选定场景 → 详细 HTML**. 每个阶段的 CLI 命令、数据集格式、输出位置、指标解读, 全部见 [**USAGE.md**](USAGE.md).

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

## 工具速查

| 工具 | 文件 | 阶段 |
|---|---|---|
| 跨数据集筛选 (hit_rate + GB/min) | `scripts/target_users_hit_rate.py` | 1 |
| txt 树 → CSV 转换 | `scripts/txt_tree_to_csv.py` | 2 |
| per-user pipeline (analyzer + HTML) | `scripts/v2_run_pipeline.py` | 2 + 3 |
| model chains + 21 点阈值扫描 | `scripts/per_user_chain_analyzer.py` + `scripts/render_chains_html.py` | 3 |

详细 CLI + 案例见 [USAGE.md](USAGE.md). 工具内部分工 (analyzer / renderer / chain finder) 见 [USAGE.md §工具速查表](USAGE.md).

## 快速开始

```bash
git clone <repo-url> && cd two_queue_ttl
python3 -m venv .venv_glm5
.venv_glm5/bin/pip install transformers tokenizers jinja2

# 阶段 1: 跨数据集筛选 (auto-top-4 per dataset)
PYTHONPATH=. .venv_glm5/bin/python3 scripts/target_users_hit_rate.py \
  --dir data --models <m1>,<m2>,<m3> \
  --encoder glm5_token --tokenizer-path models/glm5_tokenizer --chat-mode wrap_user \
  --csv-out outputs/screen.csv --md-out outputs/screen.md
```

完整命令 + 阶段 2/3 见 [USAGE.md](USAGE.md).

## 离线运行

Ascend / 实验室机断网场景: `git pull` 拉取 vendored tokenizer + 代码后即可运行, 不需要任何运行时下载. 见 [`models/README.md`](models/README.md) "Why vendor in git" 章节.
