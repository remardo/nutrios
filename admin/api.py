import os, hmac, hashlib, json
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, field_validator
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, date, time, timezone
from .db import SessionLocal, init_db, ensure_meals_extras_column, ensure_metrics_tables
from .models import Base, Client, Meal, ClientTargets, ClientDailyMetrics, ClientEvents
from .auth import require_api_key
from .analysis import df_meals, summary_macros, summary_extras, micro_top
from dotenv import load_dotenv
from pathlib import Path

app = FastAPI(title="Nutrios Admin API")

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
ensure_metrics_tables(Base)
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


# ----- Metrics & Events Schemas -----
class DailyMetricIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    water_goal_met: Optional[bool] = None
    steps: Optional[int] = Field(default=None, ge=0)
    protein_goal_met: Optional[bool] = None
    fiber_goal_met: Optional[bool] = None
    breakfast_logged_before_10: Optional[bool] = None
    dinner_logged: Optional[bool] = None
    new_recipe_logged: Optional[bool] = None


class ClientEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    occurred_at: Optional[datetime] = None
    date: Optional[date] = None
    payload: Optional[dict] = None

    @field_validator("type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        t = (v or "").strip().lower()
        if not t:
            raise ValueError("type is required")
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
        if any(ch not in allowed for ch in t):
            raise ValueError("type must contain lowercase letters, digits, '_' or '-'")
        return t

    @field_validator("payload")
    @classmethod
    def _validate_payload(cls, v):
        if v is not None and not isinstance(v, dict):
            raise ValueError("payload must be a JSON object")
        return v


def _client_or_404(db: Session, client_id: int) -> Client:
    client = db.query(Client).filter_by(id=client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


def _telegram_auth_ok(raw: str | None, expected_user_id: Optional[int]) -> bool:
    if not raw or not expected_user_id:
        return False
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return False
    try:
        parts = dict(item.split('=', 1) for item in raw.split('&') if '=' in item)
        data_json = parts.get('user')
        hash_recv = parts.get('hash')
        if not data_json or not hash_recv:
            return False
        secret = hashlib.sha256(bot_token.encode()).digest()
        check_string = '\n'.join(sorted([f"{k}={v}" for k, v in parts.items() if k != 'hash']))
        h = hmac.new(secret, msg=check_string.encode(), digestmod=hashlib.sha256).hexdigest()
        if not hmac.compare_digest(h, hash_recv):
            return False
        user = json.loads(data_json) if data_json else {}
        uid = int(user.get('id')) if user and 'id' in user else None
        return uid == expected_user_id
    except Exception:
        return False


def _authorize_client_write(
    client: Client,
    x_api_key: Optional[str],
    init_data: Optional[str],
    request: Optional[Request],
):
    expected_key = os.getenv("ADMIN_API_KEY", "supersecret")
    if x_api_key and hmac.compare_digest(x_api_key, expected_key):
        return
    if init_data and _telegram_auth_ok(init_data, client.telegram_user_id):
        return
    allow_debug = os.getenv("ALLOW_DEBUG_WEBAPP", "0").lower() in {"1", "true", "yes"}
    if allow_debug and request is not None:
        try:
            tg = request.query_params.get("tg") if hasattr(request, "query_params") else None
            if tg and client.telegram_user_id and int(tg) == client.telegram_user_id:
                return
        except Exception:
            pass
    raise HTTPException(status_code=401, detail="Unauthorized")


def _serialize_daily_metric(row: ClientDailyMetrics) -> dict:
    return {
        "id": row.id,
        "client_id": row.client_id,
        "date": row.date.isoformat() if row.date else None,
        "water_goal_met": bool(row.water_goal_met) if row.water_goal_met is not None else None,
        "steps": row.steps,
        "protein_goal_met": bool(row.protein_goal_met) if row.protein_goal_met is not None else None,
        "fiber_goal_met": bool(row.fiber_goal_met) if row.fiber_goal_met is not None else None,
        "breakfast_logged_before_10": bool(row.breakfast_logged_before_10) if row.breakfast_logged_before_10 is not None else None,
        "dinner_logged": bool(row.dinner_logged) if row.dinner_logged is not None else None,
        "new_recipe_logged": bool(row.new_recipe_logged) if row.new_recipe_logged is not None else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_event(row: ClientEvents) -> dict:
    return {
        "id": row.id,
        "client_id": row.client_id,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "occurred_on": row.occurred_on.isoformat() if row.occurred_on else None,
        "type": row.type,
        "payload": row.payload,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }

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
        uid = int(dict(request.query_params)['tg'])
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


# ----- Daily Metrics -----
@app.get("/clients/{client_id}/metrics/daily")
def get_daily_metrics(
    client_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 30,
    db: Session = Depends(get_db),
):
    _ = _client_or_404(db, client_id)
    q = db.query(ClientDailyMetrics).filter(ClientDailyMetrics.client_id == client_id)
    if start_date:
        q = q.filter(ClientDailyMetrics.date >= start_date)
    if end_date:
        q = q.filter(ClientDailyMetrics.date <= end_date)
    q = q.order_by(ClientDailyMetrics.date.desc())
    if limit:
        limit = max(1, min(int(limit), 365))
        q = q.limit(limit)
    rows = q.all()
    return [_serialize_daily_metric(r) for r in rows]


@app.put("/clients/{client_id}/metrics/daily")
def put_daily_metrics(
    client_id: int,
    payload: DailyMetricIn | List[DailyMetricIn],
    db: Session = Depends(get_db),
    request: Request = None,
    X_Telegram_Init_Data: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    client = _client_or_404(db, client_id)
    _authorize_client_write(client, x_api_key, X_Telegram_Init_Data, request)
    records = payload if isinstance(payload, list) else [payload]
    if not records:
        raise HTTPException(status_code=400, detail="Payload is empty")
    touched: List[ClientDailyMetrics] = []
    for item in records:
        values = item.model_dump(exclude_unset=True)
        metric_date = values.get("date")
        if not metric_date:
            raise HTTPException(status_code=422, detail="date is required")
        row = (
            db.query(ClientDailyMetrics)
            .filter(
                ClientDailyMetrics.client_id == client.id,
                ClientDailyMetrics.date == metric_date,
            )
            .first()
        )
        if not row:
            row = ClientDailyMetrics(client_id=client.id, date=metric_date)
            db.add(row)
        for key, value in values.items():
            if key == "date":
                continue
            setattr(row, key, value)
        touched.append(row)
    db.commit()
    for row in touched:
        db.refresh(row)
    return {"ok": True, "items": [_serialize_daily_metric(r) for r in touched]}


# ----- Events -----
@app.get("/clients/{client_id}/events")
def get_client_events(
    client_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    event_type: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    _ = _client_or_404(db, client_id)
    q = db.query(ClientEvents).filter(ClientEvents.client_id == client_id)
    if event_type:
        q = q.filter(ClientEvents.type == event_type.strip().lower())
    if start_date:
        q = q.filter(ClientEvents.occurred_on >= start_date)
    if end_date:
        q = q.filter(ClientEvents.occurred_on <= end_date)
    q = q.order_by(ClientEvents.occurred_at.desc())
    if limit:
        limit = max(1, min(int(limit), 500))
        q = q.limit(limit)
    rows = q.all()
    return [_serialize_event(r) for r in rows]


@app.post("/clients/{client_id}/events")
def post_client_events(
    client_id: int,
    payload: ClientEventIn | List[ClientEventIn],
    db: Session = Depends(get_db),
    request: Request = None,
    X_Telegram_Init_Data: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
):
    client = _client_or_404(db, client_id)
    _authorize_client_write(client, x_api_key, X_Telegram_Init_Data, request)
    records = payload if isinstance(payload, list) else [payload]
    if not records:
        raise HTTPException(status_code=400, detail="Payload is empty")
    now = datetime.now(timezone.utc)
    created: List[ClientEvents] = []
    updated: List[ClientEvents] = []
    for item in records:
        values = item.model_dump(exclude_unset=True)
        event_type = values.get("type")
        if not event_type:
            raise HTTPException(status_code=422, detail="type is required")
        occurred_at: datetime | None = values.get("occurred_at")
        event_date: date | None = values.get("date")
        if occurred_at and occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        if occurred_at and event_date and occurred_at.date() != event_date:
            raise HTTPException(status_code=422, detail="occurred_at and date mismatch")
        if not event_date:
            event_date = occurred_at.date() if occurred_at else now.date()
        if not occurred_at:
            occurred_at = datetime.combine(event_date, time.min).replace(tzinfo=timezone.utc)
        payload_data = values.get("payload")
        row = (
            db.query(ClientEvents)
            .filter(
                ClientEvents.client_id == client.id,
                ClientEvents.occurred_on == event_date,
                ClientEvents.type == event_type,
            )
            .first()
        )
        if row:
            row.occurred_at = occurred_at
            if payload_data is not None:
                row.payload = payload_data
            updated.append(row)
        else:
            row = ClientEvents(
                client_id=client.id,
                occurred_at=occurred_at,
                occurred_on=event_date,
                type=event_type,
                payload=payload_data,
            )
            db.add(row)
            created.append(row)
    db.commit()
    for row in created + updated:
        db.refresh(row)
    return {
        "ok": True,
        "created": [_serialize_event(r) for r in created],
        "updated": [_serialize_event(r) for r in updated],
    }


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
