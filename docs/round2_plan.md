# Round 2 实现计划：Eviction Policy Baseline 与公平对比基础设施

**状态**：实施中  
**前置条件**：Round 1 已通过验收（见 Round 1 验收结论）  
**范围边界**：只实现 LRU / TTL-LRU / Two-Queue TTL MVP，不做多节点、多级缓存、Non-prefix、Hard Protected、Warm-up、Demotion

---

## 0. 已确认修正（优先于下文所有草案）

| 编号 | 决策 |
|------|------|
| D1 | TTL-LRU `access()` 不因 TTL 过期返回 False；只要 block 在 cache 中就 hit |
| D2 | TTL 只影响 `evict_one()` 的 victim 优先级；命中后刷新 `ttl_expiry` |
| D3 | Trace A（LRU vs TTL-LRU hit rate 差异）降级为 unit test，不做 integration 层面验证 |
| D4 | Trace C 修正：R1=[sys,usr1], R2=[sys,usr2]（sys 第二次命中晋升 Protected），R3..Rn=[new_i]，R_last=[sys,usr3] |
| D5 | 新增 integration test：`test_ttl_expiration_does_not_cause_miss_in_prefix_cache`，capacity 足够大不触发 eviction，TTL 过期后再次访问同一 block，TTL-LRU 和 TwoQueueTTL 都必须 hit |

---

## 1. 核心语义决策（已确认）

### 1.1 TTL-LRU 语义修正（与现有实现不同）

**现有实现（错误）**：`access()` 在 TTL 过期时返回 `False` 并删除 block（lookup miss）。

**Round 2 正确语义**：

> 只要 block 仍在 cache 中，`access()` 就返回 `True`。  
> TTL 过期**只影响** `evict_one()` 的 victim 选择优先级，不影响 lookup 结果。

**理由**：保证三种策略的 lookup 语义一致，差异只体现在 eviction 优先级，从而实现公平对比。

### 1.2 三种策略 eviction 优先级规范

| 策略 | evict_one() 优先级顺序 |
|------|----------------------|
| LRU | LRU 头（最久未访问） |
| TTL-LRU | ① TTL 过期 block（LRU 顺序） → ② TTL 未过期 block（LRU 顺序） |
| TwoQueueTTL | ① Probation LRU 头 → ② Protected 中 TTL 过期（LRU 顺序） → ③ Protected 中任意（LRU 兜底） |

### 1.3 TTL-LRU 与 TwoQueueTTL lookup 行为对比

| 行为 | TTL-LRU | TwoQueueTTL |
|------|---------|-------------|
| TTL 过期 block 被访问 | **Hit**（TTL 刷新） | **Hit**（TTL 刷新） |
| TTL 过期 block 在 eviction 中 | 优先被淘汰 | Protected 内优先被淘汰 |
| 一次命中 block 的保护力度 | 无（与多次命中平等） | Probation，优先被淘汰 |
| 多次命中 block 的保护力度 | 无（TTL 续期，但无专属队列） | 升入 Protected，TTL 保护 |

---

## 2. 实现步骤（按顺序执行）

### Step 1：修复 `TTLLRUPolicy.access()` 语义

**文件**：`sim/policies/ttl_lru.py`

**修改**：删除 `access()` 中的 TTL 过期检查和 `del self._cache[block_key]` / `return False` 逻辑。
TTL 过期 block 被命中时：
- `hit_count += 1`
- 刷新 `ttl_expiry = timestamp + ttl`
- `move_to_end(block_key)`
- 返回 `True`

**修改后 access() 伪代码**：
```python
def access(self, block_key, timestamp, block_pos, user_id):
    if block_key not in self._cache:
        return False
    meta = self._cache[block_key]
    meta.hit_count += 1
    meta.users_seen.add(user_id)
    meta.ttl_expiry = timestamp + self._ttl   # 刷新（过期也刷新）
    self._cache.move_to_end(block_key)
    return True
```

