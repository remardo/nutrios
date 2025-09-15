from fastapi import Header, HTTPException
from config import ADMIN_API_KEY
async def require_api_key(x_api_key: str = Header(None)):
    if x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
