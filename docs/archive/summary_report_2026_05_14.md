# KV Cache 复用优化项目 · 阶段总结报告

> **完成时间：** 2026-05-14
> **数据基础：** 9 模型 21 user 真实生产 trace 实测
> **配套文档：**
> - [`step3_algorithm_decision_matrix.md`](step3_algorithm_decision_matrix.md) — v2 工具规则 + 21 user 归类 + 9 模型偏差日志
> - [`step2_experiment_priorities.md`](step2_experiment_priorities.md) — P0-P3 实验排期
> - [`metrics_glossary.md`](metrics_glossary.md) — 指标释义 + 图表算法
> - [`user_report_html_redesign.md`](user_report_html_redesign.md) — APP 级 HTML spec
> - [`per_user_chains_html_redesign.md`](per_user_chains_html_redesign.md) — 模型级 HTML spec

---

## 1. 背景

### 1.1 vLLM 中 prefix cache 原理

LLM 推理分两阶段：**prefill**（一次性计算输入 prompt 全部 token 的 KV）和 **decode**（自回归生成）。prefill 阶段的 FLOPs 与 prompt 长度 N 成 O(N²) 关系（attention），是 TTFT（time-to-first-token）的主要成本来源。

**vLLM prefix cache 机制**：
- KV cache 以 **block** 为粒度组织（vLLM GPU 默认 16 tokens / block；vLLM-Ascend 强制 128 tokens / block，见 `vllm_ascend/utils.py:1183`）
- 每个 block 按 token 序列计算 hash 作为 key
- 新 request 到来时，**按 token 序列查 hash → 命中的 block 直接复用 KV，跳过 prefill**
- 复用率越高 → prefill 越短 → TTFT 越低 → 同等 GPU 服务更多 QPS

**TTFT 削减直接对应**：
- 商业价值：token 单价下降（同样硬件服务更多请求）
- 用户体验：流式输出"首字延迟"变短

**vLLM-Ascend 关键约束**：
- block_size = **128 tokens**（不是 GPU vLLM 默认 16）
- 复用必须 **token 序列完整一致**（不是字节级共享）
- 量化策略（fp16 → fp8 / int8）影响 KV 存储大小但不改变命中判定

### 1.2 offline 分析平台的 prefix cache / LCP 方法

为什么需要离线分析？因为：
- 直接在 vLLM 中加指标 → 改 vLLM 代码 → 跨 7 个模型部署成本高
- 离线分析可以**跨模型横向比较**，找出谁是"真正的优化候选"

**核心算法（详见 `metrics_glossary.md` §3 + `decision_matrix.md` §1.2）**：

#### 字节级 prefix_path_key 哈希链

```
B_0, B_1, ..., B_N      ← prompt 切成 128-byte block (utf-8 字节, 不是 tokens)
K_0 = SHA256(B_0)
K_n = SHA256(K_{n-1} || B_n)    ← 哈希链, 每个 key 依赖前面所有 block
```

**关键性质**：哈希抗碰撞 → "K_n 在 seen_keys 中" ⟺ "K_0..K_{n-1} 都见过" → **LCP 计算只需单次顺序扫描 O(N)，不需要回溯**。

#### 两种 chain 检测算法

| 算法 | 函数 | 输出 | 用途 |
|---|---|---|---|
| **greedy max-child walk** | `find_lcp()` | 每 user 1 条主 chain | 模型级横向比较（`per_user_chains.html`）|
| **DFS multi-chain forest** | `find_chain_forest()` | 每 user 多条 chain（≤ 50）| APP 级深入剖析（`user_report.html`）|

两套算法用不同阈值（branch_threshold 0.25 vs 0.05），**chain 数 / 长度差异是设计如此，不是 bug**。

#### 字节级 ≠ token 级（虚高分析）

