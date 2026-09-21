"""Provider migration contract shared by the scheduler, worker, and model factory.

No credentials, environment loading, or network calls belong in this module.
Endpoint hashes use the exact configured URL so historical manifests stay valid.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit


LEGACY_MODEL = "deepseek-v4.1-flash"
OFFICIAL_MODEL = "deepseek-flash"
SUPPORTED_MODELS = frozenset((LEGACY_MODEL, OFFICIAL_MODEL))


def endpoint_sha256(base_url: str) -> str:
    return hashlib.sha256(base_url.encode("utf-8")).hexdigest()


def validate_model_contract(
    model_name: str,
    base_url: str,
    *,
    expected_model: str | None = None,
    frozen_endpoint_sha256: str | None = None,
    require_frozen_endpoint: bool = False,
) -> dict:
    """Fail before requests if the configured model or frozen endpoint drifts.

    The legacy alias retains its historical endpoint contract. The official
    alias is restricted to the verified DeepSeek HTTPS API, with no proxy,
    credentials in the URL, query, or fragment. Worker calls additionally
    require a frozen endpoint hash for this newly introduced alias.
    """
    if not isinstance(model_name, str) or model_name not in SUPPORTED_MODELS:
        raise ValueError("Configured model must be deepseek-v4.1-flash or deepseek-flash")
    if expected_model is not None and expected_model != model_name:
        raise ValueError("Experiment model does not match the configured model")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("Missing SQL model base URL")
    if model_name == OFFICIAL_MODEL:
        parsed = urlsplit(base_url)
        if (base_url != base_url.strip() or any(ord(char) < 33 for char in base_url)
                or parsed.scheme != "https"
                or parsed.netloc.lower() not in ("api.deepseek.com", "api.deepseek.com:443")
                or parsed.path not in ("", "/", "/v1", "/v1/")
                or parsed.query or parsed.fragment):
            raise ValueError("deepseek-flash requires the official api.deepseek.com HTTPS endpoint")
        if require_frozen_endpoint and frozen_endpoint_sha256 is None:
            raise ValueError("deepseek-flash requires a frozen provider endpoint hash")
    actual_hash = endpoint_sha256(base_url)
    if frozen_endpoint_sha256 is not None:
        if (not isinstance(frozen_endpoint_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", frozen_endpoint_sha256)
                or frozen_endpoint_sha256 != actual_hash):
            raise ValueError("Provider endpoint does not match the frozen endpoint hash")
    return {"model_name": model_name, "provider_endpoint_sha256": actual_hash}


def nonthinking_body(model_name: str) -> dict:
    """Use the provider's own explicit non-thinking switch, without fallback."""
    if model_name == OFFICIAL_MODEL:
        return {"thinking": {"type": "disabled"}}
    if model_name == LEGACY_MODEL:
        return {"chat_template_kwargs": {"enable_thinking": False}}
    raise ValueError("Unsupported model for non-thinking mode")
