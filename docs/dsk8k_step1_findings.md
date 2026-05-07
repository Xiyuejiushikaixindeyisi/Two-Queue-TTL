# DS-8K Step 1 阶段性发现

> **状态：Step 1.1 + 1.2 正式完工**（2026-04-30）
> 配套模块：1.1 全局 chain 检测、1.2 per-user 分析、1.2.0 阈值扫描可视化、HTML 渲染
> 1.3（跨时间稳定性）待 `dsk8k_2h_5k / 24h_10k / 2d_10k` 三份采样到位后启动
> **任何 Step 3 算法实现必须等 1.3 验证完成后才能启动**
>
> **创建：** 2026-04-30
> **数据来源：** `data/deepseek_v3.1_8k/raw/DeepSeek-V3.1-Terminus-NoThinking-8K.csv`（生产采样，2 小时窗口，17,312 请求）

---

## 1. 实验环境

| 项 | 数值 |
|----|------|
| Trace 总请求数 | 17,312 |
| 总 block 数 | 1,033,722 |
| 用户数（产品 ID） | 14 |
| Block size | 128 字节（utf8_bytes） |
| Trie 构建耗时 | 5.0 秒 |
| 完整流程（含 per-user）耗时 | 8.3 秒 |

**性能验证：** Trie + 双阈值算法在 ~17K 请求 / 100 万 block 规模下表现良好，Agent 场景（10K req × 1024 block = 1000 万 block）的 ~10 秒预估准确。

---

## 2. 阈值灵敏度分析

| branch_threshold | chain_length | chain_coverage | 终止原因 |
|-----------------|------------|---------------|---------|
| 0.95 | **0** | — | branch_threshold at pos=0 |
| 0.45 | **56** | 41.2% (7,136 / 17,312) | coverage_threshold at pos=56 |
| 0.30 | 56 | 41.2% | coverage_threshold at pos=56 |

### 关键洞察：阈值跨过 0.498 后结果稳定

Chain 在 pos=0 处只有 49.8% 流量选择主链头，剩余 50.2% 走其他链头。一旦阈值低于 0.498，能跨过这个分叉点，后续每一步保留率都 ≥99%（pos=0→55 累计仅漂移 17%），所以 0.30 / 0.40 / 0.45 给出完全相同结果。

**生产推荐 default 阈值修正：**

| 当前 default | 建议 default |
|------------|------------|
| `branch_threshold=0.95`（严格闭合） | `branch_threshold=0.45`（识别"主流量主路径 + 容许少数派"的真实业务模式） |

生产 prompt 几乎不可能在每一步都有 ≥95% 严格相同（请求 ID、时间戳、多任务混杂等都会引入分叉），0.45 是更贴近实际业务的判断阈值。

---

## 3. Per-user Chain 分析（branch_threshold=0.45）

### 3.1 用户分层

| 类型 | 用户 | 请求数 | Chain |
|------|------|--------|------|
| **主网关** | S00000...773 | 15,314 (88.5%) | 56 blocks，与 global 完全相同 |
| **半模板服务** | com.huawei.mdata.mdata20180908 | 626 | 13 blocks，65.5% 覆盖 |
| **高熵无 chain** | com.huawei.ebg.ioc.efc | 602 | 0（每请求完全不同） |
| **短模板服务** | com.huawei.apaas.koopage | 466 | 5 blocks，61.4% 覆盖 |
| **超长定制** | S008454 | 158 | 98 blocks，68.4% 覆盖 |
| **完美闭合 chain（小用户）** | com.huawei.cloud.ioc.global 等 | 1–50 | 3–85 blocks，100% 覆盖 |

### 3.2 关键观察

1. **全局 56-block chain 完全由主用户（S00000...773）贡献**：其他 13 个租户的 chain 与全局完全不重叠（`prefix_match_with_global = 0`）
2. **DS-8K 是典型多租户部署**：1 个主网关账号 + 13 个企业内部产品（命名风格 `com.huawei.X` 是 Java 包名）
3. **主用户内部仍有大量分叉**：15,314 主用户请求中只有 7,136（46.6%）走主 chain，**剩余 8,178 个请求（53.4%）走其他次级 chain**——这是当前最大的优化潜力空间

### 3.3 用户名命名规律

- `S00000...XXX`（25 位 0 + 后缀）：内部产品 / 服务 ID，主入口可能是泛接入点
- `com.huawei.X`（Java 包名）：12 个企业内部业务系统
- 其余 `S008454`、`S007011` 等短编号：特定产品

---

## 4. Chain 内容（主链解码）

### 4.1 三个位置实测内容

```
pos=0  (49.8%)
    {"model": "DeepSeek-V3.1-Terminus-NoThinking-8K", "stream": true,
     "messages": [{"role": "system", "content": "你是智能助手...

pos=27 (45.7%)
    ...典型场景为：1. 用户咨询产品的指标，例如："华为三折叠的屏幕有多大..."

pos=55 (41.2%)
    ...# 禁止事项：- 不得向用户解释、安慰、道歉、承诺或补充任何正文内容...
```

