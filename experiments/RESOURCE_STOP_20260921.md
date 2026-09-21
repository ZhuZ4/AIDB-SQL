# API 额度耗尽时的交付与恢复记录

2026-09-21 08:13:55（北京时间），SQL Agent 的服务商返回一周 token-plan
额度耗尽，监督进程保存断点后退出。这符合用户授权的停止条件；研究计划尚未全部完成。
没有继续派发题目、自动充值或切换账号。服务返回的重置时间为
`09-27 18:33:00 UTC`，按当前年份换算为 **2026-09-28 02:33 北京时间**。
这不是普通的瞬时 429 限流；判断依据是服务明确返回的 quota exhausted 文本。

## 已得到的结果

| 项目 | 已验证状态 |
|---|---|
| 数据与评分校准 | 固定 300 题及 30 题 smoke；gold 隔离；300/300 gold 自检通过、300 个错误预测均被拒绝、缺失预测计零 |
| 检索 | 11 库、75 表、798 列；1497 个列名/说明独立向量；完整双向量 RRF。BM25 尚未实现，也未宣称 BM25 收益 |
| 批量工程 | 独立进程/会话、只读 SQL、原始最终提交、共享题目预算、断点恢复、实际 Python PID 校验、额度停机与工件审计已落地 |
| 第一轮 B0 | `B0_20260921_01` 完整 300 条，官方 EX **180/300（60%）**；290 条真实可执行提交、10 条无提交，均保留；原工程审计通过，停机复查发现一处 usage 完整性标记问题，见下文 |
| 第二轮 B0 | `B0_20260921_02`：281 条终态（275 succeeded、6 语义失败），19 条 pending；283 次 attempt、2693 次实际模型调用；没有完整官方评分 |
| C1 | 输出字段与仅供筛选/排序/连接等用途的字段分开标注；基于错误证据及 DIN-SQL/RESDSQL 原文研究实现。49 项离线检查通过，真实工具路由也已离线核对；尚未发送候选模型请求 |
| 当前最佳 | 仍为 B0 `5a82da5ad0e2ad301f33bb8f5afdbdba46030fa4`，以第一轮完整结果交付。主开发分支的较新提交不是性能最佳版本 |

第一轮成绩是单次完整结果。第二轮未完成，C1 未评测，因此没有重复均值、提升幅度、
显著性或采用结论。剩余 200 题未用于挑选方法。C2 仅有研究备忘，未实施。

## 可直接查看的本地交付

所有完整数据留在项目忽略目录，Git 只发布代码、无密钥配置、ID 清单和轻量摘要。
下列路径相对于 `F:/data/VSCodeproject/AIDB-SQL`：

- `.local-services/experiments/deliverables/resource_stop_20260921/B0_20260921_01_question_sql_scores.jsonl`：
  300 条 question/evidence → 原始 submitted_final_sql → 官方 EX；含全部 10 条失败。
  B0 与当前最佳是同一个冻结版本、同一份运行结果，没有逐题拼接。
- 同目录 `B0_20260921_02_partial_question_sql.jsonl`：第二轮的 281 条终态，EX 为 null，明确标记未评分。
- 同目录 `B0_20260921_02_pending.jsonl`：剩余 19 题的 ID、数据库与 pending 状态。
- 同目录 `manifest.json`：各导出哈希及来源哈希；导出保留原始提交字节内容，未改动原结果/评分，未包含 gold SQL。
- `.local-services/experiments/runs/B0_20260921_01/`：完整预测、官方逐题评分、工程审计、原始轨迹与开发集错误分析。
- `.local-services/experiments/runs/B0_20260921_02/partial_stop_audit.json`：额度停止时状态、预测、attempt、预算与 usage 的一致性核对。
- `.local-services/experiments/quota_resume_verification.json`：冻结工作树、运行指纹、registry 和恢复命令的只读核对。
- `.local-services/experiments/C1_projection_roles_preflight.json`：候选隔离、配置单变量、数据/索引/runtime 一致性及真实工具路由核对。

