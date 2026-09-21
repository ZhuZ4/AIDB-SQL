# DeepSeek 官方接口实验系列（2026-09-22）

本系列使用官方 endpoint `https://api.deepseek.com/v1` 和已确认的模型别名 `deepseek-flash`（V4.1-Flash）。接口与模型身份改变，因此建立新的完整 300 题基线系列。模型请求的非思考参数、工具调用兼容性和用量记录须在本系列代码冻结前完成验证，并在基线与候选中保持一致。

新接口适配合同：配置 `model` 与环境 `LITE_LLM_MODEL_NAME` 必须同名；新别名仅允许官方 `https://api.deepseek.com` 或其 `/v1` endpoint，本系列固定使用后者，不在轮次间切换 URL 形式。新别名请求显式传入 `extra_body={"thinking":{"type":"disabled"}}`；旧别名保留原有参数。runner 自动生成冻结 endpoint 哈希并传给 worker 校验，worker metadata 记录 `model_name` 与 `provider_endpoint_sha256`。这些绑定由代码实现，无需在本系列配置中手填新字段。

配置为 `experiments/deepseek_official.example.json`。它与原 `experiment.example.json` 仅有两项差异：`experiment_id=B0_deepseek_official_20260922`、`model=deepseek-flash`。未增加 `series_id` 或其他配置字段；密钥由现有 `.env` 提供，不写入说明、配置或工件。

## 历史与新系列隔离

- 旧 `.local-services/experiments/research_registry.json`、B01 正式结果、B02 在 281/300 停止的工件，以及旧 `C1_projection_roles` 的提交、假设和登记记录全部保留。
- 旧 B02 的 19 个 pending 不能在新接口下续接；不能替换旧 run 的 fingerprint、模型或 endpoint 来完成它。新系列所有 300 题均重新生成，不拼接旧题结果。
- 新 registry 固定为 `.local-services/experiments/series/deepseek_official_20260922/research_registry.json`。每次 registry 命令都显式传入此路径，避免使用默认旧 registry。
- 新 run 使用独立 ID。`state.sqlite` 和运行锁可以继续共用，既有 runs/attempts 不删除、不重置。registry 路径只隔离研究登记；实际生成仍由独立 run ID、冻结 manifest 和配置隔离。
- 新旧系列不得合并 EX 均值、重复次数、配对提升或候选判定。旧 C1 的假设可作为来源说明，但新候选必须基于新基线提交创建和登记，不能直接使用旧 C1 的实验身份。

## 固定顺序与预算

| 阶段 | Run ID | 数据范围 |
| --- | --- | --- |
| 工程 smoke | `DSF_20260922_B0_smoke_01` | 已固定的 30 题 smoke 集 |
| 基线第 1 轮 | `DSF_20260922_B0_01` | 原固定 300 题及相同顺序 |
| 基线第 2 轮 | `DSF_20260922_B0_02` | 同一 300 题 |
| 候选工程 smoke | `DSF_20260922_C1_smoke_01` | 已固定的 30 题 smoke 集 |
| C1 第 1 轮 | `DSF_20260922_C1_01` | 同一 300 题 |
| C1 第 2 轮 | `DSF_20260922_C1_02` | 同一 300 题 |

先验证 smoke 的完整工程记录、提交和用量账目，再从同一冻结基线提交完成两轮 B0。两轮正式基线均完成、评分并审计后，登记新系列 C1 的单一改动；候选先通过自己的固定 30 题工程 smoke，再从同一候选提交完成两轮。smoke 不计入正式重复，不补入任何正式题目结果。不要选取表现最好的一轮，也不要调换时间顺序。

沿用每题最多 40 次模型调用、900 秒、SQL 30 秒超时；SQL 查询调用上限 4、单请求超时 120 秒使用既有默认值。瞬时故障最多重试 3 次，退避 5/20/60 秒；串行、temperature=0、预声明 `repetition_count=2`。同题重试/恢复消耗累计到该 run 的原预算，不重置 attempt 历史。

继续使用原 dataset manifest、300 ID、固定 smoke 集、业务库哈希、已完成的 798 列双向量索引，以及校准过的 SQLite 3.40.1 和官方 EX 口径。配置没有重新指定 index 字段，仍由既有环境/索引 manifest 冻结来源解析；新基线与本次仅改投影角色的 C1 应使用相同索引身份。