**evict_one() 不变**（已经是：先淘汰 TTL 过期 block，再淘汰 LRU 头）。

---

### Step 2：修复 `collector.py` 残留命名

**文件**：`sim/metrics/collector.py`

**修改**：`finalize()` 循环变量 `block_hash` → `block_key`；`_has_future_access()` 参数名 `block_hash` → `block_key`；对应 docstring 更新。

**涉及行**：187, 196, 202, 211, 212, 213。

---

### Step 3：新建 `tests/unit/test_ttl_lru_policy.py`

覆盖以下测试（TTL-LRU 当前无专属 unit test 文件）：

| 测试名 | 验证点 |
|--------|--------|
| `test_expired_block_still_a_hit` | TTL 过期后 `access()` 返回 True，hit_count 递增 |
| `test_expired_block_evicted_first` | `evict_one()` 优先淘汰 TTL 过期的 block |
| `test_non_expired_block_survives_when_expired_available` | 非过期 block 只在无过期 block 时被淘汰 |
| `test_ttl_refreshed_on_hit_even_if_expired` | 过期 block 命中后 ttl_expiry 更新到 timestamp+ttl |
| `test_ttl_has_no_effect_on_lookup_correctness` | 对比：无 TTL 的同等 LRU 在纯命中场景下结果一致 |
| `test_add_sets_ttl_expiry` | 新 block 入 cache 时 ttl_expiry == timestamp + ttl |
| `test_evict_respects_pinned` | evict_one(pinned=...) 跳过 pinned 中的 block |

---

### Step 4：补充 `tests/unit/test_two_queue_ttl_policy.py`

| 测试名 | 验证点 |
|--------|--------|
| `test_one_hit_block_stays_in_probation` | 命中 1 次不升级 Protected（threshold=2） |
| `test_probation_evicted_before_expired_protected` | Probation 优先于 TTL 过期的 Protected block |
| `test_protected_expired_evicted_before_valid_protected` | Protected 中过期 block 优先于未过期 block |

---

### Step 5：新建 `tests/integration/test_policy_comparison.py`

用三条 Toy Trace 证明策略间的行为差异。

#### Toy Trace A：TTL-LRU eviction priority 优于 LRU

```
capacity=2, TTL=10s

t=0:  request [A]     → A 入 cache
t=1:  request [B]     → B 入 cache，cache 满
t=5:  request [A]     → A 命中，TTL-LRU 刷新 A 的 TTL；LRU 将 A 移到 MRU 端
t=60: request [C]     → 需要 evict 一个 block
                         TTL-LRU: B 的 TTL(=1+10=11) < 60 → 过期，优先 evict B
                         LRU:     A 比 B 旧（t=5 vs t=1）→ evict A（错误，因为 A 更有价值）
                         实际上 LRU 也会 evict A，因为 A 最近访问是 t=5，B 是 t=1，B 更旧
                         → 需要重新设计 trace 让 LRU 做出次优选择
```

**修正版 Trace A**（让 LRU 在压力下驱逐更有价值的 block）：

```
capacity=2, TTL=10s

t=0:  request [A]     → A 入 cache（LRU 位置: [A]）
t=1:  request [B]     → B 入 cache（LRU 位置: [A, B]，A 最旧）
t=2:  request [A]     → A 命中（LRU 位置: [B, A]，B 最旧）
      此时 A.ttl_expiry=12, B.ttl_expiry=11（TTL-LRU 中）
t=50: request [C]     → 需要 evict
                         TTL-LRU: A.ttl=12<50 过期, B.ttl=11<50 过期，两者都过期
                                  按 LRU 顺序：B 最旧 → evict B；C 入 cache
                         LRU:     B 最旧 → evict B；C 入 cache
      此 trace 无差异，需要非对称 TTL 情况

```

**最终设计 Trace A（非对称访问窗口）**：

