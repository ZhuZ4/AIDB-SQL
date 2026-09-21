"""
Google ADK Text-to-SQL Agent - Web 服务版

架构特性（移植自 DeepAgent）：
1. 多阶段执行：思考规划 → 执行 → 回答/报告
2. 实时 SSE 流推送，思考过程用 <details> 包裹，内容直接输出
3. 工具调用管理（防死循环、防重复）
4. 不保存对话历史记录
"""

import ast
import asyncio
from collections import OrderedDict
import json
import logging
import os
import pathlib
import re
import sys
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# 支持直接运行 (python agent.py) 时的相对导入
if not __package__:
    _current_dir = os.path.dirname(os.path.abspath(__file__))
    _parent_dir = os.path.dirname(_current_dir)
    if _parent_dir not in sys.path:
        sys.path.insert(0, _parent_dir)
    if _current_dir not in sys.path:
        sys.path.insert(0, _current_dir)
    __package__ = "adk_agent"

from google.adk.agents import Agent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner, Runner
from google.adk.sessions import InMemorySessionService
from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types as genai_types

from tools.native_sql_tools import (
    set_database_uri,
    get_linked_schema,
    get_linked_schema_snapshot,
    reset_linked_schema,
    get_sql_execution_trace,
    reset_sql_execution_trace,
    get_correction_events,
    reset_correction_events,
    get_final_sql,
    reset_final_sql,
    reset_session,
    set_experiment_profile,
    db_search,
    sql_db_list_tables,
    sql_db_query,
    sql_db_query_checker,
    sql_db_schema,
    sql_db_table_relationship,
    sql_db_value_lookup,
    add_schema,
    build_linked_mschema,
    save_report,
    submit_final_sql,
    append_report_content,
    reset_report_content,
    sql_error_classify,
    sql_schema_validate,
    sql_join_validate,
    sql_clause_validate,
    sql_diff_report,
    sql_self_correct,
    sql_syntax_fix,
    _auto_quote_sql_identifiers,
)
from tools.tool_call_manager import get_tool_call_manager
from utils import create_model

# 尝试导入 Web 服务相关模块（可选依赖，独立运行时无需这些模块）
try:
    from constants import DataTypeEnum, IntentEnum
    from services import add_user_record, decode_jwt_token
    _HAS_WEB_DEPS = True
except ImportError:
    _HAS_WEB_DEPS = False

    # 内联枚举值，保证独立运行时不报错
    class _FakeEnum:
        def __init__(self, val): self.value = val

    class DataTypeEnum:
        ANSWER = _FakeEnum(("t02", "答案"))
        STREAM_END = _FakeEnum(("t99", "流式推流结束"))

    class IntentEnum:
        REPORT_QA = _FakeEnum(("REPORT_QA", "深度搜索"))

    async def decode_jwt_token(token):
        return {"id": "local_user"}

    async def add_user_record(**kwargs):
        return None

logger = logging.getLogger(__name__)


def _safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        print(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"
        ), **{k: v for k, v in kwargs.items() if k != "sep"})

current_dir = os.path.dirname(os.path.abspath(__file__))


def get_sql_tools() -> list:
    """唯一的 SQL 工具列表 — 所有调用方共用，避免不同步。"""
    return [
        db_search,
        sql_db_list_tables,
        sql_db_schema,
        sql_db_query,
        sql_db_query_checker,
        sql_db_table_relationship,
        sql_db_value_lookup,
        add_schema,
        build_linked_mschema,
        save_report,
        submit_final_sql,
        sql_error_classify,
        sql_schema_validate,
        sql_join_validate,
        sql_clause_validate,
        sql_diff_report,
        sql_self_correct,
        sql_syntax_fix,
    ]


# ==================== 阶段枚举与追踪 ====================


class Phase(Enum):
    """Agent 执行阶段"""

    PLANNING = "planning"  # 思考规划（首次工具调用前的输出）
    EXECUTION = "execution"  # 执行回答（默认阶段）
    REPORTING = "reporting"  # 报告生成（HTML 标记透传）


@dataclass
class PhaseTracker:
    """
    阶段追踪器：管理 <details> 标签的开关状态

    核心职责：
    - 追踪当前执行阶段
    - 管理 <details> 区域的打开/关闭
    - 判断是否已进入正式内容阶段
    """

    current_phase: Phase = Phase.PLANNING
    planning_opened: bool = False
    has_tool_called: bool = False
    has_sent_content: bool = False


# ==================== <details> 标签模板 ====================

THINKING_SECTION_OPEN = (
    '<details open style="margin:8px 0;padding:8px 12px;background:#f8f9fa;'
    "border-left:3px solid #4a90d9;border-radius:4px;font-size:14px;color:#555"
    '">\n'
    '<summary style="cursor:pointer;font-weight:600;color:#333">'
    "🧠 思考与规划</summary>\n\n"
)

SECTION_CLOSE = "\n</details>\n"


# ==================== AdkAgent 主类 ====================


