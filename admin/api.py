import os, hmac, hashlib, json
from math import isclose
from datetime import datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from pathlib import Path

from .ab_service import ABFlagService, ABFlagServiceError, get_ab_service
from .analysis import df_meals, micro_top, summary_extras, summary_macros
from .auth import AdminIdentity, require_api_key, require_roles
from .db import SessionLocal, ensure_meals_extras_column, init_db
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
MSK = ZoneInfo("Europe/Moscow")


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
# mount mini app static
app.mount("/miniapp", StaticFiles(directory="miniapp", html=True), name="miniapp")

# ----- Schemas -----
class IngestMeal(BaseModel):
    telegram_user_id: int
    telegram_username: Optional[str] = None
    captured_at_iso: str
    title: str
    portion_g: int
    confidence: int
    kcal: int
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    carbs_g: Optional[float] = None
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
    try:
        captured_at = datetime.fromisoformat(fields.pop("captured_at_iso"))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid captured_at_iso format")
    fields.pop("message_id", None)  # Remove message_id to avoid duplicate
    fields.pop("telegram_user_id", None)  # Remove client fields
    fields.pop("telegram_username", None)
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
def list_clients(db: Session = Depends(get_db)):
    rows = db.query(Client).order_by(Client.created_at.desc()).all()
    return [{"id": r.id, "telegram_user_id": r.telegram_user_id, "telegram_username": r.telegram_username} for r in rows]


@app.get("/client/by_telegram/{telegram_user_id}")
def client_by_telegram(telegram_user_id: int, db: Session = Depends(get_db), X_Telegram_Init_Data: str | None = Header(default=None), request: Request = None):
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
        tg_param = dict(request.query_params).get("tg")
        try:
            uid = int(tg_param) if tg_param is not None else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=401, detail="Invalid debug tg parameter")
        if uid is None:
            raise HTTPException(status_code=401, detail="Invalid debug tg parameter")
        if not _ok(uid):
            raise HTTPException(status_code=403, detail="Forbidden")
    else:
        raise HTTPException(status_code=401, detail="Missing Telegram auth")
    row = db.query(Client).filter_by(telegram_user_id=telegram_user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"id": row.id, "telegram_user_id": row.telegram_user_id, "telegram_username": row.telegram_username}

@app.get("/clients/{client_id}/meals")
def list_meals(client_id: int, db: Session = Depends(get_db)):
    rows = db.query(Meal).filter(Meal.client_id==client_id).order_by(Meal.captured_at.desc()).all()
    return [{
        "id": r.id, "captured_at": r.captured_at.isoformat(), "title": r.title, "portion_g": r.portion_g,
        "kcal": r.kcal, "protein_g": r.protein_g, "fat_g": r.fat_g, "carbs_g": r.carbs_g,
        "flags": r.flags, "micronutrients": r.micronutrients, "assumptions": r.assumptions,
        "extras": r.extras,
        "image_path": r.image_path, "source_type": r.source_type, "message_id": r.message_id
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
def get_targets(client_id: int, db: Session = Depends(get_db)):
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
def put_targets(client_id: int, payload: Targets, db: Session = Depends(get_db)):
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
        row = {
            "period_start": (r["captured_at"].to_pydatetime() if hasattr(r["captured_at"], "to_pydatetime") else r["captured_at"]).isoformat(),
            "kcal": float(r["kcal"]),
            "protein_g": float(r["protein_g"]),
            "fat_g": float(r["fat_g"]),
            "carbs_g": float(r["carbs_g"]),
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
def daily_progress(client_id: int, db: Session = Depends(get_db)):
    df = df_meals(db, client_id)
    agg = summary_macros(df, freq="D")
    targets = get_targets(client_id, db)
    return _progress_rows(agg, targets)


@app.get("/clients/{client_id}/progress/weekly")
def weekly_progress(client_id: int, db: Session = Depends(get_db)):
    df = df_meals(db, client_id)
    agg = summary_macros(df, freq="W")
    targets = get_targets(client_id, db)
    return _progress_rows(agg, targets)


@app.get("/clients/{client_id}/streak")
def compliance_streak(client_id: int, db: Session = Depends(get_db)):
    df = df_meals(db, client_id)
    agg = summary_macros(df, freq="D")
    if agg is None or getattr(agg, "empty", True):
        return {"streak": 0, "met_goal_7": False}
    t = get_targets(client_id, db)
    # Define compliance if kcal within 10% and macros within 20%
    def is_ok(row):
        try:
            kcal_ok = abs(float(row["kcal"]) - t["kcal_target"]) <= t["kcal_target"] * 0.10
            p_ok = abs(float(row["protein_g"]) - t["protein_target_g"]) <= max(10.0, t["protein_target_g"] * 0.20)
            f_ok = abs(float(row["fat_g"]) - t["fat_target_g"]) <= max(10.0, t["fat_target_g"] * 0.20)
            c_ok = abs(float(row["carbs_g"]) - t["carbs_target_g"]) <= max(15.0, t["carbs_target_g"] * 0.20)
            return kcal_ok and p_ok and f_ok and c_ok
        except Exception:
            return False
    # compute current streak from last day backwards
    streak = 0
    for _, r in agg.sort_values("captured_at").iterrows():
        pass
    # iterate reverse
    for _, r in agg.sort_values("captured_at", ascending=False).iterrows():
        if is_ok(r):
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
def tips_today(client_id: int, db: Session = Depends(get_db)):
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
            today = datetime.now(MSK).date().isoformat()
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
def daily_summary(client_id: int, db: Session = Depends(get_db)):
    df = df_meals(db, client_id)
    agg = summary_macros(df, freq="D")
    return json_safe(agg)

@app.get("/clients/{client_id}/summary/weekly")
def weekly_summary(client_id: int, db: Session = Depends(get_db)):
    df = df_meals(db, client_id)
    agg = summary_macros(df, freq="W")
    return json_safe(agg)

@app.get("/clients/{client_id}/micro/top")
def micro_summary(client_id: int, db: Session = Depends(get_db)):
    df = df_meals(db, client_id)
    return micro_top(df, top=10)

def json_safe(df):
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for _, r in df.iterrows():
        out.append({
            "period_start": (r["captured_at"].to_pydatetime() if hasattr(r["captured_at"], "to_pydatetime") else r["captured_at"]).isoformat(),
            "kcal": float(r["kcal"]), "protein_g": float(r["protein_g"]),
            "fat_g": float(r["fat_g"]), "carbs_g": float(r["carbs_g"]),
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
                if val is not None:
                    try:
                        row[k] = float(val)
                    except (TypeError, ValueError):
                        pass
        out.append(row)
    return out

@app.get("/clients/{client_id}/extras/daily")
def daily_extras(client_id: int, db: Session = Depends(get_db)):
    df = df_meals(db, client_id)
    agg = summary_extras(df, freq="D")
    return json_safe_extras(agg)

@app.get("/clients/{client_id}/extras/weekly")
def weekly_extras(client_id: int, db: Session = Depends(get_db)):
    df = df_meals(db, client_id)
    agg = summary_extras(df, freq="W")
    return json_safe_extras(agg)