```
capacity=2, TTL=20s

t=0:  request [warm]         → warm 入 cache，ttl_expiry=20
t=1:  request [cold]         → cold 入 cache，ttl_expiry=21；cache 满
t=15: request [warm]         → warm 命中（未过期），TTL-LRU 刷新 ttl_expiry=35；LRU 移到 MRU
t=25: request [new]          → 需要 evict
                                TTL-LRU: cold.ttl=21 < 25 → 过期 → evict cold；warm 留存
                                LRU:     warm 最近访问 t=15，cold t=1 → evict cold（结果相同）
t=40: request [new2]         → 需要 evict
                                TTL-LRU: warm.ttl=35 < 40 → 过期 → evict warm（或 new）
                                LRU:     按 LRU 顺序 evict

```

实际上在简单 capacity=2 场景下 LRU 和 TTL-LRU 很难出现差异，因为 LRU 本来就会先驱逐最旧的。

**真正有差异的场景**（capacity=3，存在访问顺序与 TTL 过期的背离）：

```
capacity=3, TTL=10s, promotion_threshold=∞（禁止升级，纯 TTL-LRU）

t=0:   [A]   → A 入 cache，ttl=10
t=1:   [B]   → B 入 cache，ttl=11
t=2:   [C]   → C 入 cache，ttl=12；cache 满
t=5:   [A]   → A 命中（TTL-LRU 刷新 ttl=15；LRU 移 A 到 MRU 端）
t=13:  [D]   → 需要 evict 一个
               TTL-LRU: B.ttl=11<13 过期, C.ttl=12<13 过期, A.ttl=15 未过期
                        过期中 LRU 顺序：B(t=1) < C(t=2) → evict B
               LRU:     LRU 顺序 A(t=5) > C(t=2) > B(t=1) → evict B（相同）
               此处依然相同！

t=14:  [E]   → 需要 evict 一个
               TTL-LRU: C.ttl=12<14 过期, A.ttl=15 未过期 → evict C（过期优先）
               LRU:     C(t=2) < A(t=5) < D(t=13) → evict C（相同）
               还是相同！

→ 需要让 LRU 顺序与 TTL 过期顺序不一致，才能体现差异。
```

**关键设计原则**：Trace A 差异出现的条件是：
- 一个 block X 最近被访问过（LRU 认为它有价值），但 TTL 已过期（TTL-LRU 认为它无价值）
- 另一个 block Y 很久没被访问（LRU 认为它优先驱逐），但 TTL 未过期（TTL-LRU 不想驱逐它）

```
capacity=2, TTL=10s

t=0:   [Y]      → Y 入 cache，ttl=10
t=1:   [X]      → X 入 cache，ttl=11；cache 满
t=9:   [X]      → X 命中（TTL-LRU 刷新 X.ttl=19；LRU 移 X 到 MRU）
                  状态：LRU 顺序 [Y(t=0), X(t=9)]；TTL: Y.ttl=10, X.ttl=19
t=12:  [Z]      → 需要 evict
                  TTL-LRU: Y.ttl=10 < 12 → 过期 → evict Y；X 留存
                  LRU:     Y(t=0) 最旧 → evict Y（相同！LRU 和 TTL-LRU 此处选择一样）

→ 关键是让 X 比 Y 旧（LRU 会驱逐 X），但 Y 比 X 先过期（TTL-LRU 会驱逐 Y）。

t=0:   [X]      → X 入 cache，ttl=10（X 先进，LRU 认为更旧）
t=1:   [Y]      → Y 入 cache，ttl=11；cache 满（Y 后进）
t=5:   [Y]      → Y 命中（TTL-LRU 刷新 Y.ttl=15；LRU: Y 移到 MRU，X 成为 LRU 头）
                  状态：LRU 顺序 [X(t=0), Y(t=5)]；TTL: X.ttl=10, Y.ttl=15
t=12:  [Z]      → 需要 evict
                  LRU:     X(t=0) 最旧 → evict X（丢弃 X）
                  TTL-LRU: X.ttl=10 < 12 过期 → evict X（相同！）

仍然相同。需要 X 未过期但 LRU 认为它旧：

t=0:   [X]      → X 入 cache，ttl=100（大 TTL）
t=1:   [Y]      → Y 入 cache，ttl=5（小 TTL，从 t=1 开始算，ttl_expiry=6）
t=3:   [Y]      → Y 命中（TTL-LRU 刷新 Y.ttl=8；LRU: Y 移到 MRU，X 成为 LRU 头）
                  LRU 顺序：[X(t=0), Y(t=3)]；TTL: X.ttl=100, Y.ttl=8
t=10:  [Z]      → 需要 evict
                  LRU:     X 最旧 → evict X（X.ttl=100，大 TTL，value 高）← 次优
                  TTL-LRU: Y.ttl=8 < 10 过期 → evict Y；X 留存 ← 更优
t=11:  [X]      → LRU: X miss（已被驱逐）；TTL-LRU: X hit ← 差异出现！
```

