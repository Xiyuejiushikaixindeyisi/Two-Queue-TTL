# KV Cache 离线分析平台 — 使用指南

## 设计目的

针对 vLLM prefix cache 场景的**离线 trace 分析平台**, 用于:
1. 跨数据集**筛选**值得研究的场景 (高理想 KV cache 命中率 / 高 cache 压力)
2. 对选定场景出 HTML **详细报告**, 决策 KV cache 优化算法 (路由 / 池化 / offloading / 量化 / 淘汰)

平台**完全离线** (air-gapped 友好), tokenizer 通过 git vendor, 不依赖任何 LLM 后端服务.

## 3 阶段 funnel 总览

| 阶段 | 工具 | 输入 | 输出 | 用途 |
|---|---|---|---|---|
| **1. 筛选** | `scripts/target_users_hit_rate.py` | 多个 CSV 数据集 | terminal + CSV + MD long 表 | 选哪些 `(model, user)` 进阶段 3 |
| **2. txt 直通** | `scripts/txt_tree_to_csv.py` + 阶段 3 工具 | txt 散文件 | HTML 报告 (部分指标缺) | 同事提供 txt 数据快速看 |
| **3. 详细分析** | `scripts/v2_run_pipeline.py` + `scripts/per_user_chain_analyzer.py` + `scripts/render_chains_html.py` | `data/<model>/raw/*.csv` | per-user HTML + model HTML + 阈值扫描图 | 决策算法选型 |

## 环境准备

```bash
git clone <repo-url> && cd two_queue_ttl
ls models/glm5_tokenizer/ models/qwen_v3_tokenizer/
# 各应有: tokenizer.json + tokenizer_config.json (+ chat_template.jinja for GLM-5) + kv_meta.json

python3 -m venv .venv_glm5
.venv_glm5/bin/pip install transformers tokenizers jinja2
# air-gapped 机器: git pull (tokenizer 已 vendor), pip 一次性在能联网机器做好
```

## 支持的 Tokenizer

| 目录 | HF repo | 架构 | KV bytes/token | 公式 |
|---|---|---|---|---|
| `models/glm5_tokenizer/` | `zai-org/GLM-5` | MLA (DSA) | 89,856 | `78 × (512+64) × 2` |
| `models/qwen_v3_tokenizer/` | `Qwen/Qwen3-8B` | GQA | 147,456 | `2 × 36 × 8 × 128 × 2` |
| `models/qwen_v35_tokenizer/` | Qwen-V3.5 | — | (待 vendor) | — |
| `models/deepseek_v31_tokenizer/` | DeepSeek-V3.1 | MLA | (待 vendor) | — |
| `models/deepseek_v4_tokenizer/` | DeepSeek-V4 | MLA | (待 vendor) | — |

分析代码与具体模型解耦: 只要 vendor 进 `models/<name>_tokenizer/`, 用
`--encoder hf_token --tokenizer-path models/<name>_tokenizer` 即刻可跑, **无需改任何代码**.

**加新模型 — 推荐 (有本地权重, 无网络)**: 模型权重目录 (如 `/mnt/esfs/DeepSeek-V3.1/`)
本身带分词器小文件, 用脚本抽出来 (绝不复制权重) + 自动从 config.json 推导 `kv_meta.json`:

```bash
python3 scripts/vendor_tokenizer_from_weights.py \
  --src /mnt/esfs/DeepSeek-V3.1 --name deepseek_v31 --verify   # 先 --dry-run 预览
git add models/deepseek_v31_tokenizer/ && git commit -m "chore(models): vendor deepseek_v31"
```

命名约定 (全小写去点): Qwen-V3.5→`qwen_v35`, DeepSeek-V3.1→`deepseek_v31`, DeepSeek-V4→`deepseek_v4`.
联网 `hf download` 的另一条路径、字段细节见 `models/README.md`.

## 数据集格式

### A. CSV (生产 trace)

每行 1 个请求, 4 列 (顺序固定, 列名支持中文别名 + UTF-8 BOM):

| 标准列名 | 中文别名 | 用途 |
|---|---|---|
| `request_id` | `请求ID` | 请求唯一 ID |
| `user_id` | `租户ID` | 用户/租户标识 |
| `raw_prompt` | `请求参数` | 原始 prompt 全文 (含 \n, CSV 自动 quote) |
| `timestamp` | (无) | UNIX 整数秒. 缺则 spike/rpm/GB-min 时序失效但 hit_rate/chain/LCP 正常 |

