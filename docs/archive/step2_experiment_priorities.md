# Step 2 实验优先级（基于 9 模型 21 user 实测）

> **创建时间：** 2026-05-14
> **数据来源：** `outputs/v2_4dim_summary.csv` + `outputs/v2_4dim_summary.md`（9 模型 21 user）
> **上游：** [`decision_matrix.md`](step3_algorithm_decision_matrix.md) §9.2 子类型推荐 + [`user_report_html_redesign.md`](user_report_html_redesign.md)
> **目的：** 把 v2 工具实测信号转化为可排期的 Step 2 实验清单（baseline + 预期收益 + 实施难度）

实测覆盖 9 模型（含 1 个 converted 数据集）× 21 user，跨 Qwen / DSK / GLM 系列 + 长上下文 + 大小模型混合。

---

## 1. 4 维度核心发现

### 1.1 hit_rate 分层（21 user）

| 段 | 数量 | 业务特征 |
|---|---|---|
| **> 0.80** | 5 user | GLM-V5.1 tianzhou (0.94) / Qwen-64K chipset2 (0.92) + S773 (0.82) / GLM-V5-32K S961 (0.82) + S3734 (0.80) — **长文档 / RAG / 工具调用模板** |
| 0.60-0.80 | 8 user | Qwen-32B-8K 多个 user + DSK-8K 部分 — **业务规范化好的多租户场景** |
| 0.30-0.60 | 3 user | DSK-8K S773 / Qwen-32B-32K ai.ocr / Qwen-32B-32K-converted ai.ocr — **过渡区** |
| **< 0.30** | 5 user | Qwen-8B-8K S773 / Qwen-32B-32K nebula / DSK-32K S773 + cloud / DSK-8K mdata — **业务上限低或污染源** |

**Insight**：13/21 = 62% 的 user 已经 hit > 0.6，平台整体不算差。**工程优化空间集中在底部 5 个**。

### 1.2 cache 压力 × hit_rate 二维分布

```
                    cache 压力 大           cache 压力 小
hit_rate 高    ① Qwen-64K S773 (1 个)    ⑤ GLM-V5.1, GLM-V5-32K,
                                            Qwen-64K chipset2 (8 个)
                                          → C(2) 弱池化 / B(2) 多队列
hit_rate 低    ② nebula 32K, S773 8B,    ③ DSK-8K mdata,
              S773 32K-converted ai.ocr    DSK-32K cloud (3 个)
              (5 个 — 污染源)            → D 业务侧
              → A(1) 隔离
```

**关键象限**：
- **① C(1) 强池化唯一候选**：Qwen-V3.5-27B-64K S773（hit 0.82 + P80 539 + unique 2M）
- **② A(1) 隔离 5 个候选**：搜索 / OCR / 答案评估业务（高 unique + 低 hit）
- **③ 业务上限低**：D 业务侧不投工程层

### 1.3 reuse_inversion 与模型规模负相关

| 模型 | 倒置 ratio | 业务画像 |
|---|---|---|
| **DSK-V3.1-32K** | **17.08** | cloud (0.04) vs mdata (0.70) 极端共存 |
| **Qwen-V3-32B-32K** | **4.8** | nebula (0.15) vs ai.ocr (0.38) vs 022 (0.73) 三段分化 |
| DSK-V3.1-8K | 2.82 | 中度分化 |
| Qwen-V3.5-27B-64K | 1.25 | 长文档专用，业务同质化 |
| Qwen-V3-32B-8K | 1.06 | 多 router 业务但规范 |
| GLM 系列 | 1.07 / 1.0 | RAG / 单租户 |

**核心 insight**：**大模型 + 32K 上下文长度的两个维度组合下，倒置最严重**。猜测：
- 大上下文 + 大模型 → 高单价 → 平台塞更多 user 在同实例 → user 间业务异质性放大
- 长上下文 → cache 压力天然大 → 任何低 hit user 都更容易驱逐他 user 的 chain

**生产暗示**：DSK / Qwen-32K 系列需要**主动业务分类 + 路由分区**，不能任由调度器混布。

### 1.4 spike 集中在长文档模型