class AdkAgent:
    """基于 Google ADK 的多阶段 Text-to-SQL 智能体"""

    DEFAULT_RECURSION_LIMIT = 150
    DEFAULT_LLM_TIMEOUT = 15 * 60
    STREAM_KEEPALIVE_INTERVAL = 25
    TASK_TIMEOUT = 30 * 60
    APP_NAME = "adk_text2sql"

    def __init__(self):
        self.tool_manager = get_tool_call_manager()
        self._skills = self._load_skills()
        self._instruction_text = self._load_agents_instruction()

        self.RECURSION_LIMIT = int(
            os.getenv("RECURSION_LIMIT", self.DEFAULT_RECURSION_LIMIT)
        )
        self.LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", self.DEFAULT_LLM_TIMEOUT))

        # 多轮上下文缓存：effective_session_id -> (runner, session_service, adk_session, user_id)
        # run_agent 复用同 session_id 的 ADK Session，保留历史消息；
        # run_query（测试路径）不使用该缓存，仍每次新建并 reset。
        self._session_runtime: "OrderedDict[str, tuple]" = OrderedDict()
        self._session_runtime_cap = int(os.getenv("ADK_SESSION_CACHE_SIZE", "64"))

    # ==================== 指令与技能加载 ====================

    def _load_agents_instruction(self) -> str:
        """加载 AGENTS.md 主指令作为 Agent instruction"""
        agents_md = os.path.join(current_dir, "AGENTS.md")
        if os.path.exists(agents_md):
            with open(agents_md, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _load_skills(self) -> list:
        """
        使用 google.adk.skills.load_skill_from_dir 加载所有技能

        ADK 的 SkillToolset 会将技能暴露为 list_skills / load_skill / load_skill_resource
        三个工具，LLM 根据任务需求按需调用，而非一股脑全部注入 instruction。
        """
        skills_dir = pathlib.Path(current_dir) / "skills"
        skills = []
        if skills_dir.exists():
            for skill_path in sorted(skills_dir.iterdir()):
                if skill_path.is_dir() and (skill_path / "SKILL.md").exists():
                    try:
                        skill = load_skill_from_dir(skill_path)
                        skills.append(skill)
                        logger.info(f"加载技能: {skill.name}")
                    except Exception as e:
                        logger.warning(f"加载技能 {skill_path.name} 失败: {e}")
        return skills

    def _create_skill_toolset(self) -> SkillToolset:
        """创建 ADK SkillToolset，按需向 LLM 暴露技能"""
        return SkillToolset(skills=self._skills)

    def get_available_skills(self) -> list:
        """获取所有可用的技能列表"""
        return [
            {"name": s.name, "description": s.frontmatter.description or ""}
            for s in self._skills
        ]

    # ==================== SSE 响应工具方法 ====================

    @staticmethod
    def _create_response(
        content: str,
        message_type: str = "continue",
        data_type: str = DataTypeEnum.ANSWER.value[0],
    ) -> str:
        """封装 SSE 响应结构"""
        res = {
            "data": {"messageType": message_type, "content": content},
            "dataType": data_type,
        }
        return "data:" + json.dumps(res, ensure_ascii=False) + "\n\n"

    async def _safe_write(
        self,
        response,
        content: str,
        message_type: str = "continue",
        data_type: str = None,
    ) -> bool:
        """安全地写入 SSE 响应，连接断开时返回 False"""
        try:
            if data_type is None:
                data_type = DataTypeEnum.ANSWER.value[0]
            await response.write(
                self._create_response(content, message_type, data_type)
            )
            if hasattr(response, "flush"):
                await response.flush()
            return True
        except Exception as e:
            if self._is_connection_error(e):
                logger.info(f"客户端连接已断开: {type(e).__name__}")
                return False
            raise

    @staticmethod
    def _is_connection_error(exception: Exception) -> bool:
        """判断是否是连接断开相关的异常"""
        error_type = type(exception).__name__
        error_msg = str(exception).lower()

        connection_error_types = [
            "ConnectionClosed",
            "ConnectionResetError",
            "BrokenPipeError",
            "ConnectionError",
            "OSError",
        ]
        connection_error_keywords = [
            "connection closed",
            "connection reset",
            "broken pipe",
            "client disconnected",
            "connection aborted",
            "transport closed",
        ]

        if error_type in connection_error_types:
            return True
        for keyword in connection_error_keywords:
            if keyword in error_msg:
                return True
        return False

    # ==================== 格式化方法 ====================

    @staticmethod
    def _normalize_header_prefix(content: str, answer_collector: list) -> str:
        """
        确保工具 header 前的换行数恰好为 2（= 1 行空行）。

        LLM 在多轮工具调用间生成的中间文本经常带多尾换行，加上 SECTION_CLOSE /
        `_format_tool_call` 自身的换行，会让 header 上方空行随工具数线性累积。
        本方法数出 answer_collector 末尾的连续换行数，去掉 content 本身的前导换行，
        并补足到恰好 2 个换行，保证每次 header 上方只出现 1 行空行。
        """
        stripped = (content or "").lstrip("\n")
        if not answer_collector:
            return stripped
        tail = "".join(answer_collector)[-20:]
        trailing_nls = len(tail) - len(tail.rstrip("\n"))
        needed = max(0, 2 - trailing_nls)
        return ("\n" * needed) + stripped

    @staticmethod
    def _cap_leading_newlines(content: str, answer_collector: list) -> str:
        """
        LLM 增量文本的前导换行 cap：
        若 answer_collector 末尾已有 ≥2 连续换行（即前面已经形成空行），
        则去掉 content 的全部前导换行，防止空行随轮次堆积。
        否则只保留能让累计换行 ≤2 的前导数量。
        """
        if not content or not answer_collector:
            return content
        tail = "".join(answer_collector)[-20:]
        trailing_nls = len(tail) - len(tail.rstrip("\n"))
        if trailing_nls >= 2:
            return content.lstrip("\n")
        leading_nls = len(content) - len(content.lstrip("\n"))
        if trailing_nls + leading_nls <= 2:
            return content
        allowed = max(0, 2 - trailing_nls)
        return ("\n" * allowed) + content.lstrip("\n")

    @staticmethod
    def _unwrap_tool_response(content: str) -> str:
        """ADK 把工具返回包成 dict（如 {'result': '...'}）。本函数取出内层字符串。"""
        if not content:
            return ""
        text = content.strip()
        if not (text.startswith("{") and text.endswith("}")):
            return text
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                inner = parsed.get("result")
                if isinstance(inner, str):
                    return inner
        except (ValueError, SyntaxError):
            pass
        return text

    @staticmethod
    def _classify_sql_query_result(content: str) -> str:
        """区分 sql_db_query 的四种状态：accepted / accepted_with_warning / rejected / error。

        accepted_with_warning：SQL 执行成功并返回了真实数据，但被语义过滤标了警告
        （例如包含 NULL 比例较高、比率超出 [0,1] 等）。这类结果 Agent 仍可能据此回答，
        因此在 generated_sql 提取时应被视为候选答案。
        """
        text = AdkAgent._unwrap_tool_response(content).strip()
        if text.startswith("✅ 查询成功"):
            return "accepted"
        if "SQL 执行失败" in text or text.startswith("错误"):
            return "error"
        if text.startswith("⚠️ 查询已执行") and "✅ 查询成功" in text:
            return "accepted_with_warning"
        return "rejected"

    @staticmethod
    def _extract_row_count_from_resp(content: str) -> int:
        """从 sql_db_query 返回文本里抓 '共 N 行'。失败返回 0。"""
        text = AdkAgent._unwrap_tool_response(content)
        m = re.search(r"共\s*(\d+)\s*行", text)
        return int(m.group(1)) if m else 0

    @staticmethod
    def _sql_is_pure_aggregation(sql: str) -> bool:
        """判断 SQL 顶层 SELECT 列表是否仅由聚合函数组成且无 GROUP BY。

        命中场景示例：
          SELECT COUNT(*) FROM t
          SELECT COUNT(*), COUNT(t.col) FROM t WHERE ...
          SELECT SUM(x), AVG(y) FROM t

        不命中：
          SELECT name, COUNT(*) FROM t GROUP BY name
          SELECT t.col FROM t LIMIT 100
        """
        if not sql:
            return False
        s = re.sub(r"\s+", " ", sql.strip(), flags=re.IGNORECASE)
        if re.search(r"\bGROUP\s+BY\b", s, flags=re.IGNORECASE):
            return False
        m = re.match(r"^\s*SELECT\s+(?:DISTINCT\s+)?(.+?)\s+FROM\b", s, flags=re.IGNORECASE)
        if not m:
            return False
        select_list = m.group(1)
        # 按顶层逗号切分（忽略括号内的逗号）
        items, depth, buf = [], 0, []
        for ch in select_list:
            if ch == "(":
                depth += 1
                buf.append(ch)
            elif ch == ")":
                depth -= 1
                buf.append(ch)
            elif ch == "," and depth == 0:
                items.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
        if buf:
            items.append("".join(buf).strip())
        if not items:
            return False
        agg_pattern = re.compile(
            r"^(?:COUNT|SUM|AVG|MIN|MAX|TOTAL)\s*\(.*\)\s*(?:AS\s+\w+|\w+)?$",
            flags=re.IGNORECASE,
        )
        return all(agg_pattern.match(item) for item in items)

    @staticmethod
    def _strip_trailing_safety_limit(sql: str) -> str:
        """移除 agent 加在 SQL 末尾的"安全 LIMIT"，保留用户要求的 Top-N LIMIT。

        仅当以下条件全满足时才剥离：
          - SQL 末尾为顶层 `LIMIT <int>`（可带可选分号/注释/空白），且不伴随 `OFFSET`
          - `LIMIT` 不在括号内（排除子查询/派生表内的 LIMIT）
          - 顶层 SQL 不含 `ORDER BY`（Top-N 通常依赖 ORDER BY 排序），或 LIMIT 数值 = 100
            （100 是 AGENTS.md 历史默认值，几乎可以认定是安全限制）

        仅用于 test 路径输出的 `generated_sql`，不影响 agent 实际执行过的 SQL。
        """
        if not sql:
            return sql
        stripped = sql.rstrip().rstrip(";").rstrip()
        m = re.search(
            r"\bLIMIT\s+(\d+)\s*(?:OFFSET\s+\d+)?\s*$",
            stripped,
            flags=re.IGNORECASE,
        )
        if not m:
            return sql
        if re.search(r"\bOFFSET\b", m.group(0), flags=re.IGNORECASE):
            return sql
        # 括号深度检查：LIMIT 起始位置前若括号未闭合，说明 LIMIT 在子查询内
        prefix = stripped[: m.start()]
        depth = 0
        for ch in prefix:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        if depth != 0:
            return sql
        limit_val = int(m.group(1))
        has_order_by = bool(re.search(r"\bORDER\s+BY\b", prefix, flags=re.IGNORECASE))
        # 用户明确 Top-N：有 ORDER BY 且 LIMIT != 100，视为用户意图，保留
        if has_order_by and limit_val != 100:
            return sql
        return prefix.rstrip()

    @staticmethod
    def _pick_last_record(records: list[dict], pred) -> Optional[dict]:
        for rec in reversed(records or []):
            if pred(rec):
                return rec
        return None

    @staticmethod
    def _record_is_verification_shape(rec: dict) -> bool:
        sql = (rec or {}).get("sql", "")
        row_count = (rec or {}).get("row_count", 0)
        return AdkAgent._sql_is_pure_aggregation(sql) and row_count <= 1

    @staticmethod
    def _select_generated_sql(
        sql_execution_trace: list[dict],
        sql_attempt_records: list[dict],
        sql_attempts: list[str],
    ) -> dict:
        """优先基于底层真实执行轨迹选出测试应使用的 generated_sql。

        仅采纳 LLM 直接调用 `sql_db_query` 的轨迹；`sql_syntax_fix`、`sql_self_correct`
        等内部修复工具的执行记录不参与判分 SQL 选取，避免修复中间产物污染最终答案。
        """
        trace_records = [dict(rec) for rec in (sql_execution_trace or [])]
        non_probe_trace = [rec for rec in trace_records if not rec.get("is_probe")]
        agent_direct_trace = [
            rec for rec in non_probe_trace
            if (rec.get("source") or "sql_db_query") == "sql_db_query"
        ]

        selected_trace = AdkAgent._pick_last_record(
            agent_direct_trace,
            lambda r: (
                r.get("state") in ("accepted", "accepted_with_warning")
                and not AdkAgent._record_is_verification_shape(r)
            ),
        )
        if selected_trace is None:
            selected_trace = AdkAgent._pick_last_record(
                agent_direct_trace,
                lambda r: r.get("state") == "accepted",
            )
        if selected_trace is None:
            selected_trace = AdkAgent._pick_last_record(
                agent_direct_trace,
                lambda r: r.get("state") == "accepted_with_warning",
            )
        if selected_trace is None:
            selected_trace = AdkAgent._pick_last_record(agent_direct_trace, lambda r: True)

        def _pick_last_sql(records: list[dict], pred) -> str:
            rec = AdkAgent._pick_last_record(records, pred)
            return rec.get("sql", "") if rec else ""

        agent_generated_sql = _pick_last_sql(
            sql_attempt_records,
            lambda r: (
                r["state"] in ("accepted", "accepted_with_warning")
                and not AdkAgent._record_is_verification_shape(r)
            ),
        )
        if not agent_generated_sql:
            agent_generated_sql = _pick_last_sql(
                sql_attempt_records,
                lambda r: r["state"] == "accepted",
            )
        if not agent_generated_sql:
            agent_generated_sql = _pick_last_sql(
                sql_attempt_records,
                lambda r: r["state"] == "accepted_with_warning",
            )
        if not agent_generated_sql and sql_attempts:
            agent_generated_sql = sql_attempts[-1]

        last_successful_sql = ""
        if selected_trace is not None and selected_trace.get("state") == "accepted":
            last_successful_sql = selected_trace.get("sql", "")
        if not last_successful_sql:
            accepted_trace = AdkAgent._pick_last_record(
                agent_direct_trace,
                lambda r: r.get("state") == "accepted",
            )
            if accepted_trace is not None:
                last_successful_sql = accepted_trace.get("sql", "")
        if not last_successful_sql:
            last_successful_sql = _pick_last_sql(
                sql_attempt_records,
                lambda r: r["state"] == "accepted",
            )

        generated_sql = selected_trace.get("sql", "") if selected_trace else ""
        generated_sql_source = selected_trace.get("source", "") if selected_trace else ""
        if not generated_sql:
            generated_sql = agent_generated_sql
            generated_sql_source = "agent_event_stream" if generated_sql else ""

        generated_sql = AdkAgent._strip_trailing_safety_limit(generated_sql)
        agent_generated_sql = AdkAgent._strip_trailing_safety_limit(agent_generated_sql)
        last_successful_sql = AdkAgent._strip_trailing_safety_limit(last_successful_sql)

        return {
            "generated_sql": generated_sql,
            "generated_sql_source": generated_sql_source,
            "agent_generated_sql": agent_generated_sql,
            "last_successful_sql": last_successful_sql,
        }

    @staticmethod
    def _tool_result_message_type(name: str, content: str) -> str:
        """为工具结果选择 SSE messageType。"""
        name_lower = (name or "").lower()
        text = (content or "").lower()
        if name_lower == "sql_db_query":
            state = AdkAgent._classify_sql_query_result(content)
            if state == "accepted":
                return "info"
            if state in ("accepted_with_warning", "rejected"):
                return "warning"
            return "error"
        if any(token in text for token in ("error", "failed", "错误", "失败")):
            return "error"
        return "info"

    @staticmethod
    def _format_tool_call(name: str, args: dict) -> Optional[str]:
        """格式化工具调用信息"""
        if name == "sql_db_query":
            query = args.get("query", "")
            return f"⚡ **Executing SQL**\n```sql\n{query.strip()}\n```\n"
        elif name == "sql_db_schema":
            table_names = args.get("table_names", "")
            if isinstance(table_names, list):
                table_names = ", ".join(table_names)
            if table_names:
                return f"🔍 **Checking Schema:** `{table_names}`\n"
            return "🔍 **Checking Schema...**\n"
        elif name == "sql_db_list_tables":
            return "📋 **Listing Tables...**\n"
        elif name == "sql_db_query_checker":
            return "✅ **Validating Query...**\n"
        elif name == "sql_db_table_relationship":
            table_names = args.get("table_names", "")
            return f"🔗 **Checking Relationships:** `{table_names}`\n"
        elif name == "sql_db_value_lookup":
            phrase = args.get("phrase") or args.get("question", "")
            return f"🔎 **Value Lookup:** `{phrase}`\n"
        elif name == "add_schema":
            schema_elements = args.get("schema_elements", "")
            return f"➕ **Adding Schema Elements:** `{schema_elements}`\n"
        elif name == "build_linked_mschema":
            db_id = args.get("db_id", "") or "(auto-detect)"
            linked = sorted(get_linked_schema())
            if linked:
                preview = ", ".join(linked[:4])
                if len(linked) > 4:
                    preview += f", ... (+{len(linked) - 4})"
                return (
                    f"🏗️ **Building Linked mSchema:** `{db_id}` "
                    f"from {len(linked)} linked columns ({preview})\n"
                )
            return f"🏗️ **Building Linked mSchema:** `{db_id}`\n"
        elif name == "save_report":
            filename = args.get("filename", "")
            return f"💾 **Saving Report:** `{filename}`\n"
        elif name == "submit_final_sql":
            sql_arg = (args.get("sql") or "").strip()
            preview = sql_arg if len(sql_arg) <= 240 else sql_arg[:237] + "..."
            return f"🏁 **Submitting Final SQL**\n```sql\n{preview}\n```\n"
        elif name == "db_search":
            keyword = args.get("keyword", "")
            return f"🌐 **Searching DB Schema:** `{keyword}`\n"
        elif name == "sql_error_classify":
            return "🔎 **Classifying SQL Error...**\n"
        elif name == "sql_schema_validate":
            return "🔬 **Validating Schema References...**\n"
        elif name == "sql_join_validate":
            return "🔗 **Validating JOIN Conditions...**\n"
        elif name == "sql_clause_validate":
            return "📋 **Validating SQL Clauses...**\n"
        elif name == "sql_diff_report":
            return "📊 **Generating Diff Report...**\n"
        elif name == "sql_self_correct":
            return "🔧 **Running SCoT2S Self-Correction Pipeline...**\n"
        return None

    @staticmethod
    def _format_tool_result(name: str, content: str) -> Optional[str]:
        """格式化工具执行结果"""
        name_lower = name.lower()
        
        # 对于查询和修改类的工具，输出简短的状态而不是大段文本
        if name_lower == "sql_db_query":
            state = AdkAgent._classify_sql_query_result(content)
            if state == "accepted":
                return "✓ Query executed successfully\n"
            if state == "accepted_with_warning":
                return f"⚠️ **Query executed with semantic warning:** {content[:300].strip()}\n"
            if state == "rejected":
                return f"⚠️ **Query result rejected:** {content[:300].strip()}\n"
            else:
                return f"✗ **Query failed:** {content[:300].strip()}\n"
        elif name_lower == "sql_db_value_lookup":
            if "error" in content.lower() or "失败" in content:
                return f"✗ **Lookup failed:** {content[:300].strip()}\n"
            else:
                return "✓ Value lookup completed\n"
        elif name_lower in ["add_schema", "build_linked_mschema", "save_report", "submit_final_sql",
                            "db_search",
                            "sql_error_classify", "sql_schema_validate", "sql_join_validate",
                            "sql_clause_validate", "sql_diff_report", "sql_self_correct"]:
            if ("error" in content.lower() or "failed" in content.lower()
                    or "错误" in content or "失败" in content):
                return f"✗ **Tool {name} failed:** {content[:300].strip()}\n"
            else:
                return f"✓ Tool {name} executed successfully\n"
                
        # 其他透传或不显示详细结果
        return None

    # ==================== 阶段检测与 <details> 管理 ====================

    @staticmethod
    def _detect_phase(content: str, tracker: PhaseTracker) -> Phase:
        """基于内容和追踪器状态检测当前阶段"""
        if "REPORT_HTML_START" in content or "REPORT_HTML_END" in content:
            return Phase.REPORTING
        if not tracker.has_tool_called:
            return Phase.PLANNING
        return Phase.EXECUTION

    async def _open_thinking_section(self, response) -> bool:
        """打开思考规划 <details> 区域"""
        return await self._safe_write(response, THINKING_SECTION_OPEN)

    async def _close_sections(self, response, tracker: PhaseTracker) -> bool:
        """关闭所有已打开的 <details> 区域"""
        if tracker.planning_opened:
            if not await self._safe_write(response, SECTION_CLOSE):
                return False
            tracker.planning_opened = False
        return True

    # ==================== Agent 创建 ====================

    def _create_sql_adk_agent(self, datasource_id: int, session_id: str):
        """
        创建 ADK Text-to-SQL Agent

        Args:
            datasource_id: 数据源 ID
            session_id: 会话 ID
        Returns:
            tuple: (runner, adk_session_id)
        """
        logger.info(f"创建 ADK Agent - 数据源: {datasource_id}, 会话: {session_id}")

        # 通过 DATABASE_URI 环境变量连接数据库（与旧版 agent_old 一致）
        database_uri = os.getenv("DATABASE_URI")
        if not database_uri:
            raise ValueError(
                "DATABASE_URI 环境变量未设置，请配置数据库连接 URI，例如:\n"
                "  export DATABASE_URI='mysql+pymysql://user:pass@host:port/db'"
            )

        set_database_uri(database_uri, session_id)

        model = create_model()
        logger.info(
            f"LLM 模型已创建，递归限制: {self.RECURSION_LIMIT}"
        )

        # SQL 工具列表（普通 Python 函数，ADK 自动包装为 FunctionTool）
        sql_tools = get_sql_tools()

        # 使用 SkillToolset 按需加载技能（list_skills → load_skill → 执行）
        skill_toolset = self._create_skill_toolset()

        agent = Agent(
            model=model,
            name="text2sql_agent",
            description="Text-to-SQL 数据库查询智能体，支持数据探索、SQL 查询和报告生成",
            instruction=self._instruction_text,
            tools=sql_tools + [skill_toolset],
        )

        # 创建 InMemoryRunner（自动注入 SessionService / ArtifactService / MemoryService）
        runner = InMemoryRunner(
            agent=agent,
            app_name=self.APP_NAME,
        )

        return runner, runner.session_service

    async def _get_or_create_runtime(
        self,
        effective_session_id: str,
        datasource_id: int,
        user_id: str,
    ):
        """按 effective_session_id 复用 Runner / Session，保留多轮对话历史。

        命中缓存：返回 (runner, session_service, adk_session, is_new=False)，
                 调用方不应再 reset tool_manager / linked_schema。
        未命中：新建 Runner + Session，写入缓存（超出容量时淘汰最旧项），
               并在外部首次执行前 reset tool_manager / linked_schema。
        """
        entry = self._session_runtime.get(effective_session_id)
        if entry is not None:
            # 触发 LRU 更新
            self._session_runtime.move_to_end(effective_session_id)
            runner, session_service, adk_session, _uid = entry
            return runner, session_service, adk_session, False

        runner, session_service = self._create_sql_adk_agent(
            datasource_id, effective_session_id
        )
        adk_session = await session_service.create_session(
            app_name=self.APP_NAME,
            user_id=user_id,
        )
        self._session_runtime[effective_session_id] = (
            runner, session_service, adk_session, user_id,
        )

        # LRU 淘汰
        while len(self._session_runtime) > self._session_runtime_cap:
            old_key, old_entry = self._session_runtime.popitem(last=False)
            try:
                old_runner, old_svc, old_sess, old_uid = old_entry
                await old_svc.delete_session(
                    app_name=self.APP_NAME,
                    user_id=old_uid,
                    session_id=old_sess.id,
                )
            except Exception as exc:
                logger.debug("AdkAgent eviction: delete_session failed for %s: %s", old_key, exc)
            try:
                reset_session(old_key)
            except Exception as exc:
                logger.debug("AdkAgent eviction: reset_session failed for %s: %s", old_key, exc)
            try:
                self.tool_manager.reset_session(old_key)
            except Exception as exc:
                logger.debug("AdkAgent eviction: tool_manager.reset_session failed for %s: %s", old_key, exc)

        return runner, session_service, adk_session, True

    # ==================== 核心执行 ====================

    async def run_agent(
        self,
        query: str,
        response,
        session_id: Optional[str] = None,
        uuid_str: str = None,
        user_token=None,
        file_list: dict = None,
        datasource_id: int = None,
    ):
        """
        运行智能体，多阶段实时流推送

        接口签名与 DeepAgent.run_agent 完全一致，可直接替换。
        """
        if not datasource_id:
            await self._safe_write(
                response,
                "❌ **错误**: 必须提供数据源ID (datasource_id)",
                "error",
                DataTypeEnum.ANSWER.value[0],
            )
            return

        user_dict = await decode_jwt_token(user_token)
        task_id = user_dict["id"]
        effective_session_id = session_id or f"adk-agent-{datasource_id}-{task_id}"

        start_time = time.time()
        connection_closed = False
        answer_collector: list[str] = []

        try:
            runner, session_service, adk_session, is_new_session = await self._get_or_create_runtime(
                effective_session_id, datasource_id, str(task_id),
            )

            # 仅新会话首次创建时清空隔离状态；命中缓存时保留多轮上下文
            if is_new_session:
                self.tool_manager.reset_session(effective_session_id)

            run_config = RunConfig(
                streaming_mode=StreamingMode.SSE,
                max_llm_calls=self.RECURSION_LIMIT,
            )

            try:
                connection_closed = await asyncio.wait_for(
                    self._stream_response(
                        runner,
                        adk_session,
                        run_config,
                        query,
                        response,
                        effective_session_id,
                        answer_collector,
                        str(task_id),
                    ),
                    timeout=self.TASK_TIMEOUT,
                )
            except asyncio.TimeoutError:
                elapsed = time.time() - start_time
                logger.error(
                    f"任务总超时 ({self.TASK_TIMEOUT}秒) - 实际耗时: {elapsed:.0f}秒"
                )
                await self._safe_write(
                    response,
                    f"\n> ⚠️ **执行超时**: 任务执行时间超过上限"
                    f"（{self.TASK_TIMEOUT // 60} 分钟），请简化查询后重试。",
                    "error",
                    DataTypeEnum.ANSWER.value[0],
                )

        except asyncio.CancelledError:
            logger.info(f"任务被取消 - 会话: {effective_session_id}")
            connection_closed = True
            raise
        except Exception as e:
            if self._is_connection_error(e):
                logger.info(f"客户端连接已断开: {type(e).__name__}")
                connection_closed = True
            else:
                logger.error(f"Agent运行异常: {e}")
                traceback.print_exception(e)
                try:
                    await self._safe_write(
                        response,
                        f"❌ **错误**: 智能体运行异常\n\n```\n{str(e)[:200]}\n```\n",
                        "error",
                        DataTypeEnum.ANSWER.value[0],
                    )
                except Exception:
                    pass
        finally:
            # 写入对话记录
            try:
                if answer_collector:
                    record_id = await add_user_record(
                        uuid_str=uuid_str or "",
                        chat_id=session_id,
                        question=query,
                        to2_answer=answer_collector,
                        to4_answer={},
                        qa_type=IntentEnum.REPORT_QA.value[0],
                        user_token=user_token,
                        file_list=file_list,
                        datasource_id=datasource_id,
                    )
                    logger.info(
                        f"对话记录已保存 - record_id: {record_id}, "
                        f"会话: {effective_session_id}, "
                        f"内容长度: {sum(len(s) for s in answer_collector)}"
                    )
            except Exception as e:
                logger.error(f"保存对话记录失败: {e}", exc_info=True)

            # 发送流结束标记
            if not connection_closed:
                try:
                    await self._safe_write(
                        response, "", "end", DataTypeEnum.STREAM_END.value[0]
                    )
                except Exception as e:
                    logger.warning(f"发送 STREAM_END 失败: {e}")

            elapsed = time.time() - start_time
            stats = self.tool_manager.get_stats(effective_session_id)
            logger.info(
                f"任务结束 - 会话: {effective_session_id}, "
                f"耗时: {elapsed:.2f}秒, 工具调用统计: {stats}"
            )

    # ==================== 核心流处理 ====================

    async def _stream_response(
        self,
        runner: Runner,
        adk_session,
        run_config: RunConfig,
        query: str,
        response,
        session_id: str,
        answer_collector: list,
        user_id: str,
    ) -> bool:
        """
        处理 ADK agent 流式响应，多阶段实时推送到前端

        执行阶段流转：
        PLANNING（思考规划，<details> 包裹）
            ↓ 首次工具调用
        EXECUTION（执行回答，直接输出）
            ↓ 完成
        REPORTING（报告输出，HTML 标记透传）

        Returns:
            bool: 连接是否已断开（True=断开）
        """
        tracker = PhaseTracker()
        token_count = 0
        connection_closed = False
        last_activity_time = time.monotonic()

        # 重置报告内容收集器，确保每次查询从空开始
        reset_report_content()

        # 文本输出状态：partial 优先，non-partial 作为回退。
        emitted_text = ""
        has_seen_partial_text = False
        skipped_text_chunks = 0

        logger.info(f"开始流式响应 - 会话: {session_id}, 查询: {query[:100]}")

        new_message = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=query)],
        )

        event_iter = runner.run_async(
            user_id=user_id,
            session_id=adk_session.id,
            new_message=new_message,
            run_config=run_config,
        )

        try:
            async for event in event_iter:
                now = time.monotonic()

                # ---- 检查工具调用管理器终止 ----
                ctx = self.tool_manager.get_session(session_id)
                if ctx.should_terminate:
                    logger.warning(
                        f"工具调用管理器触发终止: {ctx.termination_reason}"
                    )
                    await self._close_sections(response, tracker)
                    await self._safe_write(
                        response,
                        f"\n> ⚠️ **执行中止**\n\n{ctx.termination_reason}",
                        "warning",
                        DataTypeEnum.ANSWER.value[0],
                    )
                    break

                # ---- 跳过无内容事件 ----
                if not event.content or not event.content.parts:
                    # 长时间无活动，发送 keepalive
                    if now - last_activity_time > self.STREAM_KEEPALIVE_INTERVAL:
                        try:
                            await response.write(
                                'data: {"data":{"messageType": "info", '
                                '"content": ""}, "dataType": "keepalive"}\n\n'
                            )
                            if hasattr(response, "flush"):
                                await response.flush()
                            last_activity_time = now
                        except Exception as e:
                            if self._is_connection_error(e):
                                connection_closed = True
                                break
                            raise
                    continue

                last_activity_time = now

                # ---- SSE 模式：判断是否为 partial（流式 chunk）事件 ----
                # partial=True 优先输出；若某些后端只在 non-partial 携带文本，
                # 则走回退路径输出增量，避免文本丢失。
                is_partial = getattr(event, "partial", None) is True

                # ---- 处理事件中的每个 Part ----
                for part in event.content.parts:
                    if connection_closed:
                        break

                    # -- 工具调用 --
                    if part.function_call:
                        fc = part.function_call
                        name = fc.name or "unknown"
                        args = dict(fc.args) if fc.args else {}

                        # 从 PLANNING 切换到 EXECUTION
                        if tracker.current_phase == Phase.PLANNING:
                            if not await self._close_sections(response, tracker):
                                connection_closed = True
                                break
                            tracker.current_phase = Phase.EXECUTION
                            tracker.has_tool_called = True
                            tracker.has_sent_content = True

                        if not tracker.has_tool_called:
                            tracker.has_tool_called = True

                        tool_msg = self._format_tool_call(name, args)
                        if tool_msg:
                            tool_msg = self._normalize_header_prefix(
                                tool_msg, answer_collector
                            )
                            if not await self._safe_write(
                                response, tool_msg, "info"
                            ):
                                connection_closed = True
                                break
                            answer_collector.append(tool_msg)

                    # -- 工具结果 --
                    elif part.function_response:
                        fr = part.function_response
                        name = fr.name or ""
                        content_str = str(fr.response) if fr.response else ""
                        tool_result_msg = self._format_tool_result(
                            name, content_str
                        )
                        if tool_result_msg:
                            msg_type = self._tool_result_message_type(
                                name, content_str
                            )
                            if not await self._safe_write(
                                response, tool_result_msg, msg_type
                            ):
                                connection_closed = True
                                break
                            answer_collector.append(tool_result_msg)

                    # -- 文本输出（partial 优先，non-partial 回退）--
                    elif part.text:
                        raw_text = part.text

                        if is_partial:
                            has_seen_partial_text = True
                            token_text = raw_text
                        else:
                            # 某些模型/网关会把文本放在 non-partial 事件中；
                            # 若已输出过 partial 文本，则尽量只输出 non-partial 的增量。
                            if not has_seen_partial_text:
                                token_text = raw_text
                            elif raw_text == emitted_text:
                                skipped_text_chunks += 1
                                continue
                            elif emitted_text and raw_text.startswith(emitted_text):
                                token_text = raw_text[len(emitted_text) :]
                            elif raw_text and raw_text in emitted_text[-500:]:
                                skipped_text_chunks += 1
                                continue
                            else:
                                token_text = raw_text

                        if not token_text:
                            continue

                        # 阶段检测
                        new_phase = self._detect_phase(token_text, tracker)

                        # 阶段切换
                        if new_phase != tracker.current_phase:
                            closed = await self._handle_phase_transition(
                                response, tracker, new_phase
                            )
                            if not closed:
                                connection_closed = True
                                break

                        # 空行 cap：若已输出末尾连续换行数 ≥ 2，则吞掉新块的前导换行，
                        # 避免 LLM 文本叠加产生多个空行导致工具 header 上方堆空行。
                        token_text = self._cap_leading_newlines(
                            token_text, answer_collector
                        )
                        if not token_text:
                            continue

                        # 输出文本
                        if not await self._safe_write(response, token_text):
                            connection_closed = True
                            break

                        answer_collector.append(token_text)
                        # 同时追加到报告内容收集器（供 save_report 自动提取）
                        append_report_content(token_text)
                        emitted_text += token_text
                        token_count += 1

                if connection_closed:
                    break

                await asyncio.sleep(0)

        except asyncio.CancelledError:
            logger.info(f"流被取消 - 会话: {session_id}")
            connection_closed = True
            raise
        except Exception as e:
            if self._is_connection_error(e):
                logger.info(f"客户端连接已断开: {type(e).__name__}")
                connection_closed = True
            else:
                logger.error(
                    f"流式响应异常: {type(e).__name__}: {e}", exc_info=True
                )
                try:
                    await self._close_sections(response, tracker)
                    await self._safe_write(
                        response,
                        f"\n> ❌ **处理异常**: {str(e)[:200]}\n\n请稍后重试。",
                        "error",
                        DataTypeEnum.ANSWER.value[0],
                    )
                except Exception:
                    pass
        finally:
            if not connection_closed:
                try:
                    await self._close_sections(response, tracker)
                except Exception:
                    pass

        logger.info(
            f"流式响应结束 - 会话: {session_id}, "
            f"token数: {token_count}, 跳过重复块: {skipped_text_chunks}, "
            f"阶段: {tracker.current_phase.value}"
        )
        return connection_closed

    async def _handle_phase_transition(
        self,
        response,
        tracker: PhaseTracker,
        new_phase: Phase,
    ) -> bool:
        """
        处理阶段切换，管理 <details> 标签

        Returns:
            bool: True=成功, False=连接断开
        """
        old_phase = tracker.current_phase

        if new_phase == Phase.PLANNING:
            if not tracker.planning_opened:
                if not await self._open_thinking_section(response):
                    return False
                tracker.planning_opened = True

        elif new_phase == Phase.EXECUTION:
            if not await self._close_sections(response, tracker):
                return False
            tracker.has_sent_content = True

        elif new_phase == Phase.REPORTING:
            if not await self._close_sections(response, tracker):
                return False

        tracker.current_phase = new_phase
        logger.debug(f"阶段切换: {old_phase.value} → {new_phase.value}")
        return True

    # ==================== 兼容接口 ====================

    async def cancel_task(self, task_id: str) -> bool:
        """取消任务（兼容接口）"""
        logger.info(f"收到取消请求: {task_id}")
        return False


