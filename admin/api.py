import os, hmac, hashlib, json, time
from math import isclose, isfinite
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from .ab_service import ABFlagService, ABFlagServiceError, get_ab_service
from .analysis import df_meals, micro_top, summary_extras, summary_macros
from .auth import AdminIdentity, require_api_key, require_roles
from .db import SessionLocal, ensure_meals_extras_column, ensure_clients_assigned_to_column, init_db
from .models import (
    Base,
    Client,
    ClientTargets,
    Experiment,
    ExperimentRevision,
    ExperimentVariant,
    Meal,
)

app = FastAPI(title="Nutrios Admin API")


EXPERIMENT_STATUS_DRAFT = "draft"
EXPERIMENT_STATUS_RUNNING = "running"
EXPERIMENT_STATUS_PAUSED = "paused"

ROLE_EXPERIMENT_WRITE = "experiments:write"
ROLE_EXPERIMENT_PUBLISH = "experiments:publish"

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# init
# Load .env from project root to ensure env vars like ALLOW_DEBUG_WEBAPP are available
try:
    root = Path(__file__).resolve().parents[1]
    env_path = root / '.env'
    if env_path.exists():
        load_dotenv(env_path, override=True)
except Exception:
    pass
init_db(Base)
ensure_meals_extras_column()
ensure_clients_assigned_to_column()
# NOTE: We will serve SPA at root from a separate root app, see bottom of file.


