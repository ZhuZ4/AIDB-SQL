"""
BIRD Dev 开发集向量检索器 (pgvector only)
=============================================
自包含模块，不依赖 adk_agent 外部的任何函数。

功能：
  对 bird_dev_des_emb 表执行三阶段级联向量检索：
    1. 表级召回 — 定位相关表
    2. 字段级召回 — 锁定相关列
    3. 值级召回  — 实体对齐（用户说"加州" → 数据库里是 "CA"？）

  每层使用 pgvector 余弦相似度排序；列级附带"同表近名兄弟列"扩展。

存储架构：
  - 数据库: bird_dev (PostgreSQL)
  - Schema: bird_dev_emb_v2（可通过 BIRD_DEV_SCHEMA 覆盖）
  - 表:     <schema>.bird_dev_des_emb
  - 字段:   id, db_id, record_type(table/column/value),
            table_name, column_name, value_text, value_hash,
            freq, metadata(JSONB), embedding(VECTOR)

依赖：
  pip install openai sqlalchemy psycopg2-binary
"""

import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple

from openai import OpenAI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# ==================== 配置 ====================

BIRD_DEV_PG_URI = os.getenv(
    "BIRD_DEV_PG_URI",
    "postgresql+psycopg2://postgres:123456@10.10.181.38:55432/bird_dev",
)

EMBEDDING_API_URL = os.getenv("EMBEDDING_API_URL", "http://10.10.185.22:8080/v1")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "no-key")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "jina-embeddings-v3-Q8_0.gguf")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))

# Schema 名称（与嵌入脚本 bird_dev_single_db_embed_v2.py 保持一致）
DEFAULT_SCHEMA_NAME = "bird_dev_emb_v2"

# 检索参数
TABLE_TOP_K = 5
COLUMN_TOP_K = 15
VALUE_TOP_K = 20

# 二级列名扩展参数：解决 "DOC type" 只召回 DOCType 遗漏 DOC 的近名兄弟列问题
SIBLING_TOP_K_PER_HIT = int(os.getenv("SIBLING_TOP_K_PER_HIT", "2"))
SIBLING_EXPAND_TOP_HITS = int(os.getenv("SIBLING_EXPAND_TOP_HITS", "3"))
SIBLING_CAP_TOTAL = int(os.getenv("SIBLING_CAP_TOTAL", "4"))

# 阶段 3 值召回的混合排序权重：blended = α*value_sim + (1-α)*column_score
# 同一字面值在多列同时命中时，用列级相关性消歧，避免 pgvector 平局退化为插入顺序。
VALUE_BLEND_ALPHA = float(os.getenv("VALUE_BLEND_ALPHA", "0.6"))
# 兄弟列分数折扣：兄弟列是从主命中扩展来的近名列，参与值排序时打一个折扣
SIBLING_COLUMN_SCORE_DISCOUNT = float(os.getenv("SIBLING_COLUMN_SCORE_DISCOUNT", "0.85"))

# ==================== 数据模型 ====================


@dataclass
class RetrievedRecord:
    """单条检索结果"""
    id: int
    record_type: str
    table_name: Optional[str]
    column_name: Optional[str]
    value_text: str
    freq: Optional[int]
    metadata: Dict[str, Any]
    vec_score: float = 0.0
    embed_text: Optional[str] = None


@dataclass
class RetrievalResult:
    """三阶段检索汇总结果"""
    tables: List[RetrievedRecord] = field(default_factory=list)
    columns: List[RetrievedRecord] = field(default_factory=list)
    values: List[RetrievedRecord] = field(default_factory=list)
    query: str = ""
    db_id: str = ""


# ==================== 引擎单例 ====================

_dev_engine: Optional[Engine] = None
_embed_text_support_cache: Dict[str, bool] = {}


def _get_schema_name() -> str:
    return os.getenv("BIRD_DEV_SCHEMA", DEFAULT_SCHEMA_NAME)


def _get_schema_table_name() -> str:
    return f"{_get_schema_name()}.bird_dev_des_emb"


