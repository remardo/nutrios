import os
from fastapi import Header, HTTPException

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "supersecret")
async def require_api_key(x_api_key: str = Header(None)):
    if x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
