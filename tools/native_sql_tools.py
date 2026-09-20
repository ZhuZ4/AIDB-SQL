"""
自包含 SQL 工具集
基于 SQLAlchemy + langchain SQLDatabase 直连数据库，不依赖 AIX 外部模块

重构说明：
- 使用 DATABASE_URI 环境变量或 set_database_uri() 连接数据库
- 通过 SQLAlchemy Inspector 获取表结构、外键关系
- 使用 SQLDatabase.run() 执行查询
- 保留会话级工具调用管理器（防死循环、防重复）
"""

import ast
import difflib
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from dotenv import load_dotenv
from langchain_community.utilities import SQLDatabase
from sqlalchemy import create_engine, event, inspect as sa_inspect, text as sa_text
from sqlalchemy.engine import make_url

from .tool_call_manager import get_tool_call_manager

# 加载 adk_agent/.env（兜底：独立运行时 ADK 不会自动加载）
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file, override=False)

logger = logging.getLogger(__name__)

# ==================== 数据库连接管理 ====================

_db_instance: Optional[SQLDatabase] = None
_session_id: str = "default"

# 每个会话的链接架构（data-link 技能使用），格式: session_id -> set of "table.column"
_linked_schema: dict = {}

# 会话级列描述缓存（由 sql_db_value_lookup 填充，供 build_linked_mschema 使用）
# 格式: session_id -> {"table.column": {"type": str, "comment": str}}
_col_descriptions: dict = {}

# 会话级 sql_self_correct 调用计数器，防止 LLM 反复调用编排函数
# 格式: session_id -> int (调用次数)
_self_correct_counts: dict = {}
_SELF_CORRECT_MAX_PER_SESSION = 2  # 每个会话最多调用 sql_self_correct 的次数

# 召回快照：记录 add_schema 累积过的所有元素，用于度量真实召回率
# 格式: session_id -> set of "table.column"
_linked_schema_snapshot: dict = {}

# 会话级核心列集合：明确加入链接架构的列
# 格式: session_id -> set of "table.column"
_core_columns: dict = {}

# 会话级 SQL 执行轨迹：记录真实执行过的 SQL，供测试脚本追溯最终判分 SQL
# 格式: session_id -> list[{"sql": str, "source": str, "state": str, "row_count": int, "is_probe": bool}]
_sql_execution_traces: dict = {}
_sql_execution_scope_var: ContextVar[tuple[str, ...]] = ContextVar(
    "sql_execution_scope",
    default=(),
)

# 会话级实验配置：用于 Chapter 4 消融实验中约束工具和语义拦截行为
_session_experiment_profiles: dict = {}

_DEFAULT_EXPERIMENT_PROFILE = {
    "name": "full",
    "semantic_warning_blocking": True,
    "blocked_tools": set(),
    "max_sql_db_query_calls": None,
    "disable_hybrid_retrieval": False,
    "disable_schema_linking": False,
}

_EXPERIMENT_PROFILE_PRESETS = {
    "full": dict(_DEFAULT_EXPERIMENT_PROFILE),
    "draft_only": {
        "name": "draft_only",
        "semantic_warning_blocking": False,
        "blocked_tools": {
            "sql_syntax_fix",
            "sql_error_classify",
            "sql_schema_validate",
            "sql_join_validate",
            "sql_clause_validate",
            "sql_diff_report",
            "sql_self_correct",
        },
        "max_sql_db_query_calls": 1,
        "disable_hybrid_retrieval": False,
        "disable_schema_linking": False,
    },
    "syntax_only": {
        "name": "syntax_only",
        "semantic_warning_blocking": False,
        "blocked_tools": {
            "sql_error_classify",
            "sql_schema_validate",
            "sql_join_validate",
            "sql_clause_validate",
            "sql_diff_report",
            "sql_self_correct",
        },
        "max_sql_db_query_calls": 2,
        "disable_hybrid_retrieval": False,
        "disable_schema_linking": False,
    },
    "exec_validators": {
        "name": "exec_validators",
        "semantic_warning_blocking": False,
        "blocked_tools": {
            "sql_self_correct",
        },
        "max_sql_db_query_calls": 6,
        "disable_hybrid_retrieval": False,
        "disable_schema_linking": False,
    },
    "no_correction": {
        "name": "no_correction",
        "semantic_warning_blocking": False,
        "blocked_tools": {
            "sql_syntax_fix",
            "sql_error_classify",
            "sql_schema_validate",
            "sql_join_validate",
            "sql_clause_validate",
            "sql_diff_report",
            "sql_self_correct",
        },
        "max_sql_db_query_calls": 1,
        "disable_hybrid_retrieval": False,
        "disable_schema_linking": False,
    },
    "no_hybrid_retrieval": {
        "name": "no_hybrid_retrieval",
        "semantic_warning_blocking": True,
        "blocked_tools": set(),
        "max_sql_db_query_calls": None,
        "disable_hybrid_retrieval": True,
        "disable_schema_linking": False,
    },
    "no_schema_linking": {
        "name": "no_schema_linking",
        "semantic_warning_blocking": True,
        "blocked_tools": set(),
        "max_sql_db_query_calls": None,
        "disable_hybrid_retrieval": False,
        "disable_schema_linking": True,
    },
}

# 结构化纠错事件，供评测脚本读取
_correction_events: dict = {}

# 会话级最终 SQL：correct 通过后由 LLM 显式调用 submit_final_sql 写入，
# 作为 test harness 与 evaluate.py 的判分依据。
# 格式: session_id -> {"sql": str, "reasoning": str, "submitted_at": float}
_final_sql: dict = {}

# 采样示例值时每列最多取的不同值数量
_EXAMPLE_LIMIT = 3

_fallback_schema_candidates: dict = {}

# ==================== 报告内容收集器 ====================
# agent 流式输出的文本会实时追加到此列表中，
# save_report 调用时从中提取报告内容，避免 LLM 在 JSON 参数中传递长文本导致格式出错。
_report_content_collector: list[str] = []


def append_report_content(text: str) -> None:
    """将 agent 输出的文本片段追加到报告内容收集器（由 agent.py 流处理调用）"""
    _report_content_collector.append(text)


def reset_report_content() -> None:
    """重置报告内容收集器（每次新对话/查询前调用）"""
    _report_content_collector.clear()


def get_collected_report_content() -> str:
    """获取收集器中累积的全部文本"""
    return "".join(_report_content_collector)


def _materialize_profile(preset: dict) -> dict:
    """Normalize a preset (or default) into the canonical session-profile dict shape."""
    return {
        "name": preset["name"],
        "semantic_warning_blocking": bool(preset["semantic_warning_blocking"]),
        "blocked_tools": set(preset["blocked_tools"]),
        "max_sql_db_query_calls": preset["max_sql_db_query_calls"],
        "disable_hybrid_retrieval": bool(preset.get("disable_hybrid_retrieval", False)),
        "disable_schema_linking": bool(preset.get("disable_schema_linking", False)),
    }


# 默认 profile 的规范化只读视图：未设置时所有会话共享同一个对象。
# 调用方不应改写返回的 dict（grep 证实没有这种 mutation 模式）。
_DEFAULT_EXPERIMENT_PROFILE_VIEW = _materialize_profile(_DEFAULT_EXPERIMENT_PROFILE)


def set_experiment_profile(profile_name: str = "full", session_id: str = None) -> None:
    """设置会话级实验 profile，用于评测消融实验。"""
    sid = session_id or _get_session_id()
    preset = _EXPERIMENT_PROFILE_PRESETS.get(profile_name, _DEFAULT_EXPERIMENT_PROFILE)
    _session_experiment_profiles[sid] = _materialize_profile(preset)


def get_experiment_profile(session_id: str = None) -> dict:
    """获取当前会话的实验 profile。

    返回会话写入时存储的同一个 dict 引用；调用方不应改写返回值。
    未设置时返回模块级默认 view。
    """
    sid = session_id or _get_session_id()
    return _session_experiment_profiles.get(sid) or _DEFAULT_EXPERIMENT_PROFILE_VIEW


def reset_experiment_profile(session_id: str = None) -> None:
    """重置当前会话的实验 profile。"""
    sid = session_id or _get_session_id()
    _session_experiment_profiles.pop(sid, None)


def _get_effective_linked_schema(session_id: str = None) -> set:
    sid = session_id or _get_session_id()
    linked = set(_linked_schema.get(sid, set()))
    profile = get_experiment_profile(sid)
    if profile.get("disable_schema_linking"):
        linked.update(_fallback_schema_candidates.get(sid, set()))
    return linked


def _remember_schema_candidates(
    columns: Optional[list[tuple[str, str]]] = None,
    *,
    session_id: str = None,
) -> None:
    sid = session_id or _get_session_id()
    if sid not in _fallback_schema_candidates:
        _fallback_schema_candidates[sid] = set()
    for table_name, column_name in columns or []:
        if table_name and column_name:
            _fallback_schema_candidates[sid].add(f"{table_name}.{column_name}")


def _record_correction_event(kind: str, **payload: Any) -> None:
    """记录结构化纠错/诊断事件，供评测脚本分析。"""
    session_id = _get_session_id()
    events = _correction_events.setdefault(session_id, [])
    event = {"kind": kind, "session_id": session_id}
    event.update(payload)
    events.append(event)


def get_correction_events(session_id: str = None) -> list[dict]:
    """读取当前会话的结构化纠错事件。"""
    sid = session_id or _get_session_id()
    return [dict(item) for item in _correction_events.get(sid, [])]


def reset_correction_events(session_id: str = None) -> None:
    """清空当前会话的结构化纠错事件。"""
    sid = session_id or _get_session_id()
    _correction_events.pop(sid, None)


def get_final_sql(session_id: str = None) -> dict:
    """读取当前会话由 submit_final_sql 显式提交的最终 SQL。"""
    sid = session_id or _get_session_id()
    return dict(_final_sql.get(sid, {}))


def reset_final_sql(session_id: str = None) -> None:
    """清空当前会话的最终 SQL（每次新查询前调用）。"""
    sid = session_id or _get_session_id()
    _final_sql.pop(sid, None)


def set_database_uri(database_uri: str, session_id: str = None):
    """
    设置数据库连接 URI（供 agent.py 调用）

    Args:
        database_uri: SQLAlchemy 格式的数据库 URI，
                      例如 'mysql+pymysql://user:pass@host:port/db'
        session_id: 可选的会话 ID，用于工具调用管理
    """
    global _db_instance, _session_id
    old_db = _db_instance
    url = make_url(database_uri)
    if url.get_backend_name() == "sqlite":
        engine = create_engine(database_uri)

        @event.listens_for(engine, "connect")
        def _read_only_sqlite(connection, _record):
            connection.execute("PRAGMA query_only = ON")
            # mode=ro protects the source file; query_only also protects attached
            # or temporary databases. Forbid ATTACH to keep each question scoped.
            import sqlite3
            denied = {getattr(sqlite3, name) for name in (
                "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE",
                "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX",
                "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW",
                "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_CREATE_VTABLE",
                "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX",
                "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW",
                "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW", "SQLITE_DROP_VTABLE",
                "SQLITE_ALTER_TABLE", "SQLITE_REINDEX", "SQLITE_ANALYZE",
            )}
            metadata_pragmas = {"table_info", "table_xinfo", "index_info", "index_xinfo", "index_list", "foreign_key_list"}
            def authorize(action, arg1, arg2, _db, _trigger):
                if action in denied:
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA and arg2 is not None and (arg1 or "").lower() not in metadata_pragmas:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)

        @event.listens_for(engine, "before_cursor_execute")
        def _bounded_sqlite(connection, _cursor, _statement, _parameters, _context, _many):
            seconds = float(os.environ.get("SQL_QUERY_TIMEOUT_SECONDS", "30"))
            deadline = time.monotonic() + seconds
            connection.connection.driver_connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline), 10000
            )

        try:
            new_db = SQLDatabase(engine, sample_rows_in_table_info=_EXAMPLE_LIMIT)
        except BaseException:
            engine.dispose()
            raise
    else:
        new_db = SQLDatabase.from_uri(database_uri, sample_rows_in_table_info=_EXAMPLE_LIMIT)
    _db_instance = new_db
    if old_db is not None:
        old_db._engine.dispose()
    _session_id = session_id or "default"
    logger.info(f"数据库已连接: dialect={_db_instance.dialect}, session={_session_id}")


def _get_database() -> Optional[SQLDatabase]:
    """获取数据库连接实例，优先使用 set_database_uri 设置的连接，否则读取环境变量"""
    global _db_instance
    if _db_instance is not None:
        return _db_instance

    database_uri = os.getenv("DATABASE_URI")
    if database_uri:
        _db_instance = SQLDatabase.from_uri(database_uri, sample_rows_in_table_info=_EXAMPLE_LIMIT)
        return _db_instance

    return None


def _get_session_id() -> str:
    """获取当前会话 ID"""
    return _session_id


def _infer_current_db_id(db: Optional[SQLDatabase] = None) -> str:
    """从当前活跃数据库连接推断 db_id；失败时回退到 BIRD_DEV_DB_ID。"""
    current_db = db or _get_database()
    if current_db is not None:
        try:
            dialect = getattr(current_db, "dialect", "")
            raw_db = current_db._engine.url.database or ""
            if dialect == "sqlite" and raw_db:
                db_id = Path(raw_db).stem.strip()
                if db_id:
                    return db_id
            if dialect == "postgresql":
                # BIRD_minidev stores each dataset in its own schema. The
                # physical database name is shared and cannot identify it.
                schema = getattr(current_db, "_schema", None)
                if not schema:
                    with current_db._engine.connect() as conn:
                        schema = conn.execute(sa_text("SELECT current_schema()")).scalar()
                if schema and schema not in ("public", "pg_catalog", "information_schema"):
                    return str(schema).strip()
        except Exception as exc:
            logger.debug("_infer_current_db_id: dialect/url inspection failed: %s", exc)

    return os.getenv("BIRD_DEV_DB_ID", "").strip()


# ==================== 工具调用管理 ====================


def _check_tool_call(tool_name: str, query: Optional[str] = None) -> tuple:
    """
    检查工具调用是否允许

    Returns:
        tuple[bool, str]: (是否允许, 如果不允许则返回原因)
    """
    session_id = _get_session_id()
    profile = get_experiment_profile(session_id)
    manager = get_tool_call_manager()
    if tool_name in profile["blocked_tools"]:
        return False, (
            f"当前实验 profile=`{profile['name']}` 禁止调用工具 `{tool_name}`。"
            "请在允许的纠错范围内完成本轮实验。"
        )
    if tool_name == "sql_db_query" and profile["max_sql_db_query_calls"] is not None:
        ctx = manager.get_session(session_id)
        current_calls = ctx.tool_call_counts.get("sql_db_query", 0)
        if current_calls >= profile["max_sql_db_query_calls"]:
            return False, (
                f"当前实验 profile=`{profile['name']}` 最多允许执行 "
                f"{profile['max_sql_db_query_calls']} 次 `sql_db_query`。"
            )
    correction_limit = os.environ.get("MAX_SQL_QUERY_CALLS")
    if tool_name == "sql_db_query" and correction_limit:
        ctx = manager.get_session(session_id)
        if ctx.tool_call_counts.get("sql_db_query", 0) >= int(correction_limit):
            return False, f"本题 SQL 执行预算已用尽（最多 {correction_limit} 次），请结束并说明失败原因。"
    return manager.check_before_call(session_id, tool_name, query)


def _record_tool_call(tool_name: str, success: bool, query: Optional[str] = None) -> None:
    """记录工具调用"""
    session_id = _get_session_id()
    manager = get_tool_call_manager()
    manager.record_call(session_id, tool_name, success, query)
    logger.debug(f"记录工具调用: tool={tool_name}, success={success}, session={session_id}")


@contextmanager
def _sql_execution_scope(scope_name: str):
    """为内部嵌套 SQL 执行附加来源前缀，例如 sql_self_correct/sql_db_query。"""
    current_scope = _sql_execution_scope_var.get()
    token = _sql_execution_scope_var.set(current_scope + (scope_name,))
    try:
        yield
    finally:
        _sql_execution_scope_var.reset(token)


def _get_sql_execution_source(default_source: str) -> str:
    scope = _sql_execution_scope_var.get()
    if not scope:
        return default_source
    return "/".join(scope + (default_source,))


def _record_sql_execution(
    sql: str,
    state: str,
    row_count: int = 0,
    *,
    source: Optional[str] = None,
    is_probe: bool = False,
) -> None:
    """记录真实执行过的 SQL 轨迹，供测试脚本追溯最终判分 SQL。"""
    session_id = _get_session_id()
    trace = _sql_execution_traces.setdefault(session_id, [])
    trace.append(
        {
            "sql": sql,
            "source": source or _get_sql_execution_source("sql_db_query"),
            "state": state,
            "row_count": int(row_count or 0),
            "is_probe": bool(is_probe),
        }
    )


def get_sql_execution_trace(session_id: str = None) -> list[dict]:
    """获取指定会话的 SQL 执行轨迹。"""
    sid = session_id or _get_session_id()
    return [dict(item) for item in _sql_execution_traces.get(sid, [])]


def reset_sql_execution_trace(session_id: str = None) -> None:
    """重置指定会话的 SQL 执行轨迹。"""
    sid = session_id or _get_session_id()
    _sql_execution_traces.pop(sid, None)


def _is_raw_metadata(text: str) -> bool:
    """检测 value_text 是否为嵌入时存储的原始元数据而非列描述。

    嵌入表中 column 级 record 的 value_text 有时包含的是
    "db=xxx; level=column; table=xxx; column=xxx; type=TEXT; ..." 格式的元数据，
    而不是来自 CSV 的列描述文本。这类文本不应被用作 M-Schema 中的 comment。
    """
    if not text:
        return False
    # 典型元数据模式：包含 "level=column" 或 "db=...;...table=...;...column="
    return bool(re.search(r'\blevel=column\b', text) or
                re.search(r'\bdb=\w+;\s*level=', text))


def _extract_column_description(value_text: str) -> str:
    """从 column 级 value_text 中提取列描述。

    - 若 value_text 为分号分隔的元数据格式（db=...; level=column; ...），
      解析并返回其中的 column_description 字段。
    - 若 value_text 为纯文本（非元数据格式），原样返回以保持向后兼容。
    - 无描述时返回空字符串。
    """
    if not value_text:
        return ""
    if not _is_raw_metadata(value_text):
        return value_text.strip()
    for part in value_text.split(";"):
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        if key.strip() == "column_description":
            return val.strip()
    return ""


# 列名前缀 → 已存储的聚合类型（用于检测预聚合列）
# 易误匹配的前缀（Count→County, Sum→Summary 等）使用 (?![a-zA-Z]) 负向前瞻
_AGG_PREFIX_PATTERNS = [
    (re.compile(r'^Avg', re.IGNORECASE), 'average'),
    (re.compile(r'^Average', re.IGNORECASE), 'average'),
    (re.compile(r'^Mean', re.IGNORECASE), 'mean/average'),
    (re.compile(r'^Max', re.IGNORECASE), 'maximum'),
    (re.compile(r'^Min', re.IGNORECASE), 'minimum'),
    (re.compile(r'^Total', re.IGNORECASE), 'total/sum'),
    (re.compile(r'^Sum(?![a-zA-Z])', re.IGNORECASE), 'sum'),
    (re.compile(r'^Count(?![a-zA-Z])', re.IGNORECASE), 'count'),
    (re.compile(r'^Num(?![a-zA-Z])', re.IGNORECASE), 'count'),
    (re.compile(r'^Pct', re.IGNORECASE), 'percentage'),
    (re.compile(r'^Percent', re.IGNORECASE), 'percentage'),
    (re.compile(r'^Rate(?![a-zA-Z])', re.IGNORECASE), 'rate'),
]

# 仅对数值类型列应用聚合前缀检测，避免对 TEXT 等类型误报
_NUMERIC_TYPE_KEYWORDS = {
    'INT', 'FLOAT', 'REAL', 'DECIMAL', 'NUMERIC',
    'DOUBLE', 'BIGINT', 'SMALLINT', 'TINYINT', 'NUMBER',
}