def _schema_has_embed_text(engine: Engine) -> bool:
    schema_name = _get_schema_name()
    cached = _embed_text_support_cache.get(schema_name)
    if cached is not None:
        return cached

    sql = """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = :schema_name
          AND table_name = 'bird_dev_des_emb'
          AND column_name = 'embed_text'
        LIMIT 1
    """

    try:
        with engine.connect() as conn:
            has_embed_text = conn.execute(text(sql), {"schema_name": schema_name}).first() is not None
    except Exception as e:
        logger.warning("无法探测 %s 是否包含 embed_text，回退到兼容模式: %s", schema_name, e)
        has_embed_text = False

    _embed_text_support_cache[schema_name] = has_embed_text
    return has_embed_text


def _get_dev_engine() -> Engine:
    global _dev_engine
    if _dev_engine is None:
        _dev_engine = create_engine(
            BIRD_DEV_PG_URI, pool_pre_ping=True, pool_size=3, echo=False,
        )
        logger.info("BIRD Dev 检索引擎已连接: %s", BIRD_DEV_PG_URI.split("@")[-1])
    return _dev_engine


# ==================== Embedding 生成 ====================


@lru_cache(maxsize=2048)
def _embed_cached_strict(text_input: str) -> Tuple[float, ...]:
    """LRU 缓存的真正实现；失败时抛异常以避免 lru_cache 缓存 None。"""
    base_url = EMBEDDING_API_URL.strip()
    if not base_url.startswith(("http://", "https://")):
        base_url = f"http://{base_url}"

    client = OpenAI(api_key=EMBEDDING_API_KEY, base_url=base_url, timeout=60.0)
    kwargs: Dict[str, Any] = {"model": EMBEDDING_MODEL, "input": [text_input]}
    if EMBEDDING_DIM > 0:
        kwargs["dimensions"] = EMBEDDING_DIM

    resp = client.embeddings.create(**kwargs)
    if not resp.data:
        raise RuntimeError("embedding API returned no data")
    return tuple(resp.data[0].embedding)


def _embed_cached(text_input: str) -> Optional[Tuple[float, ...]]:
    """Embedding 的小缓存包装。失败时返回 None 且不缓存失败值。"""
    try:
        return _embed_cached_strict(text_input)
    except Exception as e:
        logger.warning("Embedding 生成失败: %s", e)
        return None


def _generate_embedding(text_input: str) -> Optional[List[float]]:
    """
    调用 OpenAI 兼容 API 生成向量（自包含，不引用外部模块）。
    内部走 `_embed_cached` LRU 缓存避免列名级扩展时重复打 API。
    """
    cached = _embed_cached(text_input)
    return list(cached) if cached is not None else None


# ==================== pgvector 向量检索 ====================


def _vector_rank(
    engine: Engine,
    embedding: List[float],
    db_id: str,
    record_type: str,
    top_k: int,
    table_filter: Optional[List[str]] = None,
    column_filter: Optional[List[str]] = None,
) -> List[Tuple[int, float]]:
    """
    pgvector 余弦相似度检索

    Returns:
        [(row_id, similarity_score), ...] 按相似度降序
    """
    embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

    conditions = [
        "db_id = :db_id",
        "record_type = :record_type",
        "embedding IS NOT NULL",
    ]
    params: Dict[str, Any] = {
        "db_id": db_id,
        "record_type": record_type,
        "embedding_array": embedding_str,
        "top_k": top_k,
    }

    if table_filter:
        conditions.append("table_name = ANY(:table_filter)")
        params["table_filter"] = table_filter

    if column_filter:
        # column_filter 格式: ["table.column", ...]
        tbl_col_pairs = []
        for tc in column_filter:
            parts = tc.split(".", 1)
            if len(parts) == 2:
                tbl_col_pairs.append(parts)
        if tbl_col_pairs:
            or_parts = []
            for idx, (tbl, col) in enumerate(tbl_col_pairs):
                t_key = f"cf_tbl_{idx}"
                c_key = f"cf_col_{idx}"
                or_parts.append(f"(table_name = :{t_key} AND column_name = :{c_key})")
                params[t_key] = tbl
                params[c_key] = col
            conditions.append(f"({' OR '.join(or_parts)})")

    where_clause = " AND ".join(conditions)

    sql = f"""
        SELECT id, 1 - (embedding <=> CAST(:embedding_array AS vector)) AS similarity
        FROM {_get_schema_table_name()}
        WHERE {where_clause}
        ORDER BY embedding <=> CAST(:embedding_array AS vector)
        LIMIT :top_k
    """

    try:
        with engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
            return [(int(r[0]), float(r[1])) for r in rows]
    except Exception as e:
        logger.error("pgvector 检索失败: %s", e)
        return []


