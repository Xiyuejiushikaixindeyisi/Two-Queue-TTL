# Stage 3 Prompt-Rewrite 反事实分析方案 (GLM-V5)

**版本**: v1.0
**起草**: 2026-05-19
**状态**: 草案 (用户审批后实施)
**目标读者**: 本机实施者
**关联**: [step1_6_token_level_experiment_plan.md](step1_6_token_level_experiment_plan.md), [metrics_glossary.md](metrics_glossary.md)

---

## 0. TL;DR

在离线 trace 分析平台中加入**两个独立的 prompt 改写开关**, 评估它们对 KV cache 理想命中率 / chain 数 / chain 长度 / 覆盖率的影响:

| 开关 | 作用 | 对精度的影响 |
|---|---|---|
| **tool reorder** | 把含 dynamic 内容的 tool 排到 tools 数组尾部, 让 static tool 形成共享前缀 | 低 (只改顺序) |
| **placeholder replace** | 把 tool description / default 里的路径/日期等动态内容替换为唯一占位符 `__PATH_0001__` | 中 (改了 description 文本, 真机准确率需验证) |

两开关独立, 共 4 组合 (`base / reorder / placeholder / both`), 一次 convert 同时产出 4 列 hash_ids, Stage 3 HTML 横向对比。

**Scope 边界 (硬约束)**:
1. **仅改 `body["tools"]`**, `body["messages"]` 透传不动 — 由代码层 assert 兜底, 不依赖指标反推
2. **128 token / block**, 与 vllm-ascend 对齐, **不引入 tools 段 / messages 段切片**
3. **仅 GLM-V5**, Qwen3 二期再做
4. **离线分析, 不调 API** — demo 中的 TTFT/TPOT benchmark 路径不移植
5. **统一 convert 入口 `scripts/convert_trace.py --mode raw|chat`** — 同步把旧 `convert_raw_trace.py` 的"无 chat_template / 用 tiktoken / 单块独立 hash"的不对齐路径替换为新 codepath, 避免平台留两套不可比的 hash_ids

---

## 1. 背景

### 1.1 同事 demo 的发现

跨数据集分析中很多模型 chain 数量异常多, 根因之一: agent 工具 (opencode / weclaw / Claude Code 等) 内置 tools 排布不合理, dynamic 内容 (用户路径 / 当日日期 / UUID 等) 出现在 tools 数组前部, 导致 LCP 提前分叉, prefix cache 失效。

同事提供两份 demo:
- `benchmark_reorder.py` — 真机 replay benchmark, 比较 reorder 前后的 TTFT/TPOT (**不移植**, 我们离线分析)
- `detect_dynamic_tools.py` — 检测 + 改写函数 (**核心移植对象**)

### 1.2 demo 的 transform 函数 (核心移植对象)

| demo 函数 | 改了什么 | 对应平台开关组合 |
|---|---|---|
| `sort_tools_for_caching` | 基础版 reorder, detector 含 examples 假阳 (内部演进版本, **不暴露**) | — |
| `sort_tools_for_caching_pro(tools)` | reorder + pro detector (跳 examples) | `reorder=on, placeholder=off` |
| `normalize_tool_lossless(tool)` | placeholder 替换 path/date/file_uri, 不排序 | (**新组合**) `reorder=off, placeholder=on` |
| `sort_tools_for_caching_promax(tools)` | 上两者叠加 | `reorder=on, placeholder=on` |

**注**: demo 没有"只 placeholder 不 reorder"的入口, 平台侧需要构造一个 `normalize_only(tools)` 函数 (对每个 tool 调 `normalize_tool_lossless`, 保持原顺序), 约 10 行。

---

## 2. 4 组合精确定义

| reorder | placeholder | 实现函数 | 语义 |
|---|---|---|---|
| off | off | identity | baseline (原始 tools 数组) |
| **on** | off | `sort_tools_for_caching_pro(tools)` | 静态 tool 字母序在前, 动态 tool 字母序在后 |
| off | **on** | `normalize_only(tools)` (新建) | 对每个 tool 调 `normalize_tool_lossless`, 保持原顺序 |
| **on** | **on** | `sort_tools_for_caching_promax(tools)` | demo 完整版 |

