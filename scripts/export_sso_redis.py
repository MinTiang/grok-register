#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys


DEFAULT_URL = "redis://a.z.whoyou.top:6378/0"
DEFAULT_KEY = "grok_sso"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export SSO tokens from Redis LIST/SET to a text file")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Redis URL (default: {DEFAULT_URL})")
    parser.add_argument("--key", default=DEFAULT_KEY, help=f"key (default: {DEFAULT_KEY})")
    parser.add_argument(
        "--structure",
        choices=("list", "set"),
        default="list",
        help="Redis structure (default: list)",
    )
    parser.add_argument("--output", "-o", default="sso_export.txt", help="Output txt path")
    parser.add_argument("--socket-timeout", type=float, default=5.0, help="Redis socket timeout seconds")
    args = parser.parse_args()

    try:
        import redis
    except ImportError:
        print("缺少 redis 依赖，请执行: pip install redis>=5.0.0", file=sys.stderr)
        return 1

    client = redis.from_url(
        args.url,
        socket_timeout=args.socket_timeout,
        socket_connect_timeout=args.socket_timeout,
        decode_responses=True,
    )
    try:
        if args.structure == "set":
            members = sorted(client.smembers(args.key))
        else:
            members = client.lrange(args.key, 0, -1)
    finally:
        client.close()

    with open(args.output, "w", encoding="utf-8") as file:
        for token in members:
            file.write(f"{token}\n")

    print(f"Exported {len(members)} tokens from {args.structure}:{args.key} -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