| 维度 | 我们工具 | vLLM 实际 |
|---|---|---|
| block 粒度 | 128 字节 | 128 tokens |
| 中文 token/byte | ~0.4 | — |
| 等价 vLLM block | 128 × 0.4 ≈ 51 tokens | 128 tokens |
| 单 block 字节数 | 128 字节 | ~384 字节（128 × 3 bytes/token）|

**虚高来源**：我们粒度更细 ~3 倍 → finite size 边界效应 + 字节边界 ≠ token 边界。

**虚高幅度**：
- 长 prompt 业务（≥ 500 tokens）：< 5 pp
- 短 prompt 业务（< 200 tokens）：10-30 pp
- 极端短业务（< 50 tokens）：30-50 pp

**关键结论**：ratio / 排序类指标不受影响，绝对数字系统性偏高 → **优化决策应优先看 ratio 和排序**。

#### 为什么不引入 tokenizer 对齐

1. 违反 offline 原则（tokenizer-free / 跨模型可比）
2. 每模型一个 tokenizer，维护成本 ×N
3. 收益 ≤ 5 pp 精度提升，不值

详见 `metrics_glossary.md` §6（block → GB 转换难点 + 需要的数据）。

---

## 2. 用户级 KV cache 命中率排序 + 路由策略实验

### 2.1 排序结果（21 user 实测）

| 段 | 数量 | 业务特征 | 代表 user |
|---|---|---|---|
| **> 0.80** | 5 | 长文档 / RAG / 工具调用模板 | GLM-V5.1 tianzhou (0.94), Qwen-64K chipset2 (0.92), GLM-V5-32K S961 (0.82) |
| 0.60-0.80 | 8 | 多租户规范化业务 | Qwen-32B-8K nebula/quality/S773 |
| 0.30-0.60 | 3 | 过渡区 | DSK-8K S773, Qwen-32B-32K ai.ocr |
| **< 0.30** | 5 | 业务上限低 / 污染源 | Qwen-32K nebula, DSK-32K S773, Qwen-8B-8K S773 |

**关键发现**：13/21 (62%) user 已经 hit > 0.6，平台整体不算差。**工程优化空间集中在底部 5 个**；高 hit 的顶部 5 个是**路由实验候选**（业务本身有复用价值，做 routing 能进一步压榨）。

### 2.2 路由策略实验候选（按 ROI × 难度排）

#### P0.2 — A(1) isolation routing

| 项 | 内容 |
|---|---|
| 目标 | Qwen-V3-32B-32K **nebula** |
| 数据 | hit 0.15 / unique 2.6M / 28% 流量 / P80 667 blocks/s |
| 触发 | reuse_inversion 4.8x + 低 hit + 高 unique 占比 |
| 动作 | nebula 路由到独立 instance group，同模型 ai.ocr/022 留共享池 |
| 预期收益 | nebula 保持 hit ~0.15，**ai.ocr 0.38 → 0.50+，022 0.73 → 0.85+** |
| 难度 | 低（32B 小模型多实例部署成熟） |

#### A(2) 多 chain 实例化候选

| 目标 | 数据 | 预期 |
|---|---|---|
| GLM-V5.1 tianzhou | hit 0.94 / 7 chain dom_len 156-377 / 100% 单租户 | hit 0.94 → 0.96-0.98 |
| GLM-V5-32K S3734 | hit 0.80 / 5 chain dom_len 157 / 78% 流量 | hit 0.80 → 0.85+ |

> ⚠️ 注：GLM-V5.1 实际是 200B+ MOE 大模型，工具产出"A(2) 多实例化"是因为 `model_params_class` 字段未补；补上后会落到 **A(4) 暂缓** + B(2) 多队列（详见 `decision_matrix.md §9.2.8` 偏差 1）。

#### A(3) skill / 文档 prefix routing 候选

| 目标 | 数据 | 预期 |
|---|---|---|
| Qwen-V3.5-27B-64K S773 | hit 0.82 / 1 chain dom_len 17 / 95% 流量 | 与 llm-d prefix_score 协同，hit 上界突破靠 P0.1 池化 |

