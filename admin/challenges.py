"""Challenge and habit tracking helpers."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from .models import (
    ChallengeDefinition,
    Client,
    ClientChallenge,
    ClientChallengeProgress,
    ClientTargets,
    DailyHabitLog,
    Meal,
)
from .analysis import df_meals, summary_macros


PERIOD_LENGTHS = {"daily": 1, "weekly": 7, "monthly": 30}


DEFAULT_CHALLENGES: List[Dict] = [
    {
        "code": "water_daily",
        "name": "Вода в норме",
        "description": "Выпивайте больше чистой воды в течение дня.",
        "period": "daily",
        "metric": "water_ml",
        "config": {"baseline_days": 14, "default_target": 1800, "unit": "мл"},
    },
    {
        "code": "log_meals_daily",
        "name": "Лог всех приёмов",
        "description": "Отмечайте каждый приём пищи в боте без пропусков.",
        "period": "daily",
        "metric": "logged_meals",
        "config": {"baseline_days": 14, "min_meals": 3, "unit": "шт."},
    },
    {
        "code": "protein_balance_weekly",
        "name": "Баланс белка",
        "description": "Попадите в коридор по белку в течение недели.",
        "period": "weekly",
        "metric": "protein_balance",
        "config": {"baseline_weeks": 4, "tolerance_pct": 0.20, "unit": "дней"},
    },
    {
        "code": "no_sweets_weekly",
        "name": "5 дней без сладкого",
        "description": "Минимум пять дней недели без десертов и сладостей.",
        "period": "weekly",
        "metric": "sweet_free_days",
        "config": {"baseline_weeks": 4, "minimum_days": 5, "unit": "дней"},
    },
    {
        "code": "vegetables_weekly",
        "name": "Овощной минимум 400 г/д",
        "description": "Съедайте не менее 400 г овощей в день как минимум несколько раз за неделю.",
        "period": "weekly",
        "metric": "vegetables_g",
        "config": {"baseline_weeks": 4, "daily_min": 400, "unit": "дней"},
    },
    {
        "code": "streak_21_30",
        "name": "Стрик 21/30",
        "description": "Выполняйте план хотя бы 21 день за последние 30.",
        "period": "monthly",
        "metric": "compliance_days",
        "config": {"window_days": 30, "required_days": 21, "unit": "дней"},
    },
    {
        "code": "steps_10k_monthly",
        "name": "10k шагов в 20 днях",
        "description": "Пройдите 10 000 шагов не менее чем в 20 днях месяца.",
        "period": "monthly",
        "metric": "steps",
        "config": {"baseline_days": 30, "daily_target": 10000, "required_days": 20, "unit": "дней"},
    },
]


def ensure_default_definitions(db: Session) -> List[ChallengeDefinition]:
    """Ensure default challenge definitions are present in DB."""

    existing = {c.code: c for c in db.query(ChallengeDefinition).all()}
    out: List[ChallengeDefinition] = []
    for item in DEFAULT_CHALLENGES:
        row = existing.get(item["code"])
        if not row:
            row = ChallengeDefinition(
                code=item["code"],
                name=item["name"],
                description=item.get("description"),
                period=item["period"],
                metric=item["metric"],
                config=item.get("config") or {},
            )
            db.add(row)
            db.flush()
        else:
            row.name = item["name"]
            row.description = item.get("description")
            row.period = item["period"]
            row.metric = item["metric"]
            row.config = item.get("config") or {}
        out.append(row)
    db.commit()
    return out


def _to_int(value) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except Exception:
            return 0


def _dates_range(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur = cur + timedelta(days=1)


def _client_targets(db: Session, client_id: int) -> Optional[ClientTargets]:
    return db.query(ClientTargets).filter_by(client_id=client_id).first()


def recalc_daily_log_from_meals(db: Session, client_id: int, day: date) -> DailyHabitLog:
    """Recalculate DailyHabitLog aggregations from Meal entries for the given day."""

    if isinstance(day, datetime):
        day = day.date()

    start_dt = datetime.combine(day, datetime.min.time())
    end_dt = start_dt + timedelta(days=1)

    meals: List[Meal] = (
        db.query(Meal)
        .filter(Meal.client_id == client_id, Meal.captured_at >= start_dt, Meal.captured_at < end_dt)
        .all()
    )

    auto_water = 0
    auto_vegetables = 0
    auto_sweets = False
    total_kcal = 0
    total_protein = 0
    total_fat = 0
    total_carbs = 0

    for m in meals:
        total_kcal += _to_int(m.kcal)
        total_protein += _to_int(m.protein_g)
        total_fat += _to_int(m.fat_g)
        total_carbs += _to_int(m.carbs_g)
        extras = m.extras or {}
        auto_water += _to_int(extras.get("water_ml"))
        auto_vegetables += _to_int(extras.get("vegetables_g"))
        sweet_flag = extras.get("is_sweet") or extras.get("had_sweets") or extras.get("sweet")
        if isinstance(sweet_flag, str):
            sweet_flag = sweet_flag.lower() in {"true", "1", "yes", "да"}
        auto_sweets = auto_sweets or bool(sweet_flag)

    log = (
        db.query(DailyHabitLog)
        .filter(DailyHabitLog.client_id == client_id, DailyHabitLog.date == day)
        .first()
    )
    if not log:
        log = DailyHabitLog(client_id=client_id, date=day)
        db.add(log)

    extras = log.extras or {}
    extras.setdefault("sources", {})
    extras.setdefault("sources", {}).update({"auto_water_ml": auto_water, "auto_vegetables_g": auto_vegetables})
    extras["auto_had_sweets"] = auto_sweets

    manual_water = extras.get("manual_water_ml")
    manual_vegetables = extras.get("manual_vegetables_g")
    manual_sweets = extras.get("manual_had_sweets")

    log.water_ml = _to_int(manual_water) if manual_water is not None else auto_water
    log.vegetables_g = _to_int(manual_vegetables) if manual_vegetables is not None else auto_vegetables
    log.had_sweets = bool(manual_sweets) if manual_sweets is not None else auto_sweets
    log.logged_meals = len(meals)
    log.total_kcal = total_kcal
    log.protein_g = total_protein
    log.fat_g = total_fat
    log.carbs_g = total_carbs
    log.extras = extras

    return log


def update_daily_log_manual(db: Session, client_id: int, day: date, **kwargs) -> DailyHabitLog:
    if isinstance(day, datetime):
        day = day.date()

    log = (
        db.query(DailyHabitLog)
        .filter(DailyHabitLog.client_id == client_id, DailyHabitLog.date == day)
        .first()
    )
    if not log:
        log = DailyHabitLog(client_id=client_id, date=day)
        db.add(log)

    extras = log.extras or {}
    extras.setdefault("sources", {})

    if "water_ml" in kwargs and kwargs["water_ml"] is not None:
        extras["manual_water_ml"] = _to_int(kwargs["water_ml"])
    if "vegetables_g" in kwargs and kwargs["vegetables_g"] is not None:
        extras["manual_vegetables_g"] = _to_int(kwargs["vegetables_g"])
    if "had_sweets" in kwargs and kwargs["had_sweets"] is not None:
        extras["manual_had_sweets"] = bool(kwargs["had_sweets"])
    if "steps" in kwargs and kwargs["steps"] is not None:
        log.steps = _to_int(kwargs["steps"])

    log.extras = extras

    # Re-sync derived values after manual change
    log = recalc_daily_log_from_meals(db, client_id, day)

    manual_steps = kwargs.get("steps")
    if manual_steps is not None:
        log.steps = _to_int(manual_steps)

    return log


def _average(values: Iterable[float]) -> float:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 0.0
    return float(sum(vals)) / len(vals)


def _count_sweet_free_days(logs: Iterable[DailyHabitLog]) -> int:
    return sum(1 for log in logs if log and not log.had_sweets)


def _count_days_with_threshold(logs: Iterable[DailyHabitLog], attr: str, threshold: float) -> int:
    cnt = 0
    for log in logs:
        if not log:
            continue
        try:
            val = getattr(log, attr)
        except AttributeError:
            val = None
        if val is None:
            continue
        try:
            numeric = float(val)
        except (TypeError, ValueError):
            continue
        if numeric >= threshold:
            cnt += 1
    return cnt


def _protein_success_days(logs: Dict[date, DailyHabitLog], targets: Optional[ClientTargets], tolerance: float, start: date, end: date) -> Tuple[int, int]:
    if not targets:
        return 0, len(list(_dates_range(start, end)))
    threshold = targets.protein_target_g or 0
    total_days = 0
    success = 0
    for d in _dates_range(start, end):
        total_days += 1
        log = logs.get(d)
        if not log or not threshold:
            continue
        protein = log.protein_g or 0
        allowed_delta = max(10.0, threshold * tolerance)
        if abs(protein - threshold) <= allowed_delta:
            success += 1
    return success, total_days


def _compliance_days_for_period(db: Session, client_id: int, start: date, end: date, targets: Optional[ClientTargets]) -> int:
    df = df_meals(db, client_id)
    if df is None or getattr(df, "empty", True):
        return 0
    agg = summary_macros(df, freq="D")
    if agg is None or getattr(agg, "empty", True):
        return 0

    tolerance = {
        "kcal_pct": 0.10,
        "protein_pct": 0.20,
        "fat_pct": 0.20,
        "carbs_pct": 0.20,
    }
    if targets and targets.tolerances:
        tolerance.update({k: v for k, v in targets.tolerances.items() if isinstance(v, (int, float)) and k.endswith("_pct")})
    min_g = {"p": 10.0, "f": 10.0, "c": 15.0}
    if targets and targets.tolerances and isinstance(targets.tolerances.get("min_g"), dict):
        min_g.update(targets.tolerances["min_g"])

    def in_range(row, col, target, pct, minimum):
        try:
            val = float(row[col])
        except Exception:
            return False
        if target is None:
            return False
        allowed = max(float(minimum), float(target) * float(pct))
        return abs(val - float(target)) <= allowed

    days = 0
    for _, row in agg.iterrows():
        ts = row["captured_at"]
        cur_date = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])
        if cur_date < start or cur_date > end:
            continue
        kcal_ok = in_range(row, "kcal", getattr(targets, "kcal_target", None), tolerance["kcal_pct"], min_g.get("kcal", 0))
        protein_ok = in_range(row, "protein_g", getattr(targets, "protein_target_g", None), tolerance["protein_pct"], min_g["p"])
        fat_ok = in_range(row, "fat_g", getattr(targets, "fat_target_g", None), tolerance["fat_pct"], min_g["f"])
        carbs_ok = in_range(row, "carbs_g", getattr(targets, "carbs_target_g", None), tolerance["carbs_pct"], min_g["c"])
        if kcal_ok and protein_ok and fat_ok and carbs_ok:
            days += 1
    return days


def _baseline_for_challenge(db: Session, client_id: int, definition: ChallengeDefinition) -> float:
    code = definition.code
    cfg = definition.config or {}
    today = date.today()

    if code == "water_daily":
        days = cfg.get("baseline_days", 14)
        logs = (
            db.query(DailyHabitLog)
            .filter(
                DailyHabitLog.client_id == client_id,
                DailyHabitLog.date >= today - timedelta(days=days),
                DailyHabitLog.date < today,
            )
            .order_by(DailyHabitLog.date)
            .all()
        )
        return _average(log.water_ml for log in logs if log.water_ml)

    if code == "log_meals_daily":
        days = cfg.get("baseline_days", 14)
        logs = (
            db.query(DailyHabitLog)
            .filter(
                DailyHabitLog.client_id == client_id,
                DailyHabitLog.date >= today - timedelta(days=days),
                DailyHabitLog.date < today,
            )
            .order_by(DailyHabitLog.date)
            .all()
        )
        return _average(log.logged_meals for log in logs if log.logged_meals)

    if code == "protein_balance_weekly":
        weeks = cfg.get("baseline_weeks", 4)
        start = today - timedelta(days=7 * weeks)
        logs = (
            db.query(DailyHabitLog)
            .filter(DailyHabitLog.client_id == client_id, DailyHabitLog.date >= start, DailyHabitLog.date < today)
            .order_by(DailyHabitLog.date)
            .all()
        )
        logs_by_date = {log.date: log for log in logs}
        targets = _client_targets(db, client_id)
        tolerance = cfg.get("tolerance_pct", 0.2)
        total_success = 0
        weeks_seen = 0
        cur = start
        while cur < today:
            end = cur + timedelta(days=6)
            if end > today:
                end = today
            success, total = _protein_success_days(logs_by_date, targets, tolerance, cur, end)
            if total:
                total_success += success
                weeks_seen += 1
            cur = end + timedelta(days=1)
        return float(total_success / weeks_seen) if weeks_seen else 0.0

    if code == "no_sweets_weekly":
        weeks = cfg.get("baseline_weeks", 4)
        logs = (
            db.query(DailyHabitLog)
            .filter(
                DailyHabitLog.client_id == client_id,
                DailyHabitLog.date >= today - timedelta(days=7 * weeks),
                DailyHabitLog.date < today,
            )
            .order_by(DailyHabitLog.date)
            .all()
        )
        if not logs:
            return 0.0
        return _average(_count_sweet_free_days(logs[i:i + 7]) for i in range(0, len(logs), 7))

    if code == "vegetables_weekly":
        weeks = cfg.get("baseline_weeks", 4)
        logs = (
            db.query(DailyHabitLog)
            .filter(
                DailyHabitLog.client_id == client_id,
                DailyHabitLog.date >= today - timedelta(days=7 * weeks),
                DailyHabitLog.date < today,
            )
            .order_by(DailyHabitLog.date)
            .all()
        )
        if not logs:
            return 0.0
        daily_min = cfg.get("daily_min", 400)
        chunks = [logs[i:i + 7] for i in range(0, len(logs), 7)]
        counts = [_count_days_with_threshold(chunk, "vegetables_g", daily_min) for chunk in chunks]
        return _average(counts)

    if code == "streak_21_30":
        window = cfg.get("window_days", 30)
        start = today - timedelta(days=window)
        targets = _client_targets(db, client_id)
        return float(_compliance_days_for_period(db, client_id, start, today, targets))

    if code == "steps_10k_monthly":
        days = cfg.get("baseline_days", 30)
        logs = (
            db.query(DailyHabitLog)
            .filter(
                DailyHabitLog.client_id == client_id,
                DailyHabitLog.date >= today - timedelta(days=days),
                DailyHabitLog.date < today,
            )
            .order_by(DailyHabitLog.date)
            .all()
        )
        threshold = cfg.get("daily_target", 10000)
        return float(_count_days_with_threshold(logs, "steps", threshold))

    return 0.0


def _difficulty_factor(defn: ChallengeDefinition, override: Optional[float] = None) -> float:
    if override is not None:
        return float(max(defn.difficulty_min_pct or 0.05, min(defn.difficulty_max_pct or 0.15, override)))
    low = defn.difficulty_min_pct or 0.05
    high = defn.difficulty_max_pct or 0.15
    if high < low:
        high = low
    return round((low + high) / 2.0, 3)


def _target_for_challenge(defn: ChallengeDefinition, baseline: float, factor: float) -> Tuple[float, Dict]:
    cfg = defn.config or {}
    meta: Dict[str, float] = {}
    if defn.code == "water_daily":
        base = max(cfg.get("default_target", 1800), baseline or 0)
        target = round(base * (1.0 + factor))
        meta["unit"] = cfg.get("unit", "мл")
        return float(target), meta
    if defn.code == "log_meals_daily":
        base = max(cfg.get("min_meals", 3), math.ceil(baseline) if baseline else cfg.get("min_meals", 3))
        target = max(cfg.get("min_meals", 3), math.ceil(base * (1.0 + factor)))
        meta["unit"] = cfg.get("unit", "шт.")
        return float(target), meta
    if defn.code == "protein_balance_weekly":
        base = baseline or 0
        target = max(3, min(7, math.ceil(base * (1.0 + factor))))
        meta["unit"] = cfg.get("unit", "дней")
        meta["tolerance_pct"] = cfg.get("tolerance_pct", 0.2)
        return float(target), meta
    if defn.code == "no_sweets_weekly":
        base = max(cfg.get("minimum_days", 5), baseline or 0)
        target = max(cfg.get("minimum_days", 5), min(7, math.ceil(base * (1.0 + factor))))
        meta["unit"] = cfg.get("unit", "дней")
        return float(target), meta
    if defn.code == "vegetables_weekly":
        base_days = baseline or 3
        required_days = min(7, max(3, math.ceil(base_days * (1.0 + factor))))
        meta["daily_requirement"] = cfg.get("daily_min", 400)
        meta["unit"] = cfg.get("unit", "дней")
        return float(required_days), meta
    if defn.code == "streak_21_30":
        base = max(cfg.get("required_days", 21), baseline or 0)
        target = max(cfg.get("required_days", 21), min(cfg.get("window_days", 30), math.ceil(base * (1.0 + factor))))
        meta["unit"] = cfg.get("unit", "дней")
        meta["window_days"] = cfg.get("window_days", 30)
        return float(target), meta
    if defn.code == "steps_10k_monthly":
        base_days = max(cfg.get("required_days", 20), baseline or 0)
        target_days = max(cfg.get("required_days", 20), min(cfg.get("window_days", 30), math.ceil(base_days * (1.0 + factor))))
        meta["unit"] = cfg.get("unit", "дней")
        meta["daily_steps_target"] = cfg.get("daily_target", 10000)
        return float(target_days), meta
    return float(baseline or 0), meta


def assign_challenge(db: Session, client: Client, definition: ChallengeDefinition, start: Optional[date] = None, factor: Optional[float] = None) -> ClientChallenge:
    start = start or date.today()
    period_days = PERIOD_LENGTHS.get(definition.period, 7)
    end = start + timedelta(days=period_days - 1)

    baseline = _baseline_for_challenge(db, client.id, definition)
    difficulty = _difficulty_factor(definition, factor)
    target_value, meta = _target_for_challenge(definition, baseline, difficulty)

    challenge = ClientChallenge(
        client_id=client.id,
        challenge_definition_id=definition.id,
        status="active",
        start_date=start,
        end_date=end,
        baseline_value=float(baseline or 0),
        target_value=target_value,
        difficulty_factor=difficulty,
        meta=meta,
    )
    db.add(challenge)
    db.flush()
    recalculate_challenge_progress(db, challenge)
    db.commit()
    db.refresh(challenge)
    return challenge


def _serialize_challenge(challenge: ClientChallenge, progress: Optional[ClientChallengeProgress] = None) -> Dict:
    definition = challenge.definition
    data = {
        "id": challenge.id,
        "code": definition.code if definition else None,
        "name": definition.name if definition else None,
        "description": definition.description if definition else "",
        "period": definition.period if definition else None,
        "status": challenge.status,
        "start_date": challenge.start_date.isoformat() if challenge.start_date else None,
        "end_date": challenge.end_date.isoformat() if challenge.end_date else None,
        "baseline_value": challenge.baseline_value,
        "target_value": challenge.target_value,
        "difficulty_factor": challenge.difficulty_factor,
        "meta": challenge.meta or {},
    }
    if progress:
        data["progress"] = {
            "value": progress.value,
            "target_value": progress.target_value,
            "completed": progress.completed,
            "period_start": progress.period_start.isoformat() if progress.period_start else None,
            "period_end": progress.period_end.isoformat() if progress.period_end else None,
            "meta": progress.meta or {},
        }
    return data


def serialize_challenge(challenge: ClientChallenge, progress: Optional[ClientChallengeProgress] = None) -> Dict:
    """Public wrapper for challenge serialization."""
    return _serialize_challenge(challenge, progress)


def list_available_challenges(db: Session, client: Client) -> List[Dict]:
    ensure_default_definitions(db)
    active_codes = {
        c.definition.code
        for c in db.query(ClientChallenge)
        .filter(ClientChallenge.client_id == client.id, ClientChallenge.status == "active")
        .all()
        if c.definition
    }
    options = []
    for definition in db.query(ChallengeDefinition).all():
        preview_baseline = _baseline_for_challenge(db, client.id, definition)
        factor = _difficulty_factor(definition)
        target_value, meta = _target_for_challenge(definition, preview_baseline, factor)
        options.append(
            {
                "code": definition.code,
                "name": definition.name,
                "description": definition.description,
                "period": definition.period,
                "metric": definition.metric,
                "already_active": definition.code in active_codes,
                "suggested_baseline": preview_baseline,
                "suggested_target": target_value,
                "difficulty_factor": factor,
                "meta": meta,
            }
        )
    return options


def _progress_record(db: Session, challenge: ClientChallenge) -> ClientChallengeProgress:
    row = (
        db.query(ClientChallengeProgress)
        .filter(ClientChallengeProgress.client_challenge_id == challenge.id)
        .order_by(ClientChallengeProgress.created_at.desc())
        .first()
    )
    if not row:
        row = ClientChallengeProgress(
            client_challenge_id=challenge.id,
            period_start=challenge.start_date,
            period_end=challenge.end_date,
            target_value=challenge.target_value,
        )
        db.add(row)
        db.flush()
    return row


def recalculate_challenge_progress(db: Session, challenge: ClientChallenge) -> ClientChallengeProgress:
    definition = challenge.definition
    if not definition:
        raise ValueError("Challenge definition missing")

    cfg = definition.config or {}
    start = challenge.start_date
    end = challenge.end_date
    logs = (
        db.query(DailyHabitLog)
        .filter(DailyHabitLog.client_id == challenge.client_id, DailyHabitLog.date >= start, DailyHabitLog.date <= end)
        .all()
    )
    logs_by_date = {log.date: log for log in logs}
    progress = _progress_record(db, challenge)

    value = 0.0
    meta: Dict[str, object] = {}
    completed = False

    if definition.code == "water_daily":
        log = logs_by_date.get(start)
        value = float(log.water_ml if log else 0)
        meta.update({"unit": cfg.get("unit", "мл")})
        completed = value >= (challenge.target_value or 0)

    elif definition.code == "log_meals_daily":
        log = logs_by_date.get(start)
        value = float(log.logged_meals if log else 0)
        meta.update({"unit": cfg.get("unit", "шт.")})
        completed = value >= (challenge.target_value or 0)

    elif definition.code == "protein_balance_weekly":
        targets = _client_targets(db, challenge.client_id)
        tolerance = cfg.get("tolerance_pct", 0.2)
        success_days, total_days = _protein_success_days(logs_by_date, targets, tolerance, start, end)
        value = float(success_days)
        meta.update({"total_days": total_days, "unit": cfg.get("unit", "дней")})
        completed = value >= (challenge.target_value or 0)

    elif definition.code == "no_sweets_weekly":
        value = float(_count_sweet_free_days(logs))
        meta.update({"unit": cfg.get("unit", "дней")})
        completed = value >= (challenge.target_value or 0)

    elif definition.code == "vegetables_weekly":
        requirement = challenge.meta.get("daily_requirement") if challenge.meta else cfg.get("daily_min", 400)
        value = float(_count_days_with_threshold(logs, "vegetables_g", requirement))
        meta.update({"daily_requirement": requirement, "unit": cfg.get("unit", "дней")})
        completed = value >= (challenge.target_value or 0)

    elif definition.code == "streak_21_30":
        targets = _client_targets(db, challenge.client_id)
        compliance = _compliance_days_for_period(db, challenge.client_id, start, end, targets)
        value = float(compliance)
        meta.update({"unit": cfg.get("unit", "дней"), "window_days": cfg.get("window_days", 30)})
        completed = value >= (challenge.target_value or 0)

    elif definition.code == "steps_10k_monthly":
        threshold = challenge.meta.get("daily_steps_target") if challenge.meta else cfg.get("daily_target", 10000)
        value = float(_count_days_with_threshold(logs, "steps", threshold))
        meta.update({"daily_steps_target": threshold, "unit": cfg.get("unit", "дней")})
        completed = value >= (challenge.target_value or 0)

    progress.value = value
    progress.target_value = challenge.target_value
    progress.completed = completed
    progress.period_start = challenge.start_date
    progress.period_end = challenge.end_date
    progress.meta = meta

    today = date.today()
    if completed:
        challenge.status = "completed"
    elif today > challenge.end_date:
        challenge.status = "failed"
    else:
        challenge.status = challenge.status or "active"

    db.flush()
    return progress


def active_challenges_with_progress(db: Session, client: Client) -> List[Dict]:
    rows = (
        db.query(ClientChallenge)
        .filter(ClientChallenge.client_id == client.id, ClientChallenge.status.in_(["active", "completed"]))
        .order_by(ClientChallenge.start_date.desc())
        .all()
    )
    out = []
    for row in rows:
        progress = recalculate_challenge_progress(db, row)
        out.append(serialize_challenge(row, progress))
    db.commit()
    return out


def refresh_all_active(db: Session, client_id: int) -> None:
    rows = (
        db.query(ClientChallenge)
        .filter(ClientChallenge.client_id == client_id, ClientChallenge.status.in_(["active", "completed"]))
        .all()
    )
    for row in rows:
        recalculate_challenge_progress(db, row)


def manual_factor_from_payload(payload: Dict[str, object]) -> Optional[float]:
    if not payload:
        return None
    try:
        value = payload.get("difficulty_factor")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

