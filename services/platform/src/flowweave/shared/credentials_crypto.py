from __future__ import annotations

from cryptography.fernet import Fernet

from flowweave.shared.settings import get_settings

_DEVELOPMENT_CREDENTIALS_KEY = b"I84eBL_TIqLl5IVk_DTjGPtUDyVz3pl6pVCHyT8woaE="


def _fernet() -> Fernet:
    configured = get_settings().credentials_master_key.encode()
    return Fernet(configured or _DEVELOPMENT_CREDENTIALS_KEY)


def encrypt_secret(value: str) -> bytes:
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt_secret(value: bytes) -> str:
    return _fernet().decrypt(value).decode("utf-8")