**存放约定**: `data/<model_dir>/raw/*.csv` (多 csv 视为同一数据集, 字典序拼接).

例: `data/GLM-V5-32K-0513/raw/GLM-V5-32K-0513.csv`

### B. txt 树 (同事提供的散文件)

```
flat:                            nested:
<input_dir>/                     <input_dir>/
  <file_1>.txt                     <subdir_1>/
  <file_2>.txt                       <file_a>.txt
  ...                              <subdir_2>/ ...
```

1 个 txt = 1 个请求, 文件名任意 (含中文). `txt_tree_to_csv.py` 转成阶段 1/3 通用 CSV.

---

## 🚀 新数据集上手 (skill 一键跑)

> 这两条现网分析流程已封装成项目 skill (`.claude/skills/`): `git pull` 后可直接
> `/csv-prod-analysis` / `/txt-prod-analysis` 调用, 或用自然语言让 agent 自动触发。
> 下面是"拿到新数据集后怎么用"; 背后的分阶段 CLI 见后文阶段 1/2/3。

### A. 新 CSV 数据集 (生产 trace)

`/csv-prod-analysis <model_dir1,model_dir2,...> [tok=glm5|qwen3]`
→ 阶段 1 跨数据集筛选 + 阶段 3 完整 HTML。

**第 1 步 · 放数据**(参数 = **目录名**, 不是 csv 路径):
```bash
mkdir -p data/GLM-V5-32K-0516/raw
cp /拿到的路径/GLM-V5-32K-0516.csv data/GLM-V5-32K-0516/raw/
```
- 目录名自取(建议含模型+窗口+日期), **传给 skill 的参数必须与目录名完全一致**。
- 同一数据集可多 csv(字典序拼接); 多个不同数据集就建多个 `data/<dir>/raw/`。

**第 2 步 · 确认 CSV 格式**(skill 不转格式): 4 列, 列名支持中文别名 + UTF-8 BOM
(见上文「数据集格式 · A. CSV」)。timestamp 缺 → 时序图退化, hit_rate/chain 正常。

**第 3 步 · 调用**:
```text
/csv-prod-analysis GLM-V5-32K-0516              # GLM, glm5 为默认可省
/csv-prod-analysis <dir> qwen3                  # Qwen 数据集
/csv-prod-analysis dir1,dir2,dir3               # 多数据集一起
```
或自然语言: "我把新数据集放到 `data/GLM-V5-32K-0516/raw/`, 用 GLM-5 对它做现网分析"。

**要告诉 agent**: ① 数据集目录名 ② tokenizer 家族(GLM→`glm5` 默认 / Qwen→`qwen3`)
③ 数据已就位。可选: csv 还在别处就给源路径让 agent 先搬; 本机 venv 名若非 `.venv_glm5`;
是否只跑阶段 1。

**产出**(每数据集 3 类 HTML, 阅读顺序):
`outputs/<dir>/per_user_chains.html`(全局 + chain 阈值扫描图)
→ `.../per_user_reports/cross_user_summary.html`(跨用户汇总)
→ `.../per_user_reports/<uid>/user_report.html`(单 user 7 节)。

### B. 新 txt 数据集 (同事散文件)

`/txt-prod-analysis <txt根目录> <model_dir> <user_id> [tok=qwen3|glm5]`
→ txt 树→CSV + HTML pipeline(跳过阶段 1; 无 timestamp 时序图 graceful 退化)。

**第 1 步 · 准备 txt 目录**(无需搬进 `data/`): 1 txt = 1 请求, 文件名任意/中文 OK,
flat 或 nested 均可, 你只要知道**根目录路径**:
```
/home/ma-user/work/Qwen-V3-8B-0518/
  a.txt   b.txt   ...
```

**第 2 步 · 想好两个名字**: `<model_dir>` = 落地数据集名(skill 会生成
`data/<model_dir>/raw/converted.csv`); `<user_id>` = 整个 txt 集算作的那一个用户标识。

**第 3 步 · 调用**:
```text
/txt-prod-analysis /home/ma-user/work/Qwen-V3-8B-0518 Qwen-V3-8B-0518 qwen3_app
/txt-prod-analysis <txt根目录> <model_dir> <user_id> glm5    # 换 GLM tokenizer
```
或自然语言: "对 `/path/to/txt` 这堆 txt 做现网分析, 落地名 `Qwen-V3-8B-0518`,
user 叫 `qwen3_app`, 用 Qwen3 tokenizer"。

