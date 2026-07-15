import io
import json
import unittest

from services.vector_recall_service import VectorRecallService
from vector_recall_api import DEFAULT_RECALL_PATH, VectorRecallHttpApplication


class _StubResponse:
    def __init__(self):
        self.status = ""
        self.headers = []

    def start_response(self, status, headers):
        self.status = status
        self.headers = headers


class VectorRecallHttpApplicationTests(unittest.TestCase):
    @staticmethod
    def _request(app, method, path, payload=None):
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        response = _StubResponse()
        chunks = app(environ, response.start_response)
        return response.status, json.loads(b"".join(chunks).decode("utf-8"))

    def test_post_recall_returns_uniform_envelope(self):
        service = VectorRecallService(
            engine=_NoHitEngine(), embedding_provider=lambda _query: [0.1, 0.2]
        )
        app = VectorRecallHttpApplication(service)

        status, body = self._request(
            app,
            "POST",
            DEFAULT_RECALL_PATH,
            {"query": "revenue", "datasource_id": 2, "top_k": 5},
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["query"], "revenue")
        self.assertEqual(body["data"]["datasource_id"], 2)
        self.assertEqual(body["data"]["count"], 0)

    def test_invalid_request_is_400_and_unknown_path_is_404(self):
        app = VectorRecallHttpApplication(
            VectorRecallService(
                engine=_NoHitEngine(), embedding_provider=lambda _query: [0.1]
            )
        )
        status, body = self._request(app, "POST", DEFAULT_RECALL_PATH, {})
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(body["code"], 400)

        status, body = self._request(app, "GET", "/missing")
        self.assertEqual(status, "404 Not Found")
        self.assertEqual(body["code"], 404)


class _NoHitResult:
    def mappings(self):
        return self

    def all(self):
        return []


class _NoHitConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _statement, _params):
        return _NoHitResult()


class _NoHitEngine:
    def connect(self):
        return _NoHitConnection()


if __name__ == "__main__":
    unittest.main()
