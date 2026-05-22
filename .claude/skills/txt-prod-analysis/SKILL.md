---
name: txt-prod-analysis
description: 对同事提供的 txt 散文件数据集做现网分析 —— txt 树 → CSV → 直接出 HTML pipeline(跳过阶段1; 无 timestamp 时时序图 graceful 退化)。当用户说"对 <txt目录> 做现网分析 / txt 数据集分析 / txt 出报告 / 1 个 txt 一个请求"时使用。
argument-hint: <txt_root_dir> <model_dir> <user_id> [tok=qwen3|glm5]
allowed-tools: Bash(python3 *) Bash(.venv_glm5/bin/python3 *) Bash(.venv/bin/python *) Bash(mkdir *) Bash(ls *) Read
---

对 txt 散文件数据集(1 txt = 1 请求, 文件名任意/可中文, flat 或 nested)做现网分析。复刻 USAGE.md 案例 2。

## 参数
- `TXT_DIR` = 第 1 参数: txt 根目录(`<dir>/*.txt` 或 `<dir>/<sub>/*.txt`)。
- `MODEL`   = 第 2 参数: 落地数据集名 → 输出到 `data/<MODEL>/raw/converted.csv`。
- `USER`    = 第 3 参数: 固定 user_id(整个 txt 集算一个用户)。
- `TOK`     = 第 4 参数, 默认 `qwen3`(案例 2 用 Qwen3)。映射(chat-mode `wrap_user`):
  - `qwen3` → `--encoder hf_token  --tokenizer-path models/qwen_v3_tokenizer`
  - `glm5`  → `--encoder glm5_token --tokenizer-path models/glm5_tokenizer`

> venv: txt→CSV 用纯 `python3`(无依赖); HTML pipeline 用 `.venv_glm5/bin/python3`(含 transformers+jinja2)。若本机 venv 名不同(如 `.venv`), 换成实际的。

## 步骤
0. `ls "$TXT_DIR"` 校验目录存在且含 .txt; 缺则停下报告。`mkdir -p data/$MODEL/raw outputs`。

1. **txt → CSV**(sequential rid; 末尾会打印 max prompt 字符/字节数, 是阶段2关键统计):
   ```
   python3 scripts/txt_tree_to_csv.py \
     --input-dir "$TXT_DIR" \
     --output-csv data/$MODEL/raw/converted.csv \
     --user-id-from fixed --fixed-user-id "$USER"
   ```

2. **完整 HTML pipeline**(timestamp 缺 → graceful):
   ```
   PYTHONPATH=. .venv_glm5/bin/python3 scripts/v2_run_pipeline.py \
     --data-dir data --output-dir outputs --models "$MODEL" \
     <TOK 映射> --chat-mode wrap_user
   ```

## 完成后
报告 HTML 路径 `outputs/$MODEL/per_user_reports/$USER/user_report.html`, 并说明:
- ✅ 可用: §3 user metrics(max prompt length / total / unique blocks / ideal_hit_rate)、§6 LCP top-10、§7 chain forest。
- ⚠️ 退化: §4 rpm / §5 GB-min / §6 reuse_time 时序卡片因无 timestamp 显示 caveat。

任一步非零退出 → 打印错误输出并停止, 不要假装成功。