### 4.2 业务定性

- **场景：** 华为某智能助手产品（产品 Q&A、指标查询、问题排查）
- **prompt 形态：** 身份定义 + 使用场景 + 行为禁忌的标准产品级 system prompt
- **稳定性预期：** 高（产品级模板，不会随每次请求变化）
- **跨日稳定性：** 待 1.3 验证

### 4.3 重要技术细节

链内容包含**完整 HTTP 请求体 JSON 外壳**（`{"model": "...", "stream": true, "messages": [...`），不只是 prompt 文本。这意味着：

- trace 的 `raw_prompt` 字段记录的是完整 API 请求 body
- chain 分析覆盖整个 JSON 前缀，包括模型名、流式标志、消息数组开头等
- 字节级共享前缀 = 7,168 bytes（56 blocks × 128 bytes）
- **token 级估算：** 含中文 + JSON 语法，约 2,000–3,000 tokens（< 8,192 上下文的 25–37%）

→ "chain 占 87.5% 上下文"的字节级表述应修正为 **"token 级约 25–37% prefill 内容可在 cache 中复用"**（按 8K context 算）。

**注意：** "可在 cache 中复用"≠"必须靠 pin 才能复用"。这部分前缀因访问极频繁，纯 LRU 下也大概率常驻 cache。pin 的真实价值见 §5.3。

---

## 5. 优化可行性评估（当前状态）

### 5.1 Pin 的真实价值再认识（2026-04-30 重要修正）

之前文档中"pin 56 blocks 锁定 42% 命中" / "节省 25–37% prefill" 的表述容易误导，让人以为 pin 是**平均命中率提升器**。这种理解错误。

**正确认识：纯 LRU 下，56 个 chain block 本来就大概率常驻 cache。**

依据：
- pos=0 chain block 在 2 小时内被访问 **8,621 次**（~1 次 / 秒）
- pos=55 chain block 也有 7,136 次（同样 ~1 次 / 秒）
- DS-8K 整体 insertion rate 仅 43 blocks/s
- 即使 cache 容量小到 1,000 blocks，LRU 窗口也有 ~23 秒
- chain block 在 23 秒内必然被再访问 → 不会被淘汰

**结论：稳态下，pin 与不 pin 产生的 cache 内容几乎相同。**

### 5.2 三种 cache 容量场景下 pin 的影响

| Cache 容量 | Chain 在 LRU 下命运 | Pin 边际收益 | 对其他用户影响 |
|-----------|------------------|------------|--------------|
| ≫ 工作集 | 全部常驻 | 几乎为 0 | 0 |
| ≈ 工作集 / 2 | chain 常驻 | 防止突发淘汰 | 接近 0 |
| ≪ 工作集 | chain 仍常驻（访问太频繁） | 仅消除 TTFT 长尾 | 接近 0（pin 槽位 LRU 也分配给同样的 block） |

**关键洞察：pin 占用的 56 个槽位，是 LRU 本来也会分配给这 56 个 chain block 的槽位**——pin 没有从其他用户那里"抢走"任何额外空间。

### 5.3 Pin 的真实价值在哪里

| 维度 | LRU only | LRU + Pin |
|------|---------|----------|
| Chain block 平均命中率 | 接近 100%（自然常驻） | 100%（保证） |
| Chain block **最差情况**命中率 | 突发流量下偶发淘汰 | 100%（保证） |
| TTFT 平均值 | baseline | 几乎不变 |
| **TTFT P99 / P999** | 有长尾（chain miss 重算 prefill） | **长尾消除** |
| 其他流量命中率 | baseline | **几乎不变** |

**Pin 的本质：消除 chain block 偶发淘汰带来的 TTFT 长尾，保证 chain follower 的延迟一致性；不是平均命中率提升器。**

### 5.4 真实可能造成损害的边界情形

| 情形 | 损害机制 | DS-8K 是否中招 |
|------|---------|---------------|
| Chain 变冷（prompt 漂移） | Pin 永久占用 → 死内存 | ⚠️ **待 1.3 验证** |
| Pin 数量 ≫ chain 实际价值 | 占用其他热 block 槽位 | ❌ 56 槽极少 |
| 突发高并发挤压 | 短时压制非 chain 用户 | ❌ DS-8K 是低 QPS |
| Pin 算法 bug 重复 pin | 多倍占用 | ❌ 实现细节可避免 |

### 5.5 当前总体可行性矩阵

| 维度 | 状态 |
|------|------|
| Chain 是否真实存在 | ✅ 已验证（0.45 阈值下） |
| Chain 内容稳定性（同一 trace 内） | ✅ 主流量内部 99% step retention |
| Chain 跨时间稳定性 | ⏳ **未验证**（依赖 1.3） |
| 主用户内部多 chain 结构 | ⏳ **未验证**（依赖 multi-chain detector，未实现） |
| 跨租户共享 | ❌ 已确认无跨租户共享 |
| Pin 对其他用户的负面影响（理论） | ✅ 接近 0（pin 槽位 = LRU 自然分配槽位） |
| Pin 对其他用户的负面影响（实测） | ⏳ 需 Step 2 真实 cache 容量 + Step 3 sim 对照验证 |
| Pin 的真实价值定位 | TTFT 长尾消除 + 稳定性保证（**不是**平均命中率提升） |

