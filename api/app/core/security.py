"""密码加密 + token 生成。对齐 Java 端的 jBCrypt + UUID.randomUUID() 行为。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import bcrypt

from app.core.config import get_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")


def hash_password(plain: str) -> str:
    """BCrypt 哈希密码"""
    rounds = get_settings().bcrypt_rounds
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """BCrypt 验证密码（与 Java 端 BCrypt.checkpw 等价）"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def generate_token() -> str:
    """生成不透明 token（UUID v4），与 Java 端 UUID.randomUUID().toString() 等价"""
    return str(uuid.uuid4())


def token_expire_time(hours: int | None = None) -> datetime:
    """计算 token 过期时刻；默认从配置取 12 小时"""
    if hours is None:
        hours = get_settings().token_expire_hours
    return datetime.now(SHANGHAI) + timedelta(hours=hours)