| user | 突变倍数 | hit | 业务暗示 |
|---|---|---|---|
| GLM-V5-32K S961 | ×14.0 | 0.82 | 文档批量处理 |
| Qwen-64K chipset2 | ×11.0 | 0.92 | 文档复用业务 |
| Qwen-64K supply | ×7.0 | 0.74 | 同上 |
| Qwen-32B-32K-converted ai.ocr | ×5.14 | 0.22 | OCR 异常波动 |

**Insight**：spike 与 hit 高低**不相关**（chipset2 hit 0.92 也 spike）。**spike 是业务现象，不是 cache 现象**。对应**弹性扩容**，而非路由 / 淘汰算法。

---

## 2. Step 2 实验优先级表

按 ROI × 难度排序。每个实验含：触发 model+user / 预期收益 / 实施难度 / baseline 实验设计 / 风险。

### P0 最高 ROI（先做）

#### P0.1 — C(1) 强池化 + KV fp8 量化

| 字段 | 内容 |
|---|---|
| **目标 model+user** | Qwen-V3.5-27B-64K **S773** |
| **当前数据** | hit 0.82 / P80 539 blocks/s / unique 2.04M / 95% 流量 |
| **瓶颈识别** | 唯一同时 cache 压力大 + hit 高 + unique 大的 user；cache 容量是 hit ceiling 唯一限制 |
| **实验动作** | (a) fp8 量化 KV cache（vLLM 已支持）；(b) 容量扩张到 ≥ 2.5M block (即原 unique × 1.2)；(c) 评估 fp8 量化对 hit 的影响 |
| **预期收益** | hit 0.82 → 0.95+；按 95% 流量算，每月节约 token billion 级 |
| **难度** | **中**（vLLM fp8 支持成熟，主要是部署调优 + 监控） |
| **baseline** | 现状 fp16 + 当前容量 |
| **风险** | fp8 量化可能损失 KV 精度 → 个别 corner case 命中失败；需测 hit 实际下降幅度 |
| **预期周期** | 2-3 周（含量化精度验证） |

#### P0.2 — A(1) isolation routing

| 字段 | 内容 |
|---|---|
| **目标 model+user** | Qwen-V3-32B-32K **nebula** |
| **当前数据** | hit 0.15 / unique 2.6M / 28% 流量 / P80 667 blocks/s / **模型倒置 4.8x** |
| **瓶颈识别** | nebula 大量写入低复用 chain 驱逐同模型 ai.ocr (0.38) / 022 (0.73) 的 chain |
| **实验动作** | nebula 路由到独立 instance group；同模型 ai.ocr + 022 留在原 instance group |
| **预期收益** | nebula 自己保持 hit ~0.15（业务上限）；**ai.ocr 提升到 0.50+，022 提升到 0.85+** |
| **难度** | **低**（32B 小模型多实例部署成熟） |
| **baseline** | 现状所有 user 共享 instance pool |
| **风险** | nebula 隔离后 cost / latency 变化；ai.ocr 命中提升幅度待实测 |
| **预期周期** | 1-2 周 |

> **P0.1 + P0.2 是平台**最高确定性 ROI **的两个实验**。建议同时启动（互不干扰）。

### P1 高 ROI 中等难度

#### P1.1 — B(2) 多队列 LRU + C(2) 弱池化

| 字段 | 内容 |
|---|---|
| **目标 model+user** | GLM-V5.1 **tianzhou.ai**（单租户 7 chain） |
| **当前数据** | hit 0.94 / 7 chain dom_len 156 / 100% 流量 / unique 196K |
| **瓶颈识别** | 已经高 hit，但 7 chain 共享 LRU 可能互相驱逐；多队列可让每 chain 独立保留 |
| **实验动作** | (a) vLLM 内 B(2) 多队列改造（按 chain 划分）；(b) cache 容量保留 ≥ chain 总 pin 容量 (1463 block) |
| **预期收益** | hit 0.94 → 0.96-0.98（已接近上限，边际收益小但确定性高） |
| **难度** | **中**（vLLM 内部 chain pin + 多队列改造） |
| **baseline** | 现状单队列 LRU |
| **风险** | GLM-V5.1 是大模型（355B MOE），多队列改造成本高；hit 提升空间小可能不值实施 |
| **预期周期** | 3-4 周 |