**结论：**
- 在 1.3 验证之前，DS-8K 优化方向"暂时可行但未确认"
- Pin 的真实价值需要在 Step 2 探测到真实 cache 容量、Step 3 sim 对比 LRU vs LRU+Pin 后才能定量
- **禁止在 1.3 完成前启动 Step 3 算法实现**

---

## 6. 待解决的关键问题（未启动）

### 6.1 Multi-chain 检测（候选模块，暂不实现）

**问题：** 主用户内部 53.4% 流量未跟随主 chain，可能存在多条次级 stable chain。当前算法只找单条最长 chain 看不到这些。

**算法构思（草图，仅文档化）：**

```python
def find_all_chains(node, parent_count, branch_threshold,
                    coverage_threshold, total):
    """在 trie 上递归探索所有 path-closed chains。

    每个分叉点：
      - 对所有 child.count / total >= coverage_threshold 的子节点
      - 都各自递归调用 find_all_chains
      - 收集每个分支返回的 chain，prepend 当前 child_key

    返回所有 chain，按 chain.coverage 排序。
    """
    chains = []
    for child_key, child in node.children.items():
        if child.count / total < coverage_threshold:
            continue
        ratio = child.count / parent_count
        if ratio >= branch_threshold:
            # 沿主分支继续走（同当前算法）
            sub = find_all_chains(child, child.count, ...)
            for chain in sub:
                chain.prepend(child_key)
                chains.append(chain)
        else:
            # 分叉点：但 child 仍 ≥ coverage_threshold → 独立递归
            sub = find_all_chains(child, child.count, ...)
            chains.extend(sub)
    return chains or [empty_chain]
```

**预期对 DS-8K 主用户的输出：**
- chain #1（主）：56 blocks，7,136 reqs（已知）
- chain #2（次）：? blocks，~1,000–2,000 reqs（候选 prompt 版本 / 多任务模板）
- chain #3 / #4：更小的次级 chain

**如果发现主用户内部 2–3 条次级 chain 各覆盖 ≥1,000 请求，主用户总命中覆盖率有望从 46.6% 提升到 70%+。**

**触发条件：** 1.3 验证主 chain 跨日稳定后，才考虑实现此模块。

### 6.2 Chain 跨日稳定性（依赖 1.3）

需要验证：
- 主 chain 头部 hash（`f91b65...`）是否在多天内稳定？
- chain 长度是否随时间变化？
- 主用户的多 chain 结构是否随时间稳定？

如果稳定 → 静态 pin 可行。
如果漂移 → 需要动态 chain 检测 + 滑动窗口（Step 3 复杂度上升一个量级）。

### 6.3 Token 级 chain 长度（不影响优化决策）

之前用字节估算的"87.5% 上下文"需要在 Step 2 用真实 tokenizer 验证，得到精确的"chain 占多少 token"。但这只影响 TTFT 节省的精确量化，不影响是否进入 Step 2 的决策。

---

## 7. 已验证模块的产出 artifacts

| 文件 | 内容 |
|------|------|
| `outputs/deepseek_v3.1_8k/chain_default_95.json` | 0.95 阈值结果（chain=0） |
| `outputs/deepseek_v3.1_8k/chain_loose_45.json` | 0.45 阈值结果（chain=56） |
| `outputs/deepseek_v3.1_8k/chain_loose_30.json` | 0.30 阈值结果（chain=56，与 0.45 同） |
| `outputs/deepseek_v3.1_8k/per_user_chains_95.json` | per-user 0.95（全部主流量 chain=0） |
| `outputs/deepseek_v3.1_8k/per_user_chains_45.json` | per-user 0.45（主用户 chain=56） |

---

## 8. 下一步明确动作（按时间序）

1. **等待跨时间数据到位**：`dsk8k_2h_5k`、`dsk8k_24h_10k`、`dsk8k_2d_10k`
2. **跑 1.3 跨时间稳定性分析**（待 `chain_stability_analyzer.py` 实现 + 数据到位）
3. **根据 1.3 结果决策**：
   - 主 chain 跨日 Jaccard ≥ 0.95 → 进入 Step 2 API 测试
   - 主 chain 跨日 Jaccard 0.7–0.95 → 进入 Step 2，但 Step 3 需含动态 chain 更新机制
   - 主 chain 跨日 Jaccard < 0.7 → DS-8K 不可静态优化，重新评估

**在 1.3 完成前，本文档不应启动任何 Step 3（算法实现）工作。**

---

## 9. 文档维护规则

- 每次跑新实验后，在 §7 中追加 artifacts 路径
- 1.3 完成后，更新 §5（优化可行性评估）和 §6.2（跨日稳定性）
- multi-chain detector 一旦实现，§6.1 的"候选模块，暂不实现"标记应移除并补上实测结果
