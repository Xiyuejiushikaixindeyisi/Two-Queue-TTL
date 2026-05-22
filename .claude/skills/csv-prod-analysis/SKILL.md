---
name: csv-prod-analysis
description: 对生产 CSV trace 数据集做现网分析 —— 阶段1 跨数据集筛选(ideal_hit_rate + GB/min) → 阶段3 完整 HTML(per-user 7 节 + model 跨用户汇总 + chain 21 点阈值扫描图)。当用户说"对 <数据集> 做现网分析 / CSV 现网分析 / 筛选+出完整报告 / 跑阶段1+阶段3"时使用。数据须在 data/<model>/raw/*.csv (含 timestamp)。
argument-hint: <model_dir1,model_dir2,...> [tok=glm5|qwen3]
allowed-tools: Bash(.venv_glm5/bin/python3 *) Bash(.venv/bin/python *) Bash(mkdir *) Bash(ls *) Read
---

对生产 CSV trace 做现网分析。复刻 USAGE.md 案例 1(阶段1筛选)+ 案例 3(阶段3完整 HTML)。

## 参数
- `MODELS` = 第 1 个参数: 逗号分隔的数据集目录名(对应 `data/<model>/raw/*.csv`)。
- `TOK` = 第 2 个参数, 默认 `glm5`。映射(chat-mode 一律 `wrap_user`):
  - `glm5` → `--encoder glm5_token --tokenizer-path models/glm5_tokenizer`
  - `qwen3` → `--encoder hf_token  --tokenizer-path models/qwen_v3_tokenizer`

> venv: 按 USAGE 用 `.venv_glm5/bin/python3`(含 transformers + jinja2)。若本机 tokenizer venv 名不同(如 `.venv`),换成实际的再跑。

## 步骤
0. `mkdir -p outputs`。对每个 model 用 `ls data/<model>/raw/*.csv` 校验数据存在; 缺文件就停下报告, 不要继续。

1. **阶段1 跨数据集筛选**:
   ```
   PYTHONPATH=. .venv_glm5/bin/python3 scripts/target_users_hit_rate.py \
     --dir data --models $MODELS \
     <TOK 映射> --chat-mode wrap_user \
     --csv-out outputs/screen.csv --md-out outputs/screen.md
   ```
   读 long 表, 按 USAGE 解读并报告: `ideal_hit_rate ≥ 0.5` = 高研究价值; `avg_gb_per_min` 接近/超过物理 KV cache 容量 = 池化/offloading 收益高。

2. **阶段3 完整 HTML(对 MODELS 里每个 model 各跑一遍, 互相独立)**:
   ```
   # Part A: per-user HTML + model 跨用户汇总
   PYTHONPATH=. .venv_glm5/bin/python3 scripts/v2_run_pipeline.py \
     --data-dir data --output-dir outputs --models "$M" \
     <TOK 映射> --chat-mode wrap_user \
     --analyzer-extra '--top-k-users 10 --min-request-pct 0.001'

   # Part B: model-level chains + 21 点阈值扫描图
   PYTHONPATH=. .venv_glm5/bin/python3 scripts/per_user_chain_analyzer.py \
     --raw-csv data/"$M"/raw --output outputs/"$M"/per_user_chains.json \
     <TOK 映射> --chat-mode wrap_user
   PYTHONPATH=. .venv_glm5/bin/python3 scripts/render_chains_html.py \
     --input outputs/"$M"/per_user_chains.json --output outputs/"$M"/per_user_chains.html
   ```
   `--analyzer-extra` 兜底保留长尾 user(默认 top-3+min-pct=0.01 会过滤)。

## 完成后
逐 model 报告 3 类 HTML 路径:
- `outputs/<M>/per_user_chains.html`(model + 阈值扫描图)
- `outputs/<M>/per_user_reports/cross_user_summary.html`(跨用户汇总)
- `outputs/<M>/per_user_reports/<uid>/user_report.html`(per-user 7 节)

阅读顺序: per_user_chains.html → cross_user_summary.html → 单 user report。任一步非零退出 → 打印错误输出并停止, 不要假装成功。
