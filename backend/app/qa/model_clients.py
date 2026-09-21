import atexit
import os
import threading
from functools import lru_cache
from typing import Optional

import httpx
from langchain_openai import ChatOpenAI


_http_lock = threading.Lock()
_http_client: Optional[httpx.Client] = None


def _base_url() -> str:
    raw = os.getenv("LITELLM_BASE_URL", "").rstrip("/")
    if not raw:
        return ""
    return raw if raw.endswith("/v1") else f"{raw}/v1"


@lru_cache(maxsize=32)
def _build_chat_model(
    base_url: str,
    api_key: str,
    model_alias: str,
    max_tokens: int,
) -> ChatOpenAI:
    return ChatOpenAI(
        model=model_alias,
        base_url=base_url,
        api_key=api_key,
        max_retries=0,
        max_tokens=max_tokens,
        temperature=0.0,
        timeout=30.0,
        # LiteLLM is used as an OpenAI-compatible Chat Completions gateway.
        # Keep this explicit so model-name heuristics cannot silently switch API shape.
        use_responses_api=False,
    )


def get_chat_model(model_alias: str, max_tokens: int) -> Optional[ChatOpenAI]:
    base_url = _base_url()
    if not base_url:
        return None
    api_key = os.getenv("LITELLM_MASTER_KEY", "sk-litellm-internal-master-key")
    return _build_chat_model(base_url, api_key, model_alias, max_tokens)


def get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is not None:
        return _http_client
    with _http_lock:
        if _http_client is None:
            _http_client = httpx.Client(
                timeout=httpx.Timeout(30.0, connect=5.0),
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                http2=True,
            )
    return _http_client


def close_clients() -> None:
    global _http_client
    with _http_lock:
        if _http_client is not None:
            _http_client.close()
            _http_client = None
    _build_chat_model.cache_clear()


atexit.register(close_clients)
