"""Versioned SSH credential envelopes; master key lives outside the database."""

from __future__ import annotations

import base64
import binascii
import os
import stat
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

PREFIX = "sshenc:v1:"
RESERVED_PREFIX = "sshenc:"
_KEY_AAD = b"mysterious:sshenc:v1:wrapped-key"
_PASSWORD_AAD = b"mysterious:sshenc:v1:password"
# MySQL TEXT holds 65535 bytes; envelope overhead is 88 bytes plus base64/prefix.
_MAX_PASSWORD_BYTES = ((65535 - len(PREFIX)) // 4) * 3 - 88


class CredentialEncryptionError(RuntimeError):
    """Safe to log: never include key material, passwords or ciphertext."""


def initialize_key_file(path: str | Path) -> None:
    """Explicitly create a private key; never overwrite a previous key."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(base64.b64encode(os.urandom(32)) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise CredentialEncryptionError("SSH 主密钥创建失败：目标必须不存在，父目录须存在且可写；禁止覆盖密钥") from None


def load_master_key() -> bytes:
    """Read using a no-follow descriptor, rejecting unsafe or malformed files."""
    path = get_settings().ssh_password_key_file.strip()
    if not path:
        raise CredentialEncryptionError("必须配置 SSH 主密钥文件 MYSTERIOUS_SSH_PASSWORD_KEY_FILE")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o600, 0o400):
                raise CredentialEncryptionError("SSH 主密钥必须是非符号链接的普通文件，权限须为 0600 或 0400")
            encoded = stream.read(129)
        key = base64.b64decode(encoded.strip(), validate=True)
        if len(encoded) > 128 or len(key) != 32:
            raise ValueError
        return key
    except (OSError, ValueError, binascii.Error):
        raise CredentialEncryptionError("SSH 主密钥文件缺失、不可读或格式错误；请恢复原密钥，不要重新生成") from None


def encrypt_password(password: str, *, master_key: bytes | None = None) -> str:
    """Treat ALL user input as plaintext, including reserved-prefix passwords."""
    if not password:
        return ""
    encoded_password = password.encode("utf-8")
    if len(encoded_password) > _MAX_PASSWORD_BYTES:
        raise CredentialEncryptionError("SSH 密码过长，无法安全保存为加密凭据")
    key = master_key if master_key is not None else load_master_key()
    data_key = os.urandom(32)
    wrap_nonce, data_nonce = os.urandom(12), os.urandom(12)
    wrapped = AESGCM(key).encrypt(wrap_nonce, data_key, _KEY_AAD)
    encrypted = AESGCM(data_key).encrypt(data_nonce, encoded_password, _PASSWORD_AAD)
    return PREFIX + base64.b64encode(wrap_nonce + wrapped + data_nonce + encrypted).decode("ascii")


def decrypt_password(envelope: str, *, master_key: bytes | None = None) -> str:
    """Strictly accept authenticated envelopes or empty passwords; no fallback."""
    if not envelope:
        return ""
    if not envelope.startswith(PREFIX):
        raise CredentialEncryptionError("SSH 密码不是受支持的加密格式；请完成迁移或重新录入密码")
    key = master_key if master_key is not None else load_master_key()
    try:
        raw = base64.b64decode(envelope[len(PREFIX):], validate=True)
        if len(raw) < 88:
            raise ValueError
        data_key = AESGCM(key).decrypt(raw[:12], raw[12:60], _KEY_AAD)
        return AESGCM(data_key).decrypt(raw[60:72], raw[72:], _PASSWORD_AAD).decode("utf-8")
    except (ValueError, binascii.Error, InvalidTag):
        raise CredentialEncryptionError("SSH 密码解密失败：主密钥不匹配或密文已损坏；禁止明文回退") from None