def _detect_pre_aggregated_hint(col_name: str, col_comment: str = "", col_type: str = "") -> str:
    """检测列名是否暗示该列已存储聚合值，返回语义提示。

    若列名前缀（如 Avg/Max/Total 等）表明该列已是聚合结果，
    返回包含警告的描述文本，防止 LLM 对其重复套用聚合函数。
    仅对数值类型列生效，TEXT/VARCHAR 等非数值列直接跳过。
    无匹配时返回空字符串。
    """
    # 非数值类型列跳过检测（col_type 为空时保持原有行为，兼容未传入类型的场景）
    if col_type:
        type_upper = col_type.upper()
        if not any(kw in type_upper for kw in _NUMERIC_TYPE_KEYWORDS):
            return ""

    for pattern, agg_type in _AGG_PREFIX_PATTERNS:
        if pattern.match(col_name):
            hint = (
                f"⚠ 此列已存储{agg_type}值，直接使用即可，"
                f"无需再套 AVG/SUM/MAX/MIN 等聚合函数"
            )
            if col_comment:
                return f"{col_comment} | {hint}"
            return hint
    return ""


def _detect_null_in_result(result_str: str) -> bool:
    """检测查询结果字符串中是否包含 NULL/None 值。

    检测元组/列表格式中的 None（如 (None,)、('abc', None)），
    同时排除 SQL 关键字 IS NOT NULL / IS NULL / COALESCE 等误报。
    仅检测是否存在 NULL，严重程度由 _is_all_null_result() 判断。
    """
    # 匹配 Python repr 中的 None（元组/列表元素）
    # 例如: (None,)  ('abc', None, 123)  [None, 'x']
    if re.search(r'[\(\[,]\s*None\s*[,\)\]]', result_str):
        return True
    # 匹配独立的 None 作为值（首元素）
    if re.search(r'\(None,', result_str):
        return True
    return False


def _is_all_null_result(result_str: str) -> bool:
    """判断查询结果是否所有值都是 None（完全无效数据）。

    仅当每一行的每一个字段都为 None 时返回 True。
    部分含 NULL 的结果返回 False（视为有效数据）。
    """
    try:
        parsed = ast.literal_eval(result_str)
        if not isinstance(parsed, (list, tuple)) or not parsed:
            return False
        for row in parsed:
            if not isinstance(row, (list, tuple)):
                if row is not None:
                    return False
            else:
                for val in row:
                    if val is not None:
                        return False
        return True
    except (ValueError, SyntaxError):
        rows = re.findall(r'\(([^)]*)\)', result_str)
        if not rows:
            return False
        for row_content in rows:
            values = [v.strip() for v in row_content.split(',') if v.strip()]
            for v in values:
                if v != 'None':
                    return False
        return True


def _detect_order_by_null_trap(sql: str, result_str: str) -> bool:
    """检测 ORDER BY col ASC LIMIT N 导致 NULL 排在最前的陷阱。

    SQLite 中 NULL 在 ASC 排序时排在所有非 NULL 值之前，
    因此 ORDER BY col ASC LIMIT 3 会优先返回 col 为 NULL 的行。
    当 ORDER BY 目标列在所有返回行中都为 NULL 时，返回 True。
    """
    # 仅匹配 ORDER BY ... ASC (或无 DESC) + LIMIT 的模式
    ob_match = re.search(
        r'ORDER\s+BY\s+(.+?)\s+(ASC\b|(?=LIMIT\b))',
        sql, re.IGNORECASE | re.DOTALL,
    )
    if not ob_match:
        return False
    if not re.search(r'\bLIMIT\b', sql, re.IGNORECASE):
        return False

    # 确定 ORDER BY 列在 SELECT 结果中的位置索引
    # 提取 SELECT 列列表
    sel_match = re.search(r'SELECT\s+(.*?)\s+FROM\b', sql, re.IGNORECASE | re.DOTALL)
    if not sel_match:
        return False
    select_clause = sel_match.group(1)
    ob_col_raw = ob_match.group(1).strip().rstrip(',')

    # 规范化列名用于比较：去掉引号和表别名前缀
    def _norm(c: str) -> str:
        c = c.strip().strip('`"[]')
        # 去掉 table.或 alias. 前缀
        if '.' in c:
            c = c.split('.')[-1]
        return c.lower()

    ob_col_norm = _norm(ob_col_raw)

    # 简单拆分 SELECT 列（不处理嵌套括号内的逗号）
    depth = 0
    parts: list[str] = []
    buf: list[str] = []
    for ch in select_clause:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append(''.join(buf).strip())

    # 匹配 ORDER BY 列 → SELECT 列索引
    col_idx = -1
    for i, part in enumerate(parts):
        # 处理 AS alias
        as_match = re.search(r'\bAS\s+[`"]?(\w+)[`"]?\s*$', part, re.IGNORECASE)
        alias = as_match.group(1) if as_match else None
        raw_name = re.sub(r'\s+AS\s+\S+\s*$', '', part, flags=re.IGNORECASE).strip()

        if alias and _norm(alias) == ob_col_norm:
            col_idx = i
            break
        if _norm(raw_name) == ob_col_norm:
            col_idx = i
            break
        # 对含函数的情况，比较原始文本
        if ob_col_raw.strip().lower() in part.lower():
            col_idx = i
            break

    if col_idx == -1:
        # ORDER BY 列不在 SELECT 列表中 → 无法检测
        return False

    # 解析结果并检查目标列是否全部为 None
    try:
        parsed = ast.literal_eval(result_str)
        if not isinstance(parsed, (list, tuple)) or not parsed:
            return False
        for row in parsed:
            if not isinstance(row, (list, tuple)):
                row = (row,)
            if col_idx < len(row) and row[col_idx] is not None:
                return False
        return True
    except (ValueError, SyntaxError):
        return False


def _detect_integer_division(sql: str) -> list:
    """检测 SQL 中可能的 SQLite 整数除法截断问题。

    借鉴 SQL-of-Thought 错误分类法，扩展 SCoT2S 增加 Arithmetic 错误类别。
    仅在 SQLite 方言下检测：INTEGER / INTEGER 会截断小数部分（如 3/5=0）。

    Returns:
        list of dict: 每个 dict 包含 left_operand, right_operand, suggestion
    """
    db = _get_database()
    if db is None or db.dialect != "sqlite":
        return []

    # 已有 CAST(... AS REAL) 或 * 1.0 的除法不需要告警
    safe_patterns = [
        r'CAST\s*\([^)]*\bAS\s+REAL\b[^)]*\)\s*/',
        r'/\s*CAST\s*\([^)]*\bAS\s+REAL\b[^)]*\)',
        r'\*\s*1\.0\s*/',
        r'/\s*\([^)]*\*\s*1\.0\)',
    ]

    sql_check = sql
    for pat in safe_patterns:
        sql_check = re.sub(pat, ' __SAFE_DIV__ ', sql_check, flags=re.IGNORECASE)

    # 提取未保护的除法: col_a / col_b（含可选的 table.前缀）
    div_pattern = re.compile(
        r'(?:(["`]?\w+["`]?)\s*\.\s*)?'     # 可选的 table.
        r'(["`]?\w+["`]?)'                   # left column
        r'\s*/\s*'                            # division operator
        r'(?:(["`]?\w+["`]?)\s*\.\s*)?'      # 可选的 table.
        r'(["`]?\w+["`]?)',                   # right column
        re.IGNORECASE
    )

    issues = []
    for m in div_pattern.finditer(sql_check):
        if '__SAFE_DIV__' in m.group(0):
            continue

        l_table, l_col = m.group(1), m.group(2)
        r_table, r_col = m.group(3), m.group(4)

        # 排除非列名（SQL关键字、数字常量）
        skip_words = {'AS', 'FROM', 'WHERE', 'AND', 'OR', 'ON', 'BY',
                      'SELECT', 'LIMIT', 'ORDER', 'GROUP', 'HAVING', 'CAST',
                      'REAL', 'INTEGER', 'TEXT', 'FLOAT', 'NULL', 'DISTINCT'}
        if l_col.strip('"`').upper() in skip_words or r_col.strip('"`').upper() in skip_words:
            continue
        if l_col.strip('"`').isdigit() or r_col.strip('"`').isdigit():
            continue

        # 查询列类型
        l_is_int = _is_column_integer(l_table, l_col)
        r_is_int = _is_column_integer(r_table, r_col)

        if l_is_int and r_is_int:
            left_full = f"{l_table}.{l_col}" if l_table else l_col
            right_full = f"{r_table}.{r_col}" if r_table else r_col
            left_clean = left_full.strip('"`')
            right_clean = right_full.strip('"`')
            issues.append({
                "left_operand": left_clean,
                "right_operand": right_clean,
                "suggestion": f"CAST({left_clean} AS REAL) / {right_clean}",
            })

    return issues


def _is_column_integer(table_hint, col_name: str) -> bool:
    """检查给定列是否为 INTEGER 类型。优先使用 session 缓存，回退到 Inspector。"""
    col_name_clean = col_name.strip('"`')

    # 先查 _col_descriptions 缓存
    session_id = _get_session_id()
    cached = _col_descriptions.get(session_id, {})
    if table_hint:
        table_clean = table_hint.strip('"`')
        key = f"{table_clean}.{col_name_clean}"
        entry = cached.get(key, {})
        if entry.get("type"):
            return "INT" in entry["type"].upper()
    else:
        for k, v in cached.items():
            if k.endswith(f".{col_name_clean}"):
                if v.get("type") and "INT" in v["type"].upper():
                    return True

    # 回退到 Inspector
    db = _get_database()
    if db is None:
        return False
    try:
        insp = sa_inspect(db._engine)
        tables_to_check = []
        if table_hint:
            tables_to_check.append(table_hint.strip('"`'))
        else:
            tables_to_check = db.get_usable_table_names()

        for tbl in tables_to_check:
            try:
                for col in insp.get_columns(tbl):
                    if col["name"].lower() == col_name_clean.lower():
                        ctype = str(col["type"]).upper()
                        return "INT" in ctype
            except Exception as exc:
                logger.debug("_is_integer_column: get_columns failed for %s: %s", tbl, exc)
                continue
    except Exception as exc:
        logger.debug("_is_integer_column: inspector failed: %s", exc)
    return False


def _detect_suspicious_results(sql: str, result_str: str) -> list:
    """检测查询结果中的语义可疑模式（借鉴 AGENTICS 2.0 类型安全思想）。

    即使 SQL 执行成功，也对结果做启发式语义验证，
    将静默错误转化为显式警告。

    Returns:
        list of str: 警告信息列表
    """
    warnings = []

    # 一次性解析 result_str，三项检查共享。解析失败时全部检查直接跳过。
    try:
        parsed = ast.literal_eval(result_str)
    except (ValueError, SyntaxError, TypeError):
        return warnings
    if not isinstance(parsed, (list, tuple)):
        return warnings

    sql_lower = sql.lower()

    # 检查 1: SQL 含除法表达式 + 结果中对应值全为 0
    has_division = bool(re.search(r'\w+\s*/\s*\w+', sql))
    if has_division and parsed:
        # 检查每一行中是否有计算列全为 0
        all_rows_have_zero_ratio = True
        checked = False
        for row in parsed:
            if not isinstance(row, (list, tuple)):
                row = (row,)
            for val in row:
                if isinstance(val, (int, float)):
                    checked = True
                    if val != 0:
                        all_rows_have_zero_ratio = False
                        break
            if not all_rows_have_zero_ratio:
                break

        if checked and all_rows_have_zero_ratio and len(parsed) > 0:
            warnings.append(
                "SQL 包含除法运算但所有数值结果为 0，"
                "高度可疑为 SQLite 整数除法截断（INTEGER / INTEGER = 0）。"
                "请检查是否需要 CAST(numerator AS REAL) / denominator。"
            )

        # 检查 1b: 除法结果存在 > 1 的值（问题语义是比率/百分比时高度可疑）
        ratio_hint = any(kw in sql_lower for kw in (
            "rate", "ratio", "percent", "pct", "percentage", "proportion"
        ))
        if ratio_hint:
            try:
                out_of_range = []
                for row in parsed:
                    if not isinstance(row, (list, tuple)):
                        row = (row,)
                    for val in row:
                        if isinstance(val, float) and (val > 1.0 or val < 0):
                            out_of_range.append(val)
                if out_of_range:
                    warnings.append(
                        f"SQL 语义是比率/百分比，但结果中存在超出 [0,1] 区间的值（示例: {out_of_range[:3]}）。"
                        "请检查分子分母是否颠倒、或是否漏乘/漏除某个分母。"
                    )
            except Exception as exc:
                logger.debug("_detect_suspicious_results ratio scan failed: %s", exc)

    # 检查 2: 单值结果为 None/NULL（聚合函数作用在空集上的典型症状）
    if len(parsed) == 1:
        row = parsed[0]
        if not isinstance(row, (list, tuple)):
            row = (row,)
        if row and all(v is None for v in row):
            warnings.append(
                "聚合查询返回的唯一行所有值均为 NULL，通常表示 WHERE / JOIN 过滤后候选集为空，"
                "导致 MAX/MIN/AVG/SUM 作用在空集上。请检查过滤条件或值匹配是否正确。"
            )

    # 检查 3: GROUP BY 结果中的目标聚合列全部相同，常见于分组粒度错误
    has_group = " group by " in sql_lower
    has_agg = bool(re.search(r'\b(count|sum|avg|max|min)\s*\(', sql_lower))
    if has_group and has_agg and len(parsed) > 1:
        normalized_rows = []
        row_width = None
        for row in parsed:
            if not isinstance(row, (list, tuple)):
                row = (row,)
            normalized_rows.append(tuple(row))
            row_width = len(row) if row_width is None else row_width
        if row_width:
            for col_idx in range(row_width):
                col_values = [row[col_idx] for row in normalized_rows if col_idx < len(row)]
                if len(col_values) > 1 and len(set(col_values)) == 1:
                    warnings.append(
                        f"GROUP BY 查询返回了多行，但第 {col_idx + 1} 列在所有结果中完全相同，"
                        "疑似分组粒度错误或聚合对象选错。请检查 SELECT 与 GROUP BY 是否对齐。"
                    )
                    break

    return warnings


def _count_result_rows(result) -> int:
    """计算查询结果的行数。支持 list/tuple 和字符串格式。"""
    if isinstance(result, (list, tuple)):
        return len(result)
    # 字符串格式：按行分割，过滤空行
    result_str = str(result).strip()
    if not result_str:
        return 0
    # 尝试用 ast.literal_eval 解析为 list
    try:
        parsed = ast.literal_eval(result_str)
        if isinstance(parsed, (list, tuple)):
            return len(parsed)
    except (ValueError, SyntaxError):
        pass
    # 回退：按换行符计数
    return len([line for line in result_str.split('\n') if line.strip()])


def _truncate_result(result, max_rows: int):
    """截断查询结果到指定行数。"""
    if isinstance(result, (list, tuple)):
        return result[:max_rows]
    # 字符串格式
    result_str = str(result).strip()
    try:
        parsed = ast.literal_eval(result_str)
        if isinstance(parsed, (list, tuple)):
            return str(parsed[:max_rows])
    except (ValueError, SyntaxError):
        pass
    # 回退：按换行符截断
    lines = result_str.split('\n')
    return '\n'.join(lines[:max_rows])


# ==================== 工具函数 ====================


def sql_db_list_tables() -> str:
    """列出数据库中的所有表名。"""
    db = _get_database()
    if db is None:
        return "错误: 数据库未连接，请设置 DATABASE_URI 环境变量"

    allowed, reason = _check_tool_call("sql_db_list_tables")
    if not allowed:
        return reason

    try:
        tables = db.get_usable_table_names()
        if not tables:
            _record_tool_call("sql_db_list_tables", True)
            dialect = db.dialect
            if dialect == "postgresql":
                return (
                    "当前 Schema 中没有表。\n\n"
                    "**可能原因：** 你尚未切换到目标 Schema。\n"
                    "请先使用 `db_search` 工具搜索并切换到包含数据的 Schema，然后再调用此工具。"
                )
            else:
                return "当前数据库中没有表。请检查数据库连接是否正确。"

        _record_tool_call("sql_db_list_tables", True)

        # 获取表注释
        inspector = sa_inspect(db._engine)
        result_lines = ["数据库中有以下表：\n"]
        for table_name in sorted(tables):
            try:
                comment = (inspector.get_table_comment(table_name).get("text", "") or "")
            except Exception as exc:
                logger.debug("sql_db_list_tables: comment fetch failed for %s: %s", table_name, exc)
                comment = ""
            if comment:
                result_lines.append(f"- {table_name}: {comment}")
            else:
                result_lines.append(f"- {table_name}")

        result_lines.append("\n✅ 表列表已获取完成。如需查看表结构，请使用 sql_db_schema 工具。")
        return "\n".join(result_lines)

    except Exception as e:
        _record_tool_call("sql_db_list_tables", False)
        logger.error(f"列出表失败: {e}", exc_info=True)
        return f"列出表失败: {str(e)[:100]}"


def sql_db_schema(table_names: str) -> str:
    """
    获取指定表的架构信息，包括列名、数据类型、注释和示例值。

    Args:
        table_names: 表名，可以是单个表名或多个表名（用逗号分隔）
    """
    db = _get_database()
    if db is None:
        return "错误: 数据库未连接，请设置 DATABASE_URI 环境变量"

    allowed, reason = _check_tool_call("sql_db_schema")
    if not allowed:
        return reason

    try:
        inspector = sa_inspect(db._engine)
        dialect = db.dialect

        # 自动发现 Bird 风格列描述目录
        # 优先级：环境变量 DESCRIPTION_DIR > 数据库文件同级 database_description/
        retriever = None
        try:
            description_dir = os.environ.get("DESCRIPTION_DIR", "").strip()
            if not description_dir:
                raw_db = db._engine.url.database or ""
                if raw_db and os.path.isfile(raw_db):
                    candidate = os.path.join(
                        os.path.dirname(os.path.abspath(raw_db)),
                        "database_description",
                    )
                    if os.path.isdir(candidate):
                        description_dir = candidate
            pass
        except Exception as e:
            logger.debug(f"sql_db_schema: 列描述目录加载跳过: {e}")
        retriever = None  # DescriptionRetriever 已停用，列描述由向量检索提供

        # 解析表名
        if isinstance(table_names, str):
            table_list = [t.strip() for t in table_names.split(",")]
        else:
            table_list = [table_names]

        schema_parts = []
        for table_name in table_list:
            try:
                col_meta_list = inspector.get_columns(table_name)
            except Exception as exc:
                logger.debug("sql_db_schema: get_columns failed for %s: %s", table_name, exc)
                schema_parts.append(f"表 '{table_name}' 不存在")
                continue

            # 表注释
            try:
                table_comment = inspector.get_table_comment(table_name).get("text", "") or ""
            except Exception as exc:
                logger.debug("sql_db_schema: comment fetch failed for %s: %s", table_name, exc)
                table_comment = ""

            # 采样每列的示例值
            col_examples: dict = {}
            try:
                with db._engine.connect() as conn:
                    for col in col_meta_list:
                        col_name = col["name"]
                        quoted = f'"{col_name}"' if dialect in ("postgresql", "oracle", "sqlite") else f'`{col_name}`'
                        quoted_table = f'"{table_name}"' if dialect in ("postgresql", "oracle", "sqlite") else f'`{table_name}`'
                        if dialect == "mssql":
                            stmt = f"SELECT DISTINCT TOP {_EXAMPLE_LIMIT} {quoted} FROM {quoted_table} WHERE {quoted} IS NOT NULL"
                        elif dialect == "oracle":
                            stmt = f'SELECT DISTINCT {quoted} FROM "{table_name.upper()}" WHERE {quoted} IS NOT NULL AND ROWNUM <= {_EXAMPLE_LIMIT}'
                        else:
                            stmt = f"SELECT DISTINCT {quoted} FROM {quoted_table} WHERE {quoted} IS NOT NULL LIMIT {_EXAMPLE_LIMIT}"
                        try:
                            rows = conn.execute(sa_text(stmt)).fetchall()
                            col_examples[col_name] = [str(r[0]) for r in rows if r[0] is not None]
                        except Exception as exc:
                            logger.debug("sql_db_schema: sample fetch failed for %s.%s: %s", table_name, col_name, exc)
                            col_examples[col_name] = []
            except Exception as exc:
                logger.debug("sql_db_schema: sampling outer connect failed for %s: %s", table_name, exc)

            # 外键
            foreign_keys = []
            try:
                for fk in inspector.get_foreign_keys(table_name):
                    ref_table = fk.get("referred_table", "")
                    for lc, rc in zip(
                        fk.get("constrained_columns", []),
                        fk.get("referred_columns", []),
                    ):
                        foreign_keys.append(f"{table_name}.{lc} = {ref_table}.{rc}")
            except Exception as exc:
                logger.debug("sql_db_schema: foreign keys failed for %s: %s", table_name, exc)

            # 构建 Markdown 表格输出
            schema_text = f"\n### {table_name} 表 Schema\n"
            if table_comment:
                schema_text += f"\n> {table_comment}\n"

            schema_text += "\n| 列名 | 数据类型 | 描述 | 示例值 |"
            schema_text += "\n|------|----------|------|--------|"
            for col in col_meta_list:
                col_name = col["name"]
                col_type = str(col["type"])
                col_comment = col.get("comment", "") or ""
                # 用向量检索缓存的列描述补充（DescriptionRetriever 已停用）
                if not col_comment:
                    cached_entry = _col_descriptions.get(_get_session_id(), {}).get(
                        f"{table_name}.{col_name}", {}
                    )
                    if cached_entry.get("comment"):
                        col_comment = cached_entry["comment"]
                # 预聚合列语义提示
                enriched = _detect_pre_aggregated_hint(col_name, col_comment)
                if enriched:
                    col_comment = enriched
                examples = col_examples.get(col_name, [])
                examples_str = ", ".join(examples[:_EXAMPLE_LIMIT]) if examples else ""
                schema_text += f"\n| {col_name} | {col_type} | {col_comment} | {examples_str} |"

            if foreign_keys:
                schema_text += "\n\n**外键关系：**"
                for fk in foreign_keys:
                    schema_text += f"\n- {fk}"

            schema_parts.append(schema_text)

        _record_tool_call("sql_db_schema", True)

        result = "\n".join(schema_parts) if schema_parts else "未找到表信息"
        result += "\n\n✅ 表架构已获取完成。请基于此信息编写 SQL 查询，无需重复获取架构。"
        return result
        
    except Exception as e:
        _record_tool_call("sql_db_schema", False)
        logger.error(f"获取表架构失败: {e}", exc_info=True)
        return f"获取表架构失败: {str(e)[:100]}"


