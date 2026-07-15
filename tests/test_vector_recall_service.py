import os
import unittest
from unittest.mock import patch

from services.vector_recall_service import (
    RecallRequest,
    VectorRecallService,
    get_vector_recall_service,
    reset_vector_recall_service,
)


class _FakeMappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _FakeMappings(self._rows)


class _FakeConnection:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params):
        self.engine.statement = str(statement)
        self.engine.params = dict(params)
        return _FakeResult(self.engine.rows)


class _FakeEngine:
    def __init__(self, rows):
        self.rows = rows
        self.statement = ""
        self.params = {}
        self.disposed = False

    def connect(self):
        return _FakeConnection(self)

    def dispose(self):
        self.disposed = True


class VectorRecallServiceTests(unittest.TestCase):
    def test_request_accepts_phrase_alias_and_validates_bounds(self):
        request = RecallRequest.from_mapping(
            {"phrase": " school name ", "datasource_id": "7", "top_k": "3"}
        )
        self.assertEqual(request.query, "school name")
        self.assertEqual(request.datasource_id, 7)
        self.assertEqual(request.top_k, 3)

        for payload in (
            {},
            {"query": "x", "datasource_id": 0},
            {"query": "x", "datasource_id": 1.5},
            {"query": "x", "top_k": 101},
            {"query": "x", "top_k": 2.5},
            {"query": "x", "min_similarity": 1.1},
            {"query": "x", "min_similarity": float("nan")},
            {"query": "x", "only_checked": "true"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                RecallRequest.from_mapping(payload)

    def test_recall_joins_field_table_datasource_and_filters_dimension(self):
        engine = _FakeEngine(
            [
                {
                    "field_id": 11,
                    "datasource_id": 7,
                    "datasource_name": "school-db",
                    "datasource_description": "education",
                    "datasource_type": "postgresql",
                    "table_id": 8,
                    "table_name": "schools",
                    "table_comment": "school records",
                    "field_name": "school_name",
                    "field_type": "text",
                    "field_comment": "official school name",
                    "similarity": 0.91,
                }
            ]
        )
        service = VectorRecallService(
            engine=engine,
            embedding_provider=lambda _query: [0.1, 0.2, 0.3],
        )

        result = service.recall(
            "school name", datasource_id=7, top_k=4, min_similarity=0.2
        )

        self.assertEqual(result.embedding_dimension, 3)
        self.assertEqual(result.hits[0].table_name, "schools")
        self.assertEqual(result.hits[0].field_name, "school_name")
        self.assertNotIn("configuration", result.hits[0].to_dict())
        self.assertIn("JOIN t_datasource_table AS t", engine.statement)
        self.assertIn("t.id = f.table_id", engine.statement)
        self.assertIn("t.ds_id = f.ds_id", engine.statement)
        self.assertIn("JOIN t_datasource AS d", engine.statement)
        self.assertIn("vector_dims(f.embedding)", engine.statement)
        self.assertEqual(engine.params["datasource_id"], 7)
        self.assertEqual(engine.params["embedding_dimension"], 3)
        self.assertEqual(engine.params["top_k"], 4)

    def test_optional_singleton_stays_uninitialized_without_database_uri(self):
        reset_vector_recall_service()
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_vector_recall_service(required=False))


if __name__ == "__main__":
    unittest.main()
