---
name: sql-error-analysis
description: 对 Agent 生成的 SQL 进行系统化错误检测与修正，覆盖 Schema Link、JOIN、GROUP BY、嵌套、其他子句五类错误
---
# SQL 错误分析与自纠正技能

## 何时使用此技能

本技能是**第二级修正机制**，在 `database-query-helper` 的快速自修正（第一级）失败后介入。

**触发条件**（满足任一即应使用）：

- `sql_db_query` 执行失败，且经过 1 次快速自修正后仍然失败
- 错误信息包含结构性问题关键词：表名/列名不存在（Schema Link）、JOIN 缺失、GROUP BY 不完整等
- 查询结果明显不符合用户问题意图（如空结果集、异常数值），简单修正无法解决
- 复杂多表查询生成后，需要在执行前做预检

**与 database-query-helper 的衔接**：

```
database-query-helper 生成 SQL → sql_db_query 执行
  → 成功 → 回答用户（结束）
  → 失败 → 快速自修正（第一级，修改后再执行 1 次）
      → 成功 → 回答用户（结束）
      → 仍失败 → sql_error_classify 分类 → 进入本技能的三阶段流水线（第二级）
```

**⚠️ 限制**：本技能的诊断修正（含 `sql_self_correct`）最多在一个会话中使用 2 次。超出后应向用户说明问题而非继续重试。

## 方法论基础

本技能基于 SCoT2S（Self-Correcting Text-to-SQL）框架的错误分析方法论。
该研究在 Spider 数据集上对主流 Text-to-SQL 模型的预测错误进行了系统化统计，发现：

| 错误类型                 | 占比             | 说明                                         |
| ------------------------ | ---------------- | -------------------------------------------- |
| **Schema Linking** | **73.30%** | 表名或列名预测错误（不存在于数据库中）       |
| **JOIN 操作**      | **38.93%** | 缺少必要的 JOIN 子句或 JOIN 条件错误         |
| **GROUP BY**       | **20.12%** | 缺少 GROUP BY 或分组字段不正确               |
| **Miscellaneous**  | **14.50%** | WHERE / HAVING / ORDER BY / LIMIT 等子句错误 |
| **Nested**         | **12.53%** | 缺少必要的子查询结构                         |

> 注意：一条错误 SQL 可能同时命中多个类别，因此占比之和超过 100%。

## 核心工作流程（语法修复 + 三阶段流水线纠正）

遵循 SCoT2S 的流水线修正范式：**语法修复 →** Schema Link 纠正 → JOIN 纠正 → 其他子句纠正。
每个模块的输出是修正后的 SQL，同时作为下一个模块的输入。

**语法修复优先**：在进入三阶段流水线前，`sql_self_correct` 会自动先尝试 `sql_syntax_fix` 修复语法问题
（引号、大小写、整数除法等）。如果语法修复已解决问题，流水线提前终止，避免不必要的列替换。

**快速入口**：对于首次失败的 SQL，可以直接调用 `sql_self_correct(sql, question)` 执行完整流水线（含语法修复前置）。
如果需要更精细的控制，按以下步骤操作：先调用 `sql_syntax_fix(sql, error_msg)` 尝试语法修复，
若未解决则按三阶段逐步操作。

### 第 1 阶段：Schema Link 纠正（最高优先级）

**目标**：确保 SQL 中引用的所有表名和列名在数据库中真实存在，且列与表的归属关系正确。

**操作步骤**：

1. 调用 `sql_error_classify(sql, error_msg)` 确认错误是否属于 Schema Linking 类别
2. 调用 `sql_schema_validate(sql)` 批量验证所有表名/列名
3. 根据返回的替代建议修正 SQL 中的表名/列名引用：
   - 若表名不存在 → 使用建议的最接近表名替代
   - 若列名不存在 → 使用建议的最接近列名替代
   - 若列属于另一个表 → 修正表引用或调整 FROM/JOIN 子句
4. 用 `sql_db_query` 试执行修正后的 SQL

**辅助工具**：

- `sql_db_list_tables` — 获取所有可用表名
- `sql_db_schema` — 获取表的列名、类型、示例值
- `sql_db_value_lookup` — 语义检索，定位正确的表名/列名替代项

**输出**：Schema 引用正确的 SQL 查询 S'

### 第 2 阶段：JOIN 操作纠正

**目标**：确保所有跨表查询都有正确的 JOIN 条件，且 JOIN 条件符合外键关系。

**操作步骤**：