### 2.1 Dynamic 检测的 regex 范围 (沿用 demo)

| 类别 | 模式 | 备注 |
|---|---|---|
| Windows path | `[A-Za-z]:[/\\]...` | 前置守卫避免 URL 内部匹配 |
| Unix path | `/(home\|Users\|usr\|var\|tmp\|opt\|etc\|mnt\|root\|deploy\|srv)/...` | 11 个根目录, **外置 YAML 配置** |
| file:// URI | `file:///...` | |
| UUID | RFC-4122 | |
| Date | 2020-01-01 ~ 2039-12-31, 含可选时间 | 覆盖生产 trace |
| 静态 URL 排除 | `https?://json-schema\.org/...` | 主动 strip, 避免 schema URL 假阳 |

### 2.2 Placeholder 命名规则 (沿用 demo)

- `__PATH_0001__` / `__FILE_URI_0001__` / `__DATE_0001__` / `__DEFAULT_PATH_0001__` / `__DEFAULT_DATE_0001__` / `__DEFAULT_FILE_URI_0001__`
- 单调递增, 编号在一批 tools 内全局唯一
- 离线分析**不做 rehydrate** (demo 的 `rehydrate_tools_from_bindings` 路径不移植 — 那是真机 replay 用的)

---

## 3. 数据流水线

```
<app>_raw.csv (含 request_input JSON 列)
        │
        ▼
scripts/convert_trace.py --mode chat
        │     ┌─ 对每行: row["request_input"] -> dict (含 tools + messages)
        │     ├─ 对每个变体 V ∈ {base, reorder, placeholder, both}:
        │     │      tools_V = transform_V(row["tools"])
        │     │      text_V  = apply_chat_template(messages=row["messages"],
        │     │                                    tools=tools_V,
        │     │                                    add_generation_prompt=True)
        │     │      token_ids_V = tokenizer.encode(text_V, add_special_tokens=False)
        │     │      hash_ids_V  = sha256_chain(token_ids_V, block_size=128)
        │     └─ 写出 4 列 hash_ids
        ▼
<app>_4variant.csv
    timestamp, model_id, user_id, request_type, input_length,
    hash_ids_base, hash_ids_reorder, hash_ids_placeholder, hash_ids_both
        │
        ▼
scripts/per_user_report_analyzer.py (扩展)
        │     对每列 hash_ids 独立建 trie / chain / LCP
        ▼
HTML (4 列横向对比)
```

### 3.1 关键: 统一 convert 入口, 修复旧路径

P3 决策: **新写 `scripts/convert_trace.py` 替代旧 `convert_raw_trace.py`**, 通过 `--mode raw|chat` 区分两种输入, 但**两种模式都走新 codepath** (`lib/prompt_encoder.GLM5TokenEncoder` + `apply_template` + SHA256 chain), 与 vllm-ascend 对齐。

| | `--mode raw` (替代旧脚本) | `--mode chat` (新增) |
|---|---|---|
| 输入列 | `raw_prompt` (用户文本) | `request_input` (整 JSON body) |
| tokenizer | **GLM-5** (修复: 旧脚本用 tiktoken / utf8 bytes) | GLM-5 |
| chat_template | **wrap_user** (修复: 旧脚本没应用) | `apply_chat_template(messages, tools)` |
| hash 算法 | **SHA256 chain** (修复: 旧脚本是单块独立) | SHA256 chain |
| 改写 | 无 | 4 变体 transform |
| 输出 hash_ids 列数 | 1 | 4 |
| 与 vllm-ascend 对齐? | ✅ (修复后) | ✅ |
| 适用场景 | 历史 trace / 单条文本 | 新 trace, 含 agent tools |