_SQL_LEXEME = re.compile(
    r"--[^\r\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
    r"`(?:``|[^`])*`|\[(?:\]\]|[^\]])*\]|[A-Za-z_][A-Za-z0-9_$]*|[^\s]"
)


def _read_only_query_error(query: str) -> str:
    """Check lexical SQL tokens, preserving keywords inside strings/identifiers.

    This is a conservative statement gate, not a SQL parser. Actual SQLite
    read-only enforcement is provided by mode=ro, query_only and its authorizer.
    """
    tokens = []
    for match in _SQL_LEXEME.finditer(query):
        token = match.group()
        if token.startswith(("--", "/*")):
            continue
        tokens.append(token)
    if not tokens or tokens[0].upper() not in {"SELECT", "WITH"}:
        return "错误: 只允许 SELECT 或 WITH ... SELECT 查询"
    if ";" in tokens[:-1]:
        return "错误: 每次只允许执行单条 SELECT 查询"
    words = {token.upper() for token in tokens if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", token)}
    forbidden = words & {
        "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE",
        "ATTACH", "DETACH", "VACUUM", "PRAGMA", "REINDEX", "ANALYZE", "INTO", "COPY",
        "GRANT", "REVOKE", "EXEC", "EXECUTE", "CALL", "LOAD_EXTENSION", "WRITEFILE",
    }
    if forbidden:
        return f"错误: 不允许执行 {sorted(forbidden)[0]} 操作，只允许 SELECT 查询"
    for index, token in enumerate(tokens):
        if token.upper() == "REPLACE" and (index + 1 == len(tokens) or tokens[index + 1] != "("):
            return "错误: 不允许执行 REPLACE 操作，只允许 SELECT 查询"
    if "SELECT" not in words:
        return "错误: WITH 必须用于只读 SELECT 查询"
    return ""


def sql_db_query(query: str) -> str:
    """
    执行 SQL SELECT 查询并返回结果。
    只允许执行 SELECT 查询，不允许执行 INSERT、UPDATE、DELETE、DROP 等操作。
    
    Args:
        query: 要执行的 SQL 查询语句
    """
    db = _get_database()
    if db is None:
        return "错误: 数据库未连接，请设置 DATABASE_URI 环境变量"

    # 安全检查：只允许 SELECT 查询
    safety_error = _read_only_query_error(query)
    if safety_error:
        return safety_error

    # 检查是否允许调用（包含重复查询检测）
    allowed, reason = _check_tool_call("sql_db_query", query)
    if not allowed:
        return reason

    # 与链接架构构建阶段保持一致：自动为含特殊字符（空格/括号等）的标识符补充双引号，
    # 避免召回阶段已确认的列在执行阶段因为引用方式不同而失败
    quoted_query = _auto_quote_sql_identifiers(query)

    logger.info(f"执行 SQL 查询:\n{quoted_query[:500]}")

    try:
        result = db.run(quoted_query)
        _record_tool_call("sql_db_query", True, quoted_query)

        if not result:
            _record_sql_execution(quoted_query, "rejected", row_count=0)
            return (
                "⚠️ 查询执行成功，但返回 0 行结果（empty）。\n\n"
                "❌ **不允许把『无数据』当作最终答案直接回复用户**。空结果通常意味着 SQL 逻辑错误，常见原因：\n"
                "1. WHERE 过滤条件过严或值拼写不匹配（注意大小写、空格、引号）\n"
                "2. JOIN 关联条件错误，导致连接结果为空\n"
                "3. 子查询/HAVING 过滤掉了所有候选\n"
                "4. 公式分母筛选（如 `> 0`）剔除了所有数据\n\n"
                "👉 请按下列顺序处理：\n"
                "- 先放宽 WHERE / HAVING 重新执行确认是否为数据问题\n"
                "- 若仍为空，使用 `sql_db_value_lookup` 复查值是否存在\n"
                "- 若问题确实没有匹配数据，再向用户**显式说明**理由（不要简单输出『无数据』）"
            )

        # 检测结果行数，对大结果集进行截断
        total_rows = _count_result_rows(result)
        MAX_DISPLAY_ROWS = 50
        truncated = False
        display_result = result
        if total_rows > MAX_DISPLAY_ROWS:
            truncated = True
            display_result = _truncate_result(result, MAX_DISPLAY_ROWS)

        # 程序化检测结果中的 NULL/None 值
        result_str_raw = str(result)
        has_null = _detect_null_in_result(result_str_raw)

        if has_null and _is_all_null_result(result_str_raw):
            # 所有值均为 NULL — 无效数据，拒绝并要求重写
            null_display = display_result if truncated else result
            msg = (
                f"⚠️ 查询已执行（共 {total_rows} 行），但所有结果值均为 NULL:\n\n{null_display}\n\n"
                "❌ 查询返回的数据全部为 NULL，无有效信息。\n"
                "你必须修改 SQL 来获取有效数据，然后重新执行查询。\n\n"
                "⚠️ 重要：优先使用 WHERE ... IS NOT NULL 过滤掉数据缺失的行，"
                "而不是用 COALESCE 把 NULL 替换成 0。"
                "COALESCE(col, 0) 会把数据缺失伪装成值为0，导致错误结论。\n\n"
                "请修改 SQL 后重新调用 sql_db_query。"
            )
            _record_sql_execution(quoted_query, "rejected", row_count=total_rows)
            return msg

        # ORDER BY ASC + LIMIT 的 NULL 排序陷阱检测
        # SQLite 中 NULL 在 ASC 排序时排在最前，LIMIT 会优先选出 NULL 行
        if has_null and _detect_order_by_null_trap(quoted_query, result_str_raw):
            null_display = display_result if truncated else result
            msg = (
                f"⚠️ 查询已执行（共 {total_rows} 行），但 ORDER BY 目标列在所有返回行中均为 NULL:\n\n"
                f"{null_display}\n\n"
                "❌ 这是 SQLite 的 NULL 排序陷阱：NULL 在 ASC 排序时排在所有非 NULL 值之前，\n"
                "导致 ORDER BY col ASC LIMIT N 优先返回 col 为 NULL 的无效行。\n\n"
                "你必须在 WHERE 子句中添加 IS NOT NULL 过滤条件来排除 NULL 行，然后重新执行查询。\n"
                "例如：WHERE target_column IS NOT NULL ORDER BY target_column ASC LIMIT N\n\n"
                "请修改 SQL 后重新调用 sql_db_query。"
            )
            _record_sql_execution(quoted_query, "rejected", row_count=total_rows)
            return msg

        # 构建成功消息（部分含 NULL 的结果正常返回，附加提示）
        null_note = ""
        if has_null:
            null_note = (
                "\n\n⚠️ 注意：结果中包含部分 NULL 值，这可能代表数据缺失。"
                "请在分析时注意区分 NULL 和有效数据。"
                "如果 NULL 影响了分析结论，可考虑添加 WHERE ... IS NOT NULL 过滤。"
            )

        row_info = f"（共 {total_rows} 行）"
        if truncated:
            result_str = (
                f"✅ 查询成功 {row_info}:\n\n"
                f"（以下仅显示前 {MAX_DISPLAY_ROWS} 行，共 {total_rows} 行）\n\n"
                f"{display_result}{null_note}\n\n"
                f"⚠️ 结果共 {total_rows} 行，超过显示上限，已截断为前 {MAX_DISPLAY_ROWS} 行。\n"
                f"你看到的只是部分数据。在回答用户时请注明数据总量为 {total_rows} 行。\n"
                "如需精确统计（如总数、汇总），请使用 COUNT/SUM/AVG 等聚合查询而非逐行列举。"
            )
        else:
            result_str = f"✅ 查询成功 {row_info}:\n\n{result}{null_note}"
            result_str += "\n\n✅ 数据质量检查通过。请基于以上结果进行分析回答。"

        # 执行后语义验证：把“能执行但大概率不符合查询意图”的结果转换为显式拒绝
        semantic_warnings = _detect_suspicious_results(quoted_query, result_str_raw)
        if semantic_warnings:
            profile = get_experiment_profile()
            warning_text = "\n".join(f"- {w}" for w in semantic_warnings)
            _record_correction_event(
                "semantic_result",
                sql=quoted_query,
                warnings=list(semantic_warnings),
                blocking=bool(profile["semantic_warning_blocking"]),
                row_count=total_rows,
            )
            _record_sql_execution(
                quoted_query,
                "accepted_with_warning",
                row_count=total_rows,
            )
            if not profile["semantic_warning_blocking"]:
                return (
                    f"{result_str}\n\n"
                    "⚠️ 当前实验配置记录了语义风险，但不将其作为阻断条件。\n"
                    f"⚠️ **计算结果语义警告**:\n{warning_text}"
                )
            return (
                "⚠️ 查询已执行，但结果未通过语义过滤。\n\n"
                f"{result_str}\n\n"
                "❌ 当前结果很可能不能直接回答用户问题，请继续修正 SQL。\n\n"
                f"⚠️ **计算结果语义警告**:\n{warning_text}\n\n"
                "请根据上述警告修改 SQL 后重新调用 sql_db_query。"
            )

        _record_sql_execution(quoted_query, "accepted", row_count=total_rows)
        return result_str

    except Exception as e:
        _record_tool_call("sql_db_query", False, quoted_query)
        error_msg = str(e)
        if len(error_msg) > 200:
            error_msg = error_msg[:200] + "..."
        logger.error(f"SQL 查询失败: {error_msg}")
        error_lower = str(e).lower()
        error_kind = "execution"
        if any(token in error_lower for token in ("syntax error", "unrecognized token", "misuse of aggregate")):
            error_kind = "syntax"
        elif any(token in error_lower for token in ("no such table", "no such column", "does not exist", "undefinedtable", "undefinedcolumn")):
            error_kind = "schema"
        _record_correction_event(
            "sql_error",
            sql=quoted_query,
            error_kind=error_kind,
            error_msg=error_msg,
        )
        _record_sql_execution(quoted_query, "error", row_count=0)

        # 针对"表不存在"错误给出更具体的指导
        if "undefinedtable" in error_lower or "does not exist" in error_lower or "relation" in error_lower or "no such table" in error_lower:
            dialect = db.dialect if db else "unknown"
            if dialect == "postgresql":
                return (
                    f"SQL 执行失败: {error_msg}\n\n"
                    "**表不存在，可能的原因及解决方法：**\n"
                    "1. **未切换到正确的 Schema** — 请先使用 `db_search` 工具搜索并切换到包含目标表的 Schema\n"
                    "2. **PostgreSQL 表名大小写问题** — 表名包含大写字母或中文时，必须用双引号包裹，例如：\n"
                    '   `SELECT * FROM "IMAX电影院"` 而不是 `SELECT * FROM IMAX电影院`\n'
                    "3. **表名拼写错误** — 请使用 `sql_db_list_tables` 确认正确的表名\n\n"
                    "⚠️ 请不要重复执行相同的失败 SQL，先排查上述原因后再重试。"
                )
            else:
                return (
                    f"SQL 执行失败: {error_msg}\n\n"
                    "**表不存在，可能的原因及解决方法：**\n"
                    "1. **表名拼写错误** — 请使用 `sql_db_list_tables` 确认正确的表名\n"
                    "2. **列名含特殊字符** — 包含空格、括号等特殊字符时需用反引号包裹（SQLite/MySQL）\n"
                    "   例如: SELECT `Free Meal Count (K-12)` FROM frpm\n\n"
                    "⚠️ 请不要重复执行相同的失败 SQL，先排查上述原因后再重试。"
                )

        return (
            f"SQL 执行失败: {error_msg}\n\n"
            "请检查 SQL 语法和表结构是否正确。"
            "如果之前已获取表架构，请直接使用已有信息，无需重复查询。"
        )


def sql_db_query_multi(candidates: str) -> str:
    """
    批量试探多个候选 SQL 查询，选出最佳候选后正式执行。

    适用场景：当问题存在多种合理的 SQL 写法时（如比率计算是否需要 CAST、
    使用 Evidence 公式列还是预聚合列），生成 2-3 个候选 SQL 同时测试。

    候选 SQL 之间用 --- 分隔。每个候选会先经过自动语法修复
    （引号、大小写、整数除法），再试探执行。

    选择策略：
    - 仅 1 个成功 → 正式执行该候选
    - 多个成功 → 正式执行非 NULL 数据行最多的候选
    - 全部失败 → 返回所有错误信息，建议使用 sql_self_correct 修正

    最终选定的候选会通过 sql_db_query 正式执行，确保测试框架可追踪。

    Args:
        candidates: 用 --- 分隔的多个 SQL 候选查询
    """
    db = _get_database()
    if db is None:
        return "错误: 数据库未连接"

    allowed, reason = _check_tool_call("sql_db_query_multi")
    if not allowed:
        return reason

    # 解析候选 SQL
    sql_list = [s.strip() for s in candidates.split("---") if s.strip()]
    if not sql_list:
        _record_tool_call("sql_db_query_multi", False)
        return "错误: 未提供有效的候选 SQL。请用 --- 分隔多个 SQL 查询。"
    if len(sql_list) > 3:
        sql_list = sql_list[:3]  # 最多 3 个候选

    probe_results = []  # (index, success, sql_used, row_count, error_msg)

    for i, raw_sql in enumerate(sql_list):
        # 安全检查
        upper = raw_sql.strip().upper()
        if not upper.startswith("SELECT"):
            probe_results.append((i, False, raw_sql, 0, "只允许 SELECT 查询"))
            continue

        # 语法修复（静默，不经过工具调用计数）
        fixed_sql = _auto_quote_sql_identifiers(raw_sql)
        int_issues = _detect_integer_division(fixed_sql)
        for issue in int_issues:
            left_escaped = re.escape(issue["left_operand"])
            right_escaped = re.escape(issue["right_operand"])
            div_pat = re.compile(left_escaped + r'\s*/\s*' + right_escaped, re.IGNORECASE)
            fixed_sql = div_pat.sub(issue["suggestion"], fixed_sql, count=1)

        # 试探执行（直接 db.run，不计入 sql_db_query 工具调用）
        try:
            result = db.run(fixed_sql)
            if not result:
                _record_sql_execution(
                    fixed_sql,
                    "accepted",
                    row_count=0,
                    source=_get_sql_execution_source("sql_db_query_multi"),
                    is_probe=True,
                )
                probe_results.append((i, True, fixed_sql, 0, ""))
            else:
                result_str = str(result)
                if _is_all_null_result(result_str):
                    _record_sql_execution(
                        fixed_sql,
                        "rejected",
                        row_count=_count_result_rows(result),
                        source=_get_sql_execution_source("sql_db_query_multi"),
                        is_probe=True,
                    )
                    probe_results.append((i, False, fixed_sql, 0, "结果全部为 NULL"))
                else:
                    row_count = _count_result_rows(result)
                    _record_sql_execution(
                        fixed_sql,
                        "accepted",
                        row_count=row_count,
                        source=_get_sql_execution_source("sql_db_query_multi"),
                        is_probe=True,
                    )
                    probe_results.append((i, True, fixed_sql, row_count, ""))
        except Exception as e:
            _record_sql_execution(
                fixed_sql,
                "error",
                row_count=0,
                source=_get_sql_execution_source("sql_db_query_multi"),
                is_probe=True,
            )
            probe_results.append((i, False, fixed_sql, 0, str(e)[:200]))

    _record_tool_call("sql_db_query_multi", True)

    # 选择最佳候选
    successful = [(i, sql_u, rc) for i, ok, sql_u, rc, _ in probe_results if ok]
    failed = [(i, sql_u, err) for i, ok, sql_u, _, err in probe_results if not ok]

    if successful:
        # 选非 NULL 行最多的候选
        best_i, best_sql, best_rc = max(successful, key=lambda x: x[2])
        other_info = ""
        if len(successful) > 1:
            other_info = "\n其他成功候选: " + ", ".join(
                f"候选{si+1}({src}行)" for si, _, src in successful if si != best_i
            )

        # 通过 sql_db_query 正式执行（测试框架可追踪）
        with _sql_execution_scope("sql_db_query_multi"):
            formal_result = sql_db_query(best_sql)
        return (
            f"📊 多候选试探完成：共 {len(sql_list)} 个候选，{len(successful)} 个成功。\n"
            f"选定候选 {best_i+1}（试探时 {best_rc} 行）。{other_info}\n\n"
            f"{formal_result}"
        )
    else:
        # 全部失败
        error_lines = [f"❌ 所有 {len(sql_list)} 个候选 SQL 均执行失败：\n"]
        for i, sql_used, err in failed:
            error_lines.append(f"**候选 {i+1}**:\n```sql\n{sql_used}\n```\n错误: {err}\n")
        error_lines.append(
            "\n请使用 `sql_self_correct` 对最接近正确的候选进行系统化纠正。"
        )
        return "\n".join(error_lines)


def sql_db_query_checker(query: str) -> str:
    """
    检查 SQL 查询的语法是否正确。
    注意：这只是一个基本的检查，不会实际执行查询。

    Args:
        query: 要检查的 SQL 查询语句
    """
    allowed, reason = _check_tool_call("sql_db_query_checker")
    if not allowed:
        return reason

    query_upper = query.strip().upper()

    if not query_upper:
        _record_tool_call("sql_db_query_checker", False)
        return "错误: SQL 查询为空"

    forbidden_keywords = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE"]
    for keyword in forbidden_keywords:
        if keyword in query_upper:
            _record_tool_call("sql_db_query_checker", False)
            return f"错误: 不允许执行 {keyword} 操作，只允许 SELECT 查询"

    if not query_upper.startswith("SELECT"):
        _record_tool_call("sql_db_query_checker", False)
        return "错误: 只允许执行 SELECT 查询"

    issues = []
    if "FROM" not in query_upper:
        issues.append("缺少 FROM 子句")
    if query.count("(") != query.count(")"):
        issues.append("括号不匹配")
    if query.count("'") % 2 != 0:
        issues.append("单引号不匹配")

    _record_tool_call("sql_db_query_checker", True)

    if issues:
        return f"SQL 语法警告: {', '.join(issues)}"

    # SQLite 整数除法检查
    div_issues = _detect_integer_division(query)
    if div_issues:
        warnings = []
        for d in div_issues:
            warnings.append(
                f"⚠️ SQLite 整数除法: `{d['left_operand']} / {d['right_operand']}` "
                f"两个 INTEGER 列相除会截断小数（如 3/5=0）。"
                f"建议改为: `{d['suggestion']}`"
            )
        return "✅ SQL 语法检查通过，但检测到潜在计算问题:\n" + "\n".join(warnings)

    return "✅ SQL 查询语法检查通过，可以执行。"


def sql_db_table_relationship(table_names: str = "") -> str:
    """
    获取指定表之间的关系信息（外键/关联关系）。

    使用此工具了解表之间如何关联，以便正确编写 JOIN 查询。

    Args:
        table_names: 逗号分隔的表名列表，如 "orders, customers, products"。
                    如果为空，则返回所有外键关系。
    """
    db = _get_database()
    if db is None:
        return "错误: 数据库未连接，请设置 DATABASE_URI 环境变量"

    allowed, reason = _check_tool_call("sql_db_table_relationship")
    if not allowed:
        return reason

    try:
        inspector = sa_inspect(db._engine)

        # 解析要查询的表名
        filter_tables = None
        if table_names and table_names.strip():
            filter_tables = set(t.strip() for t in table_names.split(",") if t.strip())

        # 获取所有表
        all_tables = db.get_usable_table_names()

        # 如果指定了表名，只查这些表的外键
        tables_to_check = filter_tables if filter_tables else set(all_tables)

        relationships = []
        for table_name in tables_to_check:
            if table_name not in all_tables:
                continue
            try:
                for fk in inspector.get_foreign_keys(table_name):
                    ref_table = fk.get("referred_table", "")
                    for lc, rc in zip(
                        fk.get("constrained_columns", []),
                        fk.get("referred_columns", []),
                    ):
                        fk_str = f"{table_name}.{lc} = {ref_table}.{rc}"
                        if fk_str not in relationships:
                            if filter_tables:
                                if table_name in filter_tables or ref_table in filter_tables:
                                    relationships.append(fk_str)
                            else:
                                relationships.append(fk_str)
            except Exception:
                continue

        _record_tool_call("sql_db_table_relationship", True)

        if not relationships:
            if filter_tables:
                return (
                    f"未找到表 {', '.join(sorted(filter_tables))} 之间的外键关系。\n\n"
                    "可能的原因：\n"
                    "1. 这些表之间没有定义外键约束\n"
                    "2. 可以尝试通过列名推断关系（如 customer_id 可能关联 customers.id）\n\n"
                    "提示：可以查看表架构中的列名来推断可能的关联关系。"
                )
            else:
                return (
                    "当前数据库未定义任何外键关系。\n\n"
                    "提示：可以通过表架构中的外键列（如 xxx_id）推断可能的关联关系。"
                )

        result_lines = ["表之间的关系如下：\n"]
        for rel in relationships:
            result_lines.append(f"  • {rel}")

        result_lines.append("\n✅ 表关系已获取完成。")
        result_lines.append("请使用以上关系信息编写正确的 JOIN 语句。")
        return "\n".join(result_lines)

    except Exception as e:
        _record_tool_call("sql_db_table_relationship", False)
        logger.error(f"获取表关系失败: {e}", exc_info=True)
        return f"获取表关系失败: {str(e)[:100]}"


def _get_struct_keys_for_tables(retrieval_result, inspector) -> list:
    """
    为 sql_db_value_lookup 召回的表附加主键和外键信息。
    返回格式化的输出行列表；若无结构键则返回空列表。
    同时将结构键描述写入 _col_descriptions 缓存，供 build_linked_mschema 使用。
    """
    if retrieval_result is None or not retrieval_result.tables:
        return []
    recalled_tables = [t.table_name for t in retrieval_result.tables if t.table_name]
    if not recalled_tables:
        return []

    # 收集跨召回表的所有 FK 关系
    fk_map: dict = {}  # "table.col" -> 描述字符串
    for tbl in recalled_tables:
        try:
            for fk in inspector.get_foreign_keys(tbl):
                ref_table = fk.get("referred_table", "")
                for lc, rc in zip(
                    fk.get("constrained_columns", []),
                    fk.get("referred_columns", []),
                ):
                    local_key = f"{tbl}.{lc}"
                    ref_key = f"{ref_table}.{rc}"
                    fk_map[local_key] = f"外键 → {ref_key}，用于 JOIN {tbl} 与 {ref_table}"
                    if ref_table in recalled_tables:
                        fk_map.setdefault(
                            ref_key,
                            f"被外键引用（{local_key}），用于 JOIN {ref_table} 与 {tbl}",
                        )
        except Exception:
            continue

    lines = []
    session_id = _get_session_id()
    desc_cache = _col_descriptions.setdefault(session_id, {})

    for tbl in recalled_tables:
        # 主键
        try:
            pk_cols = inspector.get_pk_constraint(tbl).get("constrained_columns", [])
        except Exception:
            pk_cols = []
        for pk_col in pk_cols:
            key = f"{tbl}.{pk_col}"
            fk_desc = fk_map.get(key, "")
            desc = f"主键 + {fk_desc}" if fk_desc else "主键"
            lines.append(f"**{key}** | 主键/外键 | {desc}")
            desc_cache.setdefault(key, {})["comment"] = desc

        # 纯外键（非主键）
        try:
            fk_infos = inspector.get_foreign_keys(tbl)
        except Exception:
            fk_infos = []
        for fk in fk_infos:
            for lc in fk.get("constrained_columns", []):
                if lc in pk_cols:
                    continue  # 已在主键中处理
                key = f"{tbl}.{lc}"
                desc = fk_map.get(key, "外键，用于 JOIN")
                lines.append(f"**{key}** | 主键/外键 | {desc}")
                desc_cache.setdefault(key, {})["comment"] = desc

    return lines


def sql_db_value_lookup(phrase: str) -> str:
    """
    对单个短语/关键词进行 BM25+向量混合检索，返回供模型消费的召回摘要字符串。

    返回值不是单条结构化结果，也不会在 RetrievalResult 内自动选择“最佳列”。
    函数会遍历召回到的 columns / values 候选，拼出包含“召回的架构元素”、
    “值匹配提示（实体对齐）”以及可选“结构键（JOIN 所需主键/外键）”的 Markdown 文本，
    供后续模型据此决定要传给 add_schema 的 schema_elements。

    调用约定：每次只传入一个从用户问题或 Evidence 中提取出的短语/关键词。
    严禁把多个短语拼成一个长字符串或列表塞入——那样会因噪声稀释向量召回精度，
    导致每个短语无法稳定锁定唯一最相似列。若问题含多个实体/指标短语，请对每个
    短语分别调用本工具一次。

    输入语言必须与数据库中存储的数据语言一致（BM25 关键字匹配依赖语言匹配），严禁翻译。

    示例:
      好: phrase="free lunch"      -> 单短语，BM25+向量都能精准定位
      好: phrase="charter school"  -> 另起一次调用
      差: phrase="哪些学校的免费午餐学生比例超过 50%"   （整句噪声大）
      差: phrase="free lunch, charter school"           （多短语拼接）

    Args:
        phrase: 单个短语或关键词（保持原始语言，与数据库存储语言一致）。

    Returns:
        str: 面向模型的召回摘要文本。真正进入链接架构的列由后续 add_schema 显式决定。
    """
    db = _get_database()
    if db is None:
        return "错误: 数据库未连接，请设置 DATABASE_URI 环境变量"

    allowed, reason = _check_tool_call("sql_db_value_lookup")
    if not allowed:
        return reason

    try:
        inspector = sa_inspect(db._engine)
        dialect = db.dialect

        tables = list(db.get_usable_table_names())

        # 对短语整体做混合检索（BM25 + 向量），保留完整语义。
        vector_recall_text = ""
        vector_recall_xml = ""
        retrieval_result = None
        profile = get_experiment_profile()
        current_db_id = _infer_current_db_id(db)
        try:
            if not profile.get("disable_hybrid_retrieval"):
                from .bird_dev_retriever import hybrid_retrieve, format_retrieval_as_text, format_retrieval_as_xml
                if current_db_id:
                    retrieval_result = hybrid_retrieve(phrase, db_id=current_db_id)
                else:
                    logger.warning("未能推断 BIRD dev db_id，跳过混合检索")
                if retrieval_result is not None and retrieval_result.tables:
                    vector_recall_text = format_retrieval_as_text(retrieval_result)
                    vector_recall_xml = format_retrieval_as_xml(retrieval_result)
                    logger.info("混合检索召回: %d 表, %d 列, %d 值",
                                len(retrieval_result.tables),
                                len(retrieval_result.columns),
                                len(retrieval_result.values))
                else:
                    logger.info("混合检索未返回结果")
            else:
                logger.info("experiment profile=%s: hybrid retrieval disabled", profile["name"])
        except Exception as vec_err:
            logger.warning("混合检索失败: %s", vec_err)
            if os.environ.get("REQUIRE_HYBRID_RETRIEVAL") == "1":
                raise RuntimeError(f"retrieval_service_error: {vec_err}") from vec_err


        text_type_names = {"TEXT", "VARCHAR", "CHAR", "NVARCHAR", "NCHAR", "CLOB", "STRING", "NTEXT"}

        # 预先收集所有表的文本列信息，避免重复查询元数据
        table_text_cols = {}
        for table_name in tables:
            try:
                col_meta_list = inspector.get_columns(table_name)
                text_cols = [
                    c["name"] for c in col_meta_list
                    if any(t in str(c["type"]).upper() for t in text_type_names)
                ]
                if text_cols:
                    table_text_cols[table_name] = text_cols
            except Exception:
                continue

        if not table_text_cols:
            _record_tool_call("sql_db_value_lookup", True)
            return "数据库中未找到文本类型的列，无法执行值搜索。"


        # ---- 构建 (table, column) -> values 映射 ----
        tc_values: dict = {}  # (table_name, column_name) -> list[str]

        # 优先从向量召回的值级结果中提取
        if retrieval_result is not None and retrieval_result.values:
            for val_rec in retrieval_result.values:
                if not val_rec.table_name or not val_rec.column_name:
                    continue
                val = (val_rec.metadata or {}).get("value") or val_rec.value_text
                if val:
                    key = (val_rec.table_name, val_rec.column_name)
                    tc_values.setdefault(key, []).append(str(val))

        # 次选：从列级召回的 metadata.samples 中提取示例值
        if not tc_values and retrieval_result is not None and retrieval_result.columns:
            for col_rec in retrieval_result.columns:
                if not col_rec.table_name or not col_rec.column_name:
                    continue
                samples = (col_rec.metadata or {}).get("samples", [])
                if isinstance(samples, list):
                    vals = [str(s["value"]) for s in samples[:5]
                            if isinstance(s, dict) and "value" in s]
                    if vals:
                        key = (col_rec.table_name, col_rec.column_name)
                        tc_values.setdefault(key, []).extend(vals)

        # 兜底：当向量召回无结果时，对文本列做 SQL LIKE 关键词搜索
        if not tc_values and table_text_cols:
            raw_kws = [w.strip("\"'.,!?;:（）") for w in phrase.split()
                       if len(w.strip("\"'.,!?;:（）")) >= 2]
            keywords = (raw_kws[:5] if raw_kws else [phrase[:30]])
            try:
                with db._engine.connect() as conn:
                    for table_name, text_cols in list(table_text_cols.items())[:10]:
                        for col_name in text_cols[:5]:
                            q_col = (f'"{col_name}"' if dialect in ("postgresql", "oracle", "sqlite")
                                     else f'`{col_name}`')
                            q_tbl = (f'"{table_name}"' if dialect in ("postgresql", "oracle", "sqlite")
                                     else f'`{table_name}`')
                            for kw in keywords:
                                safe_kw = kw.replace("'", "''")
                                if dialect == "mssql":
                                    stmt = (f"SELECT DISTINCT TOP 5 {q_col} FROM {q_tbl} "
                                            f"WHERE {q_col} LIKE '%{safe_kw}%'")
                                elif dialect == "oracle":
                                    stmt = (f"SELECT DISTINCT {q_col} FROM {q_tbl} "
                                            f"WHERE {q_col} LIKE '%{safe_kw}%' AND ROWNUM <= 5")
                                else:
                                    stmt = (f"SELECT DISTINCT {q_col} FROM {q_tbl} "
                                            f"WHERE {q_col} LIKE '%{safe_kw}%' LIMIT 5")
                                try:
                                    rows = conn.execute(sa_text(stmt)).fetchall()
                                    vals = [str(r[0]) for r in rows if r[0] is not None]
                                    if vals:
                                        key = (table_name, col_name)
                                        tc_values.setdefault(key, []).extend(vals)
                                except Exception:
                                    continue
            except Exception:
                pass

        _record_tool_call("sql_db_value_lookup", True)

        candidate_cols: list[tuple[str, str]] = []
        if retrieval_result is not None and retrieval_result.columns:
            candidate_cols.extend(
                [
                    (col_rec.table_name, col_rec.column_name)
                    for col_rec in retrieval_result.columns
                    if col_rec.table_name and col_rec.column_name
                ]
            )
        candidate_cols.extend(list(tc_values.keys()))
        _remember_schema_candidates(candidate_cols)

        # ---- 缓存列描述，供 build_linked_mschema 使用 ----
        session_id = _get_session_id()
        if retrieval_result is not None and retrieval_result.columns:
            desc_cache = _col_descriptions.setdefault(session_id, {})
            for col_rec in retrieval_result.columns:
                if col_rec.table_name and col_rec.column_name:
                    key = f"{col_rec.table_name}.{col_rec.column_name}"
                    entry = desc_cache.setdefault(key, {})
                    meta = col_rec.metadata or {}
                    col_type = meta.get("type", "")
                    if col_type:
                        entry["type"] = col_type
                    # 从 value_text（可能是 "db=...; column_description=...; ..." 格式）提取列描述
                    desc = _extract_column_description(col_rec.value_text)
                    if desc:
                        entry["comment"] = desc

        # ---- 格式化输出：Schema 片段 + 值匹配提示 ----
        result_lines = []

        # 从阶段 3 值召回的 metadata 收集列级混合分，用于排序与消歧展示
        _col_blend: Dict[Tuple[str, str], float] = {}
        if retrieval_result is not None:
            for vrec in retrieval_result.values:
                vmeta = vrec.metadata or {}
                bscore = vmeta.get("blended_score", vmeta.get("column_vec_score", 0.0))
                key = (vrec.table_name or "", vrec.column_name or "")
                if bscore > _col_blend.get(key, 0.0):
                    _col_blend[key] = bscore

        # (A) 优先从 retrieval_result 输出结构化架构片段
        if retrieval_result is not None and retrieval_result.columns:
            result_lines.append("### 召回的架构元素\n")

            # 按列级混合分降序，没有混合分的按原始 vec_score
            sorted_columns = sorted(
                retrieval_result.columns,
                key=lambda r: _col_blend.get(
                    (r.table_name or "", r.column_name or ""),
                    float(r.vec_score or 0.0),
                ),
                reverse=True,
            )

            for col_rec in sorted_columns:
                meta = col_rec.metadata or {}
                col_type = meta.get("type", "?")
                col_desc = _extract_column_description(col_rec.value_text)
                samples_raw = meta.get("samples", [])
                if isinstance(samples_raw, list):
                    samples = [str(s["value"]) for s in samples_raw[:3]
                               if isinstance(s, dict) and "value" in s]
                else:
                    samples = []
                # 优先展示已匹配的值
                matched = tc_values.get((col_rec.table_name, col_rec.column_name), [])
                display_vals = list(dict.fromkeys(matched))[:3] if matched else samples[:3]

                blend = _col_blend.get((col_rec.table_name or "", col_rec.column_name or ""))
                rel_tag = f" | rel={blend:.2f}" if blend is not None else ""

                line = f"**{col_rec.table_name}.{col_rec.column_name}**{rel_tag} | {col_type}"
                sibling_of = meta.get("sibling_of")
                if sibling_of:
                    line += f" | sibling of {sibling_of}"
                if col_desc:
                    enriched = _detect_pre_aggregated_hint(col_rec.column_name, col_desc, str(col_type))
                    line += f" | {enriched if enriched else col_desc}"
                else:
                    hint = _detect_pre_aggregated_hint(col_rec.column_name, col_type=str(col_type))
                    if hint:
                        line += f" | {hint}"
                result_lines.append(line)
                if display_vals:
                    result_lines.append(f"  示例值: {', '.join(display_vals)}")
            result_lines.append(f"\n（共 {len(retrieval_result.columns)} 个召回列）")
        elif tc_values:
            # 兜底：只有 tc_values，无向量召回列信息
            result_lines.append("### 召回的架构元素\n")
            for (tbl, col), vals in tc_values.items():
                unique_vals = list(dict.fromkeys(vals))[:3]
                result_lines.append(f"**{tbl}.{col}**")
                result_lines.append(f"  示例值: {', '.join(unique_vals)}")

        # (B) 值匹配提示（实体对齐）— 按列混合分降序，暴露相关性
        if tc_values:
            result_lines.append("\n### 值匹配提示（实体对齐）\n")
            sorted_tc = sorted(
                tc_values.items(),
                key=lambda item: _col_blend.get(item[0], 0.0),
                reverse=True,
            )
            desc_cache = _col_descriptions.get(session_id, {})
            for (tbl, col), vals in sorted_tc:
                unique_vals = list(dict.fromkeys(vals))[:5]
                vals_str = ", ".join(f'"{v}"' for v in unique_vals)
                blend = _col_blend.get((tbl, col))
                col_info = desc_cache.get(f"{tbl}.{col}", {})
                col_comment = col_info.get("comment", "")
                tag_parts = []
                if blend is not None:
                    tag_parts.append(f"rel={blend:.2f}")
                if col_comment:
                    tag_parts.append(col_comment)
                tag = f" [{' | '.join(tag_parts)}]" if tag_parts else ""
                result_lines.append(f"  - {tbl}.{col}{tag}: {vals_str}")

        if result_lines:
            # ---- 追加结构键（主键/外键）区块 ----
            struct_key_lines = _get_struct_keys_for_tables(retrieval_result, inspector)
            if struct_key_lines:
                result_lines.append("\n### 🔑 结构键（JOIN 所需主键/外键）\n")
                result_lines.extend(struct_key_lines)
                result_lines.append("请将以上结构键与业务列一起通过 add_schema 加入链接架构。")
            result_lines.append(
                "\n✅ 短语召回完成。请使用 add_schema 将相关列添加到链接架构"
                "（格式：table.column;table.column）。"
                "\n如还有其它实体/指标短语未对齐，请对每个短语分别再次调用 sql_db_value_lookup。"
            )
            return "\n".join(result_lines)
        else:
            return "未找到匹配的架构元素。请换一种表述（同义词、单复数、缩写展开），或对短语做更小粒度的切分后重试。"

    except Exception as e:
        _record_tool_call("sql_db_value_lookup", False)
        if os.environ.get("REQUIRE_HYBRID_RETRIEVAL") == "1":
            raise RuntimeError(f"retrieval_service_error: {e}") from e
        logger.error(f"值检索失败: {e}", exc_info=True)
        return f"值检索失败: {str(e)[:100]}"


def _auto_quote_sql_identifiers(query: str) -> str:
    """自动为 SQL 中未加引号的特殊标识符（含空格、括号等）添加双引号。

    利用 _linked_schema 中已知的列名识别需要引用的标识符，
    避免 LLM 生成的 SQL 因引用方式不一致而报语法错误。
    仅对含特殊字符（空格、括号、百分号等）且尚未被引号包裹的标识符生效。
    """
    session_id = _get_session_id()
    linked = _get_effective_linked_schema(session_id)
    if not linked:
        return query

    # 收集需要引用的列名（含空格或特殊字符的列名）
    special_cols = set()
    for item in linked:
        if "." in item:
            col_name = item.split(".", 1)[1]
            if re.search(r'[^a-zA-Z0-9_]', col_name):
                special_cols.add(col_name)

    if not special_cols:
        return query

    # 按长度降序排列，确保较长列名先替换（避免短列名误匹配长列名的子串）
    for col_name in sorted(special_cols, key=len, reverse=True):
        # 匹配: 未被引号/反引号包裹的裸列名（前后不是引号字符）
        # 使用负向前后查找排除已被 " 或 ` 包裹的情况
        escaped = re.escape(col_name)
        pattern = re.compile(
            r'(?<!["`])' + escaped + r'(?!["`])',
            re.IGNORECASE,
        )
        query = pattern.sub(f'"{col_name}"', query)

    return query


# ==================== 语法专用修复工具 ====================

# 语法类错误的典型特征（用于区分 syntax vs semantic 错误）
_SYNTAX_ERROR_PATTERNS = [
    re.compile(r'syntax error', re.IGNORECASE),
    re.compile(r'near "', re.IGNORECASE),
    re.compile(r'unrecognized token', re.IGNORECASE),
    re.compile(r'misuse of aggregate', re.IGNORECASE),
    re.compile(r'no such function', re.IGNORECASE),
]

# 语义类错误的典型特征（column/table 不存在 — 不由 sql_syntax_fix 处理）
_SEMANTIC_ERROR_PATTERNS = [
    re.compile(r'no such table', re.IGNORECASE),
    re.compile(r'no such column', re.IGNORECASE),
    re.compile(r'does not exist', re.IGNORECASE),
    re.compile(r'undefinedtable', re.IGNORECASE),
    re.compile(r'undefinedcolumn', re.IGNORECASE),
    re.compile(r'ambiguous column', re.IGNORECASE),
]


def sql_syntax_fix(sql: str, error_msg: str = "") -> str:
    """
    仅修复 SQL 的语法问题（标识符引用、大小写、整数除法），不改变列/表选择。

    与 sql_self_correct 的区别：本工具绝不替换为不同的列名，
    只修正当前列名的引用方式（加引号、修正大小写、添加 CAST 等）。

    适用场景：
    - 列名含空格/括号但未加引号 → 自动添加双引号
    - 列名大小写与数据库不匹配 → 修正为实际大小写
    - SQLite 整数除法 → 添加 CAST(x AS REAL)
    - 多余逗号、缺少 AS 等轻微语法问题

    不适用场景（返回原始错误，不做修改）：
    - 列/表根本不存在（语义错误，需要 sql_self_correct）
    - JOIN 缺失或条件错误
    - GROUP BY / HAVING 逻辑错误

    Args:
        sql: 待修复的 SQL 查询
        error_msg: 数据库返回的错误信息（可选，用于辅助分类）
    """
    db = _get_database()
    if db is None:
        return "错误: 数据库未连接"

    allowed, reason = _check_tool_call("sql_syntax_fix")
    if not allowed:
        return reason

    session_id = _get_session_id()

    # ---- 判断是否为纯语义错误（列/表不存在）→ 不处理 ----
    if error_msg:
        is_semantic = any(p.search(error_msg) for p in _SEMANTIC_ERROR_PATTERNS)
        is_syntax = any(p.search(error_msg) for p in _SYNTAX_ERROR_PATTERNS)
        if is_semantic and not is_syntax:
            _record_tool_call("sql_syntax_fix", False)
            _record_correction_event(
                "syntax",
                status="skipped_semantic_error",
                sql=sql,
                error_msg=error_msg[:200],
            )
            return (
                f"⚠️ 该错误属于语义错误（列/表不存在），不适合语法修复。\n"
                f"错误信息: {error_msg[:200]}\n\n"
                "请使用 `sql_self_correct` 或 `sql_schema_validate` 进行语义级纠正。"
            )

    fixed_sql = sql
    fixes_applied = []

    # ---- Fix 1: 自动引用含特殊字符的标识符 ----
    quoted = _auto_quote_sql_identifiers(fixed_sql)
    if quoted != fixed_sql:
        fixes_applied.append("自动为含特殊字符的标识符添加双引号")
        fixed_sql = quoted

    # ---- Fix 2: 修正列名大小写（匹配 _linked_schema 中的实际列名）----
    linked = _get_effective_linked_schema(session_id)
    if linked:
        # 构建 小写列名 → 实际列名 的索引
        col_case_map = {}  # lower("table.column") -> "table.column"
        for item in linked:
            if "." in item:
                col_case_map[item.lower()] = item

        # 提取 SQL 中的列引用并修正大小写
        sql_cols = _extract_sql_columns(fixed_sql)
        alias_map = _extract_sql_aliases(fixed_sql)

        for tbl_or_alias, col in sql_cols:
            # 解析别名到实际表名
            actual_table = alias_map.get(tbl_or_alias, tbl_or_alias)
            lookup_key = f"{actual_table}.{col}".lower()

            if lookup_key in col_case_map:
                correct_ref = col_case_map[lookup_key]
                correct_col = correct_ref.split(".", 1)[1]
                if correct_col != col:
                    # 替换为正确大小写（用 word boundary 避免误匹配）
                    pattern = re.compile(
                        r'(?<=\.)' + re.escape(col) + r'(?=[\s,;)"\']|$)',
                        re.IGNORECASE,
                    )
                    new_sql = pattern.sub(correct_col, fixed_sql, count=1)
                    if new_sql != fixed_sql:
                        fixes_applied.append(f"列名大小写修正: {col} → {correct_col}")
                        fixed_sql = new_sql

    # ---- Fix 3: SQLite 整数除法 → CAST ----
    int_div_issues = _detect_integer_division(fixed_sql)
    for issue in int_div_issues:
        left = issue["left_operand"]
        right = issue["right_operand"]
        suggestion = issue["suggestion"]
        # 构建匹配原始除法的模式
        left_escaped = re.escape(left)
        right_escaped = re.escape(right)
        div_pattern = re.compile(
            left_escaped + r'\s*/\s*' + right_escaped,
            re.IGNORECASE,
        )
        new_sql = div_pattern.sub(suggestion, fixed_sql, count=1)
        if new_sql != fixed_sql:
            fixes_applied.append(f"整数除法修复: {left}/{right} → {suggestion}")
            fixed_sql = new_sql

    # ---- Fix 4: 去除多余尾部逗号（SELECT 或 GROUP BY 末尾的多余逗号）----
    trailing_comma_fixed = re.sub(
        r',\s*\b(FROM|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT)\b',
        r' \1',
        fixed_sql,
        flags=re.IGNORECASE,
    )
    if trailing_comma_fixed != fixed_sql:
        fixes_applied.append("去除多余尾部逗号")
        fixed_sql = trailing_comma_fixed

    # ---- 如果没有任何修复，直接返回 ----
    if not fixes_applied:
        _record_tool_call("sql_syntax_fix", True)
        _record_correction_event(
            "syntax",
            status="no_fix",
            sql=sql,
            error_msg=error_msg[:200] if error_msg else "",
        )
        return (
            "未检测到可自动修复的语法问题。\n"
            f"原始 SQL:\n```sql\n{sql}\n```\n\n"
            f"错误信息: {error_msg[:200] if error_msg else '(无)'}\n\n"
            "如果仍有错误，请尝试 `sql_self_correct` 进行语义级纠正。"
        )

    # ---- 尝试执行修复后的 SQL ----
    fix_summary = "\n".join(f"- {f}" for f in fixes_applied)
    try:
        result = db.run(fixed_sql)
        _record_tool_call("sql_syntax_fix", True)
        _record_correction_event(
            "syntax",
            status="fixed",
            original_sql=sql,
            fixed_sql=fixed_sql,
            fixes_applied=list(fixes_applied),
        )
        result_row_count = _count_result_rows(result) if result else 0
        if not result or _is_all_null_result(str(result)):
            _record_sql_execution(
                fixed_sql,
                "rejected",
                row_count=result_row_count,
                source=_get_sql_execution_source("sql_syntax_fix"),
            )
        else:
            _record_sql_execution(
                fixed_sql,
                "accepted",
                row_count=result_row_count,
                source=_get_sql_execution_source("sql_syntax_fix"),
            )

        result_preview = str(result)[:500] if result else "(无数据)"
        return (
            f"✅ 语法修复成功！\n\n"
            f"**应用的修复**:\n{fix_summary}\n\n"
            f"**修复后 SQL**:\n```sql\n{fixed_sql}\n```\n\n"
            f"**执行结果**:\n{result_preview}"
        )
    except Exception as e:
        _record_tool_call("sql_syntax_fix", True)
        error = str(e)[:200]
        _record_correction_event(
            "syntax",
            status="fixed_but_failed",
            original_sql=sql,
            fixed_sql=fixed_sql,
            fixes_applied=list(fixes_applied),
            error_msg=error,
        )
        _record_sql_execution(
            fixed_sql,
            "error",
            row_count=0,
            source=_get_sql_execution_source("sql_syntax_fix"),
        )
        return (
            f"⚠️ 语法修复已应用，但执行仍失败。\n\n"
            f"**应用的修复**:\n{fix_summary}\n\n"
            f"**修复后 SQL**:\n```sql\n{fixed_sql}\n```\n\n"
            f"**新的错误**: {error}\n\n"
            "请尝试 `sql_self_correct` 进行更深层的诊断修正。"
        )


def add_schema(schema_elements: str) -> str:
    """
    将新发现的相关列添加到当前会话的链接架构中，供 build_linked_mschema 构建 M-Schema。
    必须与提供反馈的操作（如 sql_db_schema、sql_db_value_lookup）在同一回合中配对使用，
    不能单独调用。

    Args:
        schema_elements: 分号分隔的 table.column 列表，例如 "frpm.school_name;schools.phone"
    """
    session_id = _get_session_id()
    profile = get_experiment_profile(session_id)
    if session_id not in _linked_schema:
        _linked_schema[session_id] = set()

    elements = [e.strip() for e in schema_elements.split(";") if e.strip()]
    added = []
    skipped = []
    invalid_tables = []

    # 获取有效表名集合，用于校验
    db = _get_database()
    valid_tables_lower = set()
    if db is not None:
        try:
            valid_tables_lower = {t.lower() for t in db.get_usable_table_names()}
        except Exception:
            pass

    for elem in elements:
        if "." not in elem:
            skipped.append(elem)
            continue
        table_name = elem.split(".", 1)[0]
        if valid_tables_lower and table_name.lower() not in valid_tables_lower:
            invalid_tables.append(elem)
            continue
        if profile.get("disable_schema_linking"):
            _remember_schema_candidates([(table_name, elem.split(".", 1)[1])], session_id=session_id)
        else:
            _linked_schema[session_id].add(elem)
        added.append(elem)

    # 更新召回快照（只增不减）
    _linked_schema_snapshot.setdefault(session_id, set()).update(_get_effective_linked_schema(session_id))
    _core_columns.setdefault(session_id, set()).update(added)

    total = len(_get_effective_linked_schema(session_id))
    lines = []
    if added:
        lines.append(f"✅ 已添加 {len(added)} 个架构元素到链接架构: {', '.join(added)}")
    if skipped:
        lines.append(f"⚠️ 以下条目格式有误（需为 table.column），已跳过: {', '.join(skipped)}")
    if invalid_tables:
        lines.append(f"⚠️ 以下条目的表名在数据库中不存在，已跳过: {', '.join(invalid_tables)}")
    lines.append(f"当前链接架构共 {total} 个元素:")
    if profile.get("disable_schema_linking"):
        lines.append("Note: current profile disables explicit schema linking; the elements above are kept only as a loose candidate set.")
    for item in sorted(_get_effective_linked_schema(session_id)):
        lines.append(f"  - {item}")
    return "\n".join(lines)


def get_linked_schema(session_id: str = None) -> set:
    """获取指定会话的链接架构集合（供外部读取）"""
    sid = session_id or _get_session_id()
    return _get_effective_linked_schema(sid)


def get_linked_schema_snapshot(session_id: str = None) -> set:
    """获取指定会话的召回快照（包含所有曾 add_schema 过的元素）"""
    sid = session_id or _get_session_id()
    return _linked_schema_snapshot.get(sid, set())


def reset_linked_schema(session_id: str = None) -> None:
    """重置指定会话的链接架构（每次新对话/查询前调用）"""
    sid = session_id or _get_session_id()
    _linked_schema.pop(sid, None)
    _linked_schema_snapshot.pop(sid, None)
    _col_descriptions.pop(sid, None)
    _self_correct_counts.pop(sid, None)
    _core_columns.pop(sid, None)
    _fallback_schema_candidates.pop(sid, None)
    reset_correction_events(sid)
    reset_experiment_profile(sid)


def reset_session(session_id: str = None) -> None:
    """清空指定会话的所有模块级状态（链接架构 / 执行轨迹 / 纠错事件 / 最终 SQL / 实验 profile）。

    会话关闭或 LRU 淘汰时调用，避免长期运行的服务里残留 stale state。
    """
    sid = session_id or _get_session_id()
    reset_linked_schema(sid)
    reset_sql_execution_trace(sid)
    reset_final_sql(sid)


def build_linked_mschema(db_id: str = "") -> str:
    """
    将 data-link 技能召回的架构元素构建为 M-Schema 格式文本。

    读取当前会话由 add_schema 累积的链接架构（table.column 集合），
    从数据库中获取各列的完整元数据（类型、主键、示例值），
    并自动通过 Bird 风格的 database_description CSV 目录补充列描述，
    最终输出标准 M-Schema 文本，供 SQL 生成步骤直接使用。

    Args:
        db_id: 可选。M-Schema 头部显示的数据库标识符。
               为空则自动从连接 URI 推断。

    Returns:
        M-Schema 格式的文本字符串。
    """
    db = _get_database()
    if db is None:
        return "错误: 数据库未连接，请设置 DATABASE_URI 环境变量"

    session_id = _get_session_id()
    profile = get_experiment_profile(session_id)
    linked = get_linked_schema(session_id)

    if not linked:
        if profile.get("disable_schema_linking"):
            fallback = set(_fallback_schema_candidates.get(session_id, set()))
            if fallback:
                linked = fallback
        if not linked:
            return (
                "Linked schema is empty.\n"
                "Run the data-link stage first to collect relevant schema elements via add_schema, "
                "then call build_linked_mschema again."
            )

    core_set = _core_columns.get(session_id, set())

    # 解析 "table.column" -> {table_name: set(columns)}
    table_columns: dict = {}
    for item in linked:
        if "." not in item:
            continue
        table_name, col_name = item.split(".", 1)
        table_columns.setdefault(table_name, set()).add(col_name)

    if not table_columns:
        return "⚠️ 链接架构中没有有效的 table.column 条目。"

    # 延迟导入，避免模块级循环依赖
    try:
        from .mschema.m_schema import MSchema
    except ImportError:
        from mschema.m_schema import MSchema

    # 推断 db_id
    if not db_id:
        db_id = _infer_current_db_id(db) or "database"

    mschema = MSchema(db_id=db_id)
    inspector = sa_inspect(db._engine)
    dialect = db.dialect

    # 自动加载 Bird 风格列描述
    # 优先级：环境变量 DESCRIPTION_DIR > 数据库文件同级 database_description/
    description_dir = os.environ.get("DESCRIPTION_DIR", "").strip()
    if not description_dir:
        try:
            raw_db = db._engine.url.database or ""
            if raw_db and os.path.isfile(raw_db):
                candidate = os.path.join(
                    os.path.dirname(os.path.abspath(raw_db)),
                    "database_description",
                )
                if os.path.isdir(candidate):
                    description_dir = candidate
                    logger.info(f"自动检测到列描述目录: {description_dir}")
        except Exception:
            pass

    retriever = None  # DescriptionRetriever 已停用，列描述由向量检索缓存提供

    # 遍历召回的表，构建 MSchema
    for table_name, selected_cols in table_columns.items():
        # 获取列元数据
        try:
            col_meta_list = inspector.get_columns(table_name)
        except Exception as e:
            logger.warning(f"无法获取表 '{table_name}' 的列信息: {e}")
            continue

        # 表注释
        try:
            table_comment = inspector.get_table_comment(table_name).get("text", "") or ""
        except Exception:
            table_comment = ""

        mschema.add_table(table_name, comment=table_comment)

        # 主键列集合
        try:
            pk_cols = set(inspector.get_pk_constraint(table_name).get("constrained_columns", []))
        except Exception:
            pk_cols = set()

        # 采样示例值（仅针对召回的列）
        col_examples: dict = {}
        try:
            with db._engine.connect() as conn:
                for col in col_meta_list:
                    col_name = col["name"]
                    if col_name not in selected_cols:
                        continue
                    if dialect in ("postgresql", "oracle", "sqlite"):
                        q_col = f'"{col_name}"'
                        q_tbl = f'"{table_name}"'
                    else:
                        q_col = f'`{col_name}`'
                        q_tbl = f'`{table_name}`'
                    if dialect == "mssql":
                        stmt = f"SELECT DISTINCT TOP {_EXAMPLE_LIMIT} {q_col} FROM {q_tbl} WHERE {q_col} IS NOT NULL"
                    elif dialect == "oracle":
                        stmt = f'SELECT DISTINCT {q_col} FROM "{table_name.upper()}" WHERE {q_col} IS NOT NULL AND ROWNUM <= {_EXAMPLE_LIMIT}'
                    else:
                        stmt = f"SELECT DISTINCT {q_col} FROM {q_tbl} WHERE {q_col} IS NOT NULL LIMIT {_EXAMPLE_LIMIT}"
                    try:
                        rows = conn.execute(sa_text(stmt)).fetchall()
                        col_examples[col_name] = [str(r[0]) for r in rows if r[0] is not None]
                    except Exception:
                        col_examples[col_name] = []
        except Exception:
            pass

        # 只将召回的列加入 MSchema
        for col in col_meta_list:
            col_name = col["name"]
            if col_name not in selected_cols:
                continue

            col_type = str(col["type"])
            col_comment = col.get("comment", "") or ""

            # 用向量检索缓存的列描述补充（DescriptionRetriever 已停用）
            if not col_comment:
                cached_entry = _col_descriptions.get(session_id, {}).get(
                    f"{table_name}.{col_name}", {}
                )
                if cached_entry.get("comment"):
                    col_comment = cached_entry["comment"]

            # 预聚合列语义提示（仅数值类型列）
            enriched = _detect_pre_aggregated_hint(col_name, col_comment, col_type)
            if enriched:
                col_comment = enriched

            # 核心列标记：Evidence 公式列和显式加入链接架构的列
            is_core = f"{table_name}.{col_name}" in core_set
            if is_core:
                col_comment = f"[CORE] {col_comment}" if col_comment else "[CORE]"

            mschema.add_field(
                table_name,
                col_name,
                field_type=col_type,
                primary_key=(col_name in pk_cols),
                nullable=col.get("nullable", True),
                comment=col_comment,
                examples=col_examples.get(col_name, []),
            )

    # 只保留召回表之间的外键关系
    table_columns_lower = {k.lower(): k for k in table_columns.keys()}
    fk_relations = []
    for table_name in table_columns:
        try:
            for fk in inspector.get_foreign_keys(table_name):
                ref_table = fk.get("referred_table", "")
                if '.' in ref_table:
                    ref_table = ref_table.split('.')[-1]
                if ref_table.lower() not in table_columns_lower:
                    continue
                actual_ref = table_columns_lower[ref_table.lower()]
                for lc, rc in zip(
                    fk.get("constrained_columns", []),
                    fk.get("referred_columns", []),
                ):
                    fk_relations.append(f"{table_name}.{lc}={actual_ref}.{rc}")
        except Exception:
            continue

    if fk_relations:
        mschema.set_foreign_keys("\n".join(fk_relations))

    mschema_text = mschema.to_mschema()
    col_count = sum(len(cols) for cols in table_columns.values())

    # 核心列摘要：在 M-Schema 末尾高亮核心列，引导 LLM 优先使用
    core_in_schema = [item for item in core_set if item in linked]
    if core_in_schema:
        core_summary = ", ".join(sorted(core_in_schema))
        mschema_text += (
            f"\n\n🎯 **核心列（优先使用）**: {core_summary}"
            "\n以上 [CORE] 标记的列来自 Evidence 公式或显式加入链接架构的筛选结果，"
            "是回答问题最直接相关的列，编写 SQL 时应优先使用。"
        )

    linked_preview_items = sorted(linked)
    preview_text = ", ".join(linked_preview_items[:8])
    if len(linked_preview_items) > 8:
        preview_text += f" ...（共 {len(linked_preview_items)} 个）"

    mschema_text += (
        f"\n\n✅ M-Schema 已构建完成：{len(table_columns)} 张表，{col_count} 个召回列。"
        f"\n当前链接列预览：{preview_text}"
        "\n请基于以上 M-Schema 编写 SQL 查询。"
    )

    # SQLite 整数除法警告（借鉴 SQLFixAgent 橡皮鸭调试思想：提前暴露类型陷阱）
    if dialect == "sqlite":
        # 检查召回列中是否存在 INTEGER 类型
        has_int_col = False
        for tname, sel_cols in table_columns.items():
            try:
                for col in inspector.get_columns(tname):
                    if col["name"] in sel_cols:
                        ctype = str(col["type"]).upper()
                        if "INT" in ctype:
                            has_int_col = True
                            break
            except Exception:
                pass
            if has_int_col:
                break
        if has_int_col:
            mschema_text += (
                "\n\n⚠️ **SQLite 整数除法警告**：当前数据库为 SQLite，召回列中包含 INTEGER 类型。"
                "\n对两个 INTEGER 列做除法会执行整数除法（如 3/5=0 而非 0.6）。"
                "\n如需计算比率/百分比/比例，**必须** 使用 `CAST(column AS REAL) / other_column`。"
            )

    return mschema_text


def save_report(filename: str) -> dict:
    """
    保存分析报告到文件。当你生成了数据分析报告后，必须调用此工具将报告内容保存。
    报告内容会自动从你之前输出的文本中收集，无需手动传入。

    Args:
        filename: 文件名（不含路径），使用描述性名称如 sales_report_2024.md
    
    Returns:
        dict: 保存结果，包含 status 和 filepath
    """
    allowed, reason = _check_tool_call("save_report")
    if not allowed:
        return {"status": "error", "error_message": reason}

    try:
        # 从收集器获取 agent 输出的全部文本
        content = get_collected_report_content()
        if not content or not content.strip():
            return {
                "status": "error",
                "error_message": "没有收集到报告内容。请先输出报告内容，然后再调用 save_report。"
            }

        from datetime import datetime
        # 按 月_日_时_分 生成子目录，保存到 reports/<MM>_<DD>_<HH>_<MM>/ 下
        ts = datetime.now().strftime("%m_%d_%H_%M")
        reports_dir = Path(__file__).parent.parent / "reports" / ts
        reports_dir.mkdir(parents=True, exist_ok=True)
        
        filepath = reports_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        _record_tool_call("save_report", True)

        return {
            "status": "success",
            "message": f"报告已保存到: {filepath}",
            "filepath": str(filepath)
        }
    except Exception as e:
        _record_tool_call("save_report", False)
        return {"status": "error", "error_message": str(e)}


def submit_final_sql(sql: str, reasoning: str = "") -> dict:
    """
    提交最终 SQL。在 `correct` 流程通过过滤、确认结果可用后必须调用此工具。

    被提交的 SQL 是 test harness 与 evaluate.py 的唯一判分依据。
    多次调用时以最后一次为准（允许在再次纠错后覆盖）。
    如果当前会话最终无法得到可用 SQL，**不要** 调用此工具，
    直接以自然语言说明失败原因。

    Args:
        sql: 最终接受的 SQL（必须是最近一次 `sql_db_query` 接受的 SQL；
             不要再额外加 `LIMIT`、`DISTINCT` 等修饰）
        reasoning: 可选的一句话说明，解释为何选择这条 SQL 作为最终答案

    Returns:
        dict: {"status": "success", "final_sql": <提交的 SQL>}
              或 {"status": "error", "error_message": <原因>}
    """
    allowed, reason = _check_tool_call("submit_final_sql")
    if not allowed:
        return {"status": "error", "error_message": reason}

    if not sql or not sql.strip():
        _record_tool_call("submit_final_sql", False)
        return {"status": "error", "error_message": "sql 不能为空"}

    session_id = _get_session_id()
    cleaned = sql.strip()

    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip().rstrip(";").strip())

    trace = _sql_execution_traces.get(session_id, [])
    accepted_sqls = [
        rec["sql"]
        for rec in trace
        if rec.get("state") in ("accepted", "accepted_with_warning")
        and (rec.get("source") or "") == "sql_db_query"
        and not rec.get("is_probe")
    ]
    accepted_norm = {_norm(s) for s in accepted_sqls}
    if _norm(cleaned) not in accepted_norm:
        _record_tool_call("submit_final_sql", False)
        last_accepted = accepted_sqls[-1] if accepted_sqls else "<无>"
        return {
            "status": "error",
            "error_message": (
                "提交的 SQL 未在 sql_db_query 接受轨迹中找到。"
                "请直接传入最近一次 sql_db_query 接受的 SQL 原文（含双引号），"
                "不要在 LLM 文本里重写。\n"
                f"最近一条已接受 SQL：\n{last_accepted}"
            ),
        }

    _final_sql[session_id] = {
        # Preserve the exact tool argument for reproducible benchmark scoring.
        "sql": sql,
        "reasoning": reasoning or "",
        "submitted_at": time.time(),
    }
    _record_tool_call("submit_final_sql", True)
    return {"status": "success", "final_sql": sql}


