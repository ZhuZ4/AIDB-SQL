"""HTTP adapters for the unified AIX vector recall service.

Run a dependency-free local HTTP endpoint with::

    python vector_recall_api.py --host 0.0.0.0 --port 8090

The module also exposes ``create_sanic_blueprint`` for applications that
already run Sanic, and ``application`` for any WSGI server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any, Callable, Optional
from wsgiref.simple_server import make_server

from services.vector_recall_service import (
    RecallConfigurationError,
    RecallRequest,
    VectorRecallError,
    VectorRecallService,
    get_vector_recall_service,
)


logger = logging.getLogger(__name__)
DEFAULT_RECALL_PATH = "/api/v1/vector-recall"
MAX_REQUEST_BYTES = 1024 * 1024


def _recall(service: VectorRecallService, payload: dict[str, Any]) -> dict[str, Any]:
    request = RecallRequest.from_mapping(payload)
    result = service.recall(
        request.query,
        datasource_id=request.datasource_id,
        top_k=request.top_k,
        min_similarity=request.min_similarity,
        only_checked=request.only_checked,
    )
    return result.to_dict()


def _response_body(code: int, message: str, data: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data}


class VectorRecallHttpApplication:
    """Small WSGI application exposing health and vector-recall endpoints."""

    def __init__(
        self,
        service: Optional[VectorRecallService] = None,
        *,
        recall_path: str = DEFAULT_RECALL_PATH,
    ) -> None:
        self._service = service
        self.recall_path = recall_path

    def _get_service(self) -> VectorRecallService:
        service = self._service or get_vector_recall_service(required=True)
        if service is None:  # pragma: no cover - required=True guarantees this
            raise RecallConfigurationError("vector recall service is not configured")
        return service

    @staticmethod
    def _write(start_response: Callable, status: str, body: dict[str, Any]):
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        start_response(
            status,
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(encoded))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [encoded]

    def __call__(self, environ: dict[str, Any], start_response: Callable):
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", ""))

        if method == "GET" and path == "/health":
            configured = self._service.configured if self._service else bool(
                os.getenv("AIX_DB_PG_URI") or os.getenv("SQLALCHEMY_DATABASE_URI")
            )
            return self._write(
                start_response,
                "200 OK",
                _response_body(0, "ok", {"configured": configured}),
            )

        if path != self.recall_path:
            return self._write(
                start_response,
                "404 Not Found",
                _response_body(404, "not found"),
            )
        if method != "POST":
            return self._write(
                start_response,
                "405 Method Not Allowed",
                _response_body(405, "method not allowed"),
            )

        try:
            raw_length = environ.get("CONTENT_LENGTH") or "0"
            content_length = int(raw_length)
            if content_length <= 0:
                raise ValueError("request body is required")
            if content_length > MAX_REQUEST_BYTES:
                return self._write(
                    start_response,
                    "413 Payload Too Large",
                    _response_body(413, "request body is too large"),
                )
            raw_body = environ["wsgi.input"].read(content_length)
            payload = json.loads(raw_body.decode("utf-8"))
            data = _recall(self._get_service(), payload)
            return self._write(
                start_response,
                "200 OK",
                _response_body(0, "success", data),
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._write(
                start_response,
                "400 Bad Request",
                _response_body(400, str(exc)),
            )
        except RecallConfigurationError as exc:
            return self._write(
                start_response,
                "503 Service Unavailable",
                _response_body(503, str(exc)),
            )
        except VectorRecallError as exc:
            logger.warning("vector recall request failed: %s", exc)
            return self._write(
                start_response,
                "502 Bad Gateway",
                _response_body(502, str(exc)),
            )
        except Exception:
            logger.exception("unexpected vector recall HTTP error")
            return self._write(
                start_response,
                "500 Internal Server Error",
                _response_body(500, "internal server error"),
            )


def create_sanic_blueprint(
    service: Optional[VectorRecallService] = None,
    *,
    url_prefix: str = "/api/v1",
):
    """Create a Sanic blueprint without making Sanic an import-time dependency."""
    try:
        from sanic import Blueprint
        from sanic.response import json as sanic_json
    except ImportError as exc:  # pragma: no cover - exercised in host application
        raise RuntimeError("Sanic is required to create the recall blueprint") from exc

    blueprint = Blueprint("vector_recall", url_prefix=url_prefix)

    @blueprint.get("/vector-recall/health")
    async def vector_recall_health(_request):
        configured = service.configured if service else bool(
            os.getenv("AIX_DB_PG_URI") or os.getenv("SQLALCHEMY_DATABASE_URI")
        )
        return sanic_json(_response_body(0, "ok", {"configured": configured}))

    @blueprint.post("/vector-recall")
    async def vector_recall(request):
        try:
            payload = request.json or {}
            active_service = service or get_vector_recall_service(required=True)
            if active_service is None:  # pragma: no cover
                raise RecallConfigurationError("vector recall service is not configured")
            data = await asyncio.to_thread(_recall, active_service, payload)
            return sanic_json(_response_body(0, "success", data))
        except ValueError as exc:
            return sanic_json(_response_body(400, str(exc)), status=400)
        except RecallConfigurationError as exc:
            return sanic_json(_response_body(503, str(exc)), status=503)
        except VectorRecallError as exc:
            logger.warning("vector recall request failed: %s", exc)
            return sanic_json(_response_body(502, str(exc)), status=502)
        except Exception:
            logger.exception("unexpected vector recall Sanic error")
            return sanic_json(
                _response_body(500, "internal server error"), status=500
            )

    return blueprint


application = VectorRecallHttpApplication()


def main() -> None:
    parser = argparse.ArgumentParser(description="AIX pgvector recall HTTP service")
    parser.add_argument("--host", default=os.getenv("VECTOR_RECALL_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("VECTOR_RECALL_PORT", "8090"))
    )
    args = parser.parse_args()

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    with make_server(args.host, args.port, application) as server:
        logger.info(
            "vector recall HTTP service listening on http://%s:%d%s",
            args.host,
            args.port,
            DEFAULT_RECALL_PATH,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("vector recall HTTP service stopped")


if __name__ == "__main__":
    main()
