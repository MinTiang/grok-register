from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import requests
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN_LIST_PATH = "/admin/api/tokens"
TOKEN_ADD_PATH = "/admin/api/tokens/add"
DEFAULT_POOL = "auto"


def normalize_api_host(api_host: str) -> str:
    raw = str(api_host or "").strip().rstrip("/")
    if not raw:
        return ""

    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return raw


def build_sink_url(api_host: str, path: str) -> str:
    host = normalize_api_host(api_host)
    if not host:
        return ""
    return f"{host}{path if path.startswith('/') else '/' + path}"


def build_auth_headers(
    api_token: str = "",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    if extra_headers:
        headers.update(extra_headers)
    return headers


def extract_sink_tokens(payload: Any) -> tuple[str, list[str]] | None:
    if isinstance(payload, list):
        source = payload
        kind = "new"
    elif isinstance(payload, dict) and isinstance(payload.get("tokens"), list):
        source = payload.get("tokens", [])
        kind = "new"
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        source = payload.get("data", [])
        kind = "new"
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        source = payload.get("items", [])
        kind = "new"
    elif isinstance(payload, dict) and isinstance(payload.get("tokens"), dict):
        source = payload["tokens"].get("ssoBasic", [])
        kind = "compat"
    elif isinstance(payload, dict) and isinstance(payload.get("ssoBasic"), list):
        source = payload.get("ssoBasic", [])
        kind = "legacy"
    else:
        return None

    tokens = [
        item["token"] if isinstance(item, dict) else str(item)
        for item in source
        if item
    ]
    return kind, tokens


def fetch_tokens(
    api_host: str,
    api_token: str = "",
    timeout: int = 15,
    verify: bool = False,
) -> requests.Response:
    list_url = build_sink_url(api_host, TOKEN_LIST_PATH)
    if not list_url:
        raise ValueError("api_host is empty")
    headers = build_auth_headers(api_token)
    return requests.get(
        list_url,
        headers=headers,
        timeout=timeout,
        verify=verify,
    )


def push_tokens(
    api_host: str,
    api_token: str,
    tokens: list[str],
    timeout: int = 60,
    verify: bool = False,
) -> tuple[bool, str]:
    cleaned_tokens = [str(token).strip() for token in tokens if str(token or "").strip()]
    if not cleaned_tokens:
        return True, "No tokens to push."

    add_url = build_sink_url(api_host, TOKEN_ADD_PATH)
    if not add_url:
        return False, "API endpoint is empty."
    if not api_token:
        return False, "API token is empty."

    response = requests.post(
        add_url,
        json={"pool": DEFAULT_POOL, "tokens": cleaned_tokens},
        headers=build_auth_headers(
            api_token,
            {"Content-Type": "application/json"},
        ),
        timeout=timeout,
        verify=verify,
    )
    if response.status_code in {200, 201, 204}:
        return True, f"SSO token 已推送到 API（新增 {len(cleaned_tokens)} 个）: {add_url}"
    return False, f"推送 API 返回异常: HTTP {response.status_code} {response.text[:200]}"
