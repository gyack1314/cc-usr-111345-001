# 相关子查询去关联设计（受限关系 AST）

## 1. 问题与目标

分析师按租户反复查询“是否有失败请求”（EXISTS）与“有没有匹配的结算记录”
（IN / NOT IN / 标量聚合）。Python 查询层逐外行执行子查询，高峰期同一张
明细表被全扫几千遍。直接改成普通连接会引入两个错误：

1. **连接扇出**：内表业务键重复时，一个外行被复制成多行，账本行数改变；
2. **空键与 NULL**：普通等值连接把 NULL=NULL 当成可处理的匹配，
   且 NOT IN 在候选含 NULL 时的 UNKNOWN 语义会被 anti join 破坏。

目标：把可证明等价的相关子查询改写为**一次关系运算**（哈希半/反/mark/
聚合/标量连接），在不改变结果袋（含行多重集与 UNKNOWN）的前提下，
让内表从“每个外行全扫一遍”变成“每个查询只构建一次”。

## 2. 三层边界

```
程序化构造的受限 AST
        │  ast.validate_surface   表面校验（不解析任何 SQL 文本）
        ▼
绑定 AST（Col.ref 不透明键）
        │  binder.Binder          规范化：作用域、别名遮蔽、关联深度
        ▼
重写判定（rewrite.Rewriter）
        │  等价条件检查 + R0..R7 规则，每条带适用依据
        │  不满足条件 -> UnsupportedError（拒绝或显式回退，绝不近似）
        ▼
物理计划（physical.Px* 不可变节点）
        │  physical.run           一次构建、逐行探测、惰性、缓存
        ▼
结果袋 + 扫描统计 + 构建/缓存记录
```

- **规范化层**只回答“名字指谁、结构是否受限”，不接触行数据；
- **等价条件检查层**只做代数判定并产出 `Eligibility(rule, basis, node_id)`，
  不决定执行细节；
- **物理执行层**只认 tuple 形式的列键与合成标记列，不解释用户别名。

## 3. 受限表面

输入只能程序化构造（`subquery_data/rel/ast.py`），没有 SQL 解析入口：

- 投影与 WHERE 中的 `EXISTS`、`NOT EXISTS`、标量子查询；单列 `IN`/`NOT IN`；
- 标量子查询分两类：裸表达式（零行/一行/多行报错）与单聚合
  （COUNT/SUM/MIN，可带关联分组）；
- 子查询内允许：AND 下的等值关联、只引用内层列的局部过滤、
  COUNT(*)/COUNT(expr)/SUM/MIN；
- 关联列必须是明确作用域内的 `Col(alias, column, level=...)`；
- 没有非确定函数节点（无 now/random/udf），同快照同参数结果确定；
- 外行有独立内部行标识 `$rid`，业务键可重复；空键不与空键相等。

显式拒绝：OR 内关联、非等值关联（>、<、<> 跨块）、跨层（深度>1）捕获、
派生表关联外层、投影引用外层列、用户别名占用 `$` 内部前缀。

## 4. 三值逻辑与空集规则（baseline.py 为语义基准）

真值用 `True/False/None`，None 在布尔语境即 UNKNOWN。

| 情形 | 规则 |
|---|---|
| WHERE | 只保留 TRUE；FALSE 与 UNKNOWN 一律剔除 |
| 投影 | UNKNOWN 原样保留为 NULL，不折叠成 FALSE |
| IN | 任一候选真匹配=>TRUE；否则候选含 NULL=>UNKNOWN；再否则 FALSE |
| NOT IN | 对 IN 结果取三值反；**禁止**化简成 anti join |
| EXISTS/NOT EXISTS | 只产生确定布尔，不产生 UNKNOWN |
| COUNT | 空输入（含无匹配组）=> 0 |
| SUM/MIN | 空输入或全 NULL => None |
| 裸标量 | 零行=>None，一行=>该值，多行=>`CardinalityError`（绝不取第一条）|
| 关联键含 NULL | 进入空键桶，任何探测都不命中（NULL=NULL 不是 TRUE）|

