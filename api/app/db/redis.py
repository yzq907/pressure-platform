"""Redis 异步客户端 + FastAPI 依赖。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import redis.asyncio as aioredis

from app.core.config import get_settings

_pool: aioredis.ConnectionPool | None = None


def _get_pool() -> aioredis.ConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = aioredis.ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=50,
        )
    return _pool


async def get_redis() -> AsyncIterator[aioredis.Redis]:
    """FastAPI 依赖：获取 Redis 客户端"""
    client = aioredis.Redis(connection_pool=_get_pool())
    try:
        yield client
    finally:
        await client.aclose()


async def dispose_redis() -> None:
    """应用关闭时调用，释放连接池"""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