1. 调用 `sql_join_validate(sql)` 诊断 JOIN 完整性
2. 根据诊断结果修正 SQL：
   - 若存在笛卡尔积风险 → 根据外键关系补充 JOIN 条件
   - 若 JOIN 条件与外键不匹配 → 修正 JOIN ON 条件
   - 若有缺失的 JOIN → 按诊断建议添加 JOIN 子句
3. 用 `sql_db_query` 试执行修正后的 SQL

**辅助工具**：

- `sql_db_table_relationship` — 获取外键关系，验证和补全 JOIN 条件

**输出**：JOIN 关系正确的 SQL 查询 S''

### 第 3 阶段：其他子句纠正

**目标**：修正 WHERE、GROUP BY、HAVING、ORDER BY、LIMIT 等子句的逻辑错误。

**操作步骤**：

1. 调用 `sql_clause_validate(sql, question)` 基于用户问题语义检查子句完整性
2. 根据诊断结果修正 SQL：
   - **GROUP BY 完整性** — 若 SELECT 含聚合函数，所有非聚合列须在 GROUP BY 中
   - **HAVING vs WHERE** — 聚合结果过滤条件放 HAVING，原始数据过滤条件放 WHERE
   - **嵌套子查询** — 若问题含"最高/最低"极值语义 → 检查是否需要 `WHERE col = (SELECT MAX/MIN...)`
   - **ORDER BY + LIMIT** — 若问题含"前 N 个/Top N"语义 → 确保有 ORDER BY + LIMIT
   - **DISTINCT** — 若 JOIN 可能产生重复行且问题期望唯一结果 → 添加 DISTINCT
   - **整数除法（SQLite）** — 若 `INTEGER / INTEGER` 在 SQLite 中执行，结果会截断为整数 → 使用 `CAST(numerator AS REAL) / denominator`
3. 用 `sql_db_query` 试执行修正后的 SQL

