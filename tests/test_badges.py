from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from admin.api import refresh_client_badges
from admin.badges import evaluate_badges
from admin.db import Base
from admin.models import Client, Meal, ClientBadgeAward


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    TestingSession = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    db_session = TestingSession()
    try:
        yield db_session
    finally:
        db_session.close()


def _create_client(session, telegram_user_id: int = 100) -> Client:
    client = Client(telegram_user_id=telegram_user_id)
    session.add(client)
    session.commit()
    session.refresh(client)
    return client


def _add_meal(
    session,
    client_id: int,
    captured_at: datetime,
    kcal: int = 2000,
    protein: int = 100,
    fat: int = 70,
    carbs: int = 250,
    fiber: float = 30.0,
    omega6: float = 6.0,
    omega3: float = 3.0,
):
    extras = {
        "fiber": {"total": fiber},
        "fats": {"omega6": omega6, "omega3": omega3},
    }
    meal = Meal(
        client_id=client_id,
        message_id=int(captured_at.timestamp()),
        title="Test",
        portion_g=300,
        confidence=100,
        kcal=kcal,
        protein_g=protein,
        fat_g=fat,
        carbs_g=carbs,
        flags={},
        micronutrients=[],
        assumptions=[],
        extras=extras,
        source_type="test",
        captured_at=captured_at,
        created_at=captured_at,
        updated_at=captured_at,
    )
    session.add(meal)


def test_badge_conditions_met_and_awarded(session):
    client = _create_client(session, telegram_user_id=200)
    base = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)

    # Initial compliant streak (5 days)
    for day in range(5):
        _add_meal(session, client.id, base + timedelta(days=day))

    # New compliant streak after three skipped days (7 days)
    for day in range(8, 15):
        _add_meal(session, client.id, base + timedelta(days=day))

    session.commit()

    statuses = evaluate_badges(session, client.id)
    by_code = {row["code"]: row for row in statuses}

    assert by_code["first_meal"]["earned"] is True
    assert by_code["steady_week"]["earned"] is True
    assert by_code["fiber_fan"]["earned"] is True
    assert by_code["omega_balance"]["earned"] is True
    assert by_code["hero_return"]["earned"] is True

    refreshed = refresh_client_badges(session, client.id)
    assert all(row["earned"] for row in refreshed)
    award_codes = {award.badge_code for award in session.query(ClientBadgeAward).all()}
    assert award_codes == set(by_code.keys())

    # Second refresh should not create duplicates
    second = refresh_client_badges(session, client.id)
    assert len(session.query(ClientBadgeAward).all()) == len(by_code)
    refreshed_map = {row["code"]: row for row in refreshed}
    second_map = {row["code"]: row for row in second}
    assert refreshed_map.keys() == second_map.keys()
    for code in refreshed_map:
        assert refreshed_map[code]["earned"] == second_map[code]["earned"]
        assert refreshed_map[code]["progress"] == second_map[code]["progress"]
        assert refreshed_map[code]["latest_award_at"] == second_map[code]["latest_award_at"]


def test_badge_conditions_not_met(session):
    client = _create_client(session, telegram_user_id=300)

    statuses = evaluate_badges(session, client.id)
    by_code = {row["code"]: row for row in statuses}

    assert by_code["first_meal"]["earned"] is False
    assert by_code["steady_week"]["earned"] is False
    assert by_code["fiber_fan"]["earned"] is False
    assert by_code["omega_balance"]["earned"] is False
    assert by_code["hero_return"]["earned"] is False

    refreshed = refresh_client_badges(session, client.id)
    assert all(row["earned"] is False for row in refreshed)
    assert session.query(ClientBadgeAward).count() == 0
