import json
from dataclasses import dataclass
from typing import Set

from fastapi import Depends, Header, HTTPException

from config import ADMIN_API_KEY


@dataclass
class AdminIdentity:
    """Represents the authenticated admin making the request."""

    api_key: str
    roles: Set[str]
    subject: str | None = None


def _parse_roles(raw: str | None) -> Set[str]:
    roles: Set[str] = set()
    if not raw:
        return roles
    raw = raw.strip()
    if not raw:
        return roles
    # Accept JSON encoded lists as well as comma separated values.
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, (list, tuple)):
                for value in parsed:
                    if isinstance(value, str) and value.strip():
                        roles.add(value.strip().lower())
        except json.JSONDecodeError:
            # Fall back to comma-separated parsing below.
            pass
    if not roles:
        for part in raw.split(","):
            part = part.strip()
            if part:
                roles.add(part.lower())
    return roles


async def require_api_key(
    x_api_key: str = Header(None),
    x_admin_roles: str = Header(default=""),
    x_admin_user: str | None = Header(default=None),
) -> AdminIdentity:
    if x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return AdminIdentity(api_key=x_api_key, roles=_parse_roles(x_admin_roles), subject=x_admin_user)


def require_roles(*required_roles: str):
    required = {r.lower() for r in required_roles if r}

    async def _checker(identity: AdminIdentity = Depends(require_api_key)) -> AdminIdentity:
        if not required:
            return identity
        if "admin" in identity.roles:
            return identity
        missing = [r for r in required if r not in identity.roles]
        if missing:
            raise HTTPException(status_code=403, detail="Missing required roles: " + ", ".join(sorted(required)))
        return identity

    return _checker
