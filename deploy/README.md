# SQLite mini-dev 与 PostgreSQL 字段双向量样例

自动实验已建立独立版本的完整索引：11 库、75 表、798 列、1,497 个向量。
当前激活版本为 `column_dual_v1_624e825a0765bdb2e629`；原 20 列样例保留。
全量构建/验证使用 `deploy/build-column-index.py`，实验入口见
[`experiments/README.md`](../experiments/README.md)。下文记录最初样例部署，样例检查器不用于全量验收。

2026-09-21 已按新结构写入 **20 个字段样例**。业务数据仅从原始 SQLite 文件读取；PostgreSQL `AIDB-vector` 负责元数据和向量存储。

| 项目 | 当前状态 |
|---|---|
| PostgreSQL 地址 | `127.0.0.1:55432`，WSL Ubuntu-22.04，PG 16.15 |
| 向量数据库 / 扩展 | `AIDB-vector` / pgvector 0.8.6 |
| 当前表 | `bird_dev_emb_v2.column_embeddings` |
| 样例范围 | 5 个数据集、6 张表、20 个字段 |
| 列名向量 | 20 个，每个 1024 维；只嵌入原始列名 |
| 说明向量 | 17 个，每个 1024 维；只嵌入字段说明 |
| 缺少说明的样例 | `Laboratory.PIC`、`TAT`、`TAT2`；说明与说明向量为 NULL，保留列名向量 |
| 旧嵌入清理 | 已删除旧表的 873 条记录，`bird_dev_des_emb` 现为 0 条 |
| 业务连接 | 默认 SQLite `california_schools.sqlite`，只读 URI |
| 嵌入服务 | `http://127.0.0.1:8080/v1`，`jina-embeddings-v3-Q8_0.gguf` |
| 检索方式 | 列名向量与说明向量分别召回、按同一记录 ID 做 RRF 融合；BM25 尚未接入 |

## 查看实际入库结果

- [20 个字段的可读预览](F:/data/VSCodeproject/AIDB-SQL/.local-services/minidev-dual/preview.md)：所有库表列名、维度、实际说明和建表 SQL。
- [结构化记录预览](F:/data/VSCodeproject/AIDB-SQL/.local-services/minidev-dual/records-preview.json)：每条记录的元数据、两个向量的维度、前 8 个数值和范数。
- [完整向量数据](F:/data/VSCodeproject/AIDB-SQL/.local-services/minidev-dual/records-full.json)：从新表读回的 20 条记录，包含全部向量分量。
- [实际嵌入输入](F:/data/VSCodeproject/AIDB-SQL/.local-services/minidev-dual/embedding-inputs.json)：37 次文本嵌入对应的输入、哈希和字段映射。
- [验证结果](F:/data/VSCodeproject/AIDB-SQL/.local-services/minidev-dual/verification.json)。

## 表结构与来源

一列一行，以 `(db_id, table_name, column_name)` 唯一定位。主要字段为：

```text
id                     BIGSERIAL
 db_id                 TEXT
 table_name            TEXT
 column_name           TEXT
 description           TEXT，可为 NULL
 description_source    TEXT
 name_embedding        VECTOR(1024)，不可为 NULL
 description_embedding VECTOR(1024)，可为 NULL
 embedding_model       TEXT
 metadata              JSONB
 created_at            TIMESTAMPTZ
```

`name_embedding = embed(column_name)`；`description_embedding = embed(description)`。不额外拼接库名、表名、类型、主外键或样例值。两个向量字段分别具有 HNSW 余弦索引。记录中的 TEXT 字段已保留，但保存 TEXT 本身不代表已建立 BM25 索引。

说明优先来自项目根目录 `column_meaning.json`，没有时读取对应 `database_description/*.csv`。仅清理开头的 `#` 和首尾空白，原始说明另存在 metadata 中。空说明不会生成占位说明向量。

所有表列名称保留 SQLite 原始大小写、空格与特殊字符；类型、主外键和少量样例也从 SQLite 读取。metadata 明确记录 SQLite 路径、CSV 行、原始字段键和嵌入输入哈希。

## 配置和运行

[.env](F:/data/VSCodeproject/AIDB-SQL/.env) 已切换到：

- `BIRD_DEV_PG_URI` / `BIRD_DEV_ADMIN_URI`：只读 / 入库连接 `AIDB-vector`。
- `BIRD_DEV_SCHEMA=bird_dev_emb_v2`。
- `BIRD_DEV_COLUMN_TABLE=column_embeddings`。
- `BIRD_DEV_INDEX_LAYOUT=column_dual_v1`。
- `DATABASE_URI`：以 `mode=ro&immutable=1` 读取原始 SQLite；默认 `california_schools`。

切换业务数据集时，使用相应 SQLite 文件的连接 URI。检索器从 SQLite 文件名识别 `db_id`，两路向量查询都按它过滤。只读账号 `aidb_reader` 的 SELECT 权限和无写权限已经验证。密码仍只保存在被 Git 忽略的本地配置中。

启动服务：

```powershell
& 'F:\data\VSCodeproject\AIDB-SQL\deploy\start-services.ps1'
```

脚本复用已有进程，并以隐藏 WSL 保活进程防止 PostgreSQL 空闲退出。Windows 重启或 `wsl --shutdown` 后需重新运行；未设置 Windows 登录自启动。

重新生成这一批 20 条样例（会替换本项目 mini-dev 的旧嵌入与现有样例）：

```powershell
& .\.venv\Scripts\python.exe deploy/rebuild-column-sample.py --replace
```

脚本先完成所有嵌入和校验，再在同一事务内插入新记录、删除旧记录。业务 SQLite 文件和历史 `BIRD_minidev` PostgreSQL 业务库不参与删除。

执行检查：

```powershell
& .\.venv\Scripts\python.exe deploy/check-services.py
& .\.venv\Scripts\python.exe deploy/check-column-sample.py
```

`check-minidev.py` 在新布局下会转到同一双向量检查。已通过：20 列 SQLite 精确映射、37 个有限且归一化的 1024 维向量、重新嵌入与库内向量对照、缺失说明列的列名召回、字段说明进入 M-Schema、只读权限、两个 HNSW 索引，以及旧表清空检查。

## 当前范围

本次只写入 20 个参考字段，并非完整的 798 列索引。尚未接入 BM25、值级向量索引或执行 500 题模型评估。全量字段中仍有 99 条说明在 JSON 和原始 CSV 中都缺失。

旧的 PG 来源拼接入库脚本 `build-minidev-index.py` 已在新布局下禁用，以免误恢复旧索引。此前导入的 `BIRD_minidev` 业务库保留，但当前入库与业务查询均不再读取它。`.local-services/minidev/` 和旧导入核验文件是历史记录；当前样例和验证结果在 `.local-services/minidev-dual/`。
