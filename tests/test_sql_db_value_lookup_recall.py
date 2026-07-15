import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import tools.native_sql_tools as native_tools
except ModuleNotFoundError as exc:  # Minimal environments can still test the service layer.
    raise unittest.SkipTest(f"native SQL tool dependencies are unavailable: {exc}")

from services.vector_recall_service import RecallHit, RecallResponse
from tools.tool_call_manager import get_tool_call_manager


class _Rows:
    def fetchall(self):
        return []


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _statement):
        return _Rows()


class _Engine:
    def connect(self):
        return _Connection()


class _Database:
    dialect = "postgresql"
    _engine = _Engine()

    def get_usable_table_names(self):
        return ["schools"]


class _Inspector:
    def get_columns(self, table_name):
        if table_name == "schools":
            return [
                {"name": "id", "type": "BIGINT"},
                {"name": "school_name", "type": "TEXT"},
            ]
        return []

    def get_pk_constraint(self, _table_name):
        return {"constrained_columns": ["id"]}

    def get_foreign_keys(self, _table_name):
        return []


class _RecallService:
    def __init__(self):
        self.calls = []

    def recall(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return RecallResponse(
            query=query,
            datasource_id=kwargs["datasource_id"],
            embedding_dimension=3,
            elapsed_ms=1.0,
            hits=(
                RecallHit(
                    field_id=5,
                    datasource_id=kwargs["datasource_id"],
                    datasource_name="school-db",
                    datasource_description="",
                    datasource_type="postgresql",
                    table_id=4,
                    table_name="schools",
                    table_comment="school records",
                    field_name="school_name",
                    field_type="text",
                    field_comment="official school name",
                    similarity=0.92,
                ),
            ),
        )


class _FailingRecallService:
    def recall(self, _query, **_kwargs):
        raise RuntimeError("aix unavailable")


class _StaleRecallService:
    def recall(self, query, **kwargs):
        return RecallResponse(
            query=query,
            datasource_id=kwargs["datasource_id"],
            embedding_dimension=3,
            elapsed_ms=1.0,
            hits=(
                RecallHit(
                    field_id=99,
                    datasource_id=kwargs["datasource_id"],
                    datasource_name="stale-db",
                    datasource_description="",
                    datasource_type="postgresql",
                    table_id=98,
                    table_name="deleted_table",
                    table_comment="stale metadata",
                    field_name="deleted_field",
                    field_type="text",
                    field_comment="stale field",
                    similarity=0.99,
                ),
            ),
        )


class SqlDbValueLookupRecallTests(unittest.TestCase):
    def setUp(self):
        self.old_db = native_tools._db_instance
        self.old_session_id = native_tools._session_id
        self.session_id = "value_lookup_vector_recall_test"
        native_tools._db_instance = _Database()
        native_tools._session_id = self.session_id
        native_tools.set_datasource_context(7, self.session_id)
        get_tool_call_manager().reset_session(self.session_id)

    def tearDown(self):
        native_tools.reset_session(self.session_id)
        get_tool_call_manager().reset_session(self.session_id)
        native_tools._db_instance = self.old_db
        native_tools._session_id = self.old_session_id

    def test_value_lookup_uses_datasource_scoped_aix_recall(self):
        recall_service = _RecallService()
        with patch.object(native_tools, "sa_inspect", return_value=_Inspector()), patch(
            "services.vector_recall_service.get_vector_recall_service",
            return_value=recall_service,
        ):
            result = native_tools.sql_db_value_lookup("school name")

        self.assertEqual(recall_service.calls[0][0], "school name")
        self.assertEqual(recall_service.calls[0][1]["datasource_id"], 7)
        self.assertIn("schools.school_name", result)
        self.assertIn("official school name", result)
        self.assertIn("rel=0.92", result)

    def test_value_lookup_falls_back_to_local_schema_names(self):
        with patch.object(native_tools, "sa_inspect", return_value=_Inspector()), patch(
            "services.vector_recall_service.get_vector_recall_service",
            return_value=None,
        ), patch.dict("os.environ", {"BIRD_DEV_PG_URI": ""}):
            result = native_tools.sql_db_value_lookup("school name")

        self.assertIn("schools.school_name", result)
        self.assertNotIn("未找到匹配的架构元素", result)

    def test_aix_failure_does_not_block_bird_fallback(self):
        bird_column = SimpleNamespace(
            table_name="schools",
            column_name="school_name",
            metadata={"type": "TEXT", "samples": []},
            value_text="official school name",
            vec_score=0.88,
        )
        bird_result = SimpleNamespace(
            tables=[SimpleNamespace(table_name="schools")],
            columns=[bird_column],
            values=[],
        )
        with patch.object(native_tools, "sa_inspect", return_value=_Inspector()), patch(
            "services.vector_recall_service.get_vector_recall_service",
            return_value=_FailingRecallService(),
        ), patch(
            "tools.bird_dev_retriever.hybrid_retrieve",
            return_value=bird_result,
        ) as hybrid_retrieve, patch.dict(
            "os.environ",
            {
                "BIRD_DEV_PG_URI": "postgresql://configured",
                "BIRD_DEV_DB_ID": "configured_db",
            },
        ):
            result = native_tools.sql_db_value_lookup("school name")

        hybrid_retrieve.assert_called_once_with("school name", db_id="configured_db")
        self.assertIn("schools.school_name", result)
        self.assertIn("official school name", result)

    def test_stale_aix_hits_are_removed_before_local_fallback(self):
        with patch.object(native_tools, "sa_inspect", return_value=_Inspector()), patch(
            "services.vector_recall_service.get_vector_recall_service",
            return_value=_StaleRecallService(),
        ), patch.dict("os.environ", {"BIRD_DEV_PG_URI": ""}):
            result = native_tools.sql_db_value_lookup("school name")

        self.assertNotIn("deleted_table.deleted_field", result)
        self.assertIn("schools.school_name", result)

    def test_postgresql_schema_is_used_as_bird_db_id(self):
        database = SimpleNamespace(
            dialect="postgresql",
            _schema="california_schools",
            _engine=SimpleNamespace(url=SimpleNamespace(database="bird")),
        )
        self.assertEqual(
            native_tools._infer_current_db_id(database),
            "california_schools",
        )


if __name__ == "__main__":
    unittest.main()