#### P1.2 — A(1) 软隔离 + 业务侧 D 联动

| 字段 | 内容 |
|---|---|
| **目标 model+user** | DSK-V3.1-32K **S773**（60.8% 流量 + hit 0.09） |
| **当前数据** | hit 0.09 / unique 1.24M / 60.8% 流量 / P80 345 blocks/s / **模型倒置 17.08x** |
| **瓶颈识别** | 60.8% 流量 + hit 极低 = **最大可隔离体量**。其他 user (mdata 0.70 + cloud 0.04) 被它驱逐 |
| **实验动作** | (a) DSK-32K 大模型物理隔离成本高 → 软隔离（独立 cache 池 + request 路由分类）；(b) 业务方触达 S773 评估 prompt 改进可能性 |
| **预期收益** | S773 自己保持 hit ~0.09；mdata user 提升到 0.85+；cloud user 进一步评估（hit 4% 是业务上限） |
| **难度** | **高**（DSK 大模型不能多实例 → 调度器层改造 + cache 池软划分） |
| **baseline** | 现状所有 DSK-32K user 共享 cache 池 |
| **风险** | 软隔离调度器改造复杂；S773 业务方反馈不确定 |
| **预期周期** | 4-6 周 |

### P2 中等优先级

#### P2.1 — A(2) 多 chain 实例化

| 字段 | 内容 |
|---|---|
| **目标 model+user** | GLM-V5-32K **S3734**（5 chain × cov 14% × 78% 流量） |
| **当前数据** | hit 0.80 / 5 chain dom_len 157 / 78% 流量 / unique 89K |
| **实验动作** | 按 chain 部署多实例 + llm-d prefix routing |
| **预期收益** | hit 0.80 → 0.85-0.90 |
| **难度** | **中-高**（多实例部署 + prefix routing 配置） |
| **预期周期** | 2-3 周 |

#### P2.2 — Qwen-64K 长文档容量预约 + 弹性扩容

| 字段 | 内容 |
|---|---|
| **目标 model+user** | Qwen-64K 全部 3 user（spike + 长文档复用主导） |
| **当前数据** | 3 user 中 2 个有 spike (×11 / ×7) + S773 95% 流量 |
| **实验动作** | (a) Qwen-64K 独立扩容到长文档场景预算；(b) spike 触发弹性扩容（监控 + 自动加实例） |
| **预期收益** | spike 期 TTFT 不暴涨；S773 容量绑定后稳定 |
| **难度** | **低-中**（DevOps 层 + 监控告警） |
| **预期周期** | 1-2 周 |

### P3 低优先级

#### P3.1 — Qwen-32B-8K 多 user B(2) 多队列

| 字段 | 内容 |
|---|---|
| **目标 model+user** | Qwen-V3-32B-8K 全部 3 user（nebula / S773 / quality） |
| **当前数据** | 普遍 hit 0.71-0.75 + 多 chain |
| **实验动作** | vLLM 内 B(2) 多队列 |
| **预期收益** | 普遍 hit 提升 5-10pp |
| **难度** | **低**（vLLM 内部） |
| **预期周期** | 2 周 |

#### P3.2 — 动态扩缩容（spike user）

| 字段 | 内容 |
|---|---|
| **目标** | GLM-V5-32K S961 / Qwen-64K chipset2 / supply |
| **实验动作** | DevOps 监控 + 触发器：spike 期 5min × 5× 自动加实例 |
| **预期收益** | TTFT spike 期降低 50%+ |
| **难度** | **低**（DevOps 层） |
| **预期周期** | 1-2 周 |

---

## 3. 推荐 4 周实验排期