def _parse_allowed_origins(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


DEFAULT_ALLOWED_ORIGINS = ["http://localhost:19006"]
ALLOWED_ORIGINS = _parse_allowed_origins(os.getenv("ADMIN_ALLOWED_ORIGINS")) or DEFAULT_ALLOWED_ORIGINS
MEDIA_TOKEN_SECRET = os.getenv("MEDIA_TOKEN_SECRET")
if not MEDIA_TOKEN_SECRET:
    raise RuntimeError("MEDIA_TOKEN_SECRET must be configured")
MEDIA_TOKEN_TTL_SECONDS = int(os.getenv("MEDIA_TOKEN_TTL_SECONDS", "3600"))


def _media_signature(path: str, expires: int) -> str:
    payload = f"{path}:{expires}".encode()
    return hmac.new(MEDIA_TOKEN_SECRET.encode(), msg=payload, digestmod=hashlib.sha256).hexdigest()


def _is_within_directory(base: Path, target: Path) -> bool:
    try:
        base_resolved = base.resolve()
        target_resolved = target.resolve()
        return str(target_resolved).startswith(str(base_resolved))
    except Exception:
        return False


def _connect_src_value() -> str:
    entries = ["'self'"] + ALLOWED_ORIGINS
    return " ".join(entries)


def _media_relative_path(raw: str | None) -> str | None:
    if not raw:
        return None
    path = raw.split("?", 1)[0]
    if path.startswith("/media/"):
        path = path[len("/media/"):]
    return path.lstrip("/")


def _build_media_url(relative_path: str | None) -> str | None:
    if not relative_path:
        return None
    expires = int(time.time()) + MEDIA_TOKEN_TTL_SECONDS
    token = _media_signature(relative_path, expires)
    return f"/media/{relative_path}?expires={expires}&token={token}"


def _signed_media_path(raw: str | None) -> str | None:
    return _build_media_url(_media_relative_path(raw))

# -----------------------------
# BFF (Backend-For-Frontend) for NutriTracker-Pro web
# Provides thin-compatible endpoints mapped to our domain API so that
# the exported SPA can work without changing its network layer.

def _resolve_client_id(
    *,
    db: Session,
    cid: int | None = None,
    tg: int | None = None,
) -> int:
    """Resolve a client id, optionally auto-provisioning by telegram id.

    Priority: explicit cid > telegram id (tg). If neither provided, raise 401.
    """
    if cid:
        row = db.query(Client).filter_by(id=cid).first()
        if not row:
            raise HTTPException(status_code=404, detail="Client not found")
        return row.id
    if tg:
        row = db.query(Client).filter_by(telegram_user_id=tg).first()
        if not row:
            row = Client(telegram_user_id=tg)
            db.add(row)
            db.commit()
            db.refresh(row)
        return row.id
    raise HTTPException(status_code=401, detail="cid or tg is required")


@app.get("/ntp/user")
def ntp_user(
    cid: int | None = None,
    tg: int | None = None,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> dict:
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)
    row = db.query(Client).filter_by(id=client_id).first()
    return {
        "id": row.id,
        "telegram_user_id": row.telegram_user_id,
        "username": row.telegram_username,
    }


@app.get("/ntp/goals")
def ntp_goals(
    cid: int | None = None,
    tg: int | None = None,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> dict:
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)
    data = get_targets(client_id, db)
    try:
        if not data.get("profile"):
            data["profile"] = {"status": "seeded", "completed": True}
    except Exception:
        pass
    return data


class NTPGoals(BaseModel):
    # Accept both our names and generic ones from a foreign UI
    kcal: int | None = None
    protein_g: int | None = None
    fat_g: int | None = None
    carbs_g: int | None = None
    kcal_target: int | None = None
    protein_target_g: int | None = None
    fat_target_g: int | None = None
    carbs_target_g: int | None = None


@app.put("/ntp/goals")
def ntp_put_goals(
    payload: NTPGoals,
    cid: int | None = None,
    tg: int | None = None,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> dict:
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)
    # Normalise incoming fields
    body = {
        "kcal_target": payload.kcal_target or payload.kcal or 2000,
        "protein_target_g": payload.protein_target_g or payload.protein_g or 100,
        "fat_target_g": payload.fat_target_g or payload.fat_g or 70,
        "carbs_target_g": payload.carbs_target_g or payload.carbs_g or 250,
    }
    return put_targets(client_id, Targets(**body), db)


@app.get("/ntp/progress/daily")
def ntp_progress_daily(
    cid: int | None = None,
    tg: int | None = None,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> list[dict[str, Any]]:
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)
    return daily_summary(client_id, db)


@app.get("/ntp/progress/weekly")
def ntp_progress_weekly(
    cid: int | None = None,
    tg: int | None = None,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> list[dict[str, Any]]:
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)
    return weekly_summary(client_id, db)


@app.get("/ntp/streak")
def ntp_streak(
    cid: int | None = None,
    tg: int | None = None,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> dict:
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)
    return get_streak(client_id, db)  # type: ignore[name-defined]


@app.get("/ntp/tips")
def ntp_tips(
    cid: int | None = None,
    tg: int | None = None,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> dict:
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)
    return tips_today(client_id, db)


# -----------------------------
# Meals (create/list/update/delete) for NutriTracker-Pro

class NTPMeal(BaseModel):
    # Flexible field names to accept various clients
    title: str | None = None
    portion_g: int | None = None
    kcal: int | None = None
    calories: int | None = None
    protein_g: int | None = None
    protein: int | None = None
    fat_g: int | None = None
    fat: int | None = None
    carbs_g: int | None = None
    carbs: int | None = None
    captured_at_iso: str | None = None  # ISO string
    captured_at: str | None = None      # alternative name
    source_type: str | None = None
    image_url: str | None = None
    image_path: str | None = None  # allow alternate field name from clients
    micronutrients: list[str] | None = None
    flags: dict | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


@app.post("/ntp/meal")
def ntp_add_meal(
    payload: NTPMeal,
    cid: int | None = None,
    tg: int | None = None,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> dict:
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)
    title = payload.title or "Meal"
    portion_g = _coerce_int(payload.portion_g)
    kcal = _coerce_int(payload.kcal, _coerce_int(payload.calories, 0)) or 0
    protein_g = _coerce_int(payload.protein_g, _coerce_int(payload.protein, 0)) or 0
    fat_g = _coerce_int(payload.fat_g, _coerce_int(payload.fat, 0)) or 0
    carbs_g = _coerce_int(payload.carbs_g, _coerce_int(payload.carbs, 0)) or 0

    ts_str = payload.captured_at_iso or payload.captured_at or _now_iso()
    try:
        captured_dt = datetime.fromisoformat(ts_str)
    except Exception:
        captured_dt = datetime.now(timezone.utc)

    # Create Meal row
    # Choose image url/path from either field
    image_url_val = payload.image_url or payload.image_path
    row = Meal(
        client_id=client_id,
        message_id=int(datetime.now(timezone.utc).timestamp() * 1000),
        captured_at=captured_dt,
        title=title,
        portion_g=portion_g or 0,
        kcal=kcal,
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
        flags=payload.flags or {},
        micronutrients=payload.micronutrients or [],
        assumptions=[],
        extras=None,
        source_type=payload.source_type or "web",
        image_path=_media_relative_path(image_url_val),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    signed_media = _signed_media_path(row.image_path)
    return {"ok": True, "meal": {
        "id": row.id,
        "captured_at": row.captured_at.isoformat(),
        "title": row.title,
        "portion_g": row.portion_g,
        "kcal": row.kcal,
        "protein_g": row.protein_g,
        "fat_g": row.fat_g,
        "carbs_g": row.carbs_g,
        "image_path": signed_media,
        "photo": signed_media,
    }}


@app.get("/ntp/meals")
def ntp_list_meals(
    cid: int | None = None,
    tg: int | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> list[dict[str, Any]]:
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)
    q = db.query(Meal).filter(Meal.client_id == client_id).order_by(Meal.captured_at.desc())
    if limit:
        q = q.limit(int(limit))
    rows = q.all()
    result = []
    for r in rows:
        signed_media = _signed_media_path(r.image_path)
        result.append({
            "id": r.id,
            "captured_at": r.captured_at.isoformat(),
            "title": r.title,
            "portion_g": r.portion_g,
            "kcal": r.kcal,
            "protein_g": r.protein_g,
            "fat_g": r.fat_g,
            "carbs_g": r.carbs_g,
            "image_path": signed_media,
            "photo": signed_media,
            "source_type": r.source_type,
        })
    return result


class NTPMealPatch(BaseModel):
    title: str | None = None
    portion_g: int | None = None
    kcal: int | None = None
    protein_g: int | None = None
    fat_g: int | None = None
    carbs_g: int | None = None
    image_url: str | None = None
    image_path: str | None = None


@app.put("/ntp/meals/{meal_id}")
def ntp_update_meal(
    meal_id: int,
    payload: NTPMealPatch,
    cid: int | None = None,
    tg: int | None = None,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> dict:
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)
    meal = db.query(Meal).filter(Meal.id == meal_id, Meal.client_id == client_id).first()
    if not meal:
        raise HTTPException(status_code=404, detail="meal not found")
    for field in ["title", "portion_g", "kcal", "protein_g", "fat_g", "carbs_g"]:
        value = getattr(payload, field)
        if value is not None:
            setattr(meal, field, value)
    if payload.image_url is not None or payload.image_path is not None:
        meal.image_path = _media_relative_path(payload.image_url or payload.image_path)
    db.commit()
    db.refresh(meal)
    return {"ok": True}


@app.get("/ntp/day/quality")
def ntp_day_quality(
    cid: int | None = None,
    tg: int | None = None,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> dict:
    """Return lightweight nutrition quality summary for the current day."""
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)

    today = date.today()
    df = df_meals(db, client_id)
    macros_df = summary_macros(df, freq="D")
    targets = get_targets(client_id, db)

    kcal = protein_g = fat_g = carbs_g = 0.0
    protein_pct = fat_pct = carbs_pct = None

    if isinstance(macros_df, pd.DataFrame) and not macros_df.empty:
        macros_df = macros_df.copy()
        macros_df["captured_at"] = pd.to_datetime(macros_df["captured_at"], errors="coerce")
        row = macros_df[macros_df["captured_at"].dt.date == today]
        if row.empty:
            row = macros_df.tail(1)
        rec = row.iloc[0]
        kcal = _to_float(rec.get("kcal")) or 0.0
        protein_g = _to_float(rec.get("protein_g")) or 0.0
        fat_g = _to_float(rec.get("fat_g")) or 0.0
        carbs_g = _to_float(rec.get("carbs_g")) or 0.0
        protein_pct = _percent(protein_g * 4.0, targets.get("protein_target_g"))
        fat_pct = _percent(fat_g * 9.0, targets.get("fat_target_g"))
        carbs_pct = _percent(carbs_g * 4.0, targets.get("carbs_target_g"))

    extras_df = summary_extras(df, freq="D")
    fats_saturated_pct = fiber_total = omega_ratio = None
    crucifers = heme_cnt = nonheme_cnt = antiox = 0

    if isinstance(extras_df, pd.DataFrame) and not extras_df.empty:
        extras_df = extras_df.copy()
        extras_df["captured_at"] = pd.to_datetime(extras_df["captured_at"], errors="coerce")
        row = extras_df[extras_df["captured_at"].dt.date == today]
        if row.empty:
            row = extras_df.tail(1)
        rec = row.iloc[0]
        fats_saturated = _to_float(rec.get("fats_saturated"))
        kcal_for_pct = kcal if kcal > 0 else _to_float(rec.get("kcal")) or kcal
        if fats_saturated is not None and kcal_for_pct:
            fats_saturated_pct = round((fats_saturated * 9.0) / float(kcal_for_pct) * 100.0, 1)
        fiber_total = _to_float(rec.get("fiber_total"))
        omega_ratio = _to_float(rec.get("omega_ratio_num"))

    if not df.empty:
        df_all = df.copy()
        df_all["captured_at"] = pd.to_datetime(df_all["captured_at"], errors="coerce")
        today_meals = df_all[df_all["captured_at"].dt.date == today]
        crucifer_kw = {"брокколи", "цветная капуста", "капуста", "brussels", "kale"}
        heme_kw = {"говядина", "телятина", "печень", "рыба", "beef", "liver", "salmon"}
        for _, meal in today_meals.iterrows():
            title = str(meal.get("title") or "").lower()
            if any(word in title for word in crucifer_kw):
                crucifers += 1
            micros = [str(x).lower() for x in (meal.get("micronutrients") or [])]
            antiox += sum(1 for x in micros if "антиокс" in x or "antiox" in x)
            if any("железо" in x or "iron" in x for x in micros):
                if any(word in title for word in heme_kw):
                    heme_cnt += 1
                else:
                    nonheme_cnt += 1

    return {
        "kcal": round(kcal, 1),
        "protein_g": round(protein_g, 1),
        "fat_g": round(fat_g, 1),
        "carbs_g": round(carbs_g, 1),
        "protein_pct": protein_pct,
        "fat_pct": fat_pct,
        "carbs_pct": carbs_pct,
        "fats_saturated_pct": fats_saturated_pct,
        "fiber_total": fiber_total,
        "omega_ratio": omega_ratio,
        "crucifer_meals": crucifers,
        "heme_iron_meals": heme_cnt,
        "nonheme_iron_meals": nonheme_cnt,
        "antioxidants_mentions": antiox,
    }


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        result = float(value)
        return result if isfinite(result) else None
    except Exception:
        return None


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        return int(float(value))
    except Exception:
        return None


def _to_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    try:
        if pd.isna(value):
            return []
    except Exception:
        pass
    return [value]


def _percent(value: Optional[float], target: Optional[float]) -> Optional[float]:
    try:
        if value is None or target is None or target <= 0:
            return None
        return round(float(value) / float(target) * 100.0, 1)
    except Exception:
        return None


def _prepare_summary_frame(df: Any) -> pd.DataFrame:
    if isinstance(df, pd.DataFrame) and not df.empty:
        renamed = df.rename(columns={"captured_at": "period_start"}).copy()
        renamed["period_start"] = pd.to_datetime(renamed["period_start"], errors="coerce")
        return renamed
    return pd.DataFrame()


def _select_period_row(df: pd.DataFrame, target_date: Optional[date] = None) -> Optional[pd.Series]:
    if df.empty:
        return None
    ordered = df.sort_values("period_start")
    if target_date is not None:
        mask = ordered["period_start"].dt.date == target_date
        if mask.any():
            return ordered.loc[mask].iloc[-1]
    return ordered.iloc[-1]


def _format_macro_block(row: Optional[pd.Series], targets: dict[str, Any]) -> dict[str, Any]:
    if row is None:
        return {
            "date": None,
            "kcal": 0.0,
            "protein_g": 0.0,
            "fat_g": 0.0,
            "carbs_g": 0.0,
            "protein_pct": None,
            "fat_pct": None,
            "carbs_pct": None,
        }
    kcal = _to_float(row.get("kcal")) or 0.0
    protein = _to_float(row.get("protein_g")) or 0.0
    fat = _to_float(row.get("fat_g")) or 0.0
    carbs = _to_float(row.get("carbs_g")) or 0.0
    return {
        "date": row.get("period_start").date().isoformat() if row.get("period_start") is not None else None,
        "kcal": kcal,
        "protein_g": protein,
        "fat_g": fat,
        "carbs_g": carbs,
        "protein_pct": _percent(protein * 4.0, targets.get("protein_target_g")),
        "fat_pct": _percent(fat * 9.0, targets.get("fat_target_g")),
        "carbs_pct": _percent(carbs * 4.0, targets.get("carbs_target_g")),
    }


def _saturated_percent(extras_row: pd.Series, kcal: float) -> Optional[float]:
    if kcal <= 0:
        return None
    sat = _to_float(extras_row.get("fats_saturated"))
    if sat is None:
        return None
    return round((sat * 9.0) / kcal * 100.0, 1)


def build_dashboard_payload(client_id: int, db: Session) -> dict[str, Any]:
    df = df_meals(db, client_id)
    targets = get_targets(client_id, db)
    if not targets:
        targets = {
            "kcal_target": 2000,
            "protein_target_g": 100,
            "fat_target_g": 70,
            "carbs_target_g": 250,
        }

    daily_df = _prepare_summary_frame(summary_macros(df, freq="D"))
    weekly_df = _prepare_summary_frame(summary_macros(df, freq="W"))
    extras_df = _prepare_summary_frame(summary_extras(df, freq="D"))

    today = date.today()
    daily_row = _select_period_row(daily_df, today)
    weekly_row = _select_period_row(weekly_df)
    extras_row = _select_period_row(extras_df, today)

    daily_block = _format_macro_block(daily_row, targets)
    weekly_block = _format_macro_block(weekly_row, targets)

    if extras_row is not None:
        daily_block.update(
            {
                "fats_saturated_pct": _saturated_percent(extras_row, daily_block["kcal"]),
                "fiber_total": _to_float(extras_row.get("fiber_total")),
                "omega_ratio": _to_float(extras_row.get("omega_ratio_num")),
            }
        )
    else:
        daily_block.update({"fats_saturated_pct": None, "fiber_total": None, "omega_ratio": None})

    daily_trend = [
        {
            "period": row["period_start"].date().isoformat(),
            "kcal": _to_float(row.get("kcal")) or 0.0,
            "protein_g": _to_float(row.get("protein_g")) or 0.0,
            "fat_g": _to_float(row.get("fat_g")) or 0.0,
            "carbs_g": _to_float(row.get("carbs_g")) or 0.0,
        }
        for _, row in daily_df.iterrows()
    ]

    weekly_trend = [
        {
            "period": row["period_start"].date().isoformat(),
            "kcal": _to_float(row.get("kcal")) or 0.0,
            "protein_g": _to_float(row.get("protein_g")) or 0.0,
            "fat_g": _to_float(row.get("fat_g")) or 0.0,
            "carbs_g": _to_float(row.get("carbs_g")) or 0.0,
        }
        for _, row in weekly_df.iterrows()
    ]

    recent_meals: list[dict[str, Any]] = []
    if not df.empty:
        recent = df.sort_values("captured_at", ascending=False).head(50)
        for _, row in recent.iterrows():
            captured = row.get("captured_at")
            recent_meals.append(
                {
                    "id": int(row.get("meal_id")) if row.get("meal_id") is not None else None,
                    "captured_at": captured.isoformat() if pd.notna(captured) else None,
                    "title": row.get("title"),
                    "portion_g": _to_float(row.get("portion_g")),
                    "confidence": _to_int(row.get("confidence")),
                    "kcal": _to_float(row.get("kcal")),
                    "protein_g": _to_float(row.get("protein_g")),
                    "fat_g": _to_float(row.get("fat_g")),
                    "carbs_g": _to_float(row.get("carbs_g")),
                    "image_path": row.get("image_path"),
                    "source_type": row.get("source_type"),
                    "flags": row.get("flags") or {},
                    "micronutrients": _to_list(row.get("micronutrients")),
                    "assumptions": _to_list(row.get("assumptions")),
                    "special_groups": row.get("special_groups") or {},
                }
            )

    streak_data = compliance_streak(client_id, db)
    tips_data = tips_today(client_id, db)

    return {
        "targets": targets,
        "daily": daily_block,
        "weekly": weekly_block,
        "daily_trend": daily_trend,
        "weekly_trend": weekly_trend,
        "meals": recent_meals,
        "streak": streak_data,
        "tips": tips_data.get("tips", []) if isinstance(tips_data, dict) else [],
    }


@app.get("/clients/{client_id}/dashboard")
def client_dashboard(
    client_id: int,
    db: Session = Depends(get_db),
    identity: AdminIdentity = Depends(require_api_key),
) -> dict:
    if "nutritionist" in identity.roles and identity.subject:
        c = db.query(Client).filter_by(id=client_id).first()
        if not c:
            raise HTTPException(status_code=404, detail="client not found")
        if (c.assigned_to or None) != identity.subject:
            raise HTTPException(status_code=403, detail="forbidden")
    return build_dashboard_payload(client_id, db)

class AssignClientPayload(BaseModel):
    assigned_to: Optional[str] = None

@app.put("/clients/{client_id}/assign", dependencies=[Depends(require_roles("admin"))])
def assign_client(
    client_id: int,
    payload: AssignClientPayload,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> dict:
    c = db.query(Client).filter_by(id=client_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="client not found")
    value = (payload.assigned_to or "").strip()
    c.assigned_to = value or None
    db.commit()
    return {"ok": True, "client_id": c.id, "assigned_to": c.assigned_to}
@app.delete("/ntp/meals/{meal_id}")
def ntp_delete_meal(
    meal_id: int,
    cid: int | None = None,
    tg: int | None = None,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> dict:
    client_id = _resolve_client_id(db=db, cid=cid, tg=tg)
    meal = db.query(Meal).filter(Meal.id == meal_id, Meal.client_id == client_id).first()
    if not meal:
        raise HTTPException(status_code=404, detail="meal not found")
    db.delete(meal)
    db.commit()
    return {"ok": True}

# ----- Schemas -----
class IngestMeal(BaseModel):
    telegram_user_id: int
    telegram_username: Optional[str] = None
    captured_at_iso: str
    title: str
    portion_g: int
    confidence: int
    kcal: int
    protein_g: Optional[int] = None
    fat_g: Optional[int] = None
    carbs_g: Optional[int] = None
    flags: dict
    micronutrients: List[str] = []
    assumptions: List[str] = []
    extras: Optional[dict] = None  # {fats: {total,saturated,mono,poly,trans,omega6,omega3}, fiber: {total,soluble,insoluble}, omega_ratio: "6:3"}
    source_type: str
    image_path: Optional[str] = None
    message_id: int

# ----- Ingest -----
@app.post("/ingest/meal")
def ingest_meal_api(payload: IngestMeal, db: Session = Depends(get_db), _=Depends(require_api_key)):
    client = db.query(Client).filter_by(telegram_user_id=payload.telegram_user_id).first()
    if not client:
        client = Client(telegram_user_id=payload.telegram_user_id, telegram_username=payload.telegram_username)
        db.add(client); db.flush()
    # upsert by (client_id, message_id)
    meal = db.query(Meal).filter_by(client_id=client.id, message_id=payload.message_id).first()
    fields = payload.dict()
    captured_at = datetime.fromisoformat(fields.pop("captured_at_iso"))
    fields.pop("message_id", None)  # Remove message_id to avoid duplicate
    fields.pop("telegram_user_id", None)  # Remove client fields
    fields.pop("telegram_username", None)
    fields["image_path"] = _media_relative_path(fields.get("image_path"))
    if not meal:
        meal = Meal(client_id=client.id, message_id=payload.message_id, captured_at=captured_at, **fields)
        db.add(meal)
    else:
        for k, v in fields.items():
            setattr(meal, k, v)
        meal.captured_at = captured_at
    db.commit()
    return {"ok": True, "meal_id": meal.id, "client_id": client.id}

# ----- Lists -----
@app.get("/clients")
def list_clients(
    db: Session = Depends(get_db),
    identity: AdminIdentity = Depends(require_api_key),
    assigned: Optional[str] = Query(default=None),
):
    q = db.query(Client)
    if "nutritionist" in identity.roles and identity.subject:
        q = q.filter(Client.assigned_to == identity.subject)
    else:
        if assigned == "unassigned":
            q = q.filter((Client.assigned_to.is_(None)) | (Client.assigned_to == ""))
        elif assigned == "mine" and identity.subject:
            q = q.filter(Client.assigned_to == identity.subject)
        elif assigned:
            q = q.filter(Client.assigned_to == assigned)
    rows = q.order_by(Client.created_at.desc()).all()
    return [{
        "id": r.id,
        "telegram_user_id": r.telegram_user_id,
        "telegram_username": r.telegram_username,
        "assigned_to": r.assigned_to,
    } for r in rows]


@app.get("/client/by_telegram/{telegram_user_id}")
def client_by_telegram(
    telegram_user_id: int,
    db: Session = Depends(get_db),
    X_Telegram_Init_Data: str | None = Header(default=None),
    request: Request = None,
    _: AdminIdentity = Depends(require_api_key),
):
    # Verify Telegram initData (production). Optional local debug is allowed only when ALLOW_DEBUG_WEBAPP is explicitly enabled.
    def _ok(uid):
        return uid == telegram_user_id
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    allow_debug = os.getenv("ALLOW_DEBUG_WEBAPP", "0").lower() in {"1","true","yes"}
    if X_Telegram_Init_Data and bot_token:
        try:
            parts = dict(item.split('=',1) for item in X_Telegram_Init_Data.split('&'))
            data_json = parts.get('user'); hash_recv = parts.get('hash')
            if data_json and hash_recv:
                secret = hashlib.sha256(bot_token.encode()).digest()
                check_string = '\n'.join(sorted([f"{k}={v}" for k,v in parts.items() if k != 'hash']))
                h = hmac.new(secret, msg=check_string.encode(), digestmod=hashlib.sha256).hexdigest()
                if h == hash_recv:
                    user = json.loads(data_json) if data_json else {}
                    uid = int(user.get('id')) if user and 'id' in user else None
                    if uid and _ok(uid):
                        pass
                    else:
                        raise HTTPException(status_code=403, detail="Forbidden")
                else:
                    raise HTTPException(status_code=401, detail="Invalid Telegram init data")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid Telegram init data")
    elif allow_debug and request is not None and 'tg' in dict(request.query_params):
        uid = int(dict(request.query_params)['tg'])
        if not _ok(uid):
            raise HTTPException(status_code=403, detail="Forbidden")
    else:
        raise HTTPException(status_code=401, detail="Missing Telegram auth")
    row = db.query(Client).filter_by(telegram_user_id=telegram_user_id).first()
    if not row:
        # Auto-provision client on first authorised access to simplify onboarding for miniapp
        row = Client(telegram_user_id=telegram_user_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return {"id": row.id, "telegram_user_id": row.telegram_user_id, "telegram_username": row.telegram_username}

@app.get("/clients/{client_id}/meals")
def list_meals(
    client_id: int,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
):
    rows = db.query(Meal).filter(Meal.client_id==client_id).order_by(Meal.captured_at.desc()).all()
    return [{
        "id": r.id, "captured_at": r.captured_at.isoformat(), "title": r.title, "portion_g": r.portion_g,
        "kcal": r.kcal, "protein_g": r.protein_g, "fat_g": r.fat_g, "carbs_g": r.carbs_g,
        "flags": r.flags, "micronutrients": r.micronutrients, "assumptions": r.assumptions,
        "extras": r.extras,
        "image_path": _signed_media_path(r.image_path), "source_type": r.source_type, "message_id": r.message_id
    } for r in rows]


# ----- Targets / Questionnaire / Progress -----
class Targets(BaseModel):
    kcal_target: int
    protein_target_g: int
    fat_target_g: int
    carbs_target_g: int
    profile: Optional[dict] = None
    plan: Optional[dict] = None
    tolerances: Optional[dict] = None
    notifications: Optional[dict] = None


@app.get("/clients/{client_id}/targets")
def get_targets(
    client_id: int,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
):
    t = db.query(ClientTargets).filter_by(client_id=client_id).first()
    if not t:
        # defaults
        return {
            "kcal_target": 2000,
            "protein_target_g": 100,
            "fat_target_g": 70,
            "carbs_target_g": 250,
            "profile": None,
            "plan": None,
            "tolerances": {"kcal_pct": 0.10, "protein_pct": 0.20, "fat_pct": 0.20, "carbs_pct": 0.20, "min_g": {"p":10, "f":10, "c":15}},
            "notifications": {"reminders": False, "time": "08:00", "tips": True},
        }
    return {
        "kcal_target": t.kcal_target,
        "protein_target_g": t.protein_target_g,
        "fat_target_g": t.fat_target_g,
        "carbs_target_g": t.carbs_target_g,
        "profile": t.profile,
        "plan": t.plan,
        "tolerances": t.tolerances or {"kcal_pct": 0.10, "protein_pct": 0.20, "fat_pct": 0.20, "carbs_pct": 0.20, "min_g": {"p":10, "f":10, "c":15}},
        "notifications": t.notifications or {"reminders": False, "time": "08:00", "tips": True},
    }


@app.put("/clients/{client_id}/targets")
def put_targets(
    client_id: int,
    payload: Targets,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
):
    t = db.query(ClientTargets).filter_by(client_id=client_id).first()
    if not t:
        t = ClientTargets(client_id=client_id)
        db.add(t)
    t.kcal_target = payload.kcal_target
    t.protein_target_g = payload.protein_target_g
    t.fat_target_g = payload.fat_target_g
    t.carbs_target_g = payload.carbs_target_g
    if payload.profile is not None:
        t.profile = payload.profile
    if payload.plan is not None:
        t.plan = payload.plan
    if payload.tolerances is not None:
        t.tolerances = payload.tolerances
    if payload.notifications is not None:
        t.notifications = payload.notifications
    db.commit()
    return {"ok": True}


class Questionnaire(BaseModel):
    age: Optional[int] = None
    sex: Optional[str] = None  # m|f
    height_cm: Optional[int] = None
    weight_kg: Optional[float] = None
    activity: Optional[str] = None  # low|medium|high
    goal: Optional[str] = None  # lose|maintain|gain


@app.post("/clients/{client_id}/questionnaire")
def post_questionnaire(client_id: int, payload: Questionnaire, db: Session = Depends(get_db)):
    # baseline estimation (Mifflin-St Jeor + activity multiplier)
    # Mifflin-St Jeor basal metabolic rate approximation
    def est_bmr():
        if not payload.weight_kg or not payload.height_cm or not payload.age:
            return 1500
        sex_k = 5 if (payload.sex or "m").lower().startswith("m") else -161
        return int(10 * float(payload.weight_kg) + 6.25 * float(payload.height_cm) - 5 * int(payload.age) + sex_k)

    activity_map = {"low": 1.2, "medium": 1.4, "high": 1.6}
    bmr = est_bmr()
    tdee = int(bmr * activity_map.get((payload.activity or "medium"), 1.4))
    goal = (payload.goal or "maintain").lower()
    if goal == "lose":
        kcal = max(1200, int(tdee * 0.85))
    elif goal == "gain":
        kcal = int(tdee * 1.1)
    else:
        kcal = tdee
    # macros split: 30/30/40 (p/f/c) by kcal
    protein_g = int(round(kcal * 0.30 / 4))
    fat_g = int(round(kcal * 0.30 / 9))
    carbs_g = int(round(kcal * 0.40 / 4))

    t = db.query(ClientTargets).filter_by(client_id=client_id).first()
    if not t:
        t = ClientTargets(client_id=client_id)
        db.add(t)
    t.kcal_target = kcal
    t.protein_target_g = protein_g
    t.fat_target_g = fat_g
    t.carbs_target_g = carbs_g
    t.profile = payload.model_dump()
    t.plan = {
        "kcal": kcal,
        "protein_g": protein_g,
        "fat_g": fat_g,
        "carbs_g": carbs_g,
        "split": "30/30/40",
        "notes": "Автоматически рассчитано на основе анкеты",
    }
    db.commit()
    return {"ok": True, "targets": get_targets(client_id, db)}


def _progress_rows(df, targets):
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for _, r in df.iterrows():
        period = r["captured_at"].to_pydatetime() if hasattr(r["captured_at"], "to_pydatetime") else r["captured_at"]
        row = {
            "period_start": period.isoformat() if hasattr(period, "isoformat") else str(period),
            "kcal": _to_float(r.get("kcal")) or 0.0,
            "protein_g": _to_float(r.get("protein_g")) or 0.0,
            "fat_g": _to_float(r.get("fat_g")) or 0.0,
            "carbs_g": _to_float(r.get("carbs_g")) or 0.0,
        }
        if targets:
            def pct(v, t):
                try:
                    return round((float(v) / float(t)) * 100, 1) if t else None
                except Exception:
                    return None
            row["kcal_pct"] = pct(row["kcal"], targets["kcal_target"]) \
                if targets else None
            row["protein_pct"] = pct(row["protein_g"], targets["protein_target_g"]) \
                if targets else None
            row["fat_pct"] = pct(row["fat_g"], targets["fat_target_g"]) \
                if targets else None
            row["carbs_pct"] = pct(row["carbs_g"], targets["carbs_target_g"]) \
                if targets else None
        out.append(row)
    return out
@app.get("/clients/{client_id}/progress/daily")
def daily_progress(
    client_id: int,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> list[dict[str, Any]]:
    df = df_meals(db, client_id)
    agg = summary_macros(df, freq="D")
    targets = get_targets(client_id, db)
    return _progress_rows(agg, targets)


@app.get("/clients/{client_id}/progress/weekly")
def weekly_progress(
    client_id: int,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> list[dict[str, Any]]:
    df = df_meals(db, client_id)
    agg = summary_macros(df, freq="W")
    targets = get_targets(client_id, db)
    return _progress_rows(agg, targets)


@app.get("/clients/{client_id}/streak")
def compliance_streak(
    client_id: int,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
) -> dict[str, Any]:
    df = df_meals(db, client_id)
    agg = summary_macros(df, freq="D")
    if agg is None or getattr(agg, "empty", True):
        return {"streak": 0, "met_goal_7": False}
    targets = get_targets(client_id, db)

    def is_ok(row: pd.Series) -> bool:
        try:
            kcal_ok = abs(float(row["kcal"]) - targets["kcal_target"]) <= targets["kcal_target"] * 0.10
            protein_ok = abs(float(row["protein_g"]) - targets["protein_target_g"]) <= max(
                10.0, targets["protein_target_g"] * 0.20
            )
            fat_ok = abs(float(row["fat_g"]) - targets["fat_target_g"]) <= max(
                10.0, targets["fat_target_g"] * 0.20
            )
            carbs_ok = abs(float(row["carbs_g"]) - targets["carbs_target_g"]) <= max(
                15.0, targets["carbs_target_g"] * 0.20
            )
            return kcal_ok and protein_ok and fat_ok and carbs_ok
        except Exception:
            return False

    streak = 0
    for _, row in agg.sort_values("captured_at", ascending=False).iterrows():
        if is_ok(row):
            streak += 1
        else:
            break
    return {"streak": streak, "met_goal_7": streak >= 7}


# ----- Experiments (AB testing) -----


class VariantConfig(BaseModel):
    name: str = Field(..., min_length=1)
    weight: float = Field(..., ge=0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("variant name cannot be empty")
        return name


class ExperimentConfigPayload(BaseModel):
    rollout_percentage: float = Field(..., ge=0, le=100)
    variants: List[VariantConfig]

    @model_validator(mode="after")
    def _ensure_variants(cls, data):
        if not data.variants:
            raise ValueError("At least one variant must be provided")
        return data


def _normalize_variant_weights(variants: List[VariantConfig]) -> Dict[str, float]:
    normalized: Dict[str, float] = {}
    total = sum(float(v.weight) for v in variants)
    if total <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Variant weights must sum to a positive value",
        )
    if isclose(total, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        scale = 1.0
    elif isclose(total, 100.0, rel_tol=1e-4, abs_tol=1e-4):
        scale = 100.0
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Variant weights must sum to 1.0 or 100.0 (received {total:.4f})",
        )
    for variant in variants:
        name = variant.name
        if name in normalized:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Duplicate variant name '{name}'",
            )
        weight = float(variant.weight)
        normalized[name] = weight if scale == 1.0 else weight / 100.0
    if not any(weight > 0 for weight in normalized.values()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one variant must have a non-zero weight",
        )
    normalized_total = sum(normalized.values())
    if not isclose(normalized_total, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        normalized = {name: weight / normalized_total for name, weight in normalized.items()}
    return normalized


def _serialize_experiment(experiment: Experiment) -> Dict[str, object]:
    revisions = [rev.revision for rev in experiment.revisions] if experiment.revisions else []
    return {
        "id": experiment.id,
        "key": experiment.key,
        "description": experiment.description,
        "rollout_percentage": float(experiment.rollout_percentage or 0.0),
        "status": experiment.status,
        "variants": [
            {
                "id": variant.id,
                "name": variant.name,
                "weight": float(variant.weight or 0.0),
            }
            for variant in sorted(experiment.variants, key=lambda v: v.name)
        ],
        "created_at": experiment.created_at.isoformat() if experiment.created_at else None,
        "updated_at": experiment.updated_at.isoformat() if experiment.updated_at else None,
        "current_revision": max(revisions) if revisions else None,
    }


def _serialize_revision(revision: ExperimentRevision) -> Dict[str, object]:
    return {
        "id": revision.id,
        "revision": revision.revision,
        "status": revision.status,
        "rollout_percentage": float(revision.rollout_percentage or 0.0),
        "variant_weights": {k: float(v) for k, v in (revision.variant_weights or {}).items()},
        "published_by": revision.published_by,
        "created_at": revision.created_at.isoformat() if revision.created_at else None,
    }


@app.put("/experiments/{experiment_key}/config")
def update_experiment_config(
    experiment_key: str,
    payload: ExperimentConfigPayload,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_roles(ROLE_EXPERIMENT_WRITE)),
):
    experiment = db.query(Experiment).filter(Experiment.key == experiment_key).first()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    normalized_weights = _normalize_variant_weights(payload.variants)

    experiment.rollout_percentage = float(payload.rollout_percentage)
    existing = {variant.name: variant for variant in experiment.variants}
    incoming_names = set()
    for name, weight in normalized_weights.items():
        incoming_names.add(name)
        variant = existing.get(name)
        if variant:
            variant.weight = weight
        else:
            db.add(ExperimentVariant(experiment=experiment, name=name, weight=weight))
    for name, variant in existing.items():
        if name not in incoming_names:
            db.delete(variant)

    experiment.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(experiment)
    return {"ok": True, "experiment": _serialize_experiment(experiment)}


@app.post("/experiments/{experiment_key}/publish")
def publish_experiment(
    experiment_key: str,
    db: Session = Depends(get_db),
    identity: AdminIdentity = Depends(require_roles(ROLE_EXPERIMENT_PUBLISH)),
    ab_service: ABFlagService = Depends(get_ab_service),
):
    experiment = db.query(Experiment).filter(Experiment.key == experiment_key).first()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")
    if not experiment.variants:
        raise HTTPException(status_code=422, detail="Experiment has no variants configured")

    variant_weights = {variant.name: float(variant.weight or 0.0) for variant in experiment.variants}
    total_weight = sum(variant_weights.values())
    if total_weight <= 0 or not isclose(total_weight, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Experiment variants must be normalized before publishing",
        )

    last_revision = (
        db.query(ExperimentRevision)
        .filter(ExperimentRevision.experiment_id == experiment.id)
        .order_by(ExperimentRevision.revision.desc())
        .first()
    )
    next_revision = 1 if not last_revision else last_revision.revision + 1
    new_status = EXPERIMENT_STATUS_RUNNING if experiment.rollout_percentage > 0 else EXPERIMENT_STATUS_PAUSED

    experiment.status = new_status
    experiment.updated_at = datetime.now(timezone.utc)

    revision = ExperimentRevision(
        experiment_id=experiment.id,
        revision=next_revision,
        rollout_percentage=float(experiment.rollout_percentage),
        variant_weights=variant_weights,
        status=new_status,
        published_by=identity.subject,
    )
    db.add(revision)

    try:
        db.flush()
        ab_service.publish_experiment(
            experiment.key,
            float(experiment.rollout_percentage),
            variant_weights,
            preserve_sticky_assignments=True,
        )
    except ABFlagServiceError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to publish experiment: {exc}",
        )

    db.commit()
    db.refresh(experiment)
    db.refresh(revision)
    return {
        "ok": True,
        "experiment": _serialize_experiment(experiment),
        "revision": _serialize_revision(revision),
    }


@app.post("/experiments/{experiment_key}/pause")
def pause_experiment(
    experiment_key: str,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_roles(ROLE_EXPERIMENT_WRITE)),
    ab_service: ABFlagService = Depends(get_ab_service),
):
    experiment = db.query(Experiment).filter(Experiment.key == experiment_key).first()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")
    if experiment.status == EXPERIMENT_STATUS_PAUSED:
        return {"ok": True, "experiment": _serialize_experiment(experiment)}
    if experiment.status != EXPERIMENT_STATUS_RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot pause experiment in status '{experiment.status}'",
        )

    experiment.status = EXPERIMENT_STATUS_PAUSED
    experiment.updated_at = datetime.now(timezone.utc)

    try:
        db.flush()
        ab_service.pause_experiment(experiment.key)
    except ABFlagServiceError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to pause experiment: {exc}",
        )

    db.commit()
    db.refresh(experiment)
    return {"ok": True, "experiment": _serialize_experiment(experiment)}


@app.post("/experiments/{experiment_key}/resume")
def resume_experiment(
    experiment_key: str,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_roles(ROLE_EXPERIMENT_WRITE)),
    ab_service: ABFlagService = Depends(get_ab_service),
):
    experiment = db.query(Experiment).filter(Experiment.key == experiment_key).first()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")
    if experiment.status == EXPERIMENT_STATUS_RUNNING:
        return {"ok": True, "experiment": _serialize_experiment(experiment)}
    if experiment.status != EXPERIMENT_STATUS_PAUSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot resume experiment in status '{experiment.status}'",
        )
    if experiment.rollout_percentage <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot resume an experiment with zero rollout percentage",
        )

    variant_weights = {variant.name: float(variant.weight or 0.0) for variant in experiment.variants}
    if not variant_weights:
        raise HTTPException(status_code=422, detail="Experiment has no variants configured")
    if not isclose(sum(variant_weights.values()), 1.0, rel_tol=1e-6, abs_tol=1e-6):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Experiment variants must be normalized before resuming",
        )

    experiment.status = EXPERIMENT_STATUS_RUNNING
    experiment.updated_at = datetime.now(timezone.utc)

    try:
        db.flush()
        ab_service.resume_experiment(
            experiment.key,
            float(experiment.rollout_percentage),
            variant_weights,
        )
    except ABFlagServiceError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to resume experiment: {exc}",
        )

    db.commit()
    db.refresh(experiment)
    return {"ok": True, "experiment": _serialize_experiment(experiment)}