**最终 Trace A（有意义的差异）**：

```python
# capacity=2, lru_ttl=5s（小 TTL 给 Y），base_ttl=100s（大 TTL 给 X）
# 或者统一 TTL，但通过访问模式制造差异

# 统一 TTL=10s，capacity=2
t=0:  [X]     # X 入 cache，X.ttl=10
t=1:  [Y]     # Y 入 cache，Y.ttl=11；cache 满；LRU顺序 [X, Y]（X 最旧）
t=3:  [Y]     # Y 命中，刷新 Y.ttl=13；LRU [X(t=0), Y(t=3)]
t=12: [Z]     # evict
              # LRU: X 最旧(t=0) → evict X
              # TTL-LRU: X.ttl=10<12 过期, Y.ttl=13>12 未过期 → evict X（过期优先）
              # 两者相同！结果都 evict X

# 结论：在 capacity=2 场景，LRU 顺序和 TTL 过期顺序经常一致，
# 因为"访问最旧"通常也意味着"TTL 最早过期"。
# Trace A 必须构造 TTL 和 LRU 的判断出现分歧的情形。
```

**真正有差异的 Trace A（使用混合访问）**：

```python
# capacity=3, TTL=10s
# 关键：让中间的 block B 最近被访问但 TTL 快到期；
#       让老 block A 的 TTL 刚刚续期

t=0:  [A]   A.ttl=10, LRU=[A]
t=1:  [B]   B.ttl=11, LRU=[A,B]
t=2:  [C]   C.ttl=12, LRU=[A,B,C]; cache 满
t=8:  [A]   A 命中; A.ttl=18; LRU=[B,C,A]  ← A 变成 MRU，B 成为 LRU 头
t=12: [D]   需要 evict
            LRU: B 最旧(t=1) → evict B  ← B 的 ttl=11<12 也过期，两者一致
t=13: [E]   需要 evict
            LRU: C 最旧(t=2) → evict C  ← C 的 ttl=12<13 也过期
            TTL-LRU: C.ttl=12<13 过期 → evict C
            此时 LRU=[A(t=8), D(t=12)]；TTL-LRU 同
t=19: [F]   需要 evict
            LRU: D(t=12) < A(t=8)? 不，A 在 t=8，D 在 t=12，D 更新
                 所以 LRU: A(t=8) 最旧 → evict A
            TTL-LRU: A.ttl=18<19 过期, D.ttl=22 未过期 → evict A（过期优先）
            两者依然相同！

# 根本问题：若只有一个 TTL 值，LRU 顺序和 TTL 过期时间天然相关。
# 唯一能制造分歧的方法：TTL 刷新后，旧 block 的 TTL 比新 block 的 TTL 更长。
```

**最简有差异场景**：