# ==================== 二级列名扩展（解决兄弟列遗漏） ====================


def _expand_sibling_columns(
    primary_records: List["RetrievedRecord"],
    engine: Engine,
    db_id: str,
    existing_ids: Set[int],
) -> List[Tuple[int, str, float]]:
    """
    用 top-hit 列名作为二级查询，再跑一次列名→列向量检索，
    把同表语义相近却被一级排序挤出 top-K 的兄弟列（如 DOC ↔ DOCType）拉回来。

    返回: [(row_id, sibling_of_column_name, sim), ...]，已去重并受 SIBLING_CAP_TOTAL 限制。
    """
    if not primary_records or SIBLING_CAP_TOTAL <= 0:
        return []

    added_ids: Set[int] = set(existing_ids)
    items: List[Tuple[int, str, float]] = []
    seeds = primary_records[:SIBLING_EXPAND_TOP_HITS]

    for seed in seeds:
        col_name = seed.column_name
        table_name = seed.table_name
        if not col_name or not table_name:
            continue

        sibling_query = _get_sibling_query_text(seed)
        if not sibling_query:
            continue

        col_emb = _generate_embedding(sibling_query)
        if not col_emb:
            continue

        vec_sib = _vector_rank(
            engine, col_emb, db_id, "column",
            SIBLING_TOP_K_PER_HIT * 4, table_filter=[table_name],
        )

        picked = 0
        for rid, sim in vec_sib:
            if rid == seed.id or rid in added_ids:
                continue
            items.append((rid, col_name, sim))
            added_ids.add(rid)
            picked += 1
            if picked >= SIBLING_TOP_K_PER_HIT:
                break
            if len(items) >= SIBLING_CAP_TOTAL:
                break
        if len(items) >= SIBLING_CAP_TOTAL:
            break

    return items[:SIBLING_CAP_TOTAL]


def _get_sibling_query_text(record: RetrievedRecord) -> Optional[str]:
    if record.embed_text:
        embed_text = record.embed_text.strip()
        if embed_text:
            return embed_text

    if record.table_name and record.column_name:
        return f"{record.table_name}.{record.column_name}"

    if record.column_name:
        return record.column_name

    return None


# ==================== 行加载 ====================