```
Week 1-2:  [P0.1] C(1) 强池化 实验启动 — Qwen-V3.5-27B-64K S773
           [P0.2] A(1) isolation 实验启动 — Qwen-V3-32B-32K nebula
           [P2.2] Qwen-64K 弹性扩容 (DevOps 并行)

Week 3-4:  [P0.1] C(1) 结果分析 + fp8 量化精度评估
           [P0.2] A(1) 结果分析 + ai.ocr/022 命中率验证
           [P1.1] B(2) GLM tianzhou 多队列设计 + GLM 团队对齐

Week 5-8:  [P1.2] DSK-32K S773 软隔离方案设计 + 业务方触达
           [P3.1] Qwen-32B-8K B(2) 实施
           [P3.2] spike 动态扩缩容上线

Week 9+:   [P2.1] GLM-V5-32K S3734 A(2) 多实例化
           长尾业务的 D 业务侧改写沟通
```

---

## 4. 4 个商业 / 战略故事（给内部 pitch）

### 故事 1：Qwen-V3.5-27B-64K 的容量瓶颈

- 单 user S773 占 95% 流量、hit 已 82%、unique 2M block、P80 539 blocks/s
- 这**不是 "cache 算法不行"，是 "cache 容量不够"**
- 优化路径：fp8 量化 + 容量扩张 + 跨实例池化
- 预期：hit 0.82 → 0.95（百分点级别提升 = token billion 级节约）
- **这是平台最高确定性 ROI**

### 故事 2：DSK 大模型的"老办法失效"

- DSK-32K 倒置 17x（全平台最严重）
- 60.8% 流量是"低复用业务" S773（hit 9%）
- 共享 cache 模型让另外 14.9% mdata user (hit 0.70) 的 chain 被驱逐
- **传统多租户共享 cache 在 200B+ MOE 上失效**
- 优化路径：业务路由分类（A(1) 软隔离）+ S773 单独 cache 池 / instance group
- **这是平台最大可优化体量（60.8% 流量）**

### 故事 3：Qwen-64K 已经在被用作"长文档 inference 平台"

- 4 个 user 中 3 个有 spike（×7-11）— 长文档批量上传场景
- 单一 S773 占 95% 流量，业务高度集中
- 优化路径：弹性扩容 + 长文档 cache 池化 + 流量预约机制
- **这是业务层产品机会**（per-user weekly report 也服务这类场景）

### 故事 4：小模型 + 多租户 = 隔离的 sweet spot

- Qwen-V3-32B-32K 倒置 4.8x、3 user
- 32B 模型可多实例部署 → A(1) isolation 容易做
- nebula 隔离后，ai.ocr 和 022 都受益
- **建议**：32B 小模型作为 isolation routing **首发实验场**

---

## 5. 实验设计共通要素（baseline / 监控 / 验证）

### 5.1 Baseline 要求

每个实验启动前必须有：
- **生产监控 7 天对照期**：记录 hit / latency p50/p95 / cost
- **vLLM 监控指标**：cache hit rate (本地) / block_used / new_block_inserted
- **业务方 SLO 基线**：TTFT p95 / 单 request 完整 latency

### 5.2 验证指标

每个实验完成时必须验证：
- **hit_rate 实测**（vLLM 本地 metric，区别于我们工具的字节级上界）
- **TTFT p50 / p95**（业务侧最终感知）
- **cost / 千 token**（钱效率）
- **副作用**（其他 user 是否受影响）

### 5.3 实验回滚条件

- 任一 hit 实测下降 > 5pp → 回滚
- TTFT p95 上升 > 20% → 回滚
- 业务方反馈出现质量问题 → 立刻停

---

## 6. 与 §9.2.5 / §9.2.8 的关系

- §9.2.5 的 21 user 子类型预期表是**基于工具规则的预测**
- 本文 P0-P3 实验排期是**预测 + 实测后的执行计划**
- §9.2.8 偏差日志记录**预测 vs 实测**不符的地方（已实测后填，见 decision_matrix.md §9.2.8）
- 实验完成后回填**实际收益**到本文（成为 Step 3 算法设计的输入）

---

## 7. 不在本文 spec 内（后续）

- 各实验的详细工程设计（vLLM 配置、调度器改造、prefix routing 权重调优）
- 业务方触达流程 / 协议
- 实验数据收集 pipeline（vLLM metric → 我们 cost dashboard）
- KV 量化精度评估方法论