```python
# capacity=2, TTL=50s
# 关键：B 进入时间晚（LRU 认为 B 更新），但 B 没被再次访问（TTL 从入 cache 时算）
#       A 进入时间早（LRU 认为 A 旧），但 A 被重新访问（TTL 刷新到更远）

t=0:   [A]   A.ttl_expiry=50, LRU=[A]
t=40:  [B]   B.ttl_expiry=90, LRU=[A,B]; cache 满
             此时 LRU 头是 A（t=0 进入），TTL-LRU：A.ttl=50>40 未过期，B.ttl=90>40 未过期
t=45:  [A]   A 命中，A.ttl_expiry=95（刷新），LRU=[B,A]（B 成 LRU 头）
t=60:  [C]   需要 evict
             LRU: B(t=40) 最旧 → evict B
             TTL-LRU: A.ttl=95>60 未过期, B.ttl=90>60 未过期 → 两者都未过期 → 按 LRU: evict B
             还是相同！

# TTL-LRU 的差异只在有 block TTL 过期时才出现。
# 在 TTL 都未过期的情况下，退化为 LRU。
# 所以 Trace A 需要有 block 的 TTL 过期，且 LRU 顺序与过期顺序不一致。

t=0:   [A]   A.ttl_expiry=10（TTL=10s）
t=5:   [B]   B.ttl_expiry=15; cache 满（capacity=2）
t=8:   [B]   B 命中，B.ttl=18（刷新）; LRU=[A(t=0), B(t=8)]
             A.ttl=10, B.ttl=18
t=12:  [C]   需要 evict
             LRU: A(t=0) 最旧 → evict A     ← A.ttl=10<12 也过期，结果相同
             TTL-LRU: A.ttl=10<12 过期 → evict A

# 还是相同。因为"LRU 头"和"TTL 最先过期"经常是同一个 block。

# 唯一有效场景：B 早进入（LRU 认为 B 旧），但 B 的 TTL 被多次刷新（TTL-LRU 认为 B 有价值）
#               A 晚进入（LRU 认为 A 新），但 A 的 TTL 已快到期（TTL-LRU 认为 A 即将无价值）

t=0:   [B]   B.ttl=10, LRU=[B]（B 最旧）
t=1:   [A]   A.ttl=11, LRU=[B,A]; cache 满
t=8:   [B]   B 命中，B.ttl=18; LRU=[A,B]（A 成 LRU 头，A.ttl=11）
t=12:  [C]   需要 evict
             LRU: A(t=1) 最旧 → evict A（A 是 LRU 头，但 A.ttl=11 已过期）
             TTL-LRU: A.ttl=11<12 过期 → evict A（相同！）
t=19:  [D]   需要 evict（假设 C 在 cache）
             LRU: C(t=12) vs B(t=8) → B 更旧 → evict B（但 B.ttl=18<19 也过期）
             TTL-LRU: B.ttl=18<19 过期 → evict B（相同）

# 结论：设计真正体现 LRU vs TTL-LRU 差异的 trace 比预期困难。
# 核心问题是：TTL 从"上次访问时间"开始算，和 LRU 的"上次访问时间"高度相关。
```

**根本分析**：在 TTLLRUPolicy 中，`ttl_expiry = last_access_time + ttl`，而 LRU 也按 `last_access_time` 排序。因此，如果 TTL 固定，**TTL 最先过期的 block 往往也是 LRU 头**，两者选择一致。

差异出现的充要条件：**不同 block 有不同的有效 TTL 窗口**（即某些 block 的 TTL 刷新次数多，导致 TTL 过期时间比 LRU 顺序更晚）。

**有效的 Trace A 设计（利用 TTL 多次刷新）**：