**要告诉 agent**: ① txt 根目录路径 ② 落地 model_dir 名 ③ user_id
④ tokenizer 家族(默认 `qwen3`)。

**产出**(单 user): `outputs/<model_dir>/per_user_reports/<user_id>/user_report.html`
—— §3 user metrics / §6 LCP / §7 chain forest ✅; §4/§5 时序卡片因无 timestamp
显示 caveat ⚠️。

---

## 阶段 1: 跨数据集筛选

**目的**: 一次看 N 个数据集 × top-K user 的 hit_rate + GB/min, 选高研究价值场景.

### 通用 CLI

```bash
# 默认 auto-top-4 每数据集
PYTHONPATH=. .venv_glm5/bin/python3 scripts/target_users_hit_rate.py \
  --dir data \
  --models <m1>,<m2>,<m3> \
  --encoder glm5_token --tokenizer-path models/glm5_tokenizer --chat-mode wrap_user \
  --csv-out outputs/screen.csv --md-out outputs/screen.md

# 跨数据集对比指定 user 列表 (出 pivot 表)
... --users <uid1>,<uid2>

# 切换 Qwen3 tokenizer
... --encoder hf_token --tokenizer-path models/qwen_v3_tokenizer
```

### 输入

`data/<model>/raw/*.csv` × N (多 model 一起跑)

### 输出 (3 个产物)

| 产物 | 路径 (默认) | 内容 |
|---|---|---|
| terminal long 表 | stdout | 每行 `model + rank + user_id + reqs + hit_rate + GB/min + duration` |
| CSV | `--csv-out` (default: `./target_users_hit_rate.csv`) | 10 列 (含 trace_duration_min, avg_gb_per_min) |
| Markdown | `--md-out` (default: `./target_users_hit_rate.md`) | Long 表 + (仅 `--users` 模式) pivot 表 |

### 解读 — 选场景两个维度

- `ideal_hit_rate ≥ 0.5` → **高研究价值** (优化收益空间大; 低 → 跳过)
- `avg_gb_per_min` 与物理 KV cache 容量量级对比:
  - 远低于容量 → 现有 prefix cache 已 OK, 跳过
  - 接近或超过容量 → offloading / 池化 / 量化算法**预期收益高**

将选定 `(model, user)` 列入阶段 3 的 `--models`.

### 🎯 案例 1: GLM-V5-32K 跨数据集筛选 (0513/0514/0515)

数据集摆放:
```
data/
  GLM-V5-32K-0513/raw/GLM-V5-32K-0513.csv
  GLM-V5-32K-0514/raw/GLM-V5-32K-0514.csv
  GLM-V5-32K-0515/raw/GLM-V5-32K-0515.csv
```

命令 (auto-top-4 模式, 每个数据集自动选请求量 top-4 用户):

```bash
mkdir -p outputs
PYTHONPATH=. .venv_glm5/bin/python3 scripts/target_users_hit_rate.py \
  --dir data \
  --models GLM-V5-32K-0513,GLM-V5-32K-0514,GLM-V5-32K-0515 \
  --encoder glm5_token \
  --tokenizer-path models/glm5_tokenizer \
  --chat-mode wrap_user \
  --csv-out outputs/screen_glm_v5_32k.csv \
  --md-out  outputs/screen_glm_v5_32k.md \
  2>&1 | tee outputs/screen_glm_v5_32k.log
```

跑完会得到 12 行 (3 datasets × 4 users) long 表. 按 `ideal_hit_rate` 倒序选高的 + 看 `avg_gb_per_min` 判断 cache 压力等级, 决定进阶段 3 的 `(model, user_id)` 子集.

---

## 阶段 2: txt 数据集直接进 HTML

**目的**: 同事提供 txt 散文件 (无 timestamp), 跳过阶段 1 直接出 HTML.

### 通用 CLI

```bash
# Step 1: txt → CSV (sequential rid + 自动打印 max prompt 长度)
python3 scripts/txt_tree_to_csv.py \
  --input-dir <txt-root-path> \
  --output-csv data/<model_dir>/raw/converted.csv \
  --user-id-from fixed --fixed-user-id <user_id_string>

# Step 2: 完整 HTML pipeline (timestamp 缺 graceful)
PYTHONPATH=. .venv_glm5/bin/python3 scripts/v2_run_pipeline.py \
  --data-dir data --output-dir outputs \
  --models <model_dir> --encoder hf_token \
  --tokenizer-path models/qwen_v3_tokenizer --chat-mode wrap_user
```