def db_search(keyword: str = "") -> str:
    """
    搜索数据库中可用的 Schema 或数据库信息。

    - **PostgreSQL**：搜索匹配关键词的 Schema，找到后自动切换 search_path。
      每个 Schema 代表一个独立的数据库主题（如"交通运输"、"水果"等）。
    - **SQLite**：SQLite 没有 Schema 概念，此工具会直接列出所有可用的表。
      对于 SQLite 数据库，可以跳过此步骤，直接使用 sql_db_list_tables。
    - **其他数据库**：列出可用的表。

    Args:
        keyword: 要搜索的数据库/Schema 名称关键词，如 "水果"、"交通"、"电影"。
                 传空字符串 "" 或 "*" 可列出所有可用的 Schema（PostgreSQL）或所有表（其他数据库），不会切换连接。
    """
    db = _get_database()
    if db is None:
        return "错误: 数据库未连接，请设置 DATABASE_URI 环境变量"

    # 判断是否为"列出全部"模式
    list_all = not keyword or keyword.strip() in ("", "*", "all", "所有", "全部", "列出全部")

    dialect = db.dialect

    # ---- 非 PostgreSQL 数据库（SQLite、MySQL 等）：不需要 Schema 切换 ----
    if dialect != "postgresql":
        allowed, reason = _check_tool_call("db_search", keyword or "__list_all__")
        if not allowed:
            return reason

        try:
            tables = db.get_usable_table_names()
            _record_tool_call("db_search", True, keyword or "__list_all__")

            if not tables:
                return f"当前 {dialect} 数据库中没有找到任何表。"

            result_lines = [
                f"当前使用 **{dialect}** 数据库（无需 Schema 切换），"
                f"共有 **{len(tables)}** 张表：\n"
            ]
            for t in sorted(tables):
                result_lines.append(f"  - {t}")
            result_lines.append(
                "\n✅ 数据库已就绪，可以直接使用 `sql_db_schema`、`sql_db_query` 等工具进行操作。"
            )
            return "\n".join(result_lines)
        except Exception as e:
            _record_tool_call("db_search", False, keyword)
            logger.error(f"列出表失败: {e}", exc_info=True)
            return f"列出表失败: {str(e)[:200]}"

    # ---- PostgreSQL：Schema 搜索与切换 ----

    allowed, reason = _check_tool_call("db_search", keyword or "__list_all__")
    if not allowed:
        return reason

    try:
        engine = db._engine

        # 查询所有用户创建的 schema（排除系统 schema）
        with engine.connect() as conn:
            result = conn.execute(sa_text(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast', 'public') "
                "ORDER BY schema_name"
            ))
            all_schemas = [row[0] for row in result.fetchall()]

        if not all_schemas:
            _record_tool_call("db_search", True, keyword)
            return "当前数据库中没有找到任何用户 Schema。"

        # ---- 列出全部模式 ----
        if list_all:
            _record_tool_call("db_search", True, keyword or "__list_all__")
            result_lines = [f"当前数据库共有 **{len(all_schemas)}** 个可用 Schema：\n"]
            for idx, schema_name in enumerate(all_schemas, 1):
                result_lines.append(f"  {idx}. {schema_name}")
            result_lines.append(
                "\n💡 提示：请使用 `db_search` 并传入具体关键词（如某个 Schema 名称）来切换到目标 Schema，"
                "然后才能进行后续的表查询和数据操作。"
            )
            return "\n".join(result_lines)

        # ---- 关键词搜索模式 ----

        # 精确匹配
        exact_matches = [s for s in all_schemas if s == keyword]

        # 模糊匹配：schema 名称包含关键词，或关键词包含 schema 名称
        fuzzy_matches = [
            s for s in all_schemas
            if s != keyword and (keyword in s or s in keyword)
        ]

        matched = exact_matches + fuzzy_matches

        if not matched:
            _record_tool_call("db_search", True, keyword)
            # 没有匹配时，列出所有可用 schema 供参考
            schema_list = ", ".join(all_schemas[:50])
            remaining = len(all_schemas) - 50
            suffix = f"（还有 {remaining} 个未显示）" if remaining > 0 else ""
            return (
                f"未找到与 '{keyword}' 匹配的数据库/Schema。\n\n"
                f"当前可用的 Schema 共 {len(all_schemas)} 个：\n{schema_list}{suffix}\n\n"
                "请尝试使用更准确的关键词重新搜索。"
            )

        # 自动切换到第一个匹配的 schema
        target_schema = matched[0]

        # 切换 search_path 并重新初始化数据库连接
        global _db_instance
        # 注意：str(engine.url) 会把密码隐藏为 ***，必须用 render_as_string 保留真实密码
        base_uri = engine.url.render_as_string(hide_password=False)
        # 移除已有的 options 参数
        if "?" in base_uri:
            base_uri = base_uri.split("?")[0]
        new_uri = f"{base_uri}?options=-csearch_path%3D\"{target_schema}\""
        _db_instance = SQLDatabase.from_uri(new_uri, schema=target_schema, sample_rows_in_table_info=_EXAMPLE_LIMIT)

        # 获取该 schema 下的表列表
        tables = _db_instance.get_usable_table_names()

        _record_tool_call("db_search", True, keyword)

        result_lines = []
        if len(matched) == 1:
            result_lines.append(f"✅ 已找到并切换到数据库 Schema: **{target_schema}**\n")
        else:
            result_lines.append(f"✅ 找到 {len(matched)} 个匹配的 Schema，已切换到最佳匹配: **{target_schema}**\n")
            if len(matched) > 1:
                other = ", ".join(matched[1:10])
                result_lines.append(f"其他匹配: {other}\n")

        if tables:
            result_lines.append(f"该 Schema 下有 {len(tables)} 张表：")
            for t in sorted(tables):
                result_lines.append(f"  - {t}")
        else:
            result_lines.append("该 Schema 下没有表。")

        result_lines.append(
            "\n✅ 已切换到目标 Schema。现在可以使用 sql_db_schema、sql_db_query 等工具进行操作。"
        )
        return "\n".join(result_lines)

    except Exception as e:
        _record_tool_call("db_search", False, keyword)
        logger.error(f"搜索 Schema 失败: {e}", exc_info=True)
        return f"搜索 Schema 失败: {str(e)[:200]}"


