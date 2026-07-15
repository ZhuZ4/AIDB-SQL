"""Unified pgvector recall service for datasource field metadata.

The service queries the AIX metadata database and deliberately returns no
datasource connection configuration.  A recall hit is resolved through the
following ownership chain before it leaves the service:

    t_datasource_field -> t_datasource_table -> t_datasource

Dependencies are imported lazily so request validation and the HTTP adapter
remain testable even in a minimal Python environment.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Callable, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_TOP_K = 10
MAX_TOP_K = 100


class VectorRecallError(RuntimeError):
    """Base error raised by the vector recall layer."""


class RecallConfigurationError(VectorRecallError):
    """Raised when a required database or embedding setting is missing."""


@dataclass(frozen=True)
class RecallRequest:
    """Validated input for one vector recall operation."""

    query: str
    datasource_id: Optional[int] = None
    top_k: int = DEFAULT_TOP_K
    min_similarity: float = 0.0
    only_checked: bool = True

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "RecallRequest":
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")

        query = payload.get("query", payload.get("phrase", ""))
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")

        raw_datasource_id = payload.get("datasource_id")
        datasource_id: Optional[int] = None
        if raw_datasource_id not in (None, ""):
            if isinstance(raw_datasource_id, bool):
                raise ValueError("datasource_id must be a positive integer")
            try:
                datasource_id = int(raw_datasource_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("datasource_id must be a positive integer") from exc
            if isinstance(raw_datasource_id, float) and not raw_datasource_id.is_integer():
                raise ValueError("datasource_id must be a positive integer")
            if isinstance(raw_datasource_id, str) and raw_datasource_id.strip() != str(
                datasource_id
            ):
                raise ValueError("datasource_id must be a positive integer")
            if datasource_id <= 0:
                raise ValueError("datasource_id must be a positive integer")

        raw_top_k = payload.get("top_k", DEFAULT_TOP_K)
        if isinstance(raw_top_k, bool):
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
        try:
            top_k = int(raw_top_k)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}") from exc
        if isinstance(raw_top_k, float) and not raw_top_k.is_integer():
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
        if isinstance(raw_top_k, str) and raw_top_k.strip() != str(top_k):
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
        if not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")

        raw_similarity = payload.get("min_similarity", 0.0)
        if isinstance(raw_similarity, bool):
            raise ValueError("min_similarity must be between -1 and 1")
        try:
            min_similarity = float(raw_similarity)
        except (TypeError, ValueError) as exc:
            raise ValueError("min_similarity must be between -1 and 1") from exc
        if not math.isfinite(min_similarity) or not -1.0 <= min_similarity <= 1.0:
            raise ValueError("min_similarity must be between -1 and 1")

        only_checked = payload.get("only_checked", True)
        if not isinstance(only_checked, bool):
            raise ValueError("only_checked must be a boolean")

        return cls(
            query=query.strip(),
            datasource_id=datasource_id,
            top_k=top_k,
            min_similarity=min_similarity,
            only_checked=only_checked,
        )


@dataclass(frozen=True)
class RecallHit:
    field_id: int
    datasource_id: int
    datasource_name: str
    datasource_description: str
    datasource_type: str
    table_id: int
    table_name: str
    table_comment: str
    field_name: str
    field_type: str
    field_comment: str
    similarity: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecallResponse:
    query: str
    datasource_id: Optional[int]
    embedding_dimension: int
    hits: tuple[RecallHit, ...]
    elapsed_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "datasource_id": self.datasource_id,
            "embedding_dimension": self.embedding_dimension,
            "count": len(self.hits),
            "elapsed_ms": self.elapsed_ms,
            "hits": [hit.to_dict() for hit in self.hits],
        }


class OpenAICompatibleEmbeddingProvider:
    """Small dependency-free client for an OpenAI-compatible embedding API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("EMBEDDING_API_URL", "http://10.10.185.22:28080/v1")
        ).rstrip("/")
        self.api_key = api_key or os.getenv("EMBEDDING_API_KEY", "no-key")
        self.model = model or os.getenv("EMBEDDING_MODEL", "bge-m3-FP16.gguf")
        configured_dimensions = dimensions
        if configured_dimensions is None:
            configured_dimensions = int(os.getenv("EMBEDDING_DIM", "1024"))
        self.dimensions = configured_dimensions
        self.timeout = timeout or float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "60"))

    def __call__(self, text: str) -> list[float]:
        return list(self._embed_cached(text))

    @lru_cache(maxsize=2048)
    def _embed_cached(self, text: str) -> tuple[float, ...]:
        payload: dict[str, Any] = {"model": self.model, "input": [text]}
        if self.dimensions and self.dimensions > 0:
            payload["dimensions"] = self.dimensions

        request = Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise VectorRecallError(
                f"embedding API returned HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise VectorRecallError(f"embedding API request failed: {exc}") from exc

        data = body.get("data") if isinstance(body, dict) else None
        embedding = data[0].get("embedding") if data and isinstance(data[0], dict) else None
        if not isinstance(embedding, list) or not embedding:
            raise VectorRecallError("embedding API returned no embedding")
        try:
            return tuple(float(value) for value in embedding)
        except (TypeError, ValueError) as exc:
            raise VectorRecallError("embedding API returned an invalid embedding") from exc


_RECALL_SQL = """
WITH ranked_fields AS (
    SELECT
        f.id AS field_id,
        d.id AS datasource_id,
        d.name AS datasource_name,
        COALESCE(d.description, '') AS datasource_description,
        d.type AS datasource_type,
        t.id AS table_id,
        t.table_name,
        COALESCE(t.custom_comment, t.table_comment, '') AS table_comment,
        f.field_name,
        COALESCE(f.field_type, '') AS field_type,
        COALESCE(f.custom_comment, f.field_comment, '') AS field_comment,
        1 - (f.embedding <=> CAST(:query_embedding AS vector)) AS similarity
    FROM t_datasource_field AS f
    JOIN t_datasource_table AS t
      ON t.id = f.table_id
     AND t.ds_id = f.ds_id
    JOIN t_datasource AS d
      ON d.id = t.ds_id
    WHERE f.embedding IS NOT NULL
      AND vector_dims(f.embedding) = :embedding_dimension
      AND (:datasource_id IS NULL OR d.id = :datasource_id)
      AND (
          :only_checked = FALSE
          OR (COALESCE(f.checked, TRUE) = TRUE AND COALESCE(t.checked, TRUE) = TRUE)
      )
)
SELECT
    field_id,
    datasource_id,
    datasource_name,
    datasource_description,
    datasource_type,
    table_id,
    table_name,
    table_comment,
    field_name,
    field_type,
    field_comment,
    similarity
FROM ranked_fields
WHERE similarity >= :min_similarity
ORDER BY similarity DESC, field_id ASC
LIMIT :top_k
"""


class VectorRecallService:
    """Vector search over AIX datasource fields with joined ownership metadata."""

    def __init__(
        self,
        database_uri: Optional[str] = None,
        *,
        engine: Any = None,
        embedding_provider: Optional[Callable[[str], Sequence[float]]] = None,
    ) -> None:
        self.database_uri = database_uri or _get_database_uri()
        self._engine = engine
        self._embedding_provider = embedding_provider or OpenAICompatibleEmbeddingProvider()
        self._engine_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self._engine is not None or bool(self.database_uri)

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        if not self.database_uri:
            raise RecallConfigurationError(
                "AIX vector database is not configured; set AIX_DB_PG_URI "
                "or SQLALCHEMY_DATABASE_URI"
            )
        with self._engine_lock:
            if self._engine is None:
                try:
                    from sqlalchemy import create_engine
                except ImportError as exc:
                    raise RecallConfigurationError(
                        "SQLAlchemy is required for vector recall"
                    ) from exc
                self._engine = create_engine(
                    self.database_uri,
                    pool_pre_ping=True,
                    pool_size=int(os.getenv("VECTOR_RECALL_POOL_SIZE", "3")),
                    max_overflow=int(os.getenv("VECTOR_RECALL_MAX_OVERFLOW", "3")),
                )
        return self._engine

    @staticmethod
    def _statement() -> Any:
        try:
            from sqlalchemy import text
        except ImportError:
            return _RECALL_SQL
        return text(_RECALL_SQL)

    def recall(
        self,
        query: str,
        *,
        datasource_id: Optional[int] = None,
        top_k: int = DEFAULT_TOP_K,
        min_similarity: float = 0.0,
        only_checked: bool = True,
    ) -> RecallResponse:
        recall_request = RecallRequest.from_mapping(
            {
                "query": query,
                "datasource_id": datasource_id,
                "top_k": top_k,
                "min_similarity": min_similarity,
                "only_checked": only_checked,
            }
        )
        started_at = time.perf_counter()

        try:
            embedding = [float(value) for value in self._embedding_provider(recall_request.query)]
        except VectorRecallError:
            raise
        except Exception as exc:
            raise VectorRecallError(f"embedding generation failed: {exc}") from exc
        if not embedding:
            raise VectorRecallError("embedding generation returned an empty vector")

        embedding_text = "[" + ",".join(format(value, ".17g") for value in embedding) + "]"
        params = {
            "query_embedding": embedding_text,
            "embedding_dimension": len(embedding),
            "datasource_id": recall_request.datasource_id,
            "only_checked": recall_request.only_checked,
            "min_similarity": recall_request.min_similarity,
            "top_k": recall_request.top_k,
        }

        try:
            engine = self._get_engine()
            with engine.connect() as connection:
                rows = connection.execute(self._statement(), params).mappings().all()
        except RecallConfigurationError:
            raise
        except Exception as exc:
            raise VectorRecallError(f"aix_db vector query failed: {exc}") from exc

        hits = tuple(
            RecallHit(
                field_id=int(row["field_id"]),
                datasource_id=int(row["datasource_id"]),
                datasource_name=str(row["datasource_name"] or ""),
                datasource_description=str(row["datasource_description"] or ""),
                datasource_type=str(row["datasource_type"] or ""),
                table_id=int(row["table_id"]),
                table_name=str(row["table_name"] or ""),
                table_comment=str(row["table_comment"] or ""),
                field_name=str(row["field_name"] or ""),
                field_type=str(row["field_type"] or ""),
                field_comment=str(row["field_comment"] or ""),
                similarity=float(row["similarity"]),
            )
            for row in rows
        )
        return RecallResponse(
            query=recall_request.query,
            datasource_id=recall_request.datasource_id,
            embedding_dimension=len(embedding),
            hits=hits,
            elapsed_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )

    def close(self) -> None:
        engine = self._engine
        self._engine = None
        if engine is not None and hasattr(engine, "dispose"):
            engine.dispose()


def _get_database_uri() -> str:
    return (
        os.getenv("AIX_DB_PG_URI")
        or os.getenv("SQLALCHEMY_DATABASE_URI")
        or ""
    ).strip()


_service: Optional[VectorRecallService] = None
_service_lock = threading.Lock()


def get_vector_recall_service(*, required: bool = True) -> Optional[VectorRecallService]:
    """Return the process-wide recall service, initialized lazily."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                candidate = VectorRecallService()
                if not candidate.configured:
                    if required:
                        raise RecallConfigurationError(
                            "AIX vector database is not configured; set AIX_DB_PG_URI "
                            "or SQLALCHEMY_DATABASE_URI"
                        )
                    return None
                _service = candidate
    return _service


def reset_vector_recall_service() -> None:
    """Dispose the singleton; primarily useful for config reloads and tests."""
    global _service
    with _service_lock:
        if _service is not None:
            _service.close()
        _service = None