第一轮已知 token 小计 51,854,931，第二轮的已知小计 48,142,067。
两轮分别有 1 次和 2 次请求未返回 usage，完整 token 总量保持 unknown/null；
实际费用因缺少匹配单价/账单也保持 unknown/null。缓存 token 已单独记录，不能把已知小计当完整用量。

停机审计发现冻结导出器没有将前次 attempt 的嵌套 `usage_complete=false` 传播到累计
`usage_unknown`。B02 q1209 因而把已知 token 小计写成完整数值；B01 q960 也有标记错误，
但其完整 token 字段已为 null，Git 摘要仍正确。新审计会报告这项差异，不能继续将旧审计
“通过”解释为所有账目字段正确。原预测、官方分数和已登记哈希保留；SQL/EX 不受影响。
主线已修复累计逻辑与审计，另提供只读生成账目副本及评分前发布工具。

## 冻结版本和续跑顺序

基线工作树 `F:/data/VSCodeproject/AIDB-SQL-B0` 固定为 `5a82da5`。
候选工作树 `F:/data/VSCodeproject/AIDB-SQL-projection-roles` 固定为
`f60c2869bb6f35edecd155cc6163e9081933c6c9`，分支 `dev_20260921_060334` 已推送。
候选保持 pending；先前推送证据和本地远端跟踪引用已保存。
两者共享主项目的本地服务目录与配置，不能复制密钥进 Git。

额度可用、任务恢复后，先从冻结 B0 工作树接续原 run_id：

```powershell
Push-Location 'F:\data\VSCodeproject\AIDB-SQL-B0'
& 'F:\data\VSCodeproject\AIDB-SQL\.venv\Scripts\python.exe' -m experiments.run_batch --run-id B0_20260921_02
Pop-Location
```

q93 的额度失败 attempt1 已保留，状态为 pending；attempt2 剩余 39 次调用和约
891.593 秒，不能清零预算。已完成的 281 题复用，六条语义失败不重新抽样。
此入口只完成生成。因为旧导出器的账目问题，不要让冻结 supervisor 直接评分；
也不要用它重跑已完成的 B01。已登记的第一轮预测/评分哈希保持不变。

B02 全部 300 题终态后，从主开发工作树依次执行（任一步失败即先检查原因）：

```powershell
Set-Location 'F:\data\VSCodeproject\AIDB-SQL'
& '.\.venv\Scripts\python.exe' -m experiments.repair_usage_export --run-dir '.local-services/experiments/runs/B0_20260921_02' --publish-before-evaluation
& '.\.venv\Scripts\python.exe' -m experiments.audit_run --run-dir '.local-services/experiments/runs/B0_20260921_02'
& '.\.venv\Scripts\python.exe' -m experiments.supervisor --run-id B0_20260921_02
```

账目工具保留原始导出与哈希，仅允许 token 完整量/已知小计及 unknown 标记变化，
拒绝改动题目、提交 SQL、调用数和时长。默认不加 publish 开关时只生成独立副本。
部分运行、有活跃 worker、已有评分或正式审计的运行均不能发布替换。
主线 supervisor 在完整终态时只继续评分，不调用主线生成器，因此不会混入另一版 SQL。

之后按顺序进行：

1. 完成上述账目修复、审计和评分后，登记两轮按时间排序的 B0；不把 281 条部分结果注册为完整实验。
2. 在冻结 C1 工作树运行 `smoke_C1_projection_roles_20260921_01`（使用
   `experiments/projection_roles.example.json` 和 `--subset smoke`）；先通过工程检查。
   C1 的冻结代码也使用旧导出器，因此各次运行同样用冻结 `run_batch` 生成，再由主线修复账目、
   审计、评分（smoke 的审计与评分加 `--subset smoke`）。保持冻结生成版本不变。
3. 同一候选版本运行 `C1_projection_roles_20260921_01` 与 `C1_projection_roles_20260921_02`。
4. 完成两组按时间配对的 300 题比较，报告新增正确/回退、分组、调用量、延迟与配对不确定性，按冻结规则采用或拒绝。
5. 保留结果并依据新证据继续研究。没有达到 +2 个百分点即停止的规则，也不能预先保证候选有效。

本次没有安排额度轮询、到期自动重启或充值。上述命令仅供资源恢复后的继续执行。
