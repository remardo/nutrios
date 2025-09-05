import os, httpx
from typing import Dict, Any

ADMIN_API_BASE = os.getenv("ADMIN_API_BASE", "http://localhost:8000")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "supersecret")

def ingest_meal(payload: Dict[str, Any]) -> None:
    """
    payload включает:
      telegram_user_id, telegram_username, captured_at_iso,
      title, portion_g, confidence, kcal, protein_g, fat_g, carbs_g,
      flags, micronutrients(list[str]), assumptions(list[str]),
      source_type ('image'|'text'), image_path(optional), message_id
    """
    with httpx.Client(timeout=10.0) as client:
        client.post(f"{ADMIN_API_BASE}/ingest/meal",
                    headers={"x-api-key": ADMIN_API_KEY},
                    json=payload)
