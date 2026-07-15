# AIX pgvector 召回服务

该服务以 `t_datasource_field` 为向量入口，并通过
`t_datasource_field.table_id/ds_id → t_datasource_table.id/ds_id → t_datasource.id`
补齐表与数据源信息。返回结果不会包含 `t_datasource.configuration`。

## 配置

```bash
export AIX_DB_PG_URI='postgresql+psycopg2://user:password@host:5432/aix_db'
export EMBEDDING_API_URL='http://embedding-host:28080/v1'
export EMBEDDING_API_KEY='no-key'
export EMBEDDING_MODEL='bge-m3-FP16.gguf'
export EMBEDDING_DIM='1024'
```

`AIX_DB_PG_URI` 未设置时会回退读取 Aix-DB 已使用的
`SQLALCHEMY_DATABASE_URI`。召回 SQL 会用查询向量的实际维度过滤字段向量，
因此可兼容同一 `VECTOR` 列中存在多种维度的历史数据。

## HTTP 接口

启动独立服务：

```bash
python vector_recall_api.py --host 0.0.0.0 --port 8090
```

请求：

```bash
curl -X POST http://127.0.0.1:8090/api/v1/vector-recall \
  -H 'Content-Type: application/json' \
  -d '{"query":"学校名称","datasource_id":1,"top_k":10,"min_similarity":0.2}'
```

请求字段：

- `query`：必填，也兼容别名 `phrase`。
- `datasource_id`：可选；Text-to-SQL Agent 会从当前会话自动传入。
- `top_k`：1～100，默认 10。
- `min_similarity`：-1～1，默认 0。
- `only_checked`：是否只召回已勾选字段和表，默认 `true`。

已有 Sanic 服务可注册 `create_sanic_blueprint()` 返回的 Blueprint；WSGI 服务可直接
使用 `vector_recall_api.application`。WSGI 健康检查地址为 `GET /health`，Sanic
Blueprint 的健康检查地址为 `GET /api/v1/vector-recall/health`。

## `sql_db_value_lookup` 接入

`set_database_uri(..., datasource_id=<id>)` 会把数据源 ID 绑定到工具会话。
`sql_db_value_lookup` 优先调用本服务召回字段并缓存字段类型/注释，随后对命中的文本列
执行值匹配；未配置 AIX 服务或没有命中时，保留原有 BIRD 检索作为兼容兜底。