沿用 `max_api_cost=null`、`max_total_api_calls=null`、`candidate_cost_ratio_limit=null`、`stop_on_insufficient_balance=true`。新 registry 不自动复制或重置预算，也不代表提供方有额度。保留所有新旧消费历史；失败请求无 usage 时，已知 token 小计与完整总量分开，完整总量为 `null`。货币费用未报告时保持未知。额度耗尽停止，不自动充值或换账号。

## 已有 CLI 的执行路径

以下是后续执行模板，本说明的创建没有初始化 registry、调用 API 或启动批次。先完成新接口适配与 smoke 前置验证，再由执行者在相应冻结工作树运行。冻结工作树需要使用既有共享 `.local-services`；Python 使用主项目的虚拟环境。

```powershell
$pythonExe = 'F:\data\VSCodeproject\AIDB-SQL\.venv\Scripts\python.exe'
$datasetDir = 'F:\data\VSCodeproject\AIDB-SQL\.local-services\experiments\dataset'
$officialConfig = Join-Path (Get-Location).Path 'experiments\deepseek_official.example.json'
$seriesRegistry = 'F:\data\VSCodeproject\AIDB-SQL\.local-services\experiments\series\deepseek_official_20260922\research_registry.json'

& $pythonExe -m experiments.research_registry --registry $seriesRegistry init --required-repeats 2 --selection-manifest 'F:\data\VSCodeproject\AIDB-SQL\experiments\mini_dev_300_manifest.json'
& $pythonExe -m experiments.supervisor --run-id DSF_20260922_B0_smoke_01 --dataset-dir $datasetDir --config $officialConfig --subset smoke
```

smoke 审计通过后，按上述顺序分别调用 `experiments.supervisor`，将 run ID 改为两轮基线 ID，并使用 `--subset all`。两轮必须使用同一基线代码提交和配置；不要在重复之间修改 `.env` 的模型、endpoint 或服务身份。supervisor 会在完整生成后评分；若只希望完成生成再检查账目，可使用参数相同的 `experiments.run_batch`，它在生成完成后返回 `EVALUATE`，不自动评分。

每个完整 run 使用 `experiments.audit_run --dataset-dir $datasetDir --run-dir <RUN_DIR> --subset all --output <RUN_DIR>/engineering_audit.json`。已有文件不能覆盖；重复审计使用新文件名。只有实际审计通过才允许下述 `--checks-passed`。

registry 的全局 `--registry` 参数必须放在子命令前。占位符在执行前替换为真实绝对路径、分支及完整提交 SHA：

```text
python -m experiments.research_registry --registry <NEW_REGISTRY> register-baseline --scores <NEW_B0_01/scores.jsonl> <NEW_B0_02/scores.jsonl> --branch <NEW_BASELINE_BRANCH> --commit <FULL_BASELINE_SHA>
python -m experiments.research_registry --registry <NEW_REGISTRY> register-candidate --id C1_projection_roles --hypothesis <ONE_HYPOTHESIS> --changed-variable <ONE_CHANGE> --paper-notes <NEW_NOTES.md> --branch <NEW_CANDIDATE_BRANCH> --commit <FULL_CANDIDATE_SHA>
python -m experiments.compare --baseline <NEW_B0_01/scores.jsonl> <NEW_B0_02/scores.jsonl> --candidate <NEW_C1_01/scores.jsonl> <NEW_C1_02/scores.jsonl> --expected-count 300 --checks-passed --output <NEW_SERIES_DIR/C1_comparison.json>
python -m experiments.research_registry --registry <NEW_REGISTRY> record-decision --id C1_projection_roles --comparison <NEW_SERIES_DIR/C1_comparison.json> --decision auto
python -m experiments.research_registry --registry <NEW_REGISTRY> status
```

候选两轮使用新候选工作树/配置及上表的新 C1 run IDs，保留官方接口、模型、非思考行为、预算和数据合同，仅改变预先声明的变量。两侧重复均按生成时间顺序传入。

`compare` 本身不接收 registry，主要校验评分来源；最终 `record-decision` 还会核对模型、endpoint 哈希、预算、证据、数据及 SQLite 合同，拒绝新旧 provider/model 混配。registry 允许先登记一轮完整基线，但本系列流程要求两轮正式基线完成后再登记候选。采用结论必须来自两轮完整配对；失败、超时和未提交 SQL 均留在各轮 300 题分母内。

无需为此改变旧 registry 的 baseline/best 或旧 C1 状态。新旧 registry 的 `B0` / `C1_projection_roles` 标识只在各自文件内有效，应始终连同 registry 路径引用。
