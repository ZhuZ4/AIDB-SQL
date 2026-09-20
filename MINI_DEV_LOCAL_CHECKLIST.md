# mini-dev 本地评估下载与部署清单

部署更新：已按方案 A 完成 WSL PostgreSQL + pgvector、Windows Jina 嵌入服务和独立 Python 环境的部署与联通验证。当前服务版本、启动方式及待完成的评估准备见 [部署说明](F:/data/VSCodeproject/AIDB-SQL/deploy/README.md)。下文保留部署前核查清单，软件缺失状态以部署说明为准。

核查日期：2026-09-18。依据当前 AIDB-SQL 代码、本机软件环境、相邻数据目录和官方文档整理。本次仅核查和编写清单，未安装软件、初始化数据库或运行全量评估。

范围按本机已有的经典 BIRD Mini-Dev SQLite 版：500 题、11 个数据库。官方另有扩展的 V2/LiveSQLBench，属于不同评估范围，不能混用题目、数据库与评分脚本。[官方说明](https://github.com/bird-bench/mini_dev)

**应部署的组件**

| 用途 | 当前代码对应组件 | 本地部署选择 |
|---|---|---|
| 执行预测 SQL 和标准 SQL | SQLite，逐题选择对应的 `.sqlite` 文件 | 使用已下载的 11 个数据库和 Python sqlite3 |
| 表、列、值三级向量检索 | PostgreSQL + pgvector，余弦距离 | PostgreSQL 16 + pgvector；可在 WSL Ubuntu 直接安装，或采用 pgvector 0.8.6 容器。建议版本不代表原服务已锁定的版本 |
| 生成向量 | `jina-embeddings-v3-Q8_0.gguf`，1024 维 | llama.cpp 提供 `/v1/embeddings` 接口 |
| Agent 推理和 SQL 生成 | Google ADK + LiteLLM；默认模型名 `Qwen/Qwen3-235B-A22B` | 优先继续调用模型 API；全离线方案见下文 |
| 全量评估 | 本项目生成预测，BIRD 官方评分程序计算指标 | EX；需要完整指标时再运行 R-VES、Soft-F1 |

代码依据：[向量和模型配置](F:/data/VSCodeproject/AIDB-SQL/tools/bird_dev_retriever.py:41)、[生成模型配置](F:/data/VSCodeproject/AIDB-SQL/utils.py:65)、[最终 SQL 提交](F:/data/VSCodeproject/AIDB-SQL/tools/native_sql_tools.py:2575)。

**实际下载清单**

Docker Desktop 是可选部署方式。可以完全不安装 Docker，直接在 WSL 2 的 Ubuntu 中安装 PostgreSQL 和 pgvector。前一版清单中的 Windows llama.cpp 本来就是直接运行的嵌入服务，不依赖 Docker。

结合已有 Windows Python 环境，可以采用：WSL Ubuntu 运行 PostgreSQL + pgvector，Windows 运行 llama.cpp/Jina 和 Agent。Windows 通常可通过 localhost 访问 WSL 服务，仍需检查 PostgreSQL 的监听、认证和实际连通性。[WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)

也可以将所有组件放入 WSL：此时使用 Linux 版 Python、llama.cpp 和匹配的 CUDA 用户态库，不下载 Windows llama.cpp/DLL。WSL 的 GPU 使用 Windows NVIDIA 驱动，不应在 WSL 内另装 Linux NVIDIA 显卡驱动。[NVIDIA WSL 说明](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)

| 勾选 | 下载项 | 具体选择 / 下载入口 | 本机状态 |
|---|---|---|---|
| WSL 直接部署时 | Ubuntu 发行版、PostgreSQL 16、pgvector | 若已有 WSL Ubuntu 则复用；配置 PostgreSQL APT 源后可安装 `postgresql-16` 和 `postgresql-16-pgvector`。[官方包说明](https://github.com/pgvector/pgvector#apt) | WSL 已有；Ubuntu 是否已安装尚未确认 |
| 仅 Docker 路线 | Docker Desktop | Windows x86_64，使用 WSL 2 后端：[官方下载](https://docs.docker.com/desktop/setup/install/windows-install/) | PATH、常见安装目录及安装登记中未发现 |
| 仅 Docker 路线 | PostgreSQL + pgvector 镜像 | `pgvector/pgvector:0.8.6-pg16`：[官方项目及镜像说明](https://github.com/pgvector/pgvector#docker) | 未发现本地 PostgreSQL 服务；5432/55432 端口未监听 |
| [ ] | llama.cpp 推理程序 | Releases 中 **Windows x64 (CUDA 12)**，含 `llama-server.exe`：[官方下载](https://github.com/ggml-org/llama.cpp/releases) | 未在已检查的位置找到 |
| [ ] | llama.cpp 配套 CUDA DLL | 下载同一 Release 页面中 CUDA 12 构建旁的 DLL 包。核查时对应 CUDA 12.4 DLLs | 本机有 CUDA Toolkit 12.1；仍需按预编译包要求准备匹配 DLL |
| [ ] | Jina v3 Q8 模型 | `jina-embeddings-v3-Q8_0.gguf`，约 **601 MB**：[下载文件](https://huggingface.co/second-state/jina-embeddings-v3-GGUF/blob/main/jina-embeddings-v3-Q8_0.gguf) | 常见 Hugging Face 缓存、已检查模型目录中未发现 |
| [ ] | Python 运行依赖 | 按下方包清单通过 pip 下载、安装 | 当前两个 Conda 环境均缺少主要 Agent 依赖 |
| 可选 | Jina 检索任务 LoRA | `lora-retrieval.query-jina-embeddings-v3-f16.gguf` 和 `lora-retrieval.passage-jina-embeddings-v3-f16.gguf`，各约 10.3 MB：[文件列表](https://huggingface.co/second-state/jina-embeddings-v3-GGUF/tree/main) | 只有确定采用对应任务适配器时才需要加载 |
| 全离线时 | SQL 生成模型 | 可试用官方 [Qwen3-8B-GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF) 的 Q4_K_M；使用支持工具调用的本地服务 | 更换后属于新的模型配置，分数不能视作原 235B 模型结果的复现 |

Jina GGUF 下载源是 Second State 发布的社区转换版本，文件名与代码默认值一致，但无法仅凭文件名确认它与原内网服务的权重完全相同。严格复现还要获取原权重哈希、服务版本、任务 LoRA、pooling、归一化和截断参数。Jina 官方说明区分 `retrieval.query` 与 `retrieval.passage`；当前客户端没有显式传递任务参数，原服务是否加载 LoRA 尚不明确。[官方模型说明](https://huggingface.co/jinaai/jina-embeddings-v3)

如果更换嵌入模型或任务配置，需要重新构建整套向量；保持 1024 维本身不能保证新旧向量可混用。llama.cpp 提供兼容的 `/v1/embeddings` 接口，但下载后仍应检查该 GGUF 的实际输出维度及当前客户端的 `dimensions` 参数兼容性。[接口说明](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

**Python 环境与包**

已安装 Anaconda。base 是 Python 3.13.9，test 环境是 Python 3.11.14。建议单独创建 Python 3.11 评估环境，无需再下载安装 Anaconda。

| 类别 | 包 |
|---|---|
| Agent 直接依赖 | `google-adk`、`litellm`、`openai`、`SQLAlchemy`、`langchain-community`、`python-dotenv`、`psycopg2-binary` |
| 官方评分代码依赖 | `func-timeout`、`numpy`、`pymysql`；`psycopg2-binary` 同上 |
| 调用 `tools/mschema/schema_engine.py` 时 | `llama-index-core`；当前主路径直接构建 MSchema，并非每次都用这个模块 |
| 使用原始 Hugging Face 模型代替 GGUF 时 | 另备 `torch`、`transformers`、`sentence-transformers` 等，与对应模型版本匹配 |

准备环境的命令示例（本次未执行，尚未验证依赖组合）：

```powershell
conda create -n aidb-minidev python=3.11 pip
conda activate aidb-minidev
python -m pip install google-adk litellm openai SQLAlchemy langchain-community python-dotenv psycopg2-binary func-timeout numpy pymysql
```

`google-adk` 必须包含 `google.adk.skills.load_skill_from_dir` 和 `google.adk.tools.skill_toolset.SkillToolset`。导入及端到端测试通过后再锁定所有包版本。[ADK Skills 文档](https://adk.dev/skills/)

本机 test 环境已有 openai 1.109.1、SQLAlchemy 2.0.45、python-dotenv 1.1.1、func-timeout 4.3.5、numpy 2.0.1、torch 2.5.0、transformers 4.57.3、sentence-transformers 5.0.0；这些记录仅说明包存在，不表示整个项目已验证兼容。

本地官方 `mini_dev/requirements.txt` 是 **osx-arm64 的 Conda 导出清单**，不能直接作为 Windows 的 pip requirements 使用。官方评分器顶层导入 `pymysql` 和 `psycopg2`，因此即使评测 SQLite 也要提供这两个 Python 驱动；SQLite 评测本身不要求安装 MySQL 服务器。

**已有数据和工具，无需重复下载**

| 内容 | 本地位置 / 状态 |
|---|---|
| 500 题 JSON | [mini_dev_sqlite.json](F:/data/VSCodeproject/minidev/MINIDEV/mini_dev_sqlite.json)；500 个唯一 question_id，均有 question/evidence/SQL/difficulty/db_id |
| 11 个 SQLite 库 | [dev_databases](F:/data/VSCodeproject/minidev/MINIDEV/dev_databases)；文件总计约 1.39 GiB，每个数据库都有 database_description CSV |
| 现有 gold 文件 | [mini_dev_sqlite_gold.sql](F:/data/VSCodeproject/minidev/MINIDEV/mini_dev_sqlite_gold.sql)；500 个非空行，存在下述格式和一致性问题 |
| 官方评分程序 | [evaluation](F:/data/VSCodeproject/mini_dev/evaluation)；已有 evaluation_ex.py、evaluation_ves.py、evaluation_f1.py、evaluation_utils.py |
| 基础运行工具 | Git、Anaconda、Python、SQLite 已有 |
| WSL | 安装登记显示 2.7.3.0；发行版查询因当前访问限制失败，不能据此断定未安装 Ubuntu |
| NVIDIA | RTX 4060 Laptop GPU，8188 MiB 显存，驱动 610.74；已有 CUDA Toolkit 12.1 |
| 内存及空间 | 系统报告内存 31.2 GiB；F 盘剩余约 663.8 GiB；C 盘约 45.1 GiB |
| Ollama | 已安装，发现 deepseek-r1 7b/70b 的模型清单；服务连接失败，未验证模型文件完整性或运行能力 |

本地数据的文件数量和题目字段已经核对，但尚未逐库执行完整性检查，也未与 Hugging Face 的最新修订逐项比较。需要更新题目时使用 [BIRD 官方数据源](https://huggingface.co/datasets/birdsql/bird_mini_dev)，同时同步 gold 和评分输入。

**下载后仍须补齐的项目内容**

- [ ] 补回或实现向量入库脚本。代码注释引用 `bird_dev_single_db_embed_v2.py`，当前仓库未提供。必须从全部 11 个数据库及描述生成 table/column/value 记录，而不只是对 500 个问题做嵌入。
- [ ] 准备 `bird_dev` 数据库、`bird_dev_emb_v2.bird_dev_des_emb` 表及 pgvector 扩展。至少满足检索器需要的 id、db_id、record_type、table_name、column_name、value_text、freq、metadata、embedding 等字段；向量为 1024 维。兼容原版本时还要核对 value_hash、embed_text。索引、采样和截断策略需固定。
- [ ] 补齐 `skills/correct/SKILL.md`。AGENTS.md 要求的 correct 技能缺失，现有 sql-error-analysis 的名称和流程不能自动替代它。data-link 引用的 schema-exploration 也未提供；report-generation 在生成报告时需要，普通评估不依赖它。
- [ ] 补齐调用当前 Agent 的全量预测脚本。代码引用的 `test_bird_dev.py`、项目 `evaluate.py` 当前未提供；相邻目录已有官方评分器，可以接入使用。需要逐题切换数据库、隔离会话、记录超时、保存预测并支持断点续跑。
- [ ] 预测 SQL 以 `submit_final_sql` 提交并由 `get_final_sql` 读取；不要随意从回答文本提取另一条 SQL 判分。保存 500 题的完整顺序和失败记录，失败题不能从分母中删除。
- [ ] 修复 gold 对齐：当前 gold 有 3 行不符合 Tab 分隔格式，6 行与 JSON 的 SQL 文本不一致（包含前述 3 行）。固定选用的题目版本后，以该版本重新导出 gold，并核对顺序、SQL 和 db_id。
- [ ] 生成评分器需要的难度 JSONL。当前评分器调用 `load_jsonl`，本地题目文件是 JSON 数组；不能只改扩展名。预测 JSON 使用评分器要求的 `SQL\t----- bird -----\tdb_id` 格式，并与 gold 按位置对齐。
- [ ] 修正官方 run_evaluation.sh 的示例路径。可以直接用 Python 调用评分器，不需要照搬 Bash 脚本中的目录。
- [ ] 完成小样本端到端验证，再跑全部 500 题；EX 是执行结果准确率，需要完整指标时单独运行 R-VES、Soft-F1。固定并记录题目修订、数据库版本、模型、提示词、向量构建规则、依赖、超时和并发配置。

**部署条件与配置**

推荐先使用“本地 SQLite + 本地 pgvector + 本地 Jina + 模型 API”。按当前硬件估算，32 GB 内存和 8 GB 显存适合这一路线，仍需以实际建库规模和小样本运行测量为准。为镜像、模型、向量库和日志先在 F 盘预留 30–50 GB，属于起步预算，完整值级索引可能需要更多空间。

Docker 路线要求启用硬件虚拟化及可工作的 WSL 2 后端；是否需要启用 Windows 功能或重启，以安装器检查为准。Docker 自身可管理 Linux 环境，单独安装 Ubuntu 并非本路线的必需下载项。[Windows 安装条件](https://docs.docker.com/desktop/setup/install/windows-install/)

配置应在所有服务和数据准备好后填写，以下是目标示例，不表示服务现已可用：

```dotenv
BIRD_DEV_PG_URI=postgresql+psycopg2://postgres:<本地密码>@127.0.0.1:55432/bird_dev
BIRD_DEV_SCHEMA=bird_dev_emb_v2
EMBEDDING_API_URL=http://127.0.0.1:8080/v1
EMBEDDING_API_KEY=no-key
EMBEDDING_MODEL=jina-embeddings-v3-Q8_0.gguf
EMBEDDING_DIM=1024
LITE_LLM_BASE_URL=<生成模型服务的兼容API地址>
LITE_LLM_API_KEY=<密钥；本地无鉴权服务也需按当前代码填写非空值>
LITE_LLM_MODEL_NAME=<服务实际支持的模型名>
```

逐题设置 DATABASE_URI 和对应的 DESCRIPTION_DIR；数据库文件名需与 db_id 一致。PostgreSQL 容器可映射本地 55432 到容器 5432，并设置持久化存储。嵌入服务需返回 1024 维；生成模型服务需正确支持 Agent 的工具调用。上述端口在本次 localhost 检查中均未发现监听服务。

若要求完全离线，可在 Qwen3-8B Q4_K_M 上重新评测，先限制上下文、并发并测量显存；必要时将 Jina 放在 CPU 运行。当前默认的 Qwen3-235B-A22B 总参数规模无法放入本机 8 GB 显存与 32 GB 内存的常规推理配置，完整复现该模型设置应使用远程服务或更大服务器。[Qwen 官方模型](https://huggingface.co/Qwen/Qwen3-235B-A22B)

若要求复现既有分数，还须向原部署方取得：生成模型准确版本及推理参数、原 Jina 文件哈希和启动参数、向量构建脚本/数据快照、全部技能文件、依赖锁文件以及原评估配置。当前仓库不足以保证原实验分数能够完全复现。
