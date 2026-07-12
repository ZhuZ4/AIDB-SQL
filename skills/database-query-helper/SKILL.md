---
name: database-query-helper
description: 基于用户问题和当前 M-Schema 生成单条初始 SQL 草案。用于 data-link 已完成、M-Schema 已就绪后的 SQL 生成阶段；负责选择合适的 SQL 形状、筛选必要列和连接路径，但不执行 SQL、不调用纠错工具。
---
# database-query-helper

基于问题和 M-Schema 生成一条初始 SQL 草案。

## 输入前提

- `data-link` 已完成
- 当前上下文里已经有 M-Schema
- 你现在的任务是“写初始 SQL”，不是执行和修正 SQL

## 默认流程

1. 阅读问题和 Evidence
2. 阅读 M-Schema，识别所需表、列、join key、过滤条件和粒度
3. 选择最合适的 SQL 形状
4. 输出一条 fenced SQL 草案
5. 用一到两句话说明你选择了哪种 SQL 形状

## SQL 形状选择

- 计数问题：`COUNT(*)` 或 `COUNT(DISTINCT ...)`
- Top N / 极值：`ORDER BY ... LIMIT N`
- 比率 / 百分比：`CAST(numerator AS REAL) / denominator`
- 已有预聚合列：直接使用该列，不再套 `AVG` / `SUM` / `MAX` / `MIN`
- 极值定位：必要时使用子查询
- 多表聚合：先明确粒度，再决定 `GROUP BY`

## SELECT 投影精度

- 上游 `data-link` 在转交时会附带一段 fenced ``json`` 关键词提取结果，其中
  `查询目标` 字段是本题应当投影的字段**白名单**。先读这段 JSON，再写 SELECT。
- SELECT 必须严格按照 `查询目标` 投影，不得为可读性、排查方便或上下文展示
  而追加额外列（如 `name` / `id` / `address` / `school name` 等）。
- 比率、百分比、Evidence 公式视作**单一查询目标字段**——即使分子分母涉及多列，
  SELECT 也只输出公式计算结果这一列，不要把分子或分母再原样投影一份。
- 排序字段如果**不在** `查询目标` 内（仅用于 ORDER BY），通过 `ORDER BY <表达式>`
  排序即可，不要为了排序而把它额外投影到 SELECT 列表。
- 分组字段如果**不在** `查询目标` 内（题目只问聚合值，不要求按组展示），保持纯
  聚合输出，不要追加分组列到 SELECT。
- 例外：当 `查询目标` 为空数组（提取失败或问题不属于数据查询场景），回退到下面
  "只使用回答问题真正需要的列"的判断。

## 编写规则

- 只生成一条 SQL
- 你负责选择表、列、join 和粒度，但不负责判断结果是否正确，也不负责处理执行失败
- 只使用回答问题真正需要的列
- M-Schema 是候选集，不要求把所有召回列都用上
- 只添加问题或 Evidence 明确要求的筛选条件
- 不使用 `SELECT *`
- PostgreSQL 中包含大写、中文或特殊字符的标识符要用双引号
- SQLite 中涉及整数除法必须显式转浮点
- 仅当问题本身明确要求 Top-N / 极值时，才使用 `LIMIT`；不要为了控制展示量添加 `LIMIT`
- `DISTINCT` 使用规则：
  - 先明确本题的答案粒度：最终每一行代表什么对象或对象组合
  - 默认不使用 `DISTINCT`
  - 只有以下情况之一成立时，才允许使用 `DISTINCT`：
    - 问题明确要求去重、唯一值、不同值
    - 问题本身需要 `COUNT(DISTINCT ...)`
    - 你能明确证明当前 join 或子查询会在最终答案粒度上产生完全重复的结果行，而题目要的是该粒度的唯一集合
  - 以下情况禁止使用 `DISTINCT`：
    - 仅因为出现 `JOIN`
    - 仅为了让结果更干净
    - 仅因为怀疑主表中可能有重名或重复值
    - 仅为了掩盖错误的 join、错误的投影列或错误的结果粒度
  - 如果问题只是要求列出名称、电话、地址、学校等结果，而没有明确唯一性要求，则保持原始结果粒度，不主动去重
  - 输出 SQL 前先自检：如果去掉 `DISTINCT` 只是返回更多同粒度结果，而不是造成语义错误，则不要使用 `DISTINCT`
- SQLite 整数除法陷阱：
  - `INTEGER / INTEGER` 会截断为整数
- 涉及比率、百分比、比例时，必须显式转为浮点数
- 预聚合列检测：
  - 列名以 Avg / Max / Min / Total / Sum / Count / Num / Percent / Pct / Rate 等前缀开头时，优先视为已聚合列
  - 不要再对这些列套同类聚合函数
- 只在连接列有索引/主键时才允许相关子查询 EXISTS；否则改写为 JOIN + DISTINCT，并先检查 EXPLAIN QUERY PLAN

## 输出格式

先给一句简短说明，再输出：

```sql
SELECT ...
```

说明中要明确当前 SQL 属于哪种形状，例如 count、top-N、ratio、subquery、grouped aggregate。

## 禁止事项

- 不要调用 `sql_db_query`
- 不要调用 `sql_db_query_checker`
- 不要使用候选 SQL 批量试探工具
- 不要调用 `sql_syntax_fix`
- 不要调用 `sql_error_classify`
- 不要调用 `sql_schema_validate`
- 不要调用 `sql_join_validate`
- 不要调用 `sql_clause_validate`
- 不要调用 `sql_self_correct`

## 交接给下游

把这条初始 SQL 交给 `correct`。只有 `correct` 可以执行和修正它。
