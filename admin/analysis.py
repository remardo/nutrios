import pandas as pd
from sqlalchemy.orm import Session
from .models import Meal

MSK_TZ = "Europe/Moscow"


def _captured_at_moscow(df: pd.DataFrame) -> pd.Series:
    # Stored timestamps are UTC (or UTC-like naive). Normalize to UTC first,
    # then convert to Moscow timezone for day/week grouping.
    ts = pd.to_datetime(df["captured_at"], utc=True, errors="coerce")
    return ts.dt.tz_convert(MSK_TZ).dt.tz_localize(None)


def df_meals(session: Session, client_id: int, date_from=None, date_to=None) -> pd.DataFrame:
    q = session.query(Meal).filter(Meal.client_id==client_id)
    if date_from: q = q.filter(Meal.captured_at >= date_from)
    if date_to:   q = q.filter(Meal.captured_at < date_to)
    rows = q.all()
    if not rows: return pd.DataFrame()
    def _flt(x):
        try:
            return float(x) if x is not None else None
        except Exception:
            return None
    data = []
    for r in rows:
        ex = r.extras or {}
        fats = ex.get("fats") or {}
        fiber = ex.get("fiber") or {}
        omega6 = _flt(fats.get("omega6"))
        omega3 = _flt(fats.get("omega3"))
        ratio = None
        if omega3 and omega3 > 0:
            ratio = float(round(omega6 / omega3, 2)) if omega6 is not None else None
        data.append({
            "captured_at": r.captured_at, "title": r.title, "portion_g": r.portion_g,
            "kcal": r.kcal, "protein_g": r.protein_g, "fat_g": r.fat_g, "carbs_g": r.carbs_g,
            "flags": r.flags, "micronutrients": r.micronutrients,
            # extras flattened (floats)
            "fats_total": _flt(fats.get("total")),
            "fats_saturated": _flt(fats.get("saturated")),
            "fats_mono": _flt(fats.get("mono")),
            "fats_poly": _flt(fats.get("poly")),
            "fats_trans": _flt(fats.get("trans")),
            "omega6": omega6,
            "omega3": omega3,
            "omega_ratio_num": ratio,
            "fiber_total": _flt(fiber.get("total")),
            "fiber_soluble": _flt(fiber.get("soluble")),
            "fiber_insoluble": _flt(fiber.get("insoluble")),
        })
    return pd.DataFrame(data).sort_values("captured_at")

def summary_macros(df: pd.DataFrame, freq="D"):
    if df.empty: return {}
    work = df.copy()
    work["captured_at"] = _captured_at_moscow(work)
    work = work.dropna(subset=["captured_at"])
    g = work.set_index("captured_at").groupby(pd.Grouper(freq=freq))
    agg = g[["kcal","protein_g","fat_g","carbs_g"]].sum().round(0).reset_index()
    return agg

def summary_extras(df: pd.DataFrame, freq="D"):
    if df.empty: return {}
    work = df.copy()
    work["captured_at"] = _captured_at_moscow(work)
    work = work.dropna(subset=["captured_at"])
    g = work.set_index("captured_at").groupby(pd.Grouper(freq=freq))
    cols_sum = [
        "fats_total","fats_saturated","fats_mono","fats_poly","fats_trans",
        "omega6","omega3","fiber_total","fiber_soluble","fiber_insoluble"
    ]
    present = [c for c in cols_sum if c in work.columns]
    if not present:
        return pd.DataFrame(columns=["captured_at"])  # empty
    agg = g[present].sum(min_count=1).reset_index()
    # compute omega ratio from sums if possible
    if "omega6" in agg.columns and "omega3" in agg.columns:
        def ratio_row(r):
            try:
                return round(float(r["omega6"]) / float(r["omega3"]), 2) if r["omega3"] and r["omega3"] > 0 else None
            except Exception:
                return None
        agg["omega_ratio_num"] = agg.apply(ratio_row, axis=1)
    # rename captured_at -> period_start for API consistency in json helper
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