# ==================== SCoT2S 自纠正工具函数 ====================
# 基于 SCoT2S（Self-Correcting Text-to-SQL）论文方法论，
# 为 correct 技能提供程序化的错误诊断与修正能力。


def _extract_sql_tables(sql: str) -> list:
    """从 SQL 中提取所有被引用的表名（FROM 和 JOIN 子句）。"""
    pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+["`]?(\w+)["`]?(?:\s+(?:AS\s+)?["`]?\w+["`]?)?',
        re.IGNORECASE,
    )
    return list(dict.fromkeys(m.group(1) for m in pattern.finditer(sql)))


def _extract_sql_columns(sql: str) -> list:
    """
    从 SQL 中提取所有列引用。返回 (table_or_alias, column) 元组列表。
    对于无表前缀的裸列名，table_or_alias 为 None。
    """
    cols = []
    # 匹配 table.column 或 "table"."column" 格式
    qualified = re.findall(
        r'["`]?(\w+)["`]?\s*\.\s*["`]?(\w+)["`]?', sql
    )
    for tbl, col in qualified:
        # 排除 SQL 关键字误匹配（如 t1.column 中的 t1 不是关键字）
        if col.upper() not in (
            "FROM", "JOIN", "WHERE", "GROUP", "ORDER", "HAVING",
            "SELECT", "ON", "AND", "OR", "AS", "BY", "LIMIT",
            "DISTINCT", "IN", "NOT", "NULL", "IS", "BETWEEN", "LIKE",
            "EXISTS", "UNION", "ALL", "INSERT", "UPDATE", "DELETE",
        ):
            cols.append((tbl, col))
    return cols