### 输入 & 输出

`<input-dir>/*.txt` (flat) 或 `<input-dir>/<sub>/*.txt` (nested) → `outputs/<model_dir>/per_user_reports/`:

| 文件 | 内容 (txt 场景) | 状态 |
|---|---|---|
| `<user_id>/user_report.html` | §1+§3 hit_rate, §3 max prompt length, §6 LCP top-10, §7 chain forest | ✅ |
| `<user_id>/user_report.json` | 完整结构化数据 | ✅ |
| `user_summary.json` | 全局摘要 | ✅ |
| `model_report.json` | 跨用户汇总 | ✅ |
| HTML §4 rpm 时序 / §5 GB/min 时序 / §6 reuse_time | timestamp 缺 → caveat 提示, 图退化 | ⚠️ |

### 🎯 案例 2: Qwen-V3-8B-0518 txt 数据集

数据布局 (用户机器):
```
/home/ma-user/work/Qwen-V3-8B-0518/
  <file_1>.txt        ← 1 txt = 1 请求, 中文文件名 OK
  <file_2>.txt
  ...
```

命令 (假设 repo 在 `/home/ma-user/work/zhangxiyue/Two-Queue-TTL`):

```bash
cd /home/ma-user/work/zhangxiyue/Two-Queue-TTL

# Step 1: txt → CSV (sequential rid, 不用文件名作 rid)
python3 scripts/txt_tree_to_csv.py \
  --input-dir /home/ma-user/work/Qwen-V3-8B-0518 \
  --output-csv data/Qwen-V3-8B-0518/raw/converted.csv \
  --user-id-from fixed --fixed-user-id qwen3_app
# 输出末尾会显示 max prompt N chars / M bytes — 阶段 2 的关键统计已可见

# Step 2: 完整 HTML pipeline (Qwen3 tokenizer)
PYTHONPATH=. .venv_glm5/bin/python3 scripts/v2_run_pipeline.py \
  --data-dir data --output-dir outputs \
  --models Qwen-V3-8B-0518 \
  --encoder hf_token \
  --tokenizer-path models/qwen_v3_tokenizer \
  --chat-mode wrap_user \
  2>&1 | tee outputs/qwen3_0518.log
```

打开 `outputs/Qwen-V3-8B-0518/per_user_reports/qwen3_app/user_report.html` 看:
- §3 user metrics — `max prompt length`, `total blocks`, `unique blocks`, `ideal_hit_rate`
- §6 LCP top-10
- §7 chain forest

§4/§5/§6 time series 卡片会显示 caveat "timestamp 全部解析失败, fallback 到 0".

---

## 阶段 3: 完整 HTML 详细分析

**目的**: 阶段 1/2 选定的场景, 出**全套** HTML 做算法决策.

**阶段 3 由 2 个独立 pipeline 组合** — `v2_run_pipeline.py` 不自动跑 chain analyzer + threshold sweep, 需要追加.

### 通用 CLI (per model)

```bash
M=<your_model_dir>

# Part A: per-user HTML (含 user_report.html × N + model_report.json + cross_user_summary.html)
PYTHONPATH=. .venv_glm5/bin/python3 scripts/v2_run_pipeline.py \
  --data-dir data --output-dir outputs \
  --models "$M" \
  --encoder glm5_token --tokenizer-path models/glm5_tokenizer --chat-mode wrap_user \
  --analyzer-extra '--top-k-users 10 --min-request-pct 0.001' \
  2>&1 | tee outputs/"$M"/pipeline.log

# Part B: model-level chains + 21 点阈值扫描图
PYTHONPATH=. .venv_glm5/bin/python3 scripts/per_user_chain_analyzer.py \
  --raw-csv data/"$M"/raw \
  --output outputs/"$M"/per_user_chains.json \
  --encoder glm5_token --tokenizer-path models/glm5_tokenizer --chat-mode wrap_user \
  2>&1 | tee outputs/"$M"/chain_analyzer.log

PYTHONPATH=. .venv_glm5/bin/python3 scripts/render_chains_html.py \
  --input outputs/"$M"/per_user_chains.json \
  --output outputs/"$M"/per_user_chains.html
```