外层字段自身为 NULL 与“子查询没有返回行”在输出上都可能是 NULL，
但来源不同：前者是行值，后者是连接无匹配；物理算子分别由
“探测键为 NULL”和“桶中无该键”两条路径产生，结果袋仍与基准一致。

## 5. 重写规则与适用依据

| 规则 | 形态 | 物理算子 | 适用依据 |
|---|---|---|---|
| R0 | 无关联整表聚合 | PxAggGlobal | 单次扫描全局归约 |
| R1 | 顶层合取 EXISTS | PxHashSemiJoin | 存在性判定，键相等半连接与逐行 EXISTS 袋等价，不扇出 |
| R2 | 顶层合取 NOT EXISTS | PxHashAntiJoin | NOT EXISTS 无 UNKNOWN，反连接安全 |
| R3 | 投影/OR 内 EXISTS | PxHashMarkJoin(exists) | 需要标记列承载布尔，保留三态语境 |
| R4 | IN | PxHashMarkJoin(in) | mark 完整产出 TRUE/FALSE/UNKNOWN |
| R5 | NOT IN | R4 标记三值取反 | 候选含 NULL 时必须保留 UNKNOWN，anti join 会错 |
| R6 | 标量聚合 | PxAggJoin | 右侧先按键分组聚合再左探测，外行 1:1；无匹配按 COUNT=0、SUM/MIN=NULL |
| R7 | 裸标量 | PxScalarJoin | 按键分桶：零行 NULL、一行取值、多行 CardinalityError |

通用前提：关联谓词必须是 AND 下、一侧外层列一侧内层列的等值比较；
其余谓词只引用内层列才算局部过滤，可下推构建侧；否则拒绝。

**为什么账本行数不变**：所有连接算子都是“左侧逐行、右侧按键查字典”，
右表重复行只在构建端进入同一个桶并被聚合/存在性判定吸收，
绝不回贴到外行上。PxAggJoin/PxScalarJoin 对每个外行恰好输出一次。

## 6. 物理执行、扫描统计与缓存

- **惰性构建**：左侧零行时不物化右侧，与基准“没有外行就不执行子查询”一致；
- **构建端**：扫描一次内表，按非空关联键分桶（NULL 键入空桶不参与匹配），
  局部过滤在构建侧应用；
- **统计**：`table_scans`/`rows_scanned` 记录每张表被扫次数与行数，
  `builds` 记录每个连接的 node_id、算子类型、构建键、构建行数、桶数；
- **缓存键**：`(plan_id, node_id, snapshot_token, params_fingerprint)`。
  plan_id 按查询结构指纹稳定分配，同查询重复执行可命中、不同查询不会撞键；
  快照令牌含表身份、行数、列表身份与末行 id，参数指纹含全部参数值，
  任一变化必然换键，旧结果不可能误用。

## 7. 基准解释器与等价对照

`baseline.py` 是独立的逐外行嵌套循环解释器，不共享重写器的任何连接逻辑，
作为小资料对照口径。`Engine.assert_equivalent` 比较：

1. **错误类别**：CardinalityError / UnsupportedError / ScopeError 等必须一致；
2. **结果袋**：`Counter(bag_key(row))` 相等，行多重集（含重复外行）一致。

测试证据：
- `tests/test_decorrelation.py`：重复外行、空内表、内表 NULL、
  裸标量多行、两层别名遮蔽、扫描次数、构建键、缓存失效、拒绝/回退；
- `tests/test_fuzz_equivalence.py`：300 个固定种子随机查询×随机数据，
  全部结果袋与错误类别等价，且物理路径内表扫描次数恒 ≤1。

## 8. 不支持结构的处置

`Engine.run(query, mode=...)`：

- `reject`（默认）：`UnsupportedError` 以错误类别返回，不产生近似行；
- `fallback`：显式回退基准解释器，结果标注 `route='baseline-fallback'`
  与 `fallback_reason`，调用方明确知道这次没有去关联。

不存在“静默换一种近似算法”的路径。
