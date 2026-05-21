"""密码加密 + token 生成。对齐 Java 端的 jBCrypt + UUID.randomUUID() 行为。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import bcrypt

from app.core.config import get_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 密码策略规则
_PASSWORD_MIN_LEN = 8
_PASSWORD_MAX_LEN = 64
_HAS_UPPER = re.compile(r"[A-Z]")
_HAS_LOWER = re.compile(r"[a-z]")
_HAS_DIGIT = re.compile(r"[0-9]")


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


def check_password_strength(password: str, username: str | None = None) -> tuple[bool, str]:
    """检查密码复杂度，返回 (是否通过, 失败原因)。

    规则：
    1. 长度 8~64 位
    2. 必须同时包含大写字母、小写字母、数字
    3. 不能包含用户名（正向/反向）
    """
    if not password:
        return False, "密码不能为空"
    if len(password) < _PASSWORD_MIN_LEN:
        return False, f"密码长度不能少于 {_PASSWORD_MIN_LEN} 位"
    if len(password) > _PASSWORD_MAX_LEN:
        return False, f"密码长度不能超过 {_PASSWORD_MAX_LEN} 位"
    if not _HAS_UPPER.search(password):
        return False, "密码必须包含大写字母 A-Z"
    if not _HAS_LOWER.search(password):
        return False, "密码必须包含小写字母 a-z"
    if not _HAS_DIGIT.search(password):
        return False, "密码必须包含数字 0-9"
    if username:
        low_pwd = password.lower()
        low_user = username.lower()
        if low_user in low_pwd or low_pwd in low_user:
            return False, "密码不能包含用户名"
    return True, ""


def generate_token() -> str:
    """生成不透明 token（UUID v4），与 Java 端 UUID.randomUUID().toString() 等价"""
    return str(uuid.uuid4())


def token_expire_time(hours: int | None = None) -> datetime:
    """计算 token 过期时刻；默认从配置取 12 小时"""
    if hours is None:
        hours = get_settings().token_expire_hours
    return datetime.now(SHANGHAI) + timedelta(hours=hours)
