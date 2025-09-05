import pandas as pd
from sqlalchemy.orm import Session
from .models import Meal

def df_meals(session: Session, client_id: int, date_from=None, date_to=None) -> pd.DataFrame:
    q = session.query(Meal).filter(Meal.client_id==client_id)
    if date_from: q = q.filter(Meal.captured_at >= date_from)
    if date_to:   q = q.filter(Meal.captured_at < date_to)
    rows = q.all()
    if not rows: return pd.DataFrame()
    data = [{
        "captured_at": r.captured_at, "title": r.title, "portion_g": r.portion_g,
        "kcal": r.kcal, "protein_g": r.protein_g, "fat_g": r.fat_g, "carbs_g": r.carbs_g,
        "flags": r.flags, "micronutrients": r.micronutrients
    } for r in rows]
    return pd.DataFrame(data).sort_values("captured_at")

def summary_macros(df: pd.DataFrame, freq="D"):
    if df.empty: return {}
    g = df.set_index("captured_at").groupby(pd.Grouper(freq=freq))
    agg = g[["kcal","protein_g","fat_g","carbs_g"]].sum().round(0).reset_index()
    return agg

def micro_top(df: pd.DataFrame, top=10):
    if df.empty: return []
    # расплющим микроспики
    micro = []
    for _, row in df.iterrows():
        for it in (row.get("micronutrients") or []):
            micro.append(it)
    # простая частота упоминаний
    s = pd.Series(micro).value_counts().head(top)
    return [{"name_amount": k, "count": int(v)} for k, v in s.items()]