# ----- Tips -----
@app.get("/clients/{client_id}/tips/today")
def tips_today(
    client_id: int,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
):
    """Return a simple set of daily tips based on how far the user is from targets today.
    This is intentionally lightweight to support the miniapp UI.
    """
    tips: list[str] = []
    df = df_meals(db, client_id)
    agg = summary_macros(df, freq="D")
    t = get_targets(client_id, db)
    try:
        # pick today or latest
        row = None
        if agg is not None and not getattr(agg, "empty", True):
            today = datetime.now().date().isoformat()
            for _, r in agg.iterrows():
                if str(r["captured_at"]).startswith(today):
                    row = r
            if row is None:
                row = agg.iloc[-1]
        total = {
            "kcal": float(row["kcal"]) if row is not None else 0.0,
            "p": float(row["protein_g"]) if row is not None else 0.0,
            "f": float(row["fat_g"]) if row is not None else 0.0,
            "c": float(row["carbs_g"]) if row is not None else 0.0,
        }
        # gaps vs targets
        dk = max(0, t["kcal_target"] - total["kcal"]) if t else 0
        dp = max(0, t["protein_target_g"] - total["p"]) if t else 0
        dfat = max(0, t["fat_target_g"] - total["f"]) if t else 0
        dc = max(0, t["carbs_target_g"] - total["c"]) if t else 0
        if dk > 200:
            tips.append("Добавьте 1–2 полезных перекуса, чтобы выйти на план по калориям.")
        if dp > 20:
            tips.append("Не хватает белка — добавьте порцию творога, яйца или рыбу.")
        if dfat > 15:
            tips.append("Жиры ниже плана — орехи, оливковое масло или авокадо можуть помочь.")
        if dc > 40:
            tips.append("Углеводы ниже — можно добавить кашу, фрукты или цельнозерновой хлеб.")
        if not tips:
            tips.append("План выполняется — продолжайте в том же духе!")
    except Exception:
        tips = ["Недостаточно данных для рекомендаций на сегодня."]
    return {"tips": tips}

