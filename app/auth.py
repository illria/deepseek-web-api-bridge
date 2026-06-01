from fastapi import Header, HTTPException
from app.config import settings


async def require_bridge_auth(authorization: str | None = Header(default=None)) -> None:
    keys = settings.api_key_set
    if not keys:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header.")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Authorization must be Bearer token.")
    token = authorization[len(prefix):].strip()
    if token not in keys:
        raise HTTPException(status_code=401, detail="Invalid API key.")