旧 `convert_raw_trace.py` 改为一行 shim 调用 `convert_trace.py --mode raw --chat-mode wrap_user`, 或直接删除并更新 README 引用。**现有 `data/` 下用旧脚本产出的 hash_ids 不强制重转, 但建议**: 新分析任务用新脚本, 老 trace 作为历史快照保留, 报告里标注 "legacy hash, 未对齐 vllm"。

---

## 4. CLI 形态

```bash
# Step 1a: convert chat trace (一次产 4 列)
python scripts/convert_trace.py \
    --mode           chat \
    --input          data/<app>_raw.csv \
    --output         data/<app>_4variant.csv \
    --tokenizer      models/glm5_tokenizer \
    --chat-template  models/glm5_tokenizer/chat_template.jinja \
    --block-size     128 \
    --patterns       configs/dynamic_patterns.yaml   # 可选

# Step 1b: convert raw trace (替代旧 convert_raw_trace.py, 修复 chat_template 对齐)
python scripts/convert_trace.py \
    --mode           raw \
    --input          data/<legacy>_raw.csv \
    --output         data/<legacy>_conv.csv \
    --tokenizer      models/glm5_tokenizer \
    --chat-mode      wrap_user \
    --block-size     128

# Step 2: 出报告 (可选择展示哪几列)
python scripts/run_stage3.py \
    --trace      data/<app>_4variant.csv \
    --app        <app> \
    --variants   base,reorder,placeholder,both         # 任意子集
```

无运行时 `--reorder on/off --placeholder on/off` 开关 — 4 列在 convert 阶段一次性写出, Stage 3 仅决定**展示**哪几列。

`--mode raw` 不接受 `--chat-template / --patterns` (raw 模式下没有 tools 概念); `--mode chat` 不接受 `--chat-mode` (chat 模式强制 `apply_chat_template(messages, tools)` 完整路径)。

---

## 5. 报告指标 (HTML 顶部表格)

```
                  base    reorder  placeholder  both
─────────────────────────────────────────────────────
理想命中率         X%       X%        X%         X%
chain 数          N         N         N          N
chain avg 长度    L         L         L          L
chain 覆盖率      C%        C%        C%         C%
delta vs base    —     +X.X pp   +X.X pp     +X.X pp
精度风险标签      —       低         中(*)        中(*)
```

\* placeholder 变体改写了 tool description 文本, 工具调用准确率需真机验证, HTML 加免责说明。

**与 vllm-ascend 对齐**: 整体命中率 = 128 token block 在 SHA256 chain 上的 LCP 命中比例, 与现有 token-level 工具链 (step1.6) 同一口径, 不引入"tools 段 / messages 段"切片。

---

## 6. 验证基准

### 6.1 demo 数字对齐 (ground truth)

用 demo 提供的 `ygzs.csv` 跑一遍, 我们的实现必须满足:

| 验证项 | 阈值 |
|---|---|
| `detect_dynamic_labels_pro(tool)` 输出 | byte-for-byte 匹配 demo |
| `sort_tools_for_caching_pro(tools)` 输出 | byte-for-byte 匹配 demo |
| `sort_tools_for_caching_promax(tools)` 输出 (含 bindings) | byte-for-byte 匹配 demo |
| `avg_pairwise_prefix_len` (demo phase 3 输出) | 数值对齐 |

### 6.2 toy trace 单测

构造 5 行 toy 请求, 覆盖:
1. 全 static tool (transform 应无变化)
2. 含 examples 假阳 (basic detector 会误判, pro 不会)
3. 含 default path (placeholder 应替换)
4. 含 description 内嵌 date (placeholder 应替换)
5. tools 顺序与字母序相反 (reorder 应排序)

4 变体的 hash_ids 手算后写死在测试里。

### 6.3 messages 不被误伤的兜底

```python
# tests/unit/test_prompt_rewrite.py
def test_messages_untouched_across_variants():
    body = {"tools": [...], "messages": [...]}
    for variant in ["reorder", "placeholder", "both"]:
        transformed = apply_variant(body, variant)
        assert transformed["messages"] == body["messages"]
```

这条 assert 从代码层保证 transform 不动 messages, 比指标反推更直接。