`--analyzer-extra` 兜底确保长尾 user 不被默认 `top-3 + min-pct=0.01` 过滤.

### 输入

`data/<model>/raw/*.csv` (生产 trace, 含 timestamp)

### 输出 — `outputs/<model_dir>/`

| 文件 | 来源 | 内容 |
|---|---|---|
| `per_user_reports/<uid>/user_report.html` | Part A | **per-user HTML (7 节)** |
| `per_user_reports/<uid>/user_report.json` | Part A | 单 user 结构化数据 |
| `per_user_reports/<uid>/chain_forest.json` | Part A | 单 user chain 树 |
| `per_user_reports/user_summary.json / .csv` | Part A | model-level 摘要 + per-user 列表 |
| `per_user_reports/model_report.json` | Part A | model-level 跨用户指标 (含 encoder_meta + thresholds) |
| `per_user_reports/cross_user_summary.html` | Part A | **model-level HTML #1**: 跨用户横向对比 |
| `per_user_chains.json` | Part B | model-level chain forest + 21 点 threshold sweep 数据 |
| `per_user_chains.html` | Part B | **model-level HTML #2**: 含 **threshold sweep 图** + 跨用户 chain + LCP |

### HTML 7 节 (per-user) → 算法决策映射

| 章节 | 关键指标 | 算法决策含义 |
|---|---|---|
| **§1 模型层** | `n_users / ideal_hit_rate / rpm / GB/min p80 / reuse_inversion_ratio` | reuse_inversion ≥ 2 → **路由分流**; GB/min 高 → **多级缓存/offloading** |
| **§2 user vs model** | 3 水平条对比 | 单 user 偏离模型基线越大, 越值得单独研究 |
| **§3 user metrics** | `requests / ideal hit rate / max prompt length / total blocks / unique blocks (含当天总 GB) / hit blocks / rpm` | 命中率高 + chain 长 + 压力大 → 优化收益空间大 |
| **§4 流量时序** | req/min 图 + spike 检测 | spike 多 → 突发流量, 容量需 buffer |
| **§5 cache 压力** | blocks/min 图 + quantile 表 (P50/P80/P95/Max) + **GB/min** (token 模式) + cumulative WS 收敛状态 | 压力 ≪ 容量 → 现有 OK; ≈ 容量 → **容量分区/淘汰**; ≫ 容量 → **offloading/池化** |
| **§6 reuse time** | CDF + 4 分位数 | `reuse_time_p95 × GB/min ≈ 需要的 cache 容量` |
| **§7 chain forest** | chain 个数/长度/覆盖率/decoded 内容 + LCP top-10 直方图 | chain 少长高覆盖 → **chain pin**; 多短低覆盖 → **路由分流**; chain_shadow_pairs 大 → chain 数虚高 |

### 🎯 案例 3: 对 GLM-V5-32K-0514 + 0515 出完整 HTML

阶段 1 选出 0514 / 0515 两个场景值得详细分析. 两份**独立**报告 (因为每 model 对应独立的 cache 容量决策):

```bash
cd <repo-root>

for M in GLM-V5-32K-0514 GLM-V5-32K-0515; do
  echo "==================== $M ===================="

  # Part A: per-user reports + model-level cross_user_summary.html
  PYTHONPATH=. .venv_glm5/bin/python3 scripts/v2_run_pipeline.py \
    --data-dir data --output-dir outputs \
    --models "$M" \
    --encoder glm5_token \
    --tokenizer-path models/glm5_tokenizer \
    --chat-mode wrap_user \
    --analyzer-extra '--top-k-users 10 --min-request-pct 0.001' \
    2>&1 | tee outputs/"$M"_pipeline.log

  # Part B: model-level chains + 21-point threshold sweep
  PYTHONPATH=. .venv_glm5/bin/python3 scripts/per_user_chain_analyzer.py \
    --raw-csv data/"$M"/raw \
    --output outputs/"$M"/per_user_chains.json \
    --encoder glm5_token \
    --tokenizer-path models/glm5_tokenizer \
    --chat-mode wrap_user \
    2>&1 | tee outputs/"$M"_chain.log

  PYTHONPATH=. .venv_glm5/bin/python3 scripts/render_chains_html.py \
    --input outputs/"$M"/per_user_chains.json \
    --output outputs/"$M"/per_user_chains.html
done
```

