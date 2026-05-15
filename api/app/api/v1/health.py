"""健康检查接口。验证服务、DB、Redis 的连通性。无需鉴权。"""

from __future__ import annotations

import logging
import time

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import Response, success
from app.db.redis import get_redis
from app.db.session import get_db

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthInfo(BaseModel):
    db: str
    redis: str
    version: str
    uptime_seconds: int


async def _check_db(db: AsyncSession) -> str:
    try:
        await db.execute(text("SELECT 1"))
        return "ok"
    except Exception as e:  # noqa: BLE001
        log.warning("DB health check failed: %s", e)
        return "fail"


async def _check_redis(redis_client: aioredis.Redis) -> str:
    try:
        pong = await redis_client.ping()
        return "ok" if pong else "fail"
    except Exception as e:  # noqa: BLE001
        log.warning("Redis health check failed: %s", e)
        return "fail"


@router.get("/health", summary="健康检查", response_model=Response[HealthInfo])
async def health(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_client: aioredis.Redis = Depends(get_redis),
) -> Response[HealthInfo]:
    db_status = await _check_db(db)
    redis_status = await _check_redis(redis_client)
    start_time: float = getattr(request.app.state, "start_time", time.monotonic())
    info = HealthInfo(
        db=db_status,
        redis=redis_status,
        version="0.1.0",
        uptime_seconds=int(time.monotonic() - start_time),
    )
    return success(info)