# ==================== ADK 入口：root_agent ====================
# adk run / adk web 要求 agent.py 暴露一个模块级 root_agent 变量


def _build_root_agent() -> Agent:
    """构建供 adk run 使用的 root_agent"""
    _adk = AdkAgent()

    database_uri = os.getenv("DATABASE_URI")
    if database_uri:
        set_database_uri(database_uri, "adk_cli")

    sql_tools = get_sql_tools()

    model = create_model()
    skill_toolset = _adk._create_skill_toolset()

    return Agent(
        model=model,
        name="text2sql_agent",
        description="Text-to-SQL 数据库查询智能体，支持数据探索、SQL 查询和报告生成",
        instruction=_adk._instruction_text,
        tools=sql_tools + [skill_toolset],
    )


# 仅在作为模块导入时（adk run / adk web）构建 root_agent，
# 直接运行 python agent.py 时不执行
if __name__ != "__main__":
    root_agent = _build_root_agent()


# ==================== AgentService — 统一服务层 ====================


class AgentService:
    """
    可复用的 Agent 服务层。

    agent.py 独立运行和 test_bird_dev.py 批量测试共用此类，
    确保工具列表、Agent 构建、事件处理逻辑完全一致。
    """

    def __init__(self, experiment_profile: str = "full"):
        self._adk = AdkAgent()
        self._experiment_profile = experiment_profile or "full"
        self._agent = self._build_agent()
        self._session_runtime: "OrderedDict[str, tuple]" = OrderedDict()
        self._session_runtime_cap = int(
            os.getenv("AGENT_SERVICE_SESSION_CACHE_SIZE", "16")
        )
        self.last_run_diagnostics: dict = {}

    def _build_experiment_instruction(self) -> str:
        profile = (self._experiment_profile or "full").lower()
        if profile == "draft_only":
            return (
                "\n\n[Experiment Profile]\n"
                "Current mode: draft_only.\n"
                "- Generate the initial SQL draft and execute it exactly once.\n"
                "- Do not call correction tools: sql_syntax_fix, sql_error_classify, "
                "sql_schema_validate, sql_join_validate, sql_clause_validate, sql_diff_report, sql_self_correct.\n"
                "- Treat the first executed SQL as the final SQL for this run.\n"
            )
        if profile == "syntax_only":
            return (
                "\n\n[Experiment Profile]\n"
                "Current mode: syntax_only.\n"
                "- You may use sql_syntax_fix when the first SQL has obvious syntax or quoting issues.\n"
                "- Do not call sql_error_classify, sql_schema_validate, sql_join_validate, "
                "sql_clause_validate, sql_diff_report, or sql_self_correct.\n"
                "- After syntax repair, stop further correction.\n"
            )
        if profile == "exec_validators":
            return (
                "\n\n[Experiment Profile]\n"
                "Current mode: exec_validators.\n"
                "- You may use sql_error_classify, sql_schema_validate, sql_join_validate, and sql_clause_validate.\n"
                "- Do not call sql_self_correct.\n"
                "- If the SQL executes with only semantic warnings, do not continue repairing it in this profile.\n"
            )
        if profile == "no_correction":
            return (
                "\n\n[Experiment Profile]\n"
                "Current mode: no_correction.\n"
                "- Complete the normal data-link and SQL drafting pipeline.\n"
                "- Do not call correction tools: sql_syntax_fix, sql_error_classify, sql_schema_validate, "
                "sql_join_validate, sql_clause_validate, sql_diff_report, sql_self_correct.\n"
                "- Execute the drafted SQL at most once and treat that first execution as the final result for this run.\n"
            )
        if profile == "no_hybrid_retrieval":
            return (
                "\n\n[Experiment Profile]\n"
                "Current mode: no_hybrid_retrieval.\n"
                "- Follow the same three-stage pipeline as full mode.\n"
                "- Continue using sql_db_value_lookup normally; the backend retrieval is intentionally degraded for ablation.\n"
                "- Do not skip data-link or correction just because retrieval is weaker in this profile.\n"
            )
        if profile == "no_schema_linking":
            return (
                "\n\n[Experiment Profile]\n"
                "Current mode: no_schema_linking.\n"
                "- Follow the same three-stage pipeline as full mode.\n"
                "- Still complete data-link and still call build_linked_mschema.\n"
                "- Do not rely on manually curated linked-schema filtering; the backend will construct a looser M-Schema candidate set for this ablation.\n"
            )
        return ""

    def _build_agent(self) -> Agent:
        model = create_model()
        skill_toolset = self._adk._create_skill_toolset()
        sql_tools = get_sql_tools()

        return Agent(
            model=model,
            name="text2sql_agent",
            description="Text-to-SQL 数据库查询智能体",
            instruction=self._adk._instruction_text + self._build_experiment_instruction(),
            tools=sql_tools + [skill_toolset],
        )

    @property
    def agent(self) -> Agent:
        return self._agent

    async def _get_or_create_runtime(self, session_id: str):
        entry = self._session_runtime.get(session_id)
        if entry is not None:
            self._session_runtime.move_to_end(session_id)
            runner, session_service, session = entry
            return runner, session_service, session, False

        session_service = InMemorySessionService()
        runner = Runner(
            agent=self._agent,
            app_name="agent_service",
            session_service=session_service,
        )
        session = await session_service.create_session(
            app_name="agent_service",
            user_id="service_user",
        )
        self._session_runtime[session_id] = (runner, session_service, session)

        while len(self._session_runtime) > self._session_runtime_cap:
            old_session_id, (_runner, old_svc, old_session) = self._session_runtime.popitem(last=False)
            try:
                await old_svc.delete_session(
                    app_name="agent_service",
                    user_id="service_user",
                    session_id=old_session.id,
                )
            except Exception as exc:
                logger.debug("AgentService eviction: delete_session failed for %s: %s", old_session_id, exc)
            try:
                reset_session(old_session_id)
            except Exception as exc:
                logger.debug("AgentService eviction: reset_session failed for %s: %s", old_session_id, exc)

        return runner, session_service, session, True

    async def close_session(self, session_id: str) -> None:
        entry = self._session_runtime.pop(session_id, None)
        if entry is None:
            return
        _runner, session_service, session = entry
        try:
            await session_service.delete_session(
                app_name="agent_service",
                user_id="service_user",
                session_id=session.id,
            )
        except Exception as exc:
            logger.debug("AgentService.close_session: delete_session failed: %s", exc)
        try:
            reset_session(session_id)
        except Exception as exc:
            logger.debug("AgentService.close_session: reset_session failed: %s", exc)

    @staticmethod
    def _build_correction_path(correction_events: list[dict], tool_stats: dict) -> list[str]:
        path: list[str] = []
        seen: set[str] = set()

        for event in correction_events or []:
            kind = event.get("kind")
            if kind in {"semantic_result", "syntax", "schema", "join", "clause", "self_correct"} and kind not in seen:
                seen.add(kind)
                path.append(kind)

        tool_to_stage = {
            "sql_syntax_fix": "syntax",
            "sql_schema_validate": "schema",
            "sql_join_validate": "join",
            "sql_clause_validate": "clause",
            "sql_self_correct": "self_correct",
        }
        for tool_name in tool_stats.get("recent_tool_sequence", []) or []:
            stage = tool_to_stage.get(tool_name)
            if stage and stage not in seen:
                seen.add(stage)
                path.append(stage)

        return path

    @staticmethod
    def _extract_semantic_reject_reason(correction_events: list[dict]) -> str:
        warnings: list[str] = []
        for event in correction_events or []:
            if event.get("kind") != "semantic_result":
                continue
            for item in event.get("warnings", []) or []:
                if item and item not in warnings:
                    warnings.append(item)
        return " | ".join(warnings)

    async def run_query(
        self,
        question: str,
        database_uri: str,
        session_id: str,
        evidence: str = "",
        print_output: bool = True,
        max_llm_calls: int = 40,
        preserve_context: bool = False,
    ) -> dict:
        """
        运行 Agent 处理单个查询。

        Args:
            question: 用户问题
            database_uri: SQLAlchemy 连接字符串
            session_id: 会话 ID（用于隔离 session state）
            evidence: 补充信息（可选，会拼接到 prompt）
            print_output: 是否在终端输出工具调用详情
            max_llm_calls: LLM 最大调用次数

        Returns:
            dict:
                sql_attempts  — sql_db_query 的所有 SQL 参数
                last_successful_sql — 最后一次无错误的 SQL
                generated_sql — 测试实际用于 EX 判分的 SQL
                generated_sql_source — generated_sql 的来源工具
                agent_generated_sql — 仅基于事件流提取的顶层候选 SQL
                sql_execution_trace — 底层真实 SQL 执行轨迹
                text_output   — Agent 的文本输出拼接
        """
        # ---- 环境准备 ----
        started_at = time.perf_counter()
        tool_trace: list[dict] = []
        execution_error = None
        self.last_run_diagnostics = {"tool_trace": tool_trace}
        set_database_uri(database_uri, session_id)
        if not preserve_context:
            reset_linked_schema(session_id)
        reset_sql_execution_trace(session_id)
        reset_report_content()
        reset_correction_events(session_id)
        reset_final_sql(session_id)
        self._adk.tool_manager.reset_session(session_id)
        set_experiment_profile(self._experiment_profile, session_id)

        # ---- Runner & Session ----
        if preserve_context:
            runner, session_service, session, is_new_session = await self._get_or_create_runtime(
                session_id
            )
            if is_new_session:
                reset_linked_schema(session_id)
                set_experiment_profile(self._experiment_profile, session_id)
        else:
            session_service = InMemorySessionService()
            runner = Runner(
                agent=self._agent,
                app_name="agent_service",
                session_service=session_service,
            )
            session = await session_service.create_session(
                app_name="agent_service",
                user_id="service_user",
            )

        run_config = RunConfig(
            streaming_mode=StreamingMode.SSE,
            max_llm_calls=max_llm_calls,
        )

        prompt = question
        if evidence and evidence.strip():
            prompt += f"\n\nEvidence:\n{evidence}"

        # ---- 流式执行 ----
        sql_attempts: list[str] = []
        # 平行结构：与 sql_attempts 索引对齐；function_response 阶段回填 state / row_count
        sql_attempt_records: list[dict] = []
        text_parts: list[str] = []
        emitted_text = ""
        has_seen_partial_text = False

        try:
            async for event in runner.run_async(
                user_id="service_user",
                session_id=session.id,
                new_message=genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=prompt)],
                ),
                run_config=run_config,
            ):
                if getattr(event, "error_code", None):
                    execution_error = {
                        "type": "ModelResponseError", "code": str(event.error_code),
                        "message": str(getattr(event, "error_message", "") or event.error_code),
                    }
                    self.last_run_diagnostics["execution_error"] = execution_error
                if not (hasattr(event, "content") and event.content):
                    continue

                is_partial = getattr(event, "partial", None) is True

                if not hasattr(event.content, "parts"):
                    if hasattr(event.content, "text") and event.content.text:
                        raw_text = event.content.text
                        token_text = self._dedup_text(
                            raw_text, emitted_text, is_partial, has_seen_partial_text
                        )
                        if is_partial:
                            has_seen_partial_text = True
                        if token_text:
                            if print_output:
                                _safe_print(token_text, end="", flush=True)
                            text_parts.append(token_text)
                            append_report_content(token_text)
                            emitted_text += token_text
                    continue

                for part in event.content.parts:
                    # -- 工具调用 --
                    if hasattr(part, "function_call") and part.function_call:
                        fc = part.function_call
                        args = dict(fc.args) if fc.args else {}
                        tool_trace.append({
                            "kind": "call", "name": fc.name, "args": args,
                            "id": getattr(fc, "id", None),
                            "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                        })

                        if print_output:
                            msg = AdkAgent._format_tool_call(fc.name, args)
                            if msg:
                                _safe_print(msg, end="", flush=True)

                        if fc.name == "sql_db_query":
                            q = args.get("query", "")
                            if q:
                                # 与 sql_db_query 内部一致：用已知链接架构自动补齐
                                # 特殊标识符的双引号，保证 test 脚本重跑时与 agent
                                # 内部实际执行的 SQL 一致
                                quoted = _auto_quote_sql_identifiers(q)
                                sql_attempts.append(quoted)
                                sql_attempt_records.append({
                                    "sql": quoted,
                                    "tool_call_id": getattr(fc, "id", None),
                                    "state": "pending",
                                    "row_count": 0,
                                })

                    # -- 工具结果 --
                    elif hasattr(part, "function_response") and part.function_response:
                        fr = part.function_response
                        resp_str = str(fr.response) if fr.response else ""
                        tool_trace.append({
                            "kind": "response", "name": fr.name,
                            "response": dict(fr.response) if fr.response else {},
                            "id": getattr(fr, "id", None),
                            "elapsed_seconds": round(time.perf_counter() - started_at, 3),
                        })

                        if print_output:
                            msg = AdkAgent._format_tool_result(fr.name, resp_str)
                            if msg:
                                _safe_print(msg, end="", flush=True)

                        if fr.name == "sql_db_query" and fr.response and sql_attempt_records:
                            # 同一事件可以宣布多条 SQL，响应顺序不能用于猜测归属。
                            # 无 ID 时，即使只有一条 pending，也无法排除迟到的重复响应。
                            response_id = getattr(fr, "id", None)
                            matches = [rec for rec in sql_attempt_records
                                       if response_id and rec["tool_call_id"] == response_id]
                            if len(matches) == 1 and matches[0]["state"] == "pending":
                                matches[0]["state"] = AdkAgent._classify_sql_query_result(resp_str)
                                matches[0]["row_count"] = AdkAgent._extract_row_count_from_resp(resp_str)

                    # -- 文本 --
                    elif hasattr(part, "text") and part.text:
                        raw_text = part.text
                        token_text = self._dedup_text(
                            raw_text, emitted_text, is_partial, has_seen_partial_text
                        )
                        if is_partial:
                            has_seen_partial_text = True
                        if token_text:
                            if print_output:
                                _safe_print(token_text, end="", flush=True)
                            text_parts.append(token_text)
                            append_report_content(token_text)
                            emitted_text += token_text

        except Exception as e:
            execution_error = {
                "type": type(e).__name__, "message": str(e),
                "status_code": getattr(e, "status_code", None),
                "code": getattr(e, "code", None),
            }
            self.last_run_diagnostics["execution_error"] = execution_error
            logger.error(f"AgentService.run_query 异常: {e}", exc_info=True)
            if print_output:
                _safe_print(f"\n错误: {e}")
        finally:
            if print_output:
                _safe_print()
            if not preserve_context:
                try:
                    await session_service.delete_session(
                        app_name="agent_service",
                        user_id="service_user",
                        session_id=session.id,
                    )
                except Exception:
                    pass

        sql_execution_trace = get_sql_execution_trace(session_id)
        correction_events = get_correction_events(session_id)
        tool_stats = self._adk.tool_manager.get_stats(session_id)
        selected_sql = AdkAgent._select_generated_sql(
            sql_execution_trace=sql_execution_trace,
            sql_attempt_records=sql_attempt_records,
            sql_attempts=sql_attempts,
        )

        # 优先使用 LLM 通过 submit_final_sql 显式声明的最终 SQL；
        # 缺失时回落到基于执行轨迹的启发式选择。
        declared = get_final_sql(session_id)
        if declared.get("sql"):
            final_sql_value = AdkAgent._strip_trailing_safety_limit(declared["sql"])
            final_sql_source = "submit_final_sql"
        else:
            final_sql_value = selected_sql["generated_sql"]
            final_sql_source = selected_sql["generated_sql_source"]

        initial_sql = sql_attempts[0] if sql_attempts else ""
        initial_state = sql_attempt_records[0]["state"] if sql_attempt_records else ""

        return {
            "sql_attempts": sql_attempts,
            "sql_attempt_records": [dict(item) for item in sql_attempt_records],
            "initial_sql": initial_sql,
            "initial_state": initial_state,
            "initial_executable": initial_state in ("accepted", "accepted_with_warning"),
            "last_successful_sql": selected_sql["last_successful_sql"],
            "final_sql": final_sql_value,
            "generated_sql": final_sql_value,
            "generated_sql_source": final_sql_source,
            "agent_generated_sql": selected_sql["agent_generated_sql"],
            "submitted_final_sql": declared.get("sql", ""),
            "submitted_final_sql_reasoning": declared.get("reasoning", ""),
            "sql_execution_trace": sql_execution_trace,
            "correction_events": correction_events,
            "correction_path": self._build_correction_path(correction_events, tool_stats),
            "semantic_reject_reason": self._extract_semantic_reject_reason(correction_events),
            "triggered_self_correct": any(
                event.get("kind") == "self_correct" for event in correction_events
            ),
            "tool_stats": tool_stats,
            "tool_trace": tool_trace,
            "execution_error": execution_error,
            "latency_sec": round(time.perf_counter() - started_at, 2),
            "text_output": "".join(text_parts),
        }

    @staticmethod
    def _dedup_text(
        raw_text: str, emitted_text: str, is_partial: bool, has_seen_partial: bool
    ) -> str:
        if is_partial:
            return raw_text
        if not has_seen_partial:
            return raw_text
        if not raw_text:
            return ""
        if raw_text == emitted_text:
            return ""
        # 最终 non-partial 事件常常携带「累计结尾段落」——用后缀匹配盖长文本场景
        if emitted_text.endswith(raw_text):
            return ""
        if emitted_text and raw_text.startswith(emitted_text):
            return raw_text[len(emitted_text):]
        return raw_text


# ==================== 独立运行模式 ====================


async def run_standalone(query: str = None):
    """
    独立运行模式 - 直接用 Python 脚本运行，无需 ADK CLI 或前端

    支持两种用法：
      python agent.py              # 交互模式
      python agent.py "查询问题"    # 单次查询
    """
    database_uri = os.getenv("DATABASE_URI")
    if not database_uri:
        print("\n⚠️  警告: 数据库未配置")
        print("请设置 DATABASE_URI 环境变量，例如:")
        print("  export DATABASE_URI='mysql+pymysql://user:pass@host:port/db'")
        print("  export DATABASE_URI='sqlite:///path/to/database.db'")
        return

    print("=" * 60)
    print("🤖 Text-to-SQL ADK Agent")
    print("=" * 60)

    from tools.native_sql_tools import _get_database
    set_database_uri(database_uri, "standalone")
    db = _get_database()
    if db:
        print(f"\n✅ 数据库已连接: {db.dialect}")
        tables = db.get_usable_table_names()
        print(f"📊 可用表: {', '.join(tables)}\n")

    svc = AgentService()

    if query:
        print(f"📝 查询: {query}")
        print("-" * 40)
        await svc.run_query(
            question=query,
            database_uri=database_uri,
            session_id="standalone_single",
            print_output=True,
        )
    else:
        cli_session_id = f"standalone_{int(time.time())}"
        print("输入你的问题（输入 'exit' 退出，'/new' 重置会话）:\n")
        while True:
            try:
                user_input = input("\n[你]: ").strip()
                if user_input.lower() in ("exit", "quit", "q"):
                    print("\n👋 再见！")
                    break
                if user_input.lower() in ("/new", "/reset", "new", "reset"):
                    await svc.close_session(cli_session_id)
                    print("✅ 已重置会话（对话历史 + 链接架构已清空）")
                    continue
                if not user_input:
                    continue
                print("\n[Agent]: ", end="", flush=True)
                await svc.run_query(
                    question=user_input,
                    database_uri=database_uri,
                    session_id=cli_session_id,
                    preserve_context=True,
                    print_output=True,
                )
            except KeyboardInterrupt:
                print("\n\n👋 再见！")
                break
            except Exception as e:
                print(f"\n❌ 错误: {e}")


if __name__ == "__main__":
    _query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None
    asyncio.run(run_standalone(_query))