### 2.3 结论

**在 Qwen-V3-32B-32K（小模型 + 多租户倒置）做 A(1) 隔离实验**是路由层最佳起点：
1. 倒置 4.8x，污染源（nebula）明确
2. 32B 小模型可多实例部署
3. 隔离后**两个 user 同时受益**（ai.ocr + 022）
4. 难度低（1-2 周可上线）

详细排期见 `step2_experiment_priorities.md` §2.

---

## 3. 用户级 cache 压力排序 + 池化策略实验

### 3.1 排序结果（P80 new_block/s）

| # | model / user | P80 (blocks/s) | hit_rate | unique_blocks | 池化候选 |
|---|---|---|---|---|---|
| 1 | Qwen-V3-32B-32K-converted / ai.ocr | 958 | 0.22 | 5.4M | ✗（hit 低）|
| 2 | Qwen-V3-32B-32K / nebula | 667 | 0.15 | 2.6M | ✗（hit 低）|
| **3** | **Qwen-V3.5-27B-64K / S773** | **539** | **0.82** | **2.0M** | **✅ 唯一**|
| 4 | DSK-V3.1-32K / S773 | 345 | 0.09 | 1.2M | ✗（hit 低）|
| 5 | Qwen-V3-8B-8K / S773 | 249 | 0.17 | 1.1M | ✗（hit 低）|
| 6 | DSK-V3.1-8K / S773 | 104 | 0.55 | 411K | 边界 |

**关键发现**：6 个 user P80 > 100 blocks/s，**只有 1 个（Qwen-64K S773）同时压力大 + hit 高**。其他高压力 user 都是污染源（A(1) isolation 候选，不是 C 池化候选）。

### 3.2 cache 压力 × hit_rate 二维分布

```
                    cache 压力 大           cache 压力 小
hit_rate 高    ① Qwen-64K S773 (1)      ⑤ GLM-V5.1, GLM-V5-32K,
              → C(1) 强池化               Qwen-64K chipset2 (8)
                                          → C(2) 弱池化 / B(2)
hit_rate 低    ② nebula 32K, S773 8B,    ③ DSK-8K mdata,
              S773 32K-converted ai.ocr   DSK-32K cloud (3)
              (5 个 - 污染源)            → D 业务侧
              → A(1) 隔离
```

### 3.3 池化策略实验候选

#### P0.1 — C(1) 强池化 + KV fp8 量化

| 项 | 内容 |
|---|---|
| 目标 | Qwen-V3.5-27B-64K **S773** |
| 数据 | hit 0.82 / P80 539 / unique 2.04M / 95% 流量 |
| 瓶颈识别 | cache 容量是 hit ceiling 唯一限制（不是算法不够，是 cache 装不下） |
| 实验动作 | (a) fp8 量化 KV cache；(b) 容量扩张到 ≥ 2.5M block；(c) 评估 fp8 对 hit 的影响 |
| 预期收益 | **hit 0.82 → 0.95+，按 95% 流量算每月节约 token billion 级** |
| 难度 | 中（vLLM fp8 支持成熟） |
| 预期周期 | 2-3 周 |

### 3.4 结论

**Qwen-V3.5-27B-64K S773 是平台最高确定性 ROI 的池化实验**：
1. 唯一同时 cache 压力大 + hit 高 + unique 大的 user
2. **不是"cache 算法不行"，是"cache 容量不够"**
3. fp8 量化 + 容量扩张是确定性收益
4. 95% 流量集中度 → 单点优化 = 全模型受益

---

## 4. 现象

### 4.1 用户偏斜：需做隔离

#### reuse_inversion_ratio 排序

