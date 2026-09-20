---
name: correct
description: 执行 database-query-helper 的单条 SQL 草案，基于执行和语义反馈进行有限纠错，通过后显式提交最终 SQL。
---
# correct

仅在 data-link 已构建 M-Schema、database-query-helper 已输出一条初始 SQL 后进入本阶段。
本技能只加载一次。不得回到 data-link 重新召回，不得批量生成候选试答案。

1. 原样调用 `sql_db_query(query=初始SQL)` 执行草案。只允许只读 SELECT；不要为了展示增加 LIMIT。
2. 阅读完整工具结果，并核对问题和 Evidence 的投影列、过滤值、公式、连接、粒度和排序要求。
   - 执行报错、空结果、全 NULL、工具明确拒绝或语义过滤失败，都需要诊断。
   - 部分 NULL 或提示性警告并不自动证明 SQL 错误；不得为消除警告擅自改变问题要求。
   - SQL 能执行但不符合问题要求，也必须修正。
3. 只修正已有证据支持的问题：
   - 语法或引号问题：`sql_syntax_fix(sql=..., error_msg=...)`。
   - 表列引用问题：`sql_schema_validate(sql=...)`。
   - 连接问题：`sql_join_validate(sql=...)`。
   - 聚合、过滤、排序、粒度问题：`sql_clause_validate(sql=..., question=问题和Evidence)`。
   - 无法判断错误类别时用 `sql_error_classify(sql=..., error_msg=...)`。
   - 多类错误仍未定位时最多调用一次 `sql_self_correct(sql=..., question=..., max_rounds=3)`。
   按错误选择工具；不要每题机械执行全部诊断。直接复用已确认的表、列、关系。
4. 基于诊断输出修正后的单条 SQL，再调用 `sql_db_query` 验证。不得重复执行完全相同的 SQL。
   初稿之后最多三轮修正；最多四次顶层 `sql_db_query`，内部探测同样受到总工具和调用预算限制。
   不得用反复放宽条件或删除问题要求来强行获取非空结果。
5. 最近一次 SQL 已被接受且语义符合问题时，必须立即调用
   `submit_final_sql(sql=最近一次被接受的SQL原文, reasoning=简短判断理由)`。
   保持引号、LIMIT、DISTINCT、表达式和原文不变；内部修复工具返回的 SQL 必须先经过顶层 `sql_db_query` 接受。
   确认提交成功后简短回答并结束，不再调用其他查询工具。
6. 到达修正预算、工具预算或仍无法得到可接受结果时，说明失败原因并结束。
   此时不要调用 `submit_final_sql`，不要把尚未接受的草案当成最终结果。

`sql_syntax_fix`、`sql_error_classify` 等是真实工具；`correct` 是技能名，不能当作工具调用。
