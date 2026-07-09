from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class KeyEncryptor:
    """Fernet-based encryptor for API keys and other secrets."""

    def __init__(self, master_key: str):
        self._fernet: Fernet | None = None
        if not master_key:
            logger.warning("ENCRYPTION_KEY 未设置，API Key 加密不可用")
            return
        try:
            digest = hashlib.sha256(master_key.encode("utf-8")).digest()
            key = base64.urlsafe_b64encode(digest)
            self._fernet = Fernet(key)
        except Exception as exc:
            logger.error("初始化 KeyEncryptor 失败: %s", exc)

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> bytes:
        if not self.available or self._fernet is None:
            raise RuntimeError("加密服务不可用")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes | str) -> str:
        if not self.available or self._fernet is None:
            raise RuntimeError("加密服务不可用")
        raw = ciphertext.encode("utf-8") if isinstance(ciphertext, str) else ciphertext
        try:
            return self._fernet.decrypt(raw).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("无法解密：密钥不匹配或数据已损坏") from exc