| 模型 | ratio | 业务画像 | A(1) 隔离候选 |
|---|---|---|---|
| **DSK-V3.1-32K** | **17.08** ⚠️ | cloud (0.04) vs mdata (0.70) 极端共存 | S773（60.8% 流量 + hit 0.09）|
| **Qwen-V3-32B-32K** | **4.8** ⚠️ | nebula (0.15) / ai.ocr (0.38) / 022 (0.73) 三段分化 | nebula（28% 流量）|
| **DSK-V3.1-8K** | **2.82** ⚠️ | mdata (0.25) / ebg (0.71) / S773 (0.55) 中度分化 | mdata（低 hit）|
| Qwen-V3.5-27B-64K | 1.25 | 长文档专用，业务同质化 | — |
| Qwen-V3-32B-8K | 1.06 | 多 router 业务但规范 | — |
| GLM 系列 | 1.07 / 1.0 | RAG / 单租户 | — |

#### 核心发现：**reuse_inversion 与模型规模 + 上下文长度正相关**

| 因素 | 暗示 |
|---|---|
| 大模型 + 长上下文 | 单价高 → 平台塞更多业务 user 在同实例 → user 间异质性放大 |
| 大模型 cache 压力天然大 | 任何低 hit user 都更容易驱逐他 user 的 chain |
| 长文档模型业务同质 | 反之，业务路径单一 → 没有显著偏斜 |

**生产暗示**：DSK / Qwen-32K 系列需要**主动业务分类 + 路由分区**，不能任由调度器混布。

#### 隔离策略

| 隔离方式 | 适用场景 | 实施难度 |
|---|---|---|
| **物理隔离**（多实例部署）| 小模型（≤ 32B）| 低 |
| **软隔离**（独立 cache 池）| 大模型（DSK / GLM 200B+ MOE）| 中-高（调度器层改造）|
| **业务侧 D + A(1) 联动** | 流量大 + hit 极低（DSK-32K S773 60.8%）| 高（业务方触达 + 软隔离）|

### 4.2 请求量时序突变：需动态扩缩容

#### user 级 spike 排序

| # | model / user | spike count | max ratio | hit_rate | 业务暗示 |
|---|---|---|---|---|---|
| 1 | GLM-V5-32K / S961 | 2 | ×14.0 | 0.82 | 文档批量处理 |
| 2 | Qwen-V3.5-27B-64K / chipset2 | 2 | ×11.0 | 0.92 | 文档复用业务 |
| 3 | Qwen-V3.5-27B-64K / supply | 2 | ×7.0 | 0.74 | 同上 |
| 4 | Qwen-V3-32B-32K-converted / ai.ocr | 1 | ×5.14 | 0.22 | OCR 异常波动 |

#### 模型级 spike

| model | spike_count | max_ratio |
|---|---|---|
| GLM-V5.1 | 1 | ×6.0 |
| Qwen-V3-32B-32K-converted | 1 | ×5.14 |

#### 核心发现

1. **spike 集中在长文档模型**（GLM-V5-32K, Qwen-64K）
2. **spike 与 hit 无相关性**（chipset2 hit 0.92 也 spike，ai.ocr hit 0.22 也 spike）
3. **spike 是业务现象，不是 cache 现象** → 对应**弹性扩容**而非路由/淘汰算法

#### 动态扩缩容策略（P3.2 实验）

| 项 | 内容 |
|---|---|
| 目标 | GLM-V5-32K S961 / Qwen-64K chipset2 / supply |
| 实验动作 | DevOps 监控 + 触发器：spike 期 5min × 5× 自动加实例 |
| 预期收益 | spike 期 TTFT 降低 50%+ |
| 难度 | 低（DevOps 层） |
| 预期周期 | 1-2 周 |

---

## 5. 产品级 .html 报告 — 业务方周报

### 5.1 产品定位

| 维度 | 性能改进（§2-4 内容）| 产品周报（本节） |
|---|---|---|
| 决策方 | 平台 / 算法团队 | **业务方**（API 调用方）|
| 动作 | 路由 / 淘汰 / 池化 / 扩容 | prompt 改写 / 业务调整 / 容量预约 |
| 数据黑盒程度 | 内部用，技术细节多 | **白盒化**，业务可读 |
| 飞轮 | 平台侧单方面优化 | **业务侧主动优化**（长效）|