```python
# capacity=2, TTL=10s
# B 多次命中，TTL 一直刷新，但 LRU 顺序上 B 也一直是 MRU（所以 LRU 不驱逐 B）
# A 只进入一次，TTL 没有刷新，LRU 顺序上 A 是 LRU 头（LRU 要驱逐 A）
# 这个场景两者是一样的，因为 A 既是 LRU 头又是 TTL 最先过期

# 真正差异：在 capacity≥3 时，可以构造：
# 有一个 block M 最近命中（LRU 认为它新），但 M 的 TTL 已过期（进 cache 很久没访问）
# 等等，TTL 是从上次访问开始算，如果"最近命中"就是最新访问，TTL 不会过期

# 正确理解：TTL-LRU 中，block 的 TTL 是从"上次访问时间"算起。
# 如果一个 block 最近被访问（LRU 认为它新），它的 TTL 也刚刚刷新（TTL-LRU 也不驱逐它）。
# 所以 LRU 顺序和 TTL 过期顺序，在 TTL-LRU 策略下，天然高度一致。

# 结论：Trace A（LRU vs TTL-LRU 行为差异）在当前 TTLLRUPolicy 语义下
#       几乎不可能出现差异，因为 ttl_expiry = last_access + ttl 与 LRU 语义等价。
#
# TTL-LRU 的实际价值不在于 hit rate 差异，而在于：
# 1. 为 TwoQueueTTL 提供"只使用 TTL 的 baseline"
# 2. 在有 TTL miss（原始语义）时，TTL-LRU 用来隔离 TTL 的影响
#
# Round 2 Trace A 应改为：验证 TTL-LRU eviction 的内部机制（单元测试），
# 而非试图在 integration 层面证明 hit rate 差异。
```

#### Toy Trace B：TwoQueueTTL 保护多次命中 block 优于 TTL-LRU

```python
# capacity=3, TTL=100s, promotion_threshold=2
# 设计目标：hot block 被保护到 Protected，在缓存压力下 Probation block 被驱逐

t=0:   [hot]                → hot 入 Probation
t=1:   [hot]                → hot 命中，hit_count=2，TwoQueueTTL: 升入 Protected(ttl=101)
                               TTL-LRU: hot 留在 cache，ttl=101（刷新）
t=2:   [new1]               → new1 入 cache
t=3:   [new2]               → new2 入 cache；cache 满(capacity=3)
t=4:   [new3]               → 需要 evict
                               TwoQueueTTL: new1 在 Probation LRU 头 → evict new1；hot 安全
                               TTL-LRU: new1(t=2) 最旧 → evict new1（相同）
t=5:   [new4]               → 需要 evict（cache: hot, new2, new3）
                               TwoQueueTTL: new2 在 Probation LRU 头 → evict new2；hot 安全
                               TTL-LRU: new2(t=3) 最旧 → evict new2（相同）
...（连续 many new_i，每个只访问一次，持续驱逐 Probation block）
t=200: [hot]                → 检查 hot 是否仍在 cache
                               TwoQueueTTL: hot 在 Protected，TTL=101（早已过期），但此时 cache 中
                                            仍有 Probation block → hot 一直存活（直到 Probation 全空）
                               TTL-LRU: hot.ttl=101 < 200 → TTL 过期，在 evict 压力下早已被清
```

**此 trace 的有效性**：当 Probation block 持续补充（one-hit-wonder 流），TwoQueueTTL 的 hot block 能持续存活；TTL-LRU 在 TTL 过期后，hot block 和 new block 在 eviction 中平等竞争，被驱逐概率更高。

**但注意**：按修正后的 TTL-LRU 语义（TTL 只影响 eviction 优先级），hot block 的 TTL 刷新在访问时也更新，实际上 TTL-LRU 也会优先驱逐过期的 new block（如果它们比 hot block 更早过期）。Trace B 需要精确计算时间以确保行为差异。

#### Toy Trace C：TwoQueueTTL 抗 one-hit-wonder 污染（最有说服力的场景）