# ----- Analytics -----
@app.get("/clients/{client_id}/summary/daily")
def daily_summary(
    client_id: int,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
):
    df = df_meals(db, client_id)
    agg = summary_macros(df, freq="D")
    return json_safe(agg)

@app.get("/clients/{client_id}/summary/weekly")
def weekly_summary(
    client_id: int,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
):
    df = df_meals(db, client_id)
    agg = summary_macros(df, freq="W")
    return json_safe(agg)

@app.get("/clients/{client_id}/micro/top")
def micro_summary(
    client_id: int,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
):
    df = df_meals(db, client_id)
    return micro_top(df, top=10)

def json_safe(df):
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for _, r in df.iterrows():
        period = r["captured_at"].to_pydatetime() if hasattr(r["captured_at"], "to_pydatetime") else r["captured_at"]
        out.append({
            "period_start": period.isoformat() if hasattr(period, "isoformat") else str(period),
            "kcal": _to_float(r.get("kcal")) or 0.0,
            "protein_g": _to_float(r.get("protein_g")) or 0.0,
            "fat_g": _to_float(r.get("fat_g")) or 0.0,
            "carbs_g": _to_float(r.get("carbs_g")) or 0.0,
        })
    return out

def json_safe_extras(df):
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for _, r in df.iterrows():
        row = {"period_start": (r["captured_at"].to_pydatetime() if hasattr(r["captured_at"], "to_pydatetime") else r["captured_at"]).isoformat()}
        for k in [
            "fats_total","fats_saturated","fats_mono","fats_poly","fats_trans",
            "omega6","omega3","omega_ratio_num","fiber_total","fiber_soluble","fiber_insoluble"
        ]:
            if k in df.columns:
                val = r[k]
                parsed = _to_float(val)
                if parsed is not None:
                    row[k] = parsed
        out.append(row)
    return out

