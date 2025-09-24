import logging
from datetime import datetime, timezone, date
from typing import Dict, Any, Optional

import httpx

from config import ADMIN_API_BASE, ADMIN_API_KEY

_CLIENT_ID_CACHE: Dict[int, int] = {}


def _auth_headers() -> Dict[str, str]:
    return {"x-api-key": ADMIN_API_KEY} if ADMIN_API_KEY else {}


def ingest_meal(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    payload включает:
      telegram_user_id, telegram_username, captured_at_iso,
      title, portion_g, confidence, kcal, protein_g, fat_g, carbs_g,
      flags, micronutrients(list[str]), assumptions(list[str]), extras(dict),
      source_type ('image'|'text'), image_path(optional), message_id
    """
    with httpx.Client(timeout=10.0) as client:
        try:
            r = client.post(
                f"{ADMIN_API_BASE}/ingest/meal",
                headers=_auth_headers(),
                json=payload,
            )
            if r.status_code >= 400:
                logging.getLogger(__name__).warning(
                    "Admin ingest failed: %s %s", r.status_code, r.text[:200]
                )
                return None
            data = r.json() if r.content else None
            if isinstance(data, dict):
                cid = data.get("client_id")
                uid = payload.get("telegram_user_id")
                if cid and uid:
                    _CLIENT_ID_CACHE[int(uid)] = int(cid)
            return data
        except Exception as e:
            logging.getLogger(__name__).warning("Admin ingest error: %s", e)
            return None


def _get_client_id(telegram_user_id: int) -> Optional[int]:
    if telegram_user_id in _CLIENT_ID_CACHE:
        return _CLIENT_ID_CACHE[telegram_user_id]
    with httpx.Client(timeout=10.0) as client:
        try:
            r = client.get(
                f"{ADMIN_API_BASE}/clients",
                headers=_auth_headers(),
            )
            if r.status_code >= 400:
                logging.getLogger(__name__).warning(
                    "Admin client lookup failed: %s %s", r.status_code, r.text[:200]
                )
                return None
            for row in r.json() or []:
                if row.get("telegram_user_id") == telegram_user_id:
                    cid = row.get("id")
                    if cid:
                        _CLIENT_ID_CACHE[telegram_user_id] = int(cid)
                        return int(cid)
        except Exception as e:
            logging.getLogger(__name__).warning("Admin client lookup error: %s", e)
    return None


def _ensure_iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return datetime.now(timezone.utc).date().isoformat()


def _ensure_iso_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return datetime.now(timezone.utc).isoformat()


def upsert_daily_metrics_for_user(telegram_user_id: int, metrics: Dict[str, Any]) -> bool:
    client_id = _get_client_id(telegram_user_id)
    if not client_id:
        logging.getLogger(__name__).warning("No client id for telegram_user_id=%s", telegram_user_id)
        return False
    payload = dict(metrics)
    payload["date"] = _ensure_iso_date(payload.get("date"))
    with httpx.Client(timeout=10.0) as client:
        try:
            r = client.put(
                f"{ADMIN_API_BASE}/clients/{client_id}/metrics/daily",
                headers=_auth_headers(),
                json=payload,
            )
            if r.status_code >= 400:
                logging.getLogger(__name__).warning(
                    "Admin metrics upsert failed: %s %s", r.status_code, r.text[:200]
                )
                return False
            return True
        except Exception as e:
            logging.getLogger(__name__).warning("Admin metrics upsert error: %s", e)
            return False


def post_event_for_user(telegram_user_id: int, event: Dict[str, Any]) -> bool:
    client_id = _get_client_id(telegram_user_id)
    if not client_id:
        logging.getLogger(__name__).warning("No client id for event telegram_user_id=%s", telegram_user_id)
        return False
    payload = dict(event)
    if "type" not in payload:
        logging.getLogger(__name__).warning("Event payload missing type: %s", payload)
        return False
    if "date" in payload:
        payload["date"] = _ensure_iso_date(payload.get("date"))
    else:
        payload["date"] = datetime.now(timezone.utc).date().isoformat()
    if "occurred_at" in payload:
        payload["occurred_at"] = _ensure_iso_datetime(payload.get("occurred_at"))
    with httpx.Client(timeout=10.0) as client:
        try:
            r = client.post(
                f"{ADMIN_API_BASE}/clients/{client_id}/events",
                headers=_auth_headers(),
                json=payload,
            )
            if r.status_code >= 400:
                logging.getLogger(__name__).warning(
                    "Admin event post failed: %s %s", r.status_code, r.text[:200]
                )
                return False
            return True
        except Exception as e:
            logging.getLogger(__name__).warning("Admin event post error: %s", e)
            return False