def _extract_sql_aliases(sql: str) -> dict:
    """提取 SQL 中的表别名映射。返回 {alias: table_name} 字典。"""
    alias_map = {}
    # 匹配 FROM/JOIN table AS alias 或 FROM/JOIN table alias
    pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+["`]?(\w+)["`]?\s+(?:AS\s+)?["`]?(\w+)["`]?',
        re.IGNORECASE,
    )
    for match in pattern.finditer(sql):
        table_name = match.group(1)
        alias = match.group(2)
        # 排除 SQL 关键字被误认为别名
        if alias.upper() not in (
            "ON", "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "OUTER",
            "CROSS", "FULL", "SET", "GROUP", "ORDER", "HAVING",
            "LIMIT", "UNION", "SELECT", "AND", "OR", "NATURAL",
        ):
            alias_map[alias] = table_name
    return alias_map


def _split_sql_clauses(sql: str) -> dict:
    """将 SQL 按主要子句拆分。返回 {clause_name: clause_text} 字典。"""
    clauses = {}
    # 规范化空白
    normalized = re.sub(r'\s+', ' ', sql.strip())

    clause_keywords = [
        "SELECT", "FROM", "JOIN", "INNER JOIN", "LEFT JOIN", "RIGHT JOIN",
        "FULL JOIN", "CROSS JOIN", "WHERE", "GROUP BY", "HAVING",
        "ORDER BY", "LIMIT",
    ]

    # 找到每个子句关键字的位置
    positions = []
    for kw in clause_keywords:
        pattern = re.compile(r'\b' + kw + r'\b', re.IGNORECASE)
        for m in pattern.finditer(normalized):
            positions.append((m.start(), kw.upper(), m.end()))

    positions.sort(key=lambda x: x[0])

    for i, (start, kw, kw_end) in enumerate(positions):
        if i + 1 < len(positions):
            text = normalized[kw_end:positions[i + 1][0]].strip()
        else:
            text = normalized[kw_end:].strip()
        # 合并同类 JOIN
        clause_key = "JOIN" if "JOIN" in kw else kw
        if clause_key in clauses:
            clauses[clause_key] += f" | {kw} {text}"
        else:
            clauses[clause_key] = text

    return clauses


def sql_error_classify(sql: str, error_msg: str) -> str:
    """
    对执行失败的 SQL 和错误信息进行自动分类，判定属于五类错误中的哪一类（或多类），
    并提供修复方向建议。

    五类错误（基于 SCoT2S 论文）：
    1. Schema Linking — 表名或列名不存在
    2. JOIN 操作 — 缺少或错误的 JOIN 条件
    3. GROUP BY — 缺少或不正确的分组
    4. Miscellaneous — WHERE/HAVING/ORDER BY/LIMIT 子句问题
    5. Nested — 缺少必要的子查询结构

    Args:
        sql: 执行失败的 SQL 查询
        error_msg: 数据库返回的错误信息
    """
    allowed, reason = _check_tool_call("sql_error_classify")
    if not allowed:
        return reason

    categories = []
    suggestions = []
    error_lower = error_msg.lower()
    sql_upper = sql.upper()

    # ---- 1. Schema Linking 错误检测 ----
    schema_patterns = [
        (r'no such table', '表名不存在'),
        (r'relation ".*?" does not exist', '表名不存在（PostgreSQL）'),
        (r'table .* doesn.t exist', '表名不存在（MySQL）'),
        (r'no such column', '列名不存在'),
        (r'column ".*?" does not exist', '列名不存在（PostgreSQL）'),
        (r'unknown column', '列名不存在（MySQL）'),
        (r'ambiguous column', '列名歧义，需要指定表名前缀'),
        (r'undefinedtable', '表未定义（PostgreSQL）'),
        (r'undefinedcolumn', '列未定义（PostgreSQL）'),
    ]
    for pattern, desc in schema_patterns:
        if re.search(pattern, error_lower):
            categories.append(("Schema Linking", desc))
            suggestions.append(
                "使用 `sql_schema_validate` 批量校验 SQL 中所有表名和列名，"
                "获取不匹配项及最接近的替代候选"
            )
            break

    # ---- 2. JOIN 操作错误检测 ----
    tables = _extract_sql_tables(sql)
    if len(tables) >= 2:
        has_join = bool(re.search(r'\bJOIN\b', sql_upper))
        has_on = bool(re.search(r'\bON\b', sql_upper))
        if not has_join and not has_on:
            categories.append(("JOIN 操作", "引用了多张表但缺少 JOIN 条件（笛卡尔积风险）"))
            suggestions.append(
                "使用 `sql_join_validate` 诊断 JOIN 完整性，"
                "并根据外键关系补充 JOIN 条件"
            )
        elif has_join and not has_on:
            categories.append(("JOIN 操作", "有 JOIN 子句但缺少 ON 条件"))
            suggestions.append("检查 JOIN 子句是否有完整的 ON 条件")

    # JOIN 相关错误信息
    join_error_patterns = [
        r'missing FROM-clause entry',
        r'invalid reference to FROM-clause',
    ]
    for pattern in join_error_patterns:
        if re.search(pattern, error_lower):
            if not any(c[0] == "JOIN 操作" for c in categories):
                categories.append(("JOIN 操作", "表引用错误，可能缺少 JOIN"))
                suggestions.append("使用 `sql_join_validate` 诊断 JOIN 关系")
            break

    # ---- 3. GROUP BY 错误检测 ----
    agg_funcs = re.findall(r'\b(COUNT|SUM|AVG|MAX|MIN)\s*\(', sql_upper)
    has_group_by = bool(re.search(r'\bGROUP\s+BY\b', sql_upper))
    group_by_patterns = [
        r'not in GROUP BY',
        r'must appear in the GROUP BY clause',
        r'isn.t in GROUP BY',
        r'not contained in.*aggregate',
    ]
    for pattern in group_by_patterns:
        if re.search(pattern, error_lower):
            categories.append(("GROUP BY", "SELECT 中有非聚合列未包含在 GROUP BY 中"))
            suggestions.append(
                "使用 `sql_clause_validate` 检查 GROUP BY 完整性，"
                "确保所有非聚合列都在 GROUP BY 中"
            )
            break
    if agg_funcs and not has_group_by and not any(c[0] == "GROUP BY" for c in categories):
        categories.append(("GROUP BY", "SELECT 含聚合函数但缺少 GROUP BY 子句（可能需要补充）"))
        suggestions.append("检查是否需要 GROUP BY 子句")

    # ---- 4. Miscellaneous 错误检测 ----
    misc_patterns = [
        (r'syntax error', "SQL 语法错误"),
        (r'near ".*?":', "SQL 语法错误（关键字附近）"),
        (r'operator does not exist', "运算符类型不匹配"),
        (r'invalid input syntax', "值的类型或格式错误"),
        (r'division by zero', "除零错误"),
    ]
    for pattern, desc in misc_patterns:
        if re.search(pattern, error_lower):
            categories.append(("Miscellaneous", desc))
            suggestions.append("仔细检查 WHERE/HAVING 条件中的值类型和运算符")
            break

    # ---- 5. Nested 子查询检测（基于结构分析，非错误信息）----
    has_subquery = bool(re.search(r'\(\s*SELECT\b', sql_upper))
    if not has_subquery and re.search(r'subquery|scalar', error_lower):
        categories.append(("Nested", "可能需要子查询结构"))
        suggestions.append("检查是否需要嵌套子查询（如 WHERE col = (SELECT MAX...)）")

    # ---- 无法分类时的兜底 ----
    if not categories:
        categories.append(("未知", "无法从错误信息自动判定类别"))
        suggestions.append("请手动分析错误信息，或使用三阶段流水线逐步排查")

    _record_tool_call("sql_error_classify", True)

    # ---- 格式化输出 ----
    lines = ["## SQL 错误分类结果\n"]
    lines.append(f"**错误信息**: `{error_msg[:200]}`\n")
    lines.append("| 错误类别 | 诊断说明 |")
    lines.append("|---------|---------|")
    for cat, desc in categories:
        lines.append(f"| {cat} | {desc} |")

    lines.append("\n### 建议修复方向\n")
    for i, s in enumerate(suggestions, 1):
        lines.append(f"{i}. {s}")

    lines.append("\n### 建议修复优先级\n")
    lines.append("Schema Linking(73%) → JOIN(39%) → GROUP BY(20%) → Misc(15%) → Nested(13%)")

    return "\n".join(lines)