---

## 7. 风险与免责

| 风险 | 缓解 |
|---|---|
| placeholder 改了 description, 模型可能误解工具语义 | HTML 报告里把 placeholder / both 列标"精度风险中"; 离线分析仅给上界估计, 真机部署需 A/B 验证准确率 |
| Unix path regex 仅覆盖 11 个根目录 | YAML 外置, 部署侧可加 `/data /work /srv/` 等 |
| Date regex 仅覆盖 2020-2039 | 当前生产 trace 在此范围, 越界年份再加 |
| 长 tool 集合的渲染开销 | jinja 渲染是 convert 阶段一次性成本, 与下游分析解耦 |
| chat_template.jinja 版本漂移 | 锚定在 `models/glm5_tokenizer/chat_template.jinja` (已 vendored), git 跟踪 |
| reorder 改变工具列出顺序, 模型工具选择可能轻微偏移 | HTML 报告标"精度风险低"; 真机验证仍建议 A/B |

---

## 8. 文件落点

### 8.1 新建

```
lib/prompt_rewrite/
    __init__.py
    detect.py              # 移植 demo 核心: detect / sort / normalize 函数
    patterns.py            # 抽出 regex, 支持 YAML 外置 unix path roots
    chat_render.py         # jinja apply_chat_template 离线封装

scripts/
    convert_trace.py       # 统一 convert 入口, --mode raw|chat
    run_stage3.py          # Stage 3 HTML 入口 (如不存在)

configs/
    dynamic_patterns.yaml  # 可选: 覆盖 unix path roots / date 范围

tests/unit/
    test_prompt_rewrite.py # transform 单测 + demo 数字对齐回归

tests/fixtures/
    ygzs_demo_subset.csv   # ~10 行 demo subset, 用于 byte-exact 对齐
```

### 8.2 扩展

```
scripts/per_user_report_analyzer.py     # 支持 4 列 hash_ids 输入
scripts/render_user_report_html.py      # 4 列对比表 + 精度风险标签
docs/metrics_glossary.md                 # 加入 "delta vs base" 定义 + 新旧 codepath 区分
README.md / USAGE.md                     # 指向 convert_trace.py 统一入口
```

### 8.3 迁移 / 弃用

```
scripts/convert_raw_trace.py
    选项 A: 改为一行 shim 调 `convert_trace.py --mode raw --chat-mode wrap_user`
    选项 B: 直接删除, 改 README / pyproject 引用
    默认 A (保留向后兼容); 一周观察期后无人反映异常再切 B
```

附带任务:
- 用 1 份生产 trace 同时跑 旧 `convert_raw_trace.py` 和 新 `convert_trace.py --mode raw`, 量化 hash_ids 偏差 (印证 step1.6 plan §1.1 的 0-30pp 字节级偏差结论)
- 把偏差数字写入 `docs/metrics_glossary.md`

### 8.4 不动

```
sim/                                     # 仿真器无关
tests/ 现有 210 项                       # 必须全绿
data/ 下既有 trace                       # 保留, 不强制重转, 但报告标注 "legacy hash"
```

---

## 9. 落地计划 (6d)

| 阶段 | 模块 | 估时 | 产出 |
|---|---|---|---|
| 1 | `lib/prompt_rewrite/detect.py` + `patterns.py` (移植 + 抽配置) | 0.5d | demo 核心函数 + YAML 解析 |
| 2 | `normalize_only(tools)` 新函数 + 4 变体测试 | 0.5d | "placeholder 不 reorder" 路径 |
| 3 | `chat_render.py` (jinja 离线渲染) | 0.5d | `apply_chat_template(messages, tools)` 封装 |
| 4 | `scripts/convert_trace.py` 统一入口 (raw + chat 两 mode) | **1.5d** | 单一 CLI, raw 模式接入新 codepath, 4 列 chat 模式 |
| 5 | `per_user_report_analyzer.py` + `render_user_report_html.py` 4 列扩展 | 1d | HTML 横向对比表 |
| 6 | toy + ygzs 双重回归 (demo 数字对齐) + 旧/新 raw mode 偏差量化 | 1d | 测试全绿, byte-exact 对齐 demo, 偏差数字 |
| 7 | `convert_raw_trace.py` shim + README / pyproject 引用更新 | 0.5d | 向后兼容路径 |
| 8 | 文档完善 (本文件 + metrics_glossary 补条目) | 0.5d | 决策沉淀, 防 context 丢失 |
| **总** | | **6d** | |