性能改进解决"平台能做什么帮业务"；周报解决"业务自己能做什么 + 知道发生了什么"。**两者互补**。

### 5.2 三层价值

#### L1 透明：你的业务在平台上发生了什么

| 内容 | 业务方 takeaway | 数据源 |
|---|---|---|
| 本周 cache 命中率 | 我的 prompt 复用得多好 | `ideal_hit_rate` |
| 本周请求量 + 趋势 | 我的服务用量趋势 | total_requests + 时序 |
| 本周 cache 压力 | 我占了平台多少容量 | unique_blocks + share_of_model_unique |
| 本周环比 | 我变好了还是变差了 | 上周 snapshot 对比 |

**业务方心理变化**：从"花了多少钱"到"花的钱有多大比例被复用"——把抽象的 LLM 成本变成可感知的效率指标。

#### L2 诊断：哪些值得关注

| 异常 | 业务方 takeaway | 数据源 |
|---|---|---|
| 流量突变 ×5+ 跳变 | 业务有突发，需要提前预约容量 | user_traffic_spikes |
| chain 长但 hit 低（death chain）| 有个 system prompt 模板已经不起作用了 | is_anomaly + LCP 反常诊断 |
| unique 占比飙升 | prompt 模板可能在漂移 | share_of_model_unique 周环比 |
| 与同模型同行匿名对比 | 同样业务别人 hit 75%，我才 30% | percentile 排名 |

**业务方心理变化**：从"用着挺好"到"我有具体可见的问题需要修"。

#### L3 行动：你可以做什么

| 建议 | 业务方动作 | 触发条件 |
|---|---|---|
| 减少 prompt 动态字段 | 移除 request_id / timestamp | hit 低 + 大量 unique |
| 拆分多版本 system prompt | 业务侧整理模板 | 多 chain + cov 分散 |
| 预约容量（spike） | 提前协调平台扩容 | spike_count > 0 |
| 检查死链 prompt | 审查 chain decoded 内容 | is_anomaly = true |
| 长文档接入文档复用模式 | 调整业务模式 | hit 高 + chain 短 |

**业务方心理变化**：从"被动接受平台优化"到"我自己能做的更多"——业务侧 D 类优化形成长效飞轮。

### 5.3 与内部 user_report.html 的差异

| 维度 | 内部 user_report.html | 业务方周报 |
|---|---|---|
| 受众 | 算法 / 平台工程师 | 业务方（产品 + 开发）|
| 关键板块 | §6 reuse_time / §8 chain forest / §9 子类型推荐 | L1 成本 / L2 异常 / L3 建议 |
| 数据准确度 | 工程精度（含 caveat 提示虚高）| 业务直觉精度（隐藏 caveat，± 10% 可信）|
| 时间维度 | 一段 trace 快照 | **周环比** + 趋势 |
| chain decoded | 完整显示 | **隐藏 / 部分模糊**（隐私 + 安全）|
| 同行对比 | 无 | 匿名 percentile 排名 |
| Step 3 算法子类型 | 详细 A(1)/B(2)/C(1) | **翻译成业务语言** |

#### 关键翻译

| 工程语言 | 业务语言 |
|---|---|
| ideal_hit_rate 0.82 | "82% 的请求开头是重复的，节约约 X 张卡时" |
| chain forest 7 条 cov 14.7% | "您有 7 个常用 prompt 模板，主模板覆盖 15% 流量" |
| is_anomaly = true | "您有个不工作的旧模板，建议清理" |
| A(1) isolation routing 触发 | "您的服务被识别为重写入低复用业务，平台已在评估隔离实例" |
| recommended_queue_count = 3 | "您的业务有 3 条主前缀路径，平台将为您预留独立缓存通道" |

### 5.4 对业务方的整体价值