def sql_schema_validate(sql: str) -> str:
    """
    对 SQL 中的所有表名和列名进行批量验证，返回不匹配项及其最接近的候选替代项。
    用于 SCoT2S 三阶段流水线的第 1 阶段：Schema Link 纠正。

    Args:
        sql: 待验证的 SQL 查询语句
    """
    db = _get_database()
    if db is None:
        return "错误: 数据库未连接，请设置 DATABASE_URI 环境变量"

    allowed, reason = _check_tool_call("sql_schema_validate")
    if not allowed:
        return reason

    try:
        inspector = sa_inspect(db._engine)
        all_tables = set(db.get_usable_table_names())
        all_tables_lower = {t.lower(): t for t in all_tables}

        # 提取 SQL 中的表名
        sql_tables = _extract_sql_tables(sql)
        alias_map = _extract_sql_aliases(sql)

        valid_tables = []
        invalid_tables = []  # (sql_name, [suggestions])

        for t in sql_tables:
            if t in all_tables or t.lower() in all_tables_lower:
                valid_tables.append(t)
            else:
                # 用 difflib 寻找最接近的候选
                candidates = difflib.get_close_matches(
                    t.lower(), [x.lower() for x in all_tables], n=3, cutoff=0.4
                )
                # 映射回原始大小写
                suggestions = [all_tables_lower[c] for c in candidates if c in all_tables_lower]
                invalid_tables.append((t, suggestions))

        # 解析实际表名（考虑别名）→ {实际表名: set(列名)}
        actual_table_cols = {}
        for t in valid_tables:
            actual_name = all_tables_lower.get(t.lower(), t)
            try:
                cols = inspector.get_columns(actual_name)
                actual_table_cols[actual_name] = {c["name"] for c in cols}
                # 同时记录小写映射
                actual_table_cols[actual_name.lower()] = actual_table_cols[actual_name]
            except Exception:
                pass

        # 构建别名到实际表名的映射
        resolved_aliases = {}
        for alias, tbl in alias_map.items():
            actual = all_tables_lower.get(tbl.lower(), tbl)
            resolved_aliases[alias] = actual
            resolved_aliases[alias.lower()] = actual

        # 提取并验证列引用
        sql_columns = _extract_sql_columns(sql)
        valid_cols = []
        invalid_cols = []  # (table, col, [suggestions])

        for tbl_or_alias, col in sql_columns:
            # 解析别名
            actual_table = resolved_aliases.get(tbl_or_alias)
            if not actual_table:
                actual_table = all_tables_lower.get(tbl_or_alias.lower())
            if not actual_table:
                actual_table = tbl_or_alias

            table_cols = actual_table_cols.get(actual_table, set())
            if not table_cols:
                table_cols = actual_table_cols.get(actual_table.lower(), set())

            if col in table_cols or col.lower() in {c.lower() for c in table_cols}:
                valid_cols.append(f"{actual_table}.{col}")
            elif table_cols:
                # 优先检查 _linked_schema 中是否存在该列（可能归属其他表）
                session_id = _get_session_id()
                linked_set = _get_effective_linked_schema(session_id)
                linked_suggestions = []
                for linked_item in linked_set:
                    if "." in linked_item:
                        l_tbl, l_col = linked_item.split(".", 1)
                        if l_col.lower() == col.lower() and l_tbl.lower() != actual_table.lower():
                            linked_suggestions.append(f"{l_tbl}.{l_col}")

                # 列不存在于该表，寻找最接近的候选（cutoff 0.5 — 避免低相似度误导）
                candidates = difflib.get_close_matches(
                    col.lower(), [c.lower() for c in table_cols], n=3, cutoff=0.5
                )
                # 映射回原始大小写
                col_lower_map = {c.lower(): c for c in table_cols}
                suggestions = [f"{actual_table}.{col_lower_map[c]}" for c in candidates if c in col_lower_map]

                # 链接架构中已知的列排在建议列表最前面
                if linked_suggestions:
                    suggestions = linked_suggestions + suggestions

                # 同时检查该列是否属于其他表
                cross_table_matches = []
                for other_tbl, other_cols in actual_table_cols.items():
                    if other_tbl == actual_table or other_tbl.islower():
                        continue
                    if col in other_cols or col.lower() in {c.lower() for c in other_cols}:
                        cross_table_matches.append(f"{other_tbl}.{col}")

                invalid_cols.append((actual_table, col, suggestions, cross_table_matches))
            else:
                invalid_cols.append((actual_table, col, [], []))

        _record_tool_call("sql_schema_validate", True)

        # ---- 格式化输出 ----
        lines = ["## Schema 验证结果\n"]

        # 表名验证
        if invalid_tables:
            lines.append("### 表名验证\n")
            for t, sugs in invalid_tables:
                sug_str = ", ".join(f"`{s}`" for s in sugs) if sugs else "无候选"
                lines.append(f"- :x: `{t}` 不存在 → 建议替换为: {sug_str}")
        if valid_tables:
            lines.append(f"\n:white_check_mark: 有效表: {', '.join(f'`{t}`' for t in valid_tables)}")

        # 列名验证
        if invalid_cols:
            lines.append("\n### 列名验证\n")
            for tbl, col, sugs, cross_matches in invalid_cols:
                sug_str = ", ".join(f"`{s}`" for s in sugs) if sugs else "无候选"
                line = f"- :x: `{tbl}.{col}` 不存在 → 建议替换为: {sug_str}"
                if cross_matches:
                    line += f"\n  （该列可能属于其他表: {', '.join(f'`{m}`' for m in cross_matches)}）"
                lines.append(line)
        if valid_cols:
            lines.append(f"\n:white_check_mark: 有效列引用: {', '.join(f'`{c}`' for c in valid_cols[:20])}")
            if len(valid_cols) > 20:
                lines.append(f"  ...及另外 {len(valid_cols) - 20} 个有效引用")

        # 总结
        total_issues = len(invalid_tables) + len(invalid_cols)
        _record_correction_event(
            "schema",
            issue_count=total_issues,
            has_issue=total_issues > 0,
            invalid_table_count=len(invalid_tables),
            invalid_column_count=len(invalid_cols),
            sql=sql,
        )
        if total_issues == 0:
            lines.append("\n### 结论\n")
            lines.append(":white_check_mark: SQL 中所有表名和列名验证通过，Schema Link 无误。")
        else:
            lines.append(f"\n### 结论\n")
            lines.append(
                f":x: 发现 **{total_issues}** 处 Schema Link 问题"
                f"（{len(invalid_tables)} 个表名 + {len(invalid_cols)} 个列名）。"
                "\n请根据上述建议修正 SQL 中的表名/列名引用。"
            )

        return "\n".join(lines)

    except Exception as e:
        _record_tool_call("sql_schema_validate", False)
        logger.error(f"Schema 验证失败: {e}", exc_info=True)
        return f"Schema 验证失败: {str(e)[:200]}"


def sql_join_validate(sql: str) -> str:
    """
    检查 SQL 中的 JOIN 完整性，诊断缺失或错误的 JOIN 条件。
    用于 SCoT2S 三阶段流水线的第 2 阶段：JOIN 操作纠正。

    检查项：
    1. 多表查询是否缺少 JOIN 条件（笛卡尔积风险）
    2. 现有 JOIN 条件是否与数据库外键关系一致
    3. 是否有应该 JOIN 但未 JOIN 的表对

    Args:
        sql: 待验证的 SQL 查询语句
    """
    db = _get_database()
    if db is None:
        return "错误: 数据库未连接，请设置 DATABASE_URI 环境变量"

    allowed, reason = _check_tool_call("sql_join_validate")
    if not allowed:
        return reason

    try:
        inspector = sa_inspect(db._engine)
        all_tables = set(db.get_usable_table_names())
        all_tables_lower = {t.lower(): t for t in all_tables}

        # 提取 SQL 中引用的表
        sql_tables_raw = _extract_sql_tables(sql)
        sql_tables = [all_tables_lower.get(t.lower(), t) for t in sql_tables_raw if t.lower() in all_tables_lower]
        alias_map = _extract_sql_aliases(sql)

        # 解析别名
        alias_to_table = {}
        for alias, tbl in alias_map.items():
            actual = all_tables_lower.get(tbl.lower(), tbl)
            alias_to_table[alias.lower()] = actual

        if len(sql_tables) < 2:
            _record_tool_call("sql_join_validate", True)
            return ":white_check_mark: SQL 仅涉及单表查询，无需 JOIN 验证。"

        # 收集表间所有外键关系
        fk_relations = []  # (table1, col1, table2, col2)
        for tbl in sql_tables:
            try:
                for fk in inspector.get_foreign_keys(tbl):
                    ref_table = fk.get("referred_table", "")
                    if '.' in ref_table:
                        ref_table = ref_table.split('.')[-1]
                    actual_ref = all_tables_lower.get(ref_table.lower(), ref_table)
                    if actual_ref not in sql_tables:
                        continue
                    for lc, rc in zip(
                        fk.get("constrained_columns", []),
                        fk.get("referred_columns", []),
                    ):
                        fk_relations.append((tbl, lc, actual_ref, rc))
            except Exception:
                continue

        # 提取 SQL 中的 JOIN ON 条件
        on_pattern = re.compile(
            r'\bON\s+["`]?(\w+)["`]?\s*\.\s*["`]?(\w+)["`]?\s*=\s*["`]?(\w+)["`]?\s*\.\s*["`]?(\w+)["`]?',
            re.IGNORECASE,
        )
        existing_joins = []
        for m in on_pattern.finditer(sql):
            t1, c1, t2, c2 = m.group(1), m.group(2), m.group(3), m.group(4)
            # 解析别名
            actual_t1 = alias_to_table.get(t1.lower(), all_tables_lower.get(t1.lower(), t1))
            actual_t2 = alias_to_table.get(t2.lower(), all_tables_lower.get(t2.lower(), t2))
            existing_joins.append((actual_t1, c1, actual_t2, c2))

        # 检测是否使用了隐式 JOIN（FROM a, b WHERE a.id = b.a_id）
        where_join_pattern = re.compile(
            r'WHERE.*?["`]?(\w+)["`]?\s*\.\s*["`]?(\w+)["`]?\s*=\s*["`]?(\w+)["`]?\s*\.\s*["`]?(\w+)["`]?',
            re.IGNORECASE | re.DOTALL,
        )
        implicit_joins = []
        for m in where_join_pattern.finditer(sql):
            t1, c1, t2, c2 = m.group(1), m.group(2), m.group(3), m.group(4)
            actual_t1 = alias_to_table.get(t1.lower(), all_tables_lower.get(t1.lower(), t1))
            actual_t2 = alias_to_table.get(t2.lower(), all_tables_lower.get(t2.lower(), t2))
            implicit_joins.append((actual_t1, c1, actual_t2, c2))

        all_join_conditions = existing_joins + implicit_joins

        _record_tool_call("sql_join_validate", True)

        # ---- 诊断 ----
        lines = ["## JOIN 验证结果\n"]
        lines.append(f"**涉及的表**: {', '.join(f'`{t}`' for t in sql_tables)}\n")
        issues = []

        # (a) 检查是否有多表但完全无 JOIN 条件
        if not all_join_conditions:
            issues.append(
                ":x: **笛卡尔积风险**: SQL 引用了多张表但没有任何 JOIN 条件。"
            )

        # (b) 验证现有 JOIN 条件是否匹配外键
        if fk_relations:
            lines.append("### 数据库外键关系\n")
            for t1, c1, t2, c2 in fk_relations:
                lines.append(f"  - `{t1}.{c1}` = `{t2}.{c2}`")

            if all_join_conditions:
                lines.append("\n### 现有 JOIN 条件\n")
                for t1, c1, t2, c2 in all_join_conditions:
                    # 检查是否与某条 FK 匹配
                    matched = False
                    for ft1, fc1, ft2, fc2 in fk_relations:
                        if (
                            (t1.lower() == ft1.lower() and c1.lower() == fc1.lower() and
                             t2.lower() == ft2.lower() and c2.lower() == fc2.lower())
                            or
                            (t1.lower() == ft2.lower() and c1.lower() == fc2.lower() and
                             t2.lower() == ft1.lower() and c2.lower() == fc1.lower())
                        ):
                            matched = True
                            break
                    status = ":white_check_mark:" if matched else ":warning:"
                    lines.append(f"  {status} `{t1}.{c1}` = `{t2}.{c2}`")
                    if not matched:
                        issues.append(
                            f":warning: JOIN 条件 `{t1}.{c1} = {t2}.{c2}` "
                            "与数据库外键关系不匹配，请确认是否正确。"
                        )

        # (c) 检查是否有表对缺少 JOIN
        joined_pairs = set()
        for t1, c1, t2, c2 in all_join_conditions:
            joined_pairs.add((min(t1.lower(), t2.lower()), max(t1.lower(), t2.lower())))

        missing_joins = []
        for ft1, fc1, ft2, fc2 in fk_relations:
            pair = (min(ft1.lower(), ft2.lower()), max(ft1.lower(), ft2.lower()))
            if pair not in joined_pairs:
                missing_joins.append((ft1, fc1, ft2, fc2))

        if missing_joins:
            lines.append("\n### 缺失的 JOIN 条件\n")
            for ft1, fc1, ft2, fc2 in missing_joins:
                lines.append(f"  - 建议添加: `JOIN {ft2} ON {ft1}.{fc1} = {ft2}.{fc2}`")
                issues.append(
                    f":x: 表 `{ft1}` 和 `{ft2}` 之间有外键关系但缺少 JOIN 条件"
                )

        if not fk_relations:
            lines.append("\n:information_source: 这些表之间没有定义外键关系，请通过列名推断可能的关联（如 xxx_id 列）。")

        # 总结
        lines.append("\n### 结论\n")
        _record_correction_event(
            "join",
            issue_count=len(issues),
            has_issue=bool(issues),
            sql=sql,
        )
        if not issues:
            lines.append(":white_check_mark: JOIN 验证通过，所有多表关联条件完整且与外键关系一致。")
        else:
            lines.append(f"发现 **{len(issues)}** 个 JOIN 问题：\n")
            for issue in issues:
                lines.append(f"- {issue}")

        return "\n".join(lines)

    except Exception as e:
        _record_tool_call("sql_join_validate", False)
        logger.error(f"JOIN 验证失败: {e}", exc_info=True)
        return f"JOIN 验证失败: {str(e)[:200]}"