def _load_rows_by_ids(engine: Engine, ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """按 ID 批量加载行"""
    if not ids:
        return {}
    embed_text_select = "embed_text" if _schema_has_embed_text(engine) else "NULL AS embed_text"
    sql = f"""
        SELECT id, db_id, record_type, table_name, column_name,
               value_text, {embed_text_select}, freq, metadata
        FROM {_get_schema_table_name()}
        WHERE id = ANY(:ids)
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(sql), {"ids": ids}).mappings().all()
            result = {}
            for r in rows:
                result[int(r["id"])] = {
                    "id": int(r["id"]),
                    "db_id": r["db_id"],
                    "record_type": r["record_type"],
                    "table_name": r["table_name"],
                    "column_name": r["column_name"],
                    "value_text": r["value_text"] or "",
                    "embed_text": r["embed_text"] or None,
                    "freq": r["freq"],
                    "metadata": json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {}),
                }
            return result
    except Exception as e:
        logger.error("按 ID 加载行失败: %s", e)
        return {}


# ==================== 核心三阶段检索 ====================


def hybrid_retrieve(
    question: str,
    db_id: Optional[str] = None,
    table_top_k: int = TABLE_TOP_K,
    column_top_k: int = COLUMN_TOP_K,
    value_top_k: int = VALUE_TOP_K,
) -> RetrievalResult:
    """
    三阶段级联向量检索（pure pgvector cosine）

    阶段 1: table 级 — 缩小搜索范围
    阶段 2: column 级 — 锁定相关列（范围限定在阶段 1 命中的表）
                   附加同表近名兄弟列扩展（向量）作为安全网
    阶段 3: value 级 — 实体对齐（范围限定在阶段 2 命中的列）

    Args:
        question: 用户自然语言问题
        db_id: 数据库标识（与嵌入时的 db_id 对应）
        table_top_k: 表级返回数
        column_top_k: 字段级返回数
        value_top_k: 值级返回数

    Returns:
        RetrievalResult 包含三层检索结果
    """
    normalized_db_id = (db_id or "").strip()
    result = RetrievalResult(query=question, db_id=normalized_db_id)
    if not normalized_db_id:
        logger.warning("hybrid_retrieve called without db_id; returning empty retrieval result")
        return result

    engine = _get_dev_engine()

    logger.info("开始向量检索: question='%s', db_id=%s", question[:80], normalized_db_id)

    query_embedding = _generate_embedding(question)
    if not query_embedding:
        logger.error("Embedding 生成失败，无法执行向量检索，返回空结果")
        return result

    # ==================== 阶段 1: 表级召回 ====================
    vec_table = _vector_rank(engine, query_embedding, normalized_db_id, "table", table_top_k)

    table_data = _load_rows_by_ids(engine, [rid for rid, _ in vec_table])

    hit_table_names: List[str] = []
    _seen_tables: Dict[str, bool] = {}  # 表级去重：同一张表只保留最高分记录
    for rid, sim in vec_table:
        row = table_data.get(rid)
        if not row:
            continue
        tbl_name = row["table_name"]
        if tbl_name in _seen_tables:
            continue
        _seen_tables[tbl_name] = True
        rec = RetrievedRecord(
            id=rid,
            record_type="table",
            table_name=tbl_name,
            column_name=None,
            value_text=row["value_text"],
            freq=row["freq"],
            metadata=row["metadata"],
            vec_score=sim,
            embed_text=row.get("embed_text"),
        )
        result.tables.append(rec)
        if tbl_name and tbl_name not in hit_table_names:
            hit_table_names.append(tbl_name)

    logger.info("阶段1-表级召回: %d 张表 → %s", len(hit_table_names), hit_table_names)

    if not hit_table_names:
        logger.warning("表级召回为空，返回空结果")
        return result

    # ==================== 阶段 2: 字段级召回 ====================
    primary_col_k = max(1, column_top_k - SIBLING_CAP_TOTAL)
    vec_col = _vector_rank(
        engine, query_embedding, normalized_db_id, "column",
        primary_col_k, table_filter=hit_table_names,
    )

    col_data = _load_rows_by_ids(engine, [rid for rid, _ in vec_col])

    hit_columns: List[str] = []  # "table.column" 格式
    for rid, sim in vec_col:
        row = col_data.get(rid)
        if not row:
            continue
        rec = RetrievedRecord(
            id=rid,
            record_type="column",
            table_name=row["table_name"],
            column_name=row["column_name"],
            value_text=row["value_text"],
            freq=row["freq"],
            metadata=row["metadata"],
            vec_score=sim,
            embed_text=row.get("embed_text"),
        )
        result.columns.append(rec)
        tc = f"{row['table_name']}.{row['column_name']}"
        if tc not in hit_columns:
            hit_columns.append(tc)

    # ---- 二级列名向量扩展：把同表近名兄弟列回填 ----
    existing_ids: Set[int] = {r.id for r in result.columns}
    sibling_items = _expand_sibling_columns(
        primary_records=result.columns,
        engine=engine,
        db_id=normalized_db_id,
        existing_ids=existing_ids,
    )
    if sibling_items:
        sib_ids = [rid for rid, _, _ in sibling_items]
        sib_data = _load_rows_by_ids(engine, sib_ids)
        added_siblings: List[str] = []
        for rid, sibling_of, sim in sibling_items:
            row = sib_data.get(rid)
            if not row:
                continue
            sib_meta = dict(row["metadata"] or {})
            sib_meta["sibling_of"] = sibling_of
            rec = RetrievedRecord(
                id=rid,
                record_type="column",
                table_name=row["table_name"],
                column_name=row["column_name"],
                value_text=row["value_text"],
                freq=row["freq"],
                metadata=sib_meta,
                vec_score=sim,
                embed_text=row.get("embed_text"),
            )
            result.columns.append(rec)
            tc = f"{row['table_name']}.{row['column_name']}"
            if tc not in hit_columns:
                hit_columns.append(tc)
            added_siblings.append(f"{tc}(~{sibling_of})")
        if added_siblings:
            logger.info("阶段2扩展-兄弟列: %d 新增 → %s", len(added_siblings), added_siblings)

    logger.info("阶段2-字段级召回: %d 列 → %s", len(hit_columns), hit_columns[:10])

    # ---- 构造列级相关性表，供阶段 3 值排序加权 ----
    # 同表同列出现多条时取最大分；兄弟列因是扩展回填，按折扣计入
    column_score: Dict[str, float] = {}
    for col_rec in result.columns:
        if not col_rec.table_name or not col_rec.column_name:
            continue
        key = f"{col_rec.table_name}.{col_rec.column_name}"
        score = float(col_rec.vec_score or 0.0)
        if (col_rec.metadata or {}).get("sibling_of"):
            score *= SIBLING_COLUMN_SCORE_DISCOUNT
        if score > column_score.get(key, 0.0):
            column_score[key] = score

    # ==================== 阶段 3: 值级召回 ====================
    if hit_columns:
        vec_val = _vector_rank(
            engine, query_embedding, normalized_db_id, "value",
            value_top_k, column_filter=hit_columns,
        )

        val_data = _load_rows_by_ids(engine, [rid for rid, _ in vec_val])

        # 先收集再按 blended 排序，最后按顺序写入 result.values
        scored: List[Tuple[float, float, float, "RetrievedRecord"]] = []
        for rid, sim in vec_val:
            row = val_data.get(rid)
            if not row:
                continue
            tc_key = f"{row['table_name']}.{row['column_name']}"
            col_sim = column_score.get(tc_key, 0.0)
            blended = VALUE_BLEND_ALPHA * float(sim) + (1.0 - VALUE_BLEND_ALPHA) * col_sim
            meta = dict(row["metadata"] or {})
            meta["value_vec_score"] = float(sim)
            meta["column_vec_score"] = col_sim
            meta["blended_score"] = blended
            rec = RetrievedRecord(
                id=rid,
                record_type="value",
                table_name=row["table_name"],
                column_name=row["column_name"],
                value_text=row["value_text"],
                freq=row["freq"],
                metadata=meta,
                vec_score=blended,
                embed_text=row.get("embed_text"),
            )
            scored.append((blended, float(sim), col_sim, rec))

        scored.sort(key=lambda x: x[0], reverse=True)
        for _, _, _, rec in scored:
            result.values.append(rec)

        if scored:
            preview = [
                f"{r.table_name}.{r.column_name}=val_sim={vsim:.3f}/col_sim={csim:.3f}/blend={b:.3f}"
                for b, vsim, csim, r in scored[:5]
            ]
            logger.info("阶段3-值级召回: %d 条值, top5=%s", len(result.values), preview)
        else:
            logger.info("阶段3-值级召回: 0 条值")

    return result


# ==================== 格式化输出 ====================


def format_retrieval_as_xml(result: RetrievalResult) -> str:
    """
    将三阶段检索结果格式化为 XML，注入 LLM Prompt

    输出结构:
      <schema_context>
        <table name="..." relevance="...">
          <columns>
            <column name="..." type="..." relevance="...">
              <sample_values>...</sample_values>
            </column>
          </columns>
        </table>
      </schema_context>
      <value_hints>
        <hint table="..." column="..." value="..." freq="..." />
      </value_hints>

    relevance 为 pgvector 余弦相似度（0..1，越大越相关）。
    """
    if not result.tables:
        return ""

    parts: List[str] = []

    parts.append("<schema_context>")

    for tbl_rec in result.tables:
        tbl_name = tbl_rec.table_name or "unknown"
        meta = tbl_rec.metadata or {}
        row_count = meta.get("row_count", "?")

        parts.append(f'  <table name="{tbl_name}" relevance="{tbl_rec.vec_score:.4f}" rows="{row_count}">')
        parts.append("    <columns>")

        table_columns = [c for c in result.columns if c.table_name == tbl_name]
        for col_rec in table_columns:
            col_meta = col_rec.metadata or {}
            col_type = col_meta.get("type", "TEXT")
            pk = col_meta.get("pk", False)
            distinct = col_meta.get("distinct_count", "?")
            samples = col_meta.get("samples", [])
            sample_vals = [s["value"] for s in samples[:5]] if isinstance(samples, list) else []

            pk_attr = ' pk="true"' if pk else ""
            parts.append(
                f'      <column name="{col_rec.column_name}" type="{col_type}" '
                f'relevance="{col_rec.vec_score:.4f}" distinct="{distinct}"{pk_attr}>'
            )
            if sample_vals:
                parts.append(f"        <sample_values>{json.dumps(sample_vals, ensure_ascii=False)}</sample_values>")
            parts.append("      </column>")

        parts.append("    </columns>")
        parts.append("  </table>")

    parts.append("</schema_context>")

    if result.values:
        parts.append("")
        parts.append("<value_hints>")
        for val_rec in result.values:
            val_meta = val_rec.metadata or {}
            val_text = val_meta.get("value", "")
            freq = val_rec.freq or val_meta.get("freq", 0)
            parts.append(
                f'  <hint table="{val_rec.table_name}" column="{val_rec.column_name}" '
                f'value="{_xml_escape(str(val_text))}" freq="{freq}" '
                f'relevance="{val_rec.vec_score:.4f}" />'
            )
        parts.append("</value_hints>")

    return "\n".join(parts)


def format_retrieval_as_text(result: RetrievalResult) -> str:
    """
    将三阶段检索结果格式化为可读文本摘要（用于 BI 召回输出）
    相关度为 pgvector 余弦相似度。
    """
    if not result.tables:
        return "（向量召回无结果）"

    lines: List[str] = []
    lines.append("### 向量召回结果\n")

    lines.append("**召回的表:**")
    for t in result.tables:
        meta = t.metadata or {}
        lines.append(f"  - `{t.table_name}` (行数={meta.get('row_count', '?')}, 相关度={t.vec_score:.4f})")

    if result.columns:
        lines.append("\n**召回的字段:**")
        for c in result.columns:
            meta = c.metadata or {}
            col_type = meta.get("type", "?")
            lines.append(
                f"  - `{c.table_name}.{c.column_name}` "
                f"(type={col_type}, 相关度={c.vec_score:.4f})"
            )

    if result.values:
        lines.append("\n**召回的值 (实体对齐提示):**")
        for v in result.values[:10]:
            meta = v.metadata or {}
            val = meta.get("value", "?")
            freq = v.freq or meta.get("freq", 0)
            lines.append(
                f"  - `{v.table_name}.{v.column_name}` = \"{val}\" "
                f"(出现{freq}次, 相关度={v.vec_score:.4f})"
            )
        if len(result.values) > 10:
            lines.append(f"  - ... 还有 {len(result.values) - 10} 条")

    return "\n".join(lines)


def _xml_escape(s: str) -> str:
    """简单 XML 转义"""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
