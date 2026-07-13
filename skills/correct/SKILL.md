---
name: correct
description: 执行并修正 database-query-helper 生成的初始 SQL。用于 Text-to-SQL 默认流水线的最终执行阶段；先运行 sql_db_query，再按语法失败或语义失败分支修正并重新执行，直到结果可接受或达到重试上限，且不得回跳到召回阶段。
---
# correct

执行初始 SQL，并把“能跑但不对”的结果挡下来。

## 输入前提

- 你已经拿到用户问题
- 你已经拿到一条初始 SQL
- 不要回到 `data-link`

## 默认流程

1. 先调用 `sql_db_query` 执行初始 SQL
2. 如果是明显的语法、引用、引号、大小写或整数除法写法问题，优先调用 `sql_syntax_fix`
3. `sql_syntax_fix` 修复后重新执行
4. 如果是其他执行失败，先调用 `sql_error_classify`，再按分类进入对应校验工具
5. 如果 SQL 可执行但结果不符合用户意图，也进入语义修正
6. 每次修正后都要重新调用 `sql_db_query`
7. 只有结果通过过滤，才能回答用户

## 必须拦截的结果

以下任何一种都不能视为成功：

- 返回 0 行
- 所有结果值均为 NULL / None
- ORDER BY NULL 陷阱导致目标列全为 NULL
- 目标数值列全为 0
- 比率或百分比结果明显超出合理范围
- 目标聚合列在多行结果中全部相同，疑似分组粒度错误
- `sql_db_query` 明确给出语义风险警告

## 修正顺序

### 1. 语法修复

- 语法类错误优先使用 `sql_syntax_fix`
- 它只能修正引用方式、引号、大小写、整数除法写法
- 语法修复后必须重新调用 `sql_db_query`

### 2. 语义修正

当遇到非语法执行失败，或 SQL 能执行但结果应被拒绝时：

1. 调用 `sql_error_classify`
2. 根据分类选择：
   - schema 问题：`sql_schema_validate`
   - join 问题：`sql_join_validate`
   - clause / grouping / ratio 问题：`sql_clause_validate`
3. 仅在复杂多类问题时使用 `sql_self_correct`
4. 每次修正后都要重新调用 `sql_db_query`
5. 纠错时不要为了“让结果更干净”自行添加 `DISTINCT`、`IS NOT NULL`、`ORDER BY`、额外展示列或额外维表 join，除非问题或 Evidence 明确要求
6. 若怀疑结果中有重复行，先检查答案粒度、join 粒度、投影列和过滤条件；不要把 `DISTINCT` 当作默认修补手段
7. 只有在问题明确要求唯一结果、需要 `COUNT(DISTINCT ...)`，或你能明确证明当前 join / 子查询在最终答案粒度上制造了完全重复的结果行时，才允许补 `DISTINCT`
8. 如果多个列都能表示同一业务字段，优先保留 Evidence 或当前筛选条件已经命中的业务表中的列，不要只为拿更好看的名称而换表

## 上限

- `sql_db_query` 总执行次数不超过 6 次
- `sql_self_correct` 每会话最多 2 次
- 超限仍失败时，直接向用户说明问题

## 输出要求

- 通过过滤后，**必须** 调用 `submit_final_sql(sql=..., reasoning="...")` 提交最终 SQL，再向用户作答
- 提交的 SQL 必须是最近一次 `sql_db_query` 接受（accepted / accepted_with_warning）的 SQL，不要再额外加 `LIMIT`、`DISTINCT` 或多余的展示列
- 如果无法修好，**不要** 调用 `submit_final_sql`，直接清楚说明失败原因
- 不要把空结果、全 NULL、全 0 或语义可疑的结果当作最终答案

## 禁止事项

- 不要回跳到 `data-link`
- 不要重新启动召回
- 不要生成多候选 SQL 批量试探