**输出**：完整修正后的 SQL 查询 S'''

### 补充检查：计算与类型错误

除了 SCoT2S 的五类结构性错误，`sql_clause_validate` 还会自动检测以下语义错误：

**整数除法截断（SQLite 特有）：**

- 当 SQL 包含 `INTEGER / INTEGER` 除法且数据库为 SQLite 时，结果会截断为整数（如 3/5=0）
- `sql_clause_validate` 会自动检测此问题并报告为"整数除法"类别的错误
- 修正方法：`CAST(numerator AS REAL) / denominator`

**可疑结果模式（执行后自动检测）：**

- 比率/百分比计算结果全为 0 → 高度可疑的整数除法截断
- 所有行返回相同计算值 → 可能的公式错误
- 比率超出合理范围（如 >1 的百分比）→ 分子分母可能颠倒

## 可用工具

本技能使用以下工具完成错误诊断与修正：

### 语法修复工具（前置步骤）

- `sql_syntax_fix(sql, error_msg)` — 仅修复语法问题（标识符引用、大小写、整数除法），**绝不替换列名**。应在三阶段流水线前优先使用。

### 诊断工具（SCoT2S 专用）

- `sql_error_classify(sql, error_msg)` — 对执行失败的 SQL 进行错误分类，判定属于五类错误中的哪一类（或多类），返回分类结果及修复方向建议
- `sql_schema_validate(sql)` — 批量验证 SQL 中所有表名和列名的存在性，返回不匹配项及最接近的候选替代项
- `sql_join_validate(sql)` — 检查 SQL 中 JOIN 的完整性，诊断缺失或错误的 JOIN 条件，返回建议的 JOIN 补全语句
- `sql_clause_validate(sql, question)` — 基于用户问题语义，检查 GROUP BY / HAVING / ORDER BY / LIMIT / 嵌套子查询等子句的完整性
- `sql_diff_report(original_sql, corrected_sql)` — 生成原始 SQL 与修正后 SQL 的逐子句差异对比报告
- `sql_self_correct(sql, question, max_rounds=3)` — 三阶段流水线主编排函数，自动执行 Schema Link → JOIN → 子句的完整诊断流程

### 基础工具（复用现有）

- `sql_db_list_tables` — 获取所有可用表名，用于 Schema Link 阶段验证表名存在性
- `sql_db_schema` — 获取表的列名、类型、示例值，用于验证列名存在性和归属关系
- `sql_db_table_relationship` — 获取外键关系，用于 JOIN 阶段验证和补全 JOIN 条件
- `sql_db_value_lookup` — 语义检索，用于定位正确的表名/列名替代项
- `sql_db_query` — 执行修正后的 SQL 验证正确性
- `sql_db_query_checker` — 执行前的 SQL 语法预检

## 示例

### 示例 1：Schema Link 错误修正

**用户问题**：Find the name of the student with the highest GPA.

**初始 SQL**（错误）：

```sql
SELECT student_name FROM students
WHERE grade_point_average = (SELECT MAX(grade_point_average) FROM students)
```

**诊断流程**：

1. `sql_error_classify(sql, "no such column: student_name")` → Schema Linking 错误
2. `sql_schema_validate(sql)` → `student_name` 不存在，建议 `name`；`grade_point_average` 不存在，建议 `gpa`
3. 修正 SQL

**修正后 SQL**：

```sql
SELECT name FROM students
WHERE gpa = (SELECT MAX(gpa) FROM students)
```

### 示例 2：JOIN 缺失修正

**用户问题**：List the names of all teachers who teach courses in the Science department.

**初始 SQL**（错误）：

```sql
SELECT teachers.name FROM teachers
WHERE teachers.department = 'Science'
```

**诊断流程**：

1. `sql_join_validate(sql)` → 问题涉及 courses 表但 SQL 未引用
2. 根据外键关系 `courses.teacher_id = teachers.id` 补充 JOIN
3. `sql_schema_validate(sql)` → `teachers.department` 不存在，应为 `courses.department`

**修正后 SQL**：

```sql
SELECT DISTINCT teachers.name FROM teachers
JOIN courses ON teachers.id = courses.teacher_id
WHERE courses.department = 'Science'
```

### 示例 3：GROUP BY + HAVING 修正

**用户问题**：What is the number of countries with more than 2 car makers?

**初始 SQL**（错误）：

```sql
SELECT COUNT(*) FROM car_makers
GROUP BY car_makers.Id
HAVING COUNT(*) > 2
```

**诊断流程**：

1. `sql_clause_validate(sql, question)` → GROUP BY 粒度错误，应按 country 分组而非 Id
2. 修正 GROUP BY 字段
3. `sql_join_validate(sql)` → 需要 JOIN countries 表

**修正后 SQL**：

```sql
SELECT COUNT(distinct car_makers.country) FROM car_makers
GROUP BY car_makers.country
HAVING COUNT(*) > 2
```

### 示例 4：使用 sql_self_correct 一键修正

**用户问题**：哪些学校的免费午餐学生比例超过50%？

**操作**：直接调用 `sql_self_correct(failed_sql, "哪些学校的免费午餐学生比例超过50%")`

- 第 1 阶段自动验证表名/列名，发现 `school_name` 应为 `School Name`，自动替换
- 第 2 阶段验证 JOIN 完整性，通过
- 第 3 阶段验证子句，发现缺少 LIMIT，建议补充
- 返回完整诊断报告 + 修正后 SQL

### 示例 5：SQLite 整数除法修正

**用户问题**：Please list the phone numbers of the schools with the top 3 SAT excellence rate. Excellence rate = NumGE1500 / NumTstTakr

**初始 SQL**（错误）：

```sql
SELECT NumGE1500 / NumTstTakr AS ExcellenceRate, Phone
FROM satscores JOIN schools ON satscores.cds = schools.CDSCode
ORDER BY ExcellenceRate DESC LIMIT 3
```

**结果**：ExcellenceRate 全为 0 — 整数除法截断

**诊断**：`sql_clause_validate` 检测到 `NumGE1500 / NumTstTakr` 两个 INTEGER 列在 SQLite 中做整数除法

**修正后 SQL**：

```sql
SELECT CAST(NumGE1500 AS REAL) / NumTstTakr AS ExcellenceRate, Phone
FROM satscores JOIN schools ON satscores.cds = schools.CDSCode
ORDER BY ExcellenceRate DESC LIMIT 3
```

## 错误修正优先级

修正应按照错误占比从高到低的优先级执行：

1. **Schema Link**（73.30%）— 最先修正，因为后续所有修正都依赖正确的表/列引用
2. **JOIN**（38.93%）— 第二优先级，影响查询结果的正确性
3. **GROUP BY**（20.12%）— 影响聚合结果
4. **Miscellaneous**（14.50%）— WHERE/HAVING/ORDER BY/LIMIT 子句
5. **Nested**（12.53%）— 子查询结构

## 质量指南

- 每次修正后必须用 `sql_db_query` 试执行验证
- 若修正后仍失败，分析新的错误信息进入下一轮修正（最多 3 轮）
- 修正应最小化改动——只修改诊断为错误的部分，保留正确的结构
- 修正完成后调用 `sql_diff_report` 生成对比报告，说明每处修改的原因
- 不要过度修正：如果原始 SQL 在某个子句上是正确的，不要为了"优化"而改变它
