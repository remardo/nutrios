import os
from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from .db import SessionLocal, init_db, ensure_meals_extras_column
from .models import Base, Client, Meal
from .auth import require_api_key
from .analysis import df_meals, summary_macros, micro_top

app = FastAPI(title="Nutrios Admin API")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# init
init_db(Base)
ensure_meals_extras_column()

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