---

## 10. 决策记录

| # | 决策 | 时间 | 理由 |
|---|---|---|---|
| D1 | 2 个独立开关而非 4 变体 (`--reorder on/off --placeholder on/off`) | 2026-05-19 | reorder 与 placeholder 是两个不同特性, 收益和精度风险不同, 需分开评估; demo 把它们合在 promax 里是工程便利, 非产品语义 |
| D2 | 不分 tools 段 / messages 段, 整体命中率单一指标 | 2026-05-19 | 与 vllm-ascend 对齐; 分段会引入 vllm 不存在的概念, 报告易误导; messages 不被误伤改靠代码层 assert |
| D3 | 仅改 `body["tools"]`, messages 透传 | 2026-05-19 | demo 的 transform 函数边界本就如此; 消息段的 dynamic 内容 (用户 query 里的时间/路径) 不在本次 scope |
| D4 | 仅 GLM-V5, Qwen3 二期 | 2026-05-19 | GLM-5 chat template 已 vendored, 路径成熟; Qwen3 chat template 还需调研 (是否支持 tools 字段) |
| D5 | `docs/` (复数) 而非 `doc/` | 2026-05-19 | 与项目现有 `docs/` 目录一致 |
| D6 | YAML 外置 regex 配置 | 2026-05-19 | 不同部署的 unix path 根目录不一致 (`/data /srv` 等), 不应 hardcode |
| D7 | 不暴露 demo 的 basic detector (含 examples 假阳) | 2026-05-19 | basic 是 pro 的演进前身, 没有独立产品价值 |
| D8 | 不移植 demo 的 `benchmark_reorder.py` (真机 replay 路径) | 2026-05-19 | 平台严格离线分析, 不调 API; benchmark 由同事在生产侧另行运行 |
| D9 | 离线分析不做 rehydrate | 2026-05-19 | rehydrate 是真机 replay 时把 placeholder 还原回真值用的, 离线分析仅关心 token 序列差异 |
| D10 | 统一 convert 入口 (P3 方案), 替代旧 `convert_raw_trace.py` | 2026-05-19 | 旧脚本走 `sim/io/prompt_tokenizer` (tiktoken / utf8 bytes, 无 chat_template, 单块独立 hash), **从未对齐 vllm-ascend**。新增 chat 模式时顺手把 raw 模式切到 Step 1.6 新 codepath (`lib/prompt_encoder` GLM-5 + wrap_user + SHA256 chain), 避免平台留两套不可比的 hash_ids。旧 `convert_raw_trace.py` 保留 shim 一周后再考虑删除 |

---

## 11. 未决项 / 二期

| 项 | 计划 |
|---|---|
| Qwen3 支持 | 二期。需确认 Qwen3 chat template 对 `tools` 参数的支持, vendored tokenizer 是否完整 |
| messages 段 dynamic 改写 | 暂不做。需先确认 demo 同事是否计划扩展到 messages |
| reorder/placeholder 真机 A/B (工具调用准确率) | 由同事在生产侧负责, 平台不参与 |
| 跨 app trace 综合报告 | 暂不做, 当前每 app 单独 HTML 即可 |
| 旧 `data/` trace 批量重转 | 不强制。新分析任务用新 convert; 老 trace 报告里标注 "legacy hash, 未对齐 vllm"。如某天发现历史结论需要 vllm 对齐数字, 再批量重跑 |
| 删除 `convert_raw_trace.py` shim | shim 上线一周后, 若无脚本/CI 反映异常, 删除并清理 README / pyproject 引用 |