1. **成本透明**：把 KV cache 黑盒变白盒，业务方能感知到自己 prompt 设计的效率
2. **业务健康度**：流量稳定性、prompt 漂移、突变检测都是业务侧 SLO 信号
3. **优化指引**：每个数据异常配可执行建议 + 预期收益数字
4. **可信度建立**：平台主动暴露透明数据 = 信任增加 = churn 下降
5. **数据飞轮**：业务方做 D 类 prompt 优化 → 平台整体 hit_rate 提升 → 成本下降 → 价格竞争力

### 5.5 商业 / 战略价值

1. **降低 churn**：业务方看到数据透明 = 信任增加
2. **提升留存**：自助优化指南 → 业务方更深入使用平台
3. **降低支持成本**：异常自动暴露 → 减少业务方提工单
4. **平台口碑**：相比竞品（同行只给账单），周报是差异化
5. **数据飞轮**：业务方 D 优化 → 平台 hit 提升 → 成本下降 → 价格竞争力

---

## 6. 后续工作清单（按优先级）

| 优先级 | 项目 | 责任方 | 周期 |
|---|---|---|---|
| **P0** | C(1) 强池化 + fp8 量化 (Qwen-64K S773) | 算法 + 部署 | 2-3 周 |
| **P0** | A(1) isolation (Qwen-32B-32K nebula) | 调度 + 部署 | 1-2 周 |
| P1 | B(2) 多队列 (GLM-V5.1 tianzhou) | vLLM 算法 | 3-4 周 |
| P1 | A(1) 软隔离 + D 业务侧 (DSK-32K S773) | 调度 + 业务 | 4-6 周 |
| P2 | A(2) 多实例 (GLM-V5-32K S3734) | 部署 | 2-3 周 |
| P2 | Qwen-64K 弹性扩容 + 容量预约 | DevOps | 1-2 周 |
| P3 | Qwen-32B-8K B(2) 多 user 多队列 | vLLM 算法 | 2 周 |
| P3 | spike 监控 + 动态扩缩容 | DevOps | 1-2 周 |
| **产品** | 业务方周报 MVP（L1 透明）| 产品 + 算法 | 4 周 |
| **产品** | 业务方周报 L2/L3（异常诊断 + 自助建议）| 产品 | 4-8 周 |

---

## 附录：数据完整性追踪

### 已实测项

- ✅ 9 模型 21 user trace 全部跑通 v2 工具
- ✅ 4 维度排序产出（hit / cache 压力 / 倒置 / spike）
- ✅ 子类型推荐自动产出（A(0/1/2/3/4) + B(1/2) + C(1/2) + D）
- ✅ 跨模型实测偏差识别（3 处与预期不符，详见 `decision_matrix.md §9.2.8`）

### 待修订项（不影响整体结论）

- [ ] 编辑 model_report.json 补 `model_params_class` 字段（GLM / DSK 大模型）后重跑 renderer
- [ ] `decision_matrix.md` §9.2.5 修订 Qwen-64K supply/chipset2 行（预期 A(2) 实际 A0 — share < 30% 不触发是正确判断）
- [ ] `decision_matrix.md` §3 主菜规则补边界条件 `hit < 0.20 AND unique > 1M → 强制 D 主菜`

### 不在本次范围（后续）

- block → GB KV cache 容量换算（需生产标定单一因子）
- 各 P0-P3 实验的详细工程设计
- 业务方周报的 UI 设计与触达渠道
- 跨用户 shadow group 自动检测（语义层任务，超出 offline 工具范围）

---

## 一句话总结

**这套离线分析工具用字节级 hash chain 替代 vLLM 的 token 级 hash chain，付出 < 30 pp 短 prompt 虚高的代价，换来跨 9 模型 21 user 横向比较能力 + 4 维度自动子类型推荐 + 产品级周报飞轮 — 把"凭感觉调 cache"变成"按数据找最高 ROI 实验"。**
