from __future__ import annotations
import re
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_id(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[^a-zA-Z0-9_\-.]+", "-", value)
    return value[:80].strip("-") or "account"
