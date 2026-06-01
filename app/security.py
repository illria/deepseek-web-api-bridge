from cryptography.fernet import Fernet
from app.config import settings


def get_fernet() -> Fernet | None:
    key = settings.state_encryption_key
    if key:
        try:
            return Fernet(key.encode("utf-8"))
        except Exception as exc:
            raise RuntimeError("STATE_ENCRYPTION_KEY 格式无效，请重新生成。") from exc
    if settings.allow_plaintext_state:
        return None
    raise RuntimeError("缺少 STATE_ENCRYPTION_KEY。请先运行 python -m app.tools.generate_key 生成。")


def encrypt_bytes(raw: bytes) -> tuple[bytes, bool]:
    f = get_fernet()
    if f is None:
        return raw, False
    return f.encrypt(raw), True


def decrypt_bytes(raw: bytes) -> tuple[bytes, bool]:
    f = get_fernet()
    if f is None:
        return raw, False
    return f.decrypt(raw), True