def sql_clause_validate(sql: str, question: str) -> str:
    """
    基于用户问题语义，检查 SQL 中 GROUP BY / HAVING / ORDER BY / LIMIT / 嵌套子查询等子句的完整性和正确性。
    用于 SCoT2S 三阶段流水线的第 3 阶段：其他子句纠正。

    Args:
        sql: 待验证的 SQL 查询语句
        question: 用户的原始问题（自然语言），用于语义比对
    """
    allowed, reason = _check_tool_call("sql_clause_validate")
    if not allowed:
        return reason

    sql_upper = sql.upper()
    question_lower = question.lower()
    issues = []

    # ---- 1. GROUP BY 检查 ----
    agg_funcs = re.findall(r'\b(COUNT|SUM|AVG|MAX|MIN)\s*\(', sql_upper)
    has_group_by = bool(re.search(r'\bGROUP\s+BY\b', sql_upper))

    # 问题中是否暗含分组语义
    group_signals_zh = ["每个", "各个", "各", "按", "分组", "统计", "分别"]
    group_signals_en = ["per ", "each ", "every ", "group by", "by each", "for each", "breakdown"]
    has_group_signal = (
        any(s in question_lower for s in group_signals_zh)
        or any(s in question_lower for s in group_signals_en)
    )

    if agg_funcs and not has_group_by:
        # SELECT 中有聚合函数但无 GROUP BY
        # 提取 SELECT 子句中的非聚合列
        select_match = re.search(r'\bSELECT\b(.*?)\bFROM\b', sql_upper, re.DOTALL)
        if select_match:
            select_clause = select_match.group(1)
            # 简单检测：去掉聚合函数后是否还有列引用
            without_agg = re.sub(r'\b(COUNT|SUM|AVG|MAX|MIN)\s*\([^)]*\)', '', select_clause)
            remaining_cols = re.findall(r'["`]?\w+["`]?\s*\.\s*["`]?\w+["`]?|\b(?!AS\b|DISTINCT\b)[a-zA-Z_]\w*', without_agg)
            remaining_cols = [c.strip() for c in remaining_cols if c.strip() and c.strip() != ',']
            if remaining_cols:
                issues.append({
                    "category": "GROUP BY",
                    "severity": "错误",
                    "detail": f"SELECT 中包含聚合函数（{', '.join(agg_funcs[:3])}）和非聚合列（{', '.join(remaining_cols[:3])}），但缺少 GROUP BY 子句。",
                    "suggestion": f"添加 `GROUP BY {', '.join(remaining_cols[:3])}` 子句。",
                })
    elif has_group_signal and not agg_funcs and not has_group_by:
        issues.append({
            "category": "GROUP BY",
            "severity": "警告",
            "detail": "用户问题暗含分组/按类统计的语义，但 SQL 中缺少聚合函数和 GROUP BY。",
            "suggestion": "检查是否需要 COUNT/SUM/AVG 等聚合函数配合 GROUP BY 使用。",
        })

    # ---- 1b. 聚合冗余检查（对预聚合列重复套用聚合函数）----
    agg_on_preagg_pattern = re.compile(
        r'\b(AVG|SUM|MAX|MIN|COUNT)\s*\(\s*'
        r'(?:["`]?\w+["`]?\s*\.\s*)?'  # 可选的表前缀
        r'["`]?(Avg\w+|Average\w+|Mean\w+|Max\w+|Min\w+|Total\w+|Sum\w+|Count\w+|Num\w+)["`]?'
        r'\s*\)',
        re.IGNORECASE
    )
    preagg_matches = agg_on_preagg_pattern.findall(sql)
    if preagg_matches:
        for agg_func, col_name in preagg_matches:
            issues.append({
                "category": "聚合冗余",
                "severity": "错误",
                "detail": (
                    f"对预聚合列 `{col_name}` 使用了 {agg_func.upper()}()，"
                    f"可能产生'average of averages'错误。"
                    f"该列名前缀暗示其已存储聚合值。"
                ),
                "suggestion": (
                    f"移除 {agg_func.upper()}({col_name})，直接引用 {col_name}。"
                    f"例如：ORDER BY {col_name} DESC LIMIT 1"
                ),
            })

    # ---- 2. HAVING vs WHERE 检查 ----
    has_having = bool(re.search(r'\bHAVING\b', sql_upper))
    has_where = bool(re.search(r'\bWHERE\b', sql_upper))

    # 检查 WHERE 中是否误用了聚合函数
    if has_where:
        where_match = re.search(r'\bWHERE\b(.*?)(?:\bGROUP\b|\bORDER\b|\bLIMIT\b|\bHAVING\b|$)', sql_upper, re.DOTALL)
        if where_match:
            where_clause = where_match.group(1)
            agg_in_where = re.findall(r'\b(COUNT|SUM|AVG|MAX|MIN)\s*\(', where_clause)
            if agg_in_where:
                issues.append({
                    "category": "HAVING vs WHERE",
                    "severity": "错误",
                    "detail": f"WHERE 子句中使用了聚合函数（{', '.join(agg_in_where)}），聚合条件应放在 HAVING 中。",
                    "suggestion": "将聚合函数的过滤条件从 WHERE 移到 HAVING 子句中。",
                })

    # 问题中是否暗含聚合过滤
    agg_filter_signals = ["超过", "大于", "至少", "不少于", "多于", "more than", "at least", "greater than", "exceed"]
    has_agg_filter_signal = any(s in question_lower for s in agg_filter_signals)
    if has_agg_filter_signal and agg_funcs and has_group_by and not has_having:
        issues.append({
            "category": "HAVING",
            "severity": "警告",
            "detail": "用户问题暗含对聚合结果的过滤（如'超过N个'），SQL 有 GROUP BY 和聚合函数但缺少 HAVING。",
            "suggestion": "检查是否需要添加 HAVING 子句来过滤聚合结果。",
        })

    # ---- 3. 嵌套子查询检查 ----
    has_subquery = bool(re.search(r'\(\s*SELECT\b', sql_upper))
    extreme_signals_zh = ["最高", "最低", "最大", "最小", "最多", "最少", "最长", "最短"]
    extreme_signals_en = ["highest", "lowest", "maximum", "minimum", "most", "least", "largest", "smallest"]
    has_extreme_signal = (
        any(s in question_lower for s in extreme_signals_zh)
        or any(s in question_lower for s in extreme_signals_en)
    )

    set_signals_zh = ["在…之中", "属于", "包含在"]
    set_signals_en = ["among", "in the set", "belong to"]
    has_set_signal = (
        any(s in question_lower for s in set_signals_zh)
        or any(s in question_lower for s in set_signals_en)
    )

    has_order_limit = (
        bool(re.search(r'\bORDER\s+BY\b', sql_upper))
        and bool(re.search(r'\bLIMIT\b', sql_upper))
    )

    if has_extreme_signal and not has_subquery and not has_order_limit:
        issues.append({
            "category": "Nested/极值",
            "severity": "警告",
            "detail": "用户问题包含极值语义（最高/最低等），但 SQL 中既无子查询也无 ORDER BY + LIMIT。",
            "suggestion": "考虑使用 `WHERE col = (SELECT MAX/MIN(col) ...)` 或 `ORDER BY col DESC LIMIT 1`。",
        })

    if has_set_signal and not has_subquery:
        issues.append({
            "category": "Nested/集合",
            "severity": "警告",
            "detail": "用户问题暗含集合判定语义，但 SQL 中没有子查询。",
            "suggestion": "考虑使用 `WHERE col IN (SELECT ...)` 子查询。",
        })

    # ---- 4. ORDER BY + LIMIT 检查 ----
    topn_signals_zh = ["前", "排名", "前几", "前n", "top"]
    topn_signals_en = ["top ", "first ", "ranking", "rank"]
    has_topn_signal = (
        any(s in question_lower for s in topn_signals_zh)
        or any(s in question_lower for s in topn_signals_en)
    )

    has_order_by = bool(re.search(r'\bORDER\s+BY\b', sql_upper))
    has_limit = bool(re.search(r'\bLIMIT\b', sql_upper))

    if has_topn_signal and not has_order_by:
        issues.append({
            "category": "ORDER BY",
            "severity": "警告",
            "detail": "用户问题包含排名/Top N 语义，但 SQL 缺少 ORDER BY 子句。",
            "suggestion": "添加 ORDER BY + LIMIT 实现 Top N 排序。",
        })
    elif has_topn_signal and has_order_by and not has_limit:
        issues.append({
            "category": "LIMIT",
            "severity": "警告",
            "detail": "有 ORDER BY 但缺少 LIMIT，无法限制为 Top N 结果。",
            "suggestion": "添加 LIMIT N 子句。",
        })

    # 检查 ORDER BY 方向
    if has_order_by:
        # 检查 "最高/最大" 是否用了 ASC（应为 DESC）
        desc_signals = ["最高", "最大", "最多", "最长", "highest", "largest", "most", "maximum"]
        asc_signals = ["最低", "最小", "最少", "最短", "lowest", "smallest", "least", "minimum"]
        has_desc_signal = any(s in question_lower for s in desc_signals)
        has_asc_signal = any(s in question_lower for s in asc_signals)

        order_match = re.search(r'\bORDER\s+BY\b\s+.*?\b(ASC|DESC)\b', sql_upper)
        if order_match:
            direction = order_match.group(1)
            if has_desc_signal and direction == "ASC":
                issues.append({
                    "category": "ORDER BY 方向",
                    "severity": "警告",
                    "detail": "用户问题暗含'最大/最高'语义，但 ORDER BY 使用了 ASC（升序）。",
                    "suggestion": "将 ORDER BY 改为 DESC（降序）。",
                })
            elif has_asc_signal and direction == "DESC":
                issues.append({
                    "category": "ORDER BY 方向",
                    "severity": "警告",
                    "detail": "用户问题暗含'最小/最低'语义，但 ORDER BY 使用了 DESC（降序）。",
                    "suggestion": "将 ORDER BY 改为 ASC（升序）。",
                })

    # ---- 5. DISTINCT 检查 ----
    distinct_signals_zh = ["不同的", "唯一的", "去重", "不重复", "列出所有不同", "列出所有不重复"]
    distinct_signals_en = ["distinct", "unique", "different", "non-repeating", "deduplicate"]
    has_distinct_signal = (
        any(s in question_lower for s in distinct_signals_zh)
        or any(s in question_lower for s in distinct_signals_en)
    )
    has_distinct = bool(re.search(r'\bDISTINCT\b', sql_upper))
    has_count_distinct = bool(re.search(r'\bCOUNT\s*\(\s*DISTINCT\b', sql_upper))
    has_top_level_select_distinct = bool(re.search(r'^\s*SELECT\s+DISTINCT\b', sql_upper))

    if has_distinct_signal and not has_distinct:
        issues.append({
            "category": "DISTINCT",
            "severity": "建议",
            "detail": "用户问题包含去重或唯一结果语义，但 SQL 中未体现 DISTINCT / COUNT(DISTINCT ...) 的约束。",
            "suggestion": "先确认最终答案粒度；如果题目要的是唯一集合或去重计数，再考虑使用 DISTINCT。",
        })

    if has_top_level_select_distinct and not has_count_distinct and not has_distinct_signal:
        issues.append({
            "category": "DISTINCT",
            "severity": "警告",
            "detail": "用户问题没有明确的去重或唯一结果语义，但 SQL 使用了 SELECT DISTINCT，可能掩盖了 join 粒度或投影列选择错误。",
            "suggestion": "优先检查答案粒度、JOIN 条件、投影列和过滤条件；如果 DISTINCT 只是让结果更干净，建议移除。",
        })

    # ---- 6. SQLite 整数除法检查（SQL-of-Thought 错误分类法扩展：Arithmetic 类别）----
    div_issues = _detect_integer_division(sql)
    for d in div_issues:
        issues.append({
            "category": "整数除法",
            "severity": "错误",
            "detail": (
                f"SQLite 整数除法截断: `{d['left_operand']} / {d['right_operand']}` "
                f"两个操作数均为 INTEGER 类型，结果将被截断为整数（如 3/5=0）。"
            ),
            "suggestion": f"使用 `{d['suggestion']}` 确保浮点除法。",
        })

    _record_tool_call("sql_clause_validate", True)

    # ---- 格式化输出 ----
    lines = ["## 子句验证结果\n"]

    _record_correction_event(
        "clause",
        issue_count=len(issues),
        has_issue=bool(issues),
        issue_categories=[issue["category"] for issue in issues],
        sql=sql,
    )
    if not issues:
        lines.append(":white_check_mark: SQL 子句验证通过，GROUP BY / HAVING / ORDER BY / LIMIT / Nested 等结构与问题语义一致。")
    else:
        lines.append(f"发现 **{len(issues)}** 个子句问题：\n")
        lines.append("| 类别 | 严重程度 | 问题描述 | 修正建议 |")
        lines.append("|------|---------|---------|---------|")
        for issue in issues:
            lines.append(
                f"| {issue['category']} | {issue['severity']} "
                f"| {issue['detail']} | {issue['suggestion']} |"
            )

    return "\n".join(lines)


def sql_diff_report(original_sql: str, corrected_sql: str) -> str:
    """
    生成原始 SQL 与修正后 SQL 的结构化差异报告，便于理解修正了什么及为什么修正。
    按子句（SELECT / FROM / JOIN / WHERE / GROUP BY / HAVING / ORDER BY / LIMIT）逐一对比，
    并为每处差异标注可能的错误类别。

    Args:
        original_sql: 修正前的原始 SQL
        corrected_sql: 修正后的 SQL
    """
    allowed, reason = _check_tool_call("sql_diff_report")
    if not allowed:
        return reason

    _record_tool_call("sql_diff_report", True)

    orig_clauses = _split_sql_clauses(original_sql)
    corr_clauses = _split_sql_clauses(corrected_sql)

    all_keys = list(dict.fromkeys(
        list(orig_clauses.keys()) + list(corr_clauses.keys())
    ))

    lines = ["## SQL 修正对比报告\n"]
    lines.append("| 子句 | 原始 SQL | 修正后 SQL | 变更类型 |")
    lines.append("|------|---------|-----------|---------|")

    changes_count = 0
    for key in all_keys:
        orig = orig_clauses.get(key, "（无）")
        corr = corr_clauses.get(key, "（无）")

        # 规范化比较
        orig_norm = re.sub(r'\s+', ' ', orig.strip().lower())
        corr_norm = re.sub(r'\s+', ' ', corr.strip().lower())

        if orig_norm == corr_norm:
            lines.append(f"| {key} | `{orig[:60]}` | 无变更 | — |")
        else:
            changes_count += 1
            # 推断变更类型
            change_type = "Miscellaneous"
            if key in ("FROM",):
                change_type = "Schema Linking"
            elif key == "JOIN":
                change_type = "JOIN 操作"
            elif key == "GROUP BY":
                change_type = "GROUP BY"
            elif key == "HAVING":
                change_type = "HAVING/WHERE"
            elif key == "SELECT":
                # SELECT 变更可能是 Schema Link 或 DISTINCT
                if "DISTINCT" in corr.upper() and "DISTINCT" not in orig.upper():
                    change_type = "DISTINCT 添加"
                else:
                    change_type = "Schema Linking / SELECT 修正"
            elif key in ("ORDER BY", "LIMIT"):
                change_type = "ORDER BY / LIMIT"
            elif key == "WHERE":
                change_type = "WHERE 条件修正"

            # 截断显示
            orig_display = orig[:60] + "..." if len(orig) > 60 else orig
            corr_display = corr[:60] + "..." if len(corr) > 60 else corr
            lines.append(f"| {key} | `{orig_display}` | `{corr_display}` | **{change_type}** |")

    lines.append(f"\n**共 {changes_count} 处变更**。")

    if changes_count == 0:
        lines.append("\n:white_check_mark: 两条 SQL 完全相同，无需修正。")

    return "\n".join(lines)


def sql_self_correct(sql: str, question: str, max_rounds: int = 3) -> str:
    """
    SCoT2S 三阶段自纠正流水线的主编排函数。
    接收初始 SQL 和用户问题，自动执行三阶段流水线诊断：
    1. Schema Link 纠正 — 验证并修正表名/列名
    2. JOIN 操作纠正 — 诊断 JOIN 完整性
    3. 其他子句纠正 — 检查 GROUP BY / HAVING / ORDER BY / LIMIT / Nested

    每阶段返回诊断结果。对于 Schema Link 错误，尝试自动替换；
    对于 JOIN 和子句错误，返回诊断报告供 LLM 决策修正。

    Args:
        sql: 初始 SQL 查询（可能包含错误）
        question: 用户的原始问题（自然语言）
        max_rounds: 最大修正轮次（默认 3），每轮对应一个阶段
    """
    allowed, reason = _check_tool_call("sql_self_correct")
    if not allowed:
        return reason

    # 会话级调用次数限制：防止 LLM 反复调用编排函数导致工具调用膨胀
    session_id = _get_session_id()
    count = _self_correct_counts.get(session_id, 0)
    if count >= _SELF_CORRECT_MAX_PER_SESSION:
        _record_tool_call("sql_self_correct", False)
        return (
            f"⚠️ **sql_self_correct 已在本会话中调用 {count} 次，已达上限（{_SELF_CORRECT_MAX_PER_SESSION} 次）。**\n\n"
            "自纠正流水线未能解决问题，请改为：\n"
            "1. 手动检查表结构（`sql_db_schema`）和外键关系（`sql_db_table_relationship`）\n"
            "2. 使用单个诊断工具（`sql_schema_validate` / `sql_join_validate` / `sql_clause_validate`）定向排查\n"
            "3. 或向用户说明当前遇到的困难"
        )
    _self_correct_counts[session_id] = count + 1
    _record_correction_event(
        "self_correct",
        status="triggered",
        sql=sql,
        question=question,
        max_rounds=max_rounds,
        invocation_count=_self_correct_counts[session_id],
    )

    current_sql = sql
    report_lines = ["## SCoT2S 三阶段自纠正流水线\n"]
    report_lines.append(f"**原始 SQL**:\n```sql\n{sql}\n```\n")
    report_lines.append(f"**用户问题**: {question}\n")

    # ---- 第 0 阶段：语法修复（不改变列选择）----
    report_lines.append("---\n### 第 0 阶段：语法修复\n")
    with _sql_execution_scope("sql_self_correct"):
        syntax_result = sql_syntax_fix(current_sql, "")
    if "语法修复成功" in syntax_result:
        report_lines.append(syntax_result)
        report_lines.append("\n✅ **语法修复已解决问题！** 流水线提前终止。\n")
        _record_tool_call("sql_self_correct", True)
        return "\n".join(report_lines)
    elif "语法修复已应用" in syntax_result:
        # 语法修复应用了但执行仍失败 → 提取修复后的 SQL 继续流水线
        fix_match = re.search(r'```sql\n(.+?)\n```', syntax_result, re.DOTALL)
        if fix_match:
            current_sql = fix_match.group(1)
        report_lines.append(syntax_result)
        report_lines.append("\n语法修复已应用但问题未完全解决，继续 Schema Link 纠正。\n")
    else:
        report_lines.append("无可自动修复的语法问题，继续 Schema Link 纠正。\n")

    # ---- 第 1 阶段：Schema Link 纠正 ----
    report_lines.append("---\n### 第 1 阶段：Schema Link 纠正\n")
    schema_result = sql_schema_validate(current_sql)
    report_lines.append(schema_result)

    # 尝试自动替换 Schema 错误（仅高相似度替换，避免用无关列替代）
    if "不存在" in schema_result and "建议替换为" in schema_result:
        # 解析出替换建议并自动应用
        replacements = []
        skipped_low_sim = []
        # 匹配表名替换: `xxx` 不存在 → 建议替换为: `yyy`
        for m in re.finditer(r'`(\w+)` 不存在 → 建议替换为: `(\w+)`', schema_result):
            old_name, new_name = m.group(1), m.group(2)
            if old_name != new_name:
                replacements.append((old_name, new_name))
        # 匹配列名替换: `table.col` 不存在 → 建议替换为: `table.newcol`
        for m in re.finditer(r'`(\w+\.\w+)` 不存在 → 建议替换为: `(\w+\.\w+)`', schema_result):
            old_ref, new_ref = m.group(1), m.group(2)
            old_parts = old_ref.split(".")
            new_parts = new_ref.split(".")
            if len(old_parts) == 2 and len(new_parts) == 2:
                if old_parts[1] != new_parts[1]:
                    # 相似度门控：仅当列名相似度 >= 0.6 时自动替换，否则仅报告
                    sim = difflib.SequenceMatcher(
                        None, old_parts[1].lower(), new_parts[1].lower()
                    ).ratio()
                    if sim >= 0.6:
                        replacements.append((old_parts[1], new_parts[1]))
                    else:
                        skipped_low_sim.append(
                            (old_parts[1], new_parts[1], f"{sim:.2f}")
                        )

        if skipped_low_sim:
            skip_info = "; ".join(
                f"{old}→{new}(相似度{s})" for old, new, s in skipped_low_sim
            )
            report_lines.append(
                f"\n⚠️ 跳过低相似度自动替换（可能是无关列）: {skip_info}\n"
                "请根据用户问题语义判断是否需要手动替换。\n"
            )

        if replacements:
            modified_sql = current_sql
            for old, new in replacements:
                # 用 word boundary 替换避免部分匹配
                modified_sql = re.sub(
                    r'\b' + re.escape(old) + r'\b',
                    new,
                    modified_sql,
                )
            if modified_sql != current_sql:
                report_lines.append(f"\n**自动替换后 SQL**:\n```sql\n{modified_sql}\n```\n")
                current_sql = modified_sql

                # 试执行
                try:
                    with _sql_execution_scope("sql_self_correct"):
                        exec_result = sql_db_query(current_sql)
                    if "查询成功" in exec_result:
                        report_lines.append(":white_check_mark: **Schema Link 纠正后执行成功！** 流水线提前终止。\n")
                        report_lines.append(f"**最终 SQL**:\n```sql\n{current_sql}\n```")
                        _record_tool_call("sql_self_correct", True)
                        return "\n".join(report_lines)
                    else:
                        report_lines.append(f"Schema Link 纠正后执行结果:\n{exec_result[:200]}\n")
                except Exception as e:
                    report_lines.append(f"Schema Link 纠正后执行失败: {str(e)[:100]}\n")
    else:
        report_lines.append("\n:white_check_mark: Schema Link 验证通过。\n")

    if max_rounds < 2:
        _record_tool_call("sql_self_correct", True)
        report_lines.append(f"\n**当前 SQL**:\n```sql\n{current_sql}\n```")
        return "\n".join(report_lines)

    # ---- 第 2 阶段：JOIN 操作纠正 ----
    report_lines.append("---\n### 第 2 阶段：JOIN 操作纠正\n")
    join_result = sql_join_validate(current_sql)
    report_lines.append(join_result)

    if "验证通过" in join_result:
        report_lines.append("\n:white_check_mark: JOIN 验证通过。\n")
    else:
        report_lines.append("\n:warning: JOIN 存在问题，请根据上述诊断修正 SQL 中的 JOIN 条件。\n")

    if max_rounds < 3:
        _record_tool_call("sql_self_correct", True)
        report_lines.append(f"\n**当前 SQL**:\n```sql\n{current_sql}\n```")
        return "\n".join(report_lines)

    # ---- 第 3 阶段：其他子句纠正 ----
    report_lines.append("---\n### 第 3 阶段：其他子句纠正\n")
    clause_result = sql_clause_validate(current_sql, question)
    report_lines.append(clause_result)

    if "验证通过" in clause_result:
        report_lines.append("\n:white_check_mark: 子句验证通过。\n")
    else:
        report_lines.append("\n:warning: 子句存在问题，请根据上述诊断修正 SQL。\n")

    # ---- 生成差异报告 ----
    if current_sql != sql:
        report_lines.append("---\n### 修正对比\n")
        diff_result = sql_diff_report(sql, current_sql)
        report_lines.append(diff_result)

    report_lines.append(f"\n**最终 SQL**:\n```sql\n{current_sql}\n```")

    _record_tool_call("sql_self_correct", True)
    return "\n".join(report_lines)
