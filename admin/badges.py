"""Badge evaluation logic for Nutrios admin."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional

import pandas as pd
from sqlalchemy.orm import Session

from .analysis import df_meals, summary_extras, summary_macros
from .models import ClientTargets


DEFAULT_TARGETS = {
    "kcal_target": 2000,
    "protein_target_g": 100,
    "fat_target_g": 70,
    "carbs_target_g": 250,
    "tolerances": {
        "kcal_pct": 0.10,
        "protein_pct": 0.20,
        "fat_pct": 0.20,
        "carbs_pct": 0.20,
        "min_g": {"p": 10, "f": 10, "c": 15},
    },
}


@dataclass
class BadgeEvaluation:
    earned: bool
    progress: float
    meta: Optional[Dict[str, float]] = None


@dataclass
class BadgeContext:
    session: Session
    client_id: int
    meals: pd.DataFrame
    daily_macros: pd.DataFrame
    daily_extras: pd.DataFrame
    targets: Dict[str, object]
    compliance_bools: List[bool]
    compliance_segments: List[tuple[bool, int]]
    current_streak: int
    best_streak: int


@dataclass
class Badge:
    code: str
    title: str
    description: str
    evaluator: Callable[[BadgeContext], BadgeEvaluation]


def _ensure_dataframe(obj: object) -> pd.DataFrame:
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    return pd.DataFrame()


def _load_targets(session: Session, client_id: int) -> Dict[str, object]:
    row = session.query(ClientTargets).filter_by(client_id=client_id).first()
    if not row:
        return DEFAULT_TARGETS.copy()
    tolerances = row.tolerances or DEFAULT_TARGETS["tolerances"]
    return {
        "kcal_target": row.kcal_target,
        "protein_target_g": row.protein_target_g,
        "fat_target_g": row.fat_target_g,
        "carbs_target_g": row.carbs_target_g,
        "tolerances": tolerances,
    }


def _safe_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _default_tolerances(targets: Dict[str, object]) -> Dict[str, object]:
    tol = targets.get("tolerances") if targets else None
    if not tol:
        tol = DEFAULT_TARGETS["tolerances"]
    tol.setdefault("min_g", DEFAULT_TARGETS["tolerances"]["min_g"])
    return tol


def _is_day_compliant(row: pd.Series, targets: Dict[str, object]) -> bool:
    if not isinstance(row, pd.Series) or not targets:
        return False
    tol = _default_tolerances(targets)
    try:
        kcal_target = float(targets.get("kcal_target") or 0)
        protein_target = float(targets.get("protein_target_g") or 0)
        fat_target = float(targets.get("fat_target_g") or 0)
        carbs_target = float(targets.get("carbs_target_g") or 0)
    except Exception:
        return False

    kcal = _safe_float(row.get("kcal"))
    protein = _safe_float(row.get("protein_g"))
    fat = _safe_float(row.get("fat_g"))
    carbs = _safe_float(row.get("carbs_g"))

    if None in (kcal, protein, fat, carbs):
        return False

    def within(actual: float, target: float, pct: float, min_g: float = 0.0) -> bool:
        if target <= 0:
            return False
        allowed = max(min_g, target * pct)
        return abs(actual - target) <= allowed

    try:
        kcal_ok = within(kcal, kcal_target, tol.get("kcal_pct", 0.1))
        protein_ok = within(protein, protein_target, tol.get("protein_pct", 0.2), tol.get("min_g", {}).get("p", 10))
        fat_ok = within(fat, fat_target, tol.get("fat_pct", 0.2), tol.get("min_g", {}).get("f", 10))
        carbs_ok = within(carbs, carbs_target, tol.get("carbs_pct", 0.2), tol.get("min_g", {}).get("c", 15))
    except Exception:
        return False
    return kcal_ok and protein_ok and fat_ok and carbs_ok


def _fill_compliance_gaps(sorted_rows: List[tuple[datetime, bool]]) -> List[bool]:
    if not sorted_rows:
        return []
    result: List[bool] = []
    previous_date: Optional[datetime] = None
    for date, compliant in sorted_rows:
        if previous_date is not None:
            delta = (date.date() - previous_date.date()).days
            for _ in range(1, max(delta, 0)):
                result.append(False)
        result.append(bool(compliant))
        previous_date = date
    return result


def _segments_from_bools(bools: Iterable[bool]) -> List[tuple[bool, int]]:
    segments: List[tuple[bool, int]] = []
    current_val: Optional[bool] = None
    current_len = 0
    for value in bools:
        if current_val is None:
            current_val = value
            current_len = 1
            continue
        if value == current_val:
            current_len += 1
        else:
            segments.append((current_val, current_len))
            current_val = value
            current_len = 1
    if current_val is not None:
        segments.append((current_val, current_len))
    return segments


def _build_context(session: Session, client_id: int) -> BadgeContext:
    meals = df_meals(session, client_id)
    daily_macros = _ensure_dataframe(summary_macros(meals, freq="D"))
    daily_extras = _ensure_dataframe(summary_extras(meals, freq="D"))
    if not daily_macros.empty and "captured_at" in daily_macros.columns:
        daily_macros = daily_macros.sort_values("captured_at")
    if not daily_extras.empty and "captured_at" in daily_extras.columns:
        daily_extras = daily_extras.sort_values("captured_at")

    targets = _load_targets(session, client_id)
    compliance_source: List[tuple[datetime, bool]] = []
    if not daily_macros.empty:
        for _, row in daily_macros.iterrows():
            captured = row.get("captured_at")
            if isinstance(captured, pd.Timestamp):
                captured_dt = captured.to_pydatetime()
            else:
                captured_dt = captured if isinstance(captured, datetime) else None
            if not captured_dt:
                continue
            compliant = _is_day_compliant(row, targets)
            compliance_source.append((captured_dt, compliant))

    compliance_bools = _fill_compliance_gaps(compliance_source)
    segments = _segments_from_bools(compliance_bools)
    current_streak = 0
    for compliant in reversed(compliance_bools):
        if compliant:
            current_streak += 1
        else:
            break
    best_streak = 0
    for compliant, length in segments:
        if compliant:
            best_streak = max(best_streak, length)

    return BadgeContext(
        session=session,
        client_id=client_id,
        meals=meals,
        daily_macros=daily_macros,
        daily_extras=daily_extras,
        targets=targets,
        compliance_bools=compliance_bools,
        compliance_segments=segments,
        current_streak=current_streak,
        best_streak=best_streak,
    )


def _eval_first_meal(ctx: BadgeContext) -> BadgeEvaluation:
    total_meals = 0 if ctx.meals is None else len(ctx.meals.index) if isinstance(ctx.meals, pd.DataFrame) else len(ctx.meals)
    earned = total_meals > 0
    progress = 1.0 if earned else 0.0
    return BadgeEvaluation(earned=earned, progress=progress, meta={"total_meals": float(total_meals)})


def _eval_steady_week(ctx: BadgeContext) -> BadgeEvaluation:
    required = 7
    progress = min(1.0, ctx.current_streak / required) if required else 0.0
    earned = ctx.current_streak >= required
    return BadgeEvaluation(
        earned=earned,
        progress=progress,
        meta={
            "current_streak": float(ctx.current_streak),
            "best_streak": float(ctx.best_streak),
        },
    )


def _eval_fiber_fan(ctx: BadgeContext) -> BadgeEvaluation:
    if ctx.daily_extras.empty or "fiber_total" not in ctx.daily_extras.columns:
        return BadgeEvaluation(earned=False, progress=0.0, meta={"days": 0.0, "avg_fiber": 0.0})
    rows = []
    for _, row in ctx.daily_extras.iterrows():
        captured = row.get("captured_at")
        if isinstance(captured, pd.Timestamp):
            captured_dt = captured.to_pydatetime()
        elif isinstance(captured, datetime):
            captured_dt = captured
        else:
            continue
        fiber = _safe_float(row.get("fiber_total")) or 0.0
        rows.append((captured_dt, fiber))
    if not rows:
        return BadgeEvaluation(earned=False, progress=0.0, meta={"days": 0.0, "avg_fiber": 0.0})
    rows.sort(key=lambda x: x[0])
    unique_by_day: Dict[datetime, float] = {}
    for captured_dt, fiber in rows:
        unique_by_day[captured_dt.date()] = fiber
    last_days = list(sorted(unique_by_day.keys()))[-7:]
    values = [unique_by_day[d] for d in last_days]
    days_count = len(values)
    avg_fiber = sum(values) / days_count if days_count else 0.0
    target_avg = 25.0
    earned = days_count >= 3 and avg_fiber >= target_avg
    coverage_progress = min(1.0, days_count / 3.0) if days_count else 0.0
    avg_progress = min(1.0, avg_fiber / target_avg) if target_avg else 0.0
    progress = min(1.0, (coverage_progress + avg_progress) / 2) if days_count else 0.0
    return BadgeEvaluation(
        earned=earned,
        progress=progress,
        meta={"days": float(days_count), "avg_fiber": float(round(avg_fiber, 2))},
    )


def _eval_omega_balance(ctx: BadgeContext) -> BadgeEvaluation:
    if ctx.daily_extras.empty:
        return BadgeEvaluation(earned=False, progress=0.0, meta={"days": 0.0, "in_range": 0.0})
    rows = []
    for _, row in ctx.daily_extras.iterrows():
        captured = row.get("captured_at")
        if isinstance(captured, pd.Timestamp):
            captured_dt = captured.to_pydatetime()
        elif isinstance(captured, datetime):
            captured_dt = captured
        else:
            continue
        ratio = row.get("omega_ratio_num")
        if ratio is None and {"omega6", "omega3"}.issubset(row.index):
            ratio = _compute_ratio(row.get("omega6"), row.get("omega3"))
        ratio_f = _safe_float(ratio)
        rows.append((captured_dt, ratio_f))
    if not rows:
        return BadgeEvaluation(earned=False, progress=0.0, meta={"days": 0.0, "in_range": 0.0})
    rows.sort(key=lambda x: x[0])
    unique_by_day: Dict[datetime, Optional[float]] = {}
    for captured_dt, ratio in rows:
        unique_by_day[captured_dt.date()] = ratio
    last_days = list(sorted(unique_by_day.keys()))[-7:]
    ratios = [unique_by_day[d] for d in last_days if unique_by_day[d] is not None]
    days_with_ratio = len(ratios)
    in_range = len([r for r in ratios if 2.0 <= r <= 5.0])
    required_days = 3
    earned = in_range >= required_days
    progress = 0.0
    if required_days:
        progress = min(1.0, in_range / required_days)
    return BadgeEvaluation(
        earned=earned,
        progress=progress,
        meta={"days": float(days_with_ratio), "in_range": float(in_range)},
    )


def _compute_ratio(omega6: object, omega3: object) -> Optional[float]:
    six = _safe_float(omega6)
    three = _safe_float(omega3)
    if six is None or three in (None, 0):
        return None
    try:
        return round(six / three, 2)
    except Exception:
        return None


def _eval_hero_return(ctx: BadgeContext) -> BadgeEvaluation:
    segments = ctx.compliance_segments
    if not segments or not segments[-1][0]:
        return BadgeEvaluation(
            earned=False,
            progress=0.0,
            meta={
                "current_streak": float(ctx.current_streak),
                "previous_best": float(ctx.best_streak),
                "break_length": 0.0,
            },
        )
    current_len = segments[-1][1]
    break_len = segments[-2][1] if len(segments) >= 2 and not segments[-2][0] else 0
    previous_best = 0
    if len(segments) >= 3:
        for compliant, length in reversed(segments[:-2]):
            if compliant:
                previous_best = max(previous_best, length)
                if previous_best >= 5:
                    break
    progress_parts: List[float] = []
    progress_parts.append(min(1.0, current_len / 3.0))
    if previous_best:
        progress_parts.append(min(1.0, previous_best / 5.0))
    else:
        progress_parts.append(0.0)
    if break_len:
        progress_parts.append(min(1.0, break_len / 3.0))
    else:
        progress_parts.append(0.0)
    progress = sum(progress_parts) / len(progress_parts) if progress_parts else 0.0
    earned = current_len >= 3 and break_len >= 3 and previous_best >= 5
    return BadgeEvaluation(
        earned=earned,
        progress=min(1.0, progress),
        meta={
            "current_streak": float(ctx.current_streak),
            "previous_best": float(previous_best),
            "break_length": float(break_len),
        },
    )


BADGES: List[Badge] = [
    Badge(
        code="first_meal",
        title="Первый шаг",
        description="Клиент зафиксировал первую запись о приёме пищи.",
        evaluator=_eval_first_meal,
    ),
    Badge(
        code="steady_week",
        title="В ритме недели",
        description="7 дней подряд придерживается целевых макросов.",
        evaluator=_eval_steady_week,
    ),
    Badge(
        code="fiber_fan",
        title="Фанат клетчатки",
        description="Среднее потребление клетчатки ≥ 25 г минимум три дня за последнюю неделю.",
        evaluator=_eval_fiber_fan,
    ),
    Badge(
        code="omega_balance",
        title="Баланс омега",
        description="Баланс омега-6/омега-3 в диапазоне 2–5 не менее трёх дней за неделю.",
        evaluator=_eval_omega_balance,
    ),
    Badge(
        code="hero_return",
        title="Возвращение героя",
        description="После перерыва вернулся к плану и держит новую серию не менее трёх дней.",
        evaluator=_eval_hero_return,
    ),
]


def evaluate_badges(session: Session, client_id: int) -> List[Dict[str, object]]:
    """Evaluate all badges for the client and return structured progress."""

    ctx = _build_context(session, client_id)
    results: List[Dict[str, object]] = []
    for badge in BADGES:
        evaluation = badge.evaluator(ctx)
        results.append(
            {
                "code": badge.code,
                "title": badge.title,
                "description": badge.description,
                "earned": bool(evaluation.earned),
                "progress": float(max(0.0, min(1.0, evaluation.progress or 0.0))),
                "meta": evaluation.meta or {},
            }
        )
    return results

