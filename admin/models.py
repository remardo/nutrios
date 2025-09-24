from sqlalchemy import Column, Integer, Float, String, Boolean, ForeignKey, DateTime, JSON, UniqueConstraint, Date
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .db import Base
from sqlalchemy import UniqueConstraint

class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(Integer, unique=True, index=True, nullable=False)
    telegram_username = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    meals = relationship("Meal", back_populates="client")

class Meal(Base):
    __tablename__ = "meals"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True)
    message_id = Column(Integer, index=True)         # id ботовского сообщения (для апдейтов)
    title = Column(String, default="Блюдо")
    portion_g = Column(Integer)
    confidence = Column(Integer)
    kcal = Column(Integer)
    protein_g = Column(Integer)
    fat_g = Column(Integer)
    carbs_g = Column(Integer)
    flags = Column(JSON)             # {vegetarian, vegan, glutenfree, lactosefree}
    micronutrients = Column(JSON)    # ["Витамин C — 30 mg",...]
    assumptions = Column(JSON)
    extras = Column(JSON)            # расширенные поля: жиры (sat/mono/poly/trans, omega6/omega3), клетчатка (soluble/insoluble)
    source_type = Column(String)     # image|text
    image_path = Column(String, nullable=True)
    captured_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    client = relationship("Client", back_populates="meals")
    __table_args__ = (UniqueConstraint('client_id', 'message_id', name='uq_client_message'),)


class ClientTargets(Base):
    __tablename__ = "client_targets"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, unique=True)
    kcal_target = Column(Integer, default=2000)
    protein_target_g = Column(Integer, default=100)
    fat_target_g = Column(Integer, default=70)
    carbs_target_g = Column(Integer, default=250)
    profile = Column(JSON, nullable=True)    # questionnaire answers (detailed)
    plan = Column(JSON, nullable=True)       # generated nutrition plan summary
    tolerances = Column(JSON, nullable=True) # {kcal_pct:0.1, protein_pct:0.2, fat_pct:0.2, carbs_pct:0.2, min_g:{p:10,f:10,c:15}}
    notifications = Column(JSON, nullable=True) # preferences: {reminders:true, time:"08:00", tips:true}
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ChallengeDefinition(Base):
    __tablename__ = "challenge_definitions"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    period = Column(String, nullable=False)  # daily|weekly|monthly
    metric = Column(String, nullable=False)
    config = Column(JSON, nullable=True)
    difficulty_min_pct = Column(Float, default=0.05)
    difficulty_max_pct = Column(Float, default=0.15)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ClientChallenge(Base):
    __tablename__ = "client_challenges"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, nullable=False)
    challenge_definition_id = Column(Integer, ForeignKey("challenge_definitions.id"), nullable=False)
    status = Column(String, default="active")  # active|completed|failed|archived
    assigned_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    baseline_value = Column(Float, nullable=True)
    target_value = Column(Float, nullable=True)
    difficulty_factor = Column(Float, nullable=True)
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    definition = relationship("ChallengeDefinition")
    client = relationship("Client")


class ClientChallengeProgress(Base):
    __tablename__ = "client_challenge_progress"
    id = Column(Integer, primary_key=True)
    client_challenge_id = Column(Integer, ForeignKey("client_challenges.id"), index=True, nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    value = Column(Float, nullable=True)
    target_value = Column(Float, nullable=True)
    completed = Column(Boolean, default=False)
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    challenge = relationship("ClientChallenge")


class DailyHabitLog(Base):
    __tablename__ = "daily_habit_logs"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, nullable=False)
    date = Column(Date, nullable=False)
    water_ml = Column(Integer, default=0)
    steps = Column(Integer, default=0)
    vegetables_g = Column(Integer, default=0)
    had_sweets = Column(Boolean, default=False)
    logged_meals = Column(Integer, default=0)
    total_kcal = Column(Integer, default=0)
    protein_g = Column(Integer, default=0)
    fat_g = Column(Integer, default=0)
    carbs_g = Column(Integer, default=0)
    extras = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    client = relationship("Client")
    __table_args__ = (UniqueConstraint('client_id', 'date', name='uq_daily_habit_client_date'),)