@app.get("/clients/{client_id}/extras/daily")
def daily_extras(
    client_id: int,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
):
    df = df_meals(db, client_id)
    agg = summary_extras(df, freq="D")
    return json_safe_extras(agg)

@app.get("/clients/{client_id}/extras/weekly")
def weekly_extras(
    client_id: int,
    db: Session = Depends(get_db),
    _: AdminIdentity = Depends(require_api_key),
):
    df = df_meals(db, client_id)
    agg = summary_extras(df, freq="W")
    return json_safe_extras(agg)

# Allow dev origins (e.g., Expo 19006) to call API during debugging (applies to API app; root app will serve SPA)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Add CSP relaxed enough for Expo Web runtime (allows eval for RN web/wasm)
@app.middleware("http")
async def add_csp_headers(request: Request, call_next):  # type: ignore[override]
    response = await call_next(request)
    # API app: relax CSP for all responses under /api when mounted (root app also sets CSP for static)
    path = request.url.path
    if True:
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-eval' 'wasm-unsafe-eval' blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            f"connect-src {_connect_src_value()}; "
            "worker-src 'self' blob:; "
            "frame-ancestors 'self'; "
            "base-uri 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


# -----------------------------
# Root application serving SPA at '/'; API mounted at '/api'
def _build_root_app() -> FastAPI:
    root = FastAPI(title="Nutrios Root")

    # Mount API under /api
    root.mount("/api", app)

    # Mount SPA (Expo export)
    base_dir = Path(__file__).resolve().parents[1]
    _dist = base_dir / "NutriTracker-Pro" / "NutriTracker-Pro-main" / "dist"
    media_dir = base_dir / "bot" / "downloads"
    if media_dir.exists():
        @root.get("/media/{file_path:path}")
        def _serve_media(file_path: str, token: str = Query(...), expires: int = Query(...)):
            if expires <= 0:
                raise HTTPException(status_code=400, detail="Invalid expiry")
            if expires < int(time.time()):
                raise HTTPException(status_code=410, detail="Media link expired")
            expected = _media_signature(file_path, expires)
            if not hmac.compare_digest(expected, token):
                raise HTTPException(status_code=403, detail="Invalid media token")
            target = (media_dir / file_path).resolve()
            if not _is_within_directory(media_dir, target) or not target.exists() or not target.is_file():
                raise HTTPException(status_code=404, detail="Media not found")
            return FileResponse(target)
    if _dist.exists():
        # Static assets first to avoid being shadowed by catch-all
        if (_dist / "_expo").exists():
            root.mount("/_expo", StaticFiles(directory=str(_dist / "_expo"), html=False), name="expo_static")
        if (_dist / "assets").exists():
            root.mount("/assets", StaticFiles(directory=str(_dist / "assets"), html=False), name="expo_assets")
        # Dedicated routes for exported top-level pages (optional)
        # Explicit top-level pages
        # Выберем неонбординговую страницу по умолчанию (например, progress)
        progress_html = _dist / "progress.html"
        index_html = _dist / "index.html"
        onboarding_html = _dist / "onboarding.html"
        if progress_html.exists():
            @root.get("/")
            def _serve_progress(fp=str(progress_html)):
                return FileResponse(fp)
        elif index_html.exists():
            @root.get("/")
            def _serve_index(fp=str(index_html)):
                return FileResponse(fp)
        if onboarding_html.exists():
            @root.get("/onboarding")
            def _serve_onboarding(request: Request, fp=str(onboarding_html)):
                # Разрешаем открывать онбординг только по явному параметру ?open=1
                qp = dict(request.query_params)
                if qp.get("open") in {"1", "true", "yes"}:
                    return FileResponse(fp)
                # иначе — на главную, сохраняя ?tg=...
                from fastapi.responses import RedirectResponse
                query = ("?" + str(request.url.query)) if request.url.query else ""
                return RedirectResponse(url=f"/{query}", status_code=307)
        # Catch-all SPA route: serve index.html, client router handles actual path
        if index_html.exists():
            @root.get("/{full_path:path}")
            def _spa_fallback(full_path: str, fp=str(index_html)):
                # Avoid shadowing asset requests
                if full_path.startswith(("_expo/", "assets/")):
                    raise HTTPException(status_code=404, detail="Not Found")
                return FileResponse(fp)
    else:
        # Fallback legacy
        root.mount("/", StaticFiles(directory="miniapp", html=True), name="spa_legacy")

    from fastapi.responses import RedirectResponse

    @root.get("/miniapp")
    @root.get("/miniapp/")
    def _redirect_miniapp(request: Request):  # type: ignore[override]
        # Раньше вели на /onboarding, теперь — на главную (без опросника)
        query = ("?" + str(request.url.query)) if request.url.query else ""
        return RedirectResponse(url=f"/{query}", status_code=307)

    # Duplicate CSP on root to cover static served pages
    @root.middleware("http")
    async def _root_csp(request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        p = request.url.path
        if p.startswith(("/_expo", "/assets", "/(tabs)", "/onboarding", "/", )):
            csp = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob:; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "font-src 'self' data:; "
                f"connect-src {_connect_src_value()}; "
                "worker-src 'self' blob:; "
                "frame-ancestors 'self'; "
                "base-uri 'self'"
            )
            response.headers.setdefault("Content-Security-Policy", csp)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    return root


# Expose root app separately to avoid shadowing API app used in tests
root_app = _build_root_app()