```python
# capacity=4, TTL=1000s（大 TTL，不影响结果）
# 目标：sys_block 是多次复用的系统 prompt block，usr_block 是一次性用户 block

# 前两个请求建立 sys_block 的 hot 状态
t=0:   [sys, usr_0]         → sys 入 Probation, usr_0 入 Probation
t=1:   [sys, usr_1]         → sys 命中(hit=1), usr_1 入; TwoQ: sys 还在 Probation
t=2:   [sys, usr_2]         → sys 命中(hit=2), TwoQ: sys → Protected；usr_2 入
                               LRU/TTL-LRU: sys 仍在 cache

# 然后连续请求只有 usr_i，不复用 sys
t=3:   [usr_3]              → 填 cache（capacity=4）
t=4:   [usr_4]              → 需要 evict; TwoQ: usr_0 在 Probation LRU 头 → evict usr_0
t=5:   [usr_5]              → 需要 evict; TwoQ: evict usr_1（Probation）；sys 安全
...（20 个 usr_i）

# LRU/TTL-LRU 在容量压力下可能 evict sys
t=20:  [sys, usr_20]        → 检查 sys 是否命中
                               TwoQueueTTL: sys 在 Protected，命中 → HIT
                               LRU: sys 可能已被 evict（依赖具体 LRU 顺序）
```

**预期结果**：在持续 one-hit-wonder 流场景，TwoQueueTTL 的 sys_block 命中率显著高于 LRU 和 TTL-LRU。

---

### Step 6：验证端到端 summary metrics

在 `tests/integration/test_runner.py` 或新文件中，验证三种策略在 `sample_trace.csv` 上产出以下字段：

```python
required_fields = {
    "policy", "prefix_block_hit_rate", "saved_prefill_tokens",
    "eviction_count", "hot_prefix_eviction_count",
    "evicted_before_next_hit_count",
    "protected_eviction_count", "probation_eviction_count",
    "protected_pollution_rate", "promotion_count",
}
```

对 LRU 策略：`protected_eviction_count`、`promotion_count` 等字段为 0 是正常的。

---

## 3. 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `sim/policies/ttl_lru.py` | 修改 | 删除 `access()` 中的 TTL 过期 → miss 逻辑 |
| `sim/metrics/collector.py` | 修改 | `block_hash` 命名残留 → `block_key` |
| `tests/unit/test_ttl_lru_policy.py` | 新建 | TTL-LRU 专属 unit tests（7 个） |
| `tests/unit/test_two_queue_ttl_policy.py` | 修改 | 补充 3 个测试 |
| `tests/integration/test_policy_comparison.py` | 新建 | Toy Trace B/C 对比测试 |
| `docs/terminology.md` | 已完成 | Round 1 已写入 |
| `docs/round2_plan.md` | 已完成 | 本文件 |

---

## 4. 不确定项与待确认

| 问题 | 当前假设 |
|------|---------|
| Trace A（LRU vs TTL-LRU）是否有意义 | **有疑问**：TTL 从 last_access 算，与 LRU 语义高度一致，可能无法构造有效差异 trace。建议 Trace A 降级为纯 unit test 验证 eviction priority，integration 层面只做 B/C |
| TTL-LRU 的 TTL 是否从"入 cache 时间"还是"上次访问时间"算起 | 当前实现：入 cache 时设置 `ttl_expiry=entry_time+ttl`，命中时刷新 `ttl_expiry=access_time+ttl` |
| TwoQueueTTL Protected TTL 过期后是否 demotion | Round 2 不做 demotion，只影响 eviction 优先级 |

---

## 5. 完成标准（Round 2 验收）

1. `TTLLRUPolicy.access()` 在 TTL 过期时返回 `True`（hit）。
2. `test_ttl_lru_policy.py` 全部通过（≥7 个测试）。
3. `test_policy_comparison.py` 中 Trace B/C 验证 TwoQueueTTL 的保护优势。
4. 全部 101+ 测试通过，无回归。
5. `collector.py` 中无 `block_hash` 命名残留。
