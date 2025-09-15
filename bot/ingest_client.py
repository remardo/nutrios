import logging
import httpx
from typing import Dict, Any
from config import ADMIN_API_BASE, ADMIN_API_KEY

def ingest_meal(payload: Dict[str, Any]) -> None:
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
                headers={"x-api-key": ADMIN_API_KEY},
                json=payload,
            )
            if r.status_code >= 400:
                logging.getLogger(__name__).warning(
                    "Admin ingest failed: %s %s", r.status_code, r.text[:200]
                )
        except Exception as e:
            logging.getLogger(__name__).warning("Admin ingest error: %s", e)
