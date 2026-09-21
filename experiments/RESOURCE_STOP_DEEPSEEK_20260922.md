# DeepSeek 官方接口余额停止记录

2026-09-22 **04:34:31（Asia/Shanghai）**，`DSF_20260922_B0_02` 的官方接口返回
`402 / Insufficient Balance`。监督进程停止派发并退出，实际进程身份及全局 state
均确认没有活跃 worker。这是服务明确报告的余额不足，未按普通限流处理；没有自动
充值、换账号、探测余额或安排自动重启。研究计划尚未完成。

## 已保存的结果

| 项目 | 结果与边界 |
|---|---|
| 完整首轮 | `DSF_20260922_B0_01`，官方 EX **184/300（61.33%）**；292 条可执行提交、8 条无提交，评分无超时。完整工程审计及 31 项独立复核通过，300 行交付已导出 |
| 第二轮 | `DSF_20260922_B0_02`，**66 条终态：64 成功、2 语义失败；234 条 pending**。66 个 attempt/session，全部已开始的题均终结；没有正式评分或完整 300 题工程通过结论 |
| 停止触发点 | q1238 attempt1 第 9 次模型请求余额不足；此前原始 `submit_final_sql` 已成功，因此该题合法保留 succeeded 和原 SQL，不能重跑 |
| 进度显示差异 | `progress.json` 为 65，源于停止分支在保存第 66 条结果后直接返回；state、predictions 和逐题工件一致为 66。原进度文件保留，未伪造写回 |
| 接续边界 | 234 条 pending 全部 attempt0，从 q1241 开始。已完成的 66 条及两条语义失败均复用 |
| 版本合同 | 两轮同一 `58de3cb344fcc2337ea01a168060065a8dd2cc7c`、`deepseek-flash`、官方 endpoint、数据/索引/预算；运行指纹 `ed6e7d98e630bb6b140d5d5561e5264b53849e15a1e0b3a82eb4d609a8ce172b` |
| 方法状态 | 新系列尚未登记完整基线对、选择或实施候选。没有配对重复均值、候选收益或采用结论；旧 Token Plan 系列保留且不混合 |

全量索引仍为 11 库、75 表、798 列、1497 个列名/说明向量，SQLite 是业务数据
来源。当前基线为双向量 RRF；BM25 尚未实现，未宣称混合检索方法收益。

首轮用量：2898 次调用，已知 prompt 51,553,342、completion 421,731、total
51,975,073、cached 46,838,267。q1252 一次传输失败未返回 usage，完整总量为 null。
第二轮用量：623 次调用，已知 prompt 11,058,940、completion 91,005、total
11,149,945、cached 10,068,725。q1238 的余额错误请求未返回 usage，完整总量为 null。
两个 run 的 reasoning token 均未确报，实际货币费用未知；默认累计 0 不能当作确报 0。
固定 30 题 smoke 和两次连通性探针分别留账，不并入正式重复结果。

## 本地交付与证据

以下路径以 `F:/data/VSCodeproject/AIDB-SQL` 为根，完整工件留在 Git 忽略目录。

- `.local-services/experiments/deliverables/DSF_20260922_B0_01/question_sql_scores.jsonl`：
  首轮 300 条 question/evidence、原始提交和官方逐题评分；同目录 manifest 绑定全部来源。
- `.local-services/experiments/deliverables/resource_stop_deepseek_official_20260922/`：
  第二轮 66 条 `DSF_20260922_B0_02_partial_question_sql.jsonl`、234 条
  `DSF_20260922_B0_02_pending.jsonl`，以及 `manifest.json`。部分记录 EX 为 null，
  manifest 引用原首轮完整导出，未拼接 SQL。
- `.local-services/experiments/runs/DSF_20260922_B0_02/partial_stop_audit.json`：
  23 项部分停止检查、只读 state 事务快照、全部实际请求、原始提交及 206 个来源绑定。
- `experiments/DSF_20260922_B0_01_summary.json`、
  `experiments/DSF_20260922_B0_02_stop_summary.json`：可提交 Git 的独立轻量摘要。
- `.local-services/experiments/series/deepseek_official_20260922/B0_02_process_resource_stopped.json`：
  实际监督进程身份、已结束状态和停止原因。
- 部分交付目录的 `independent_verification.json`：20 项独立复核全部通过。
  `.local-services/experiments/official_balance_resume_verification_20260922.json`：
  19 项本地恢复条件核对通过，覆盖冻结代码、正规化配置、数据/索引/runtime 哈希、
  进程退出及待处理边界；没有执行恢复命令或调用 API。

首轮错误分析覆盖 108 条可执行错误，其中 46 条人工细读。33 条列宽不同；11 条有
上游支持项进入输出目标的证据。预声明的纯投影事后诊断中，7 题可保持其他子句不动：
4 题匹配、3 题仍不匹配；另 4 题因排序别名依赖而排除。它们没有生成新的模型预测，
不能换算为候选收益。论文原文及实现复核保存在首轮 `paper_notes.md`，详细矩阵、
问题–Gold 张力和反事实均隔离在 `diagnostics/semantic_20260922/`。

首轮审阅还发现旧 agent 的派生 `sql_attempt_records` 会在同批响应中错配状态或
行数。原始工具 ID、native 执行轨迹及明确提交保持一致，官方评分来源未受影响。
主线 `b66d5d7` 已改为唯一非空调用 ID 匹配，26 项离线检查和 q26/q95 原轨迹复放
通过；两轮冻结代码及原记录未改写。后续分析使用原始工具事件和 native ledger。

## 资源恢复后的原断点接续

本段是恢复说明，余额不足期间不执行。资源恢复并继续本任务后，先确认仍使用同一
官方 endpoint、模型与冻结合同，再从原工作树、原 run ID 接续；不能用主线较新
代码生成剩余题，也不能将旧 Token Plan 系列接到官方接口上。

```powershell
Push-Location 'F:\data\VSCodeproject\AIDB-SQL-DSF-B0'
& 'F:\data\VSCodeproject\AIDB-SQL\.venv\Scripts\python.exe' -m experiments.supervisor --run-id DSF_20260922_B0_02 --dataset-dir 'F:\data\VSCodeproject\AIDB-SQL\.local-services\experiments\dataset' --config 'F:\data\VSCodeproject\AIDB-SQL-DSF-B0\experiments\deepseek_official.example.json' --subset all
Pop-Location
```

监督进程依 state 跳过全部 66 条终态，保留已消费的 623 次调用与未知用量；下一题
q1241 尚无 attempt，使用冻结的每题预算。q1238 的提交后余额错误不构成重试理由。
本系列已包含累计未知用量修复，不使用旧 Token Plan 的账目替换流程。

完成剩余 234 题后，监督进程生成正式评分。从主工作区运行当前工程审计，再使用
`experiments.export_results` 输出新的完整 `deliverables/DSF_20260922_B0_02`；保留
现有部分停止导出和审计。然后在明确的官方系列 registry 登记两轮按时间排序的基线，
依据新证据选择单一候选，固定 smoke 后做两轮完整匹配实验。当前没有完整重复对，
不提前采用方法，也不把资源停止标为研究目标完成。