每份完成后, **3 类 HTML 都齐全**:
```
outputs/GLM-V5-32K-0514/
├── per_user_chains.html               ← model HTML + 阈值扫描图 (Part B)
└── per_user_reports/
    ├── cross_user_summary.html         ← model HTML 跨用户汇总 (Part A)
    └── <safe_user_id>/
        └── user_report.html            ← per-user HTML (Part A)
outputs/GLM-V5-32K-0515/
└── (同上结构)
```

阅读顺序建议: `per_user_chains.html` (看全局 + threshold sweep 选 branch_threshold) → `cross_user_summary.html` (看哪些 user 偏离基线) → `<uid>/user_report.html` (深入单 user).

---

## 核心指标速查

| 指标 | 公式 / 含义 | 决策含义 |
|---|---|---|
| **ideal_hit_rate** | `hit_blocks / total_blocks` (LCP 累加; cache 无限) | prefix cache 优化的**上限**; 实际 ≤ 这个值 |
| **reuse_inversion_ratio** | `max(user hit_rate) / min(user hit_rate)` | ≥ 2 → 路由分流必要 |
| **avg_gb_per_min** (阶段 1) / **GB/min p80** (阶段 3) | `unique × block_size × kv_bytes/token / 1024³ / time` | × `reuse_time_p95` ≈ 需要的 cache 容量 |
| **reuse_time** | block 复用的时间间隔分布 | 短 → 容量需求低; 长 → 需大 cache 才能维持 |
| **chain coverage_pct** | 主 chain 占总 block 百分比 | 高 → chain pin 收益大; 低 → 多 template 路由 |
| **per-request LCP top-10** | 主要前缀模式 | 哪些前缀复用最多 |
| **max_prompt_length** | 字符数 + utf-8 字节数 | 配合 block_size 推算单请求 block 数 |
| **threshold sweep 曲线** | 21 个 branch_threshold 对应的 dominant chain length | 曲线"拐点"是合适的 branch_threshold |

## 常见陷阱

1. **byte 模式 vs token 模式**: byte_v1 hit_rate **系统性偏高 0-30pp**. 算法决策必须看 token 模式数字; byte 仅作 regression baseline.
2. **timestamp 全空**: spike / rpm / GB-min 时序失效, hit_rate / chain / LCP / max_prompt 正常 (HTML caveat 提示).
3. **top-K 默认过滤**: analyzer 默认 `--top-k=3 --min-pct=0.01`. 长尾 user 用 `--analyzer-extra '--top-k-users 50 --min-request-pct 0'` 强保留.
4. **chat_mode 差异**: GLM-5 wrap_user 5 tokens (含 `<think>`), Qwen3 8 tokens (无 `<think>`). 影响 block 0 但不影响 hit_rate 算法.
5. **`<placeholder>` 是占位符**: CLI 示例的 `<model_dir>` / `<txt-root-path>` 要替换成实际值.
6. **GLM-5 是 MLA 架构**: 不是标准 GQA, KV bytes/token 公式不同 (89,856 vs 标准几 MB), 已 vendor 在 `kv_meta.json` 透明标注.

## 工具速查表

| 工具 | 文件 | 阶段 | 说明 |
|---|---|---|---|
| 跨数据集筛选 | `scripts/target_users_hit_rate.py` | 1 | auto-top-k 默认 N=4 |
| txt → CSV 转换 | `scripts/txt_tree_to_csv.py` | 2 | sequential rid 默认 |
| per-user pipeline | `scripts/v2_run_pipeline.py` | 2 + 3 | 调 analyzer + renderer |
| model chains analyzer | `scripts/per_user_chain_analyzer.py` | 3 | 出 JSON + 21 点 threshold sweep |
| model chains renderer | `scripts/render_chains_html.py` | 3 | 出含 sweep 图的 HTML |
| (内部) 单数据集 analyzer | `scripts/per_user_report_analyzer.py` | (pipeline 调用) | per-user JSON |
| (内部) per-user HTML 渲染 | `scripts/render_user_report_html.py` | (pipeline 调用) | per-user HTML |

## 相关参考

- `models/README.md` — tokenizer vendoring + KV bytes/token refresh 步骤
- `docs/step1_6_token_level_experiment_plan.md` — token-level 编码设计
- `docs/metrics_glossary.md` — 指标定义详解
