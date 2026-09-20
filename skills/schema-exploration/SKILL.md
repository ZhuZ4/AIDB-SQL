---
name: schema-exploration
description: 在 data-link 召回不明确时检查候选表、完整列名和外键关系，帮助选择相关列；不执行 SQL 或整库加入链接架构。
---
# schema-exploration

本技能仅协助当前 data-link 召回，不启动独立生成或纠错流程。

1. 优先使用已有召回候选和工具结果。尚不清楚候选表时才调用一次 `sql_db_list_tables()`。
2. 调用 `sql_db_schema(table_names="候选表1,候选表2")` 检查必要候选的列名、类型、描述和样例。
   保留完整标识符，不拆开含空格的列名，不翻译真实列名。
3. 需要确认连接时调用 `sql_db_table_relationship(table_names="候选表1,候选表2")`；复用已知主外键路径。
4. 将确认的完整业务短语交回 data-link，由它继续单短语检索、选择必要业务列及 join key，
   再 `add_schema` 和 `build_linked_mschema`。不要将 schema 工具返回的全部列加入链接架构。

禁止调用 `sql_db_query`、SQL 纠错工具和 `submit_final_sql`。不得重复获取已经确认的表或关系。
SQLite 无需 db_search；PostgreSQL 的目标 Schema 必须在开始 data-link 之前已由 db_search 定位。
