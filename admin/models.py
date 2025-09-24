from sqlalchemy import (
    Column,
    Integer,
    Float,
    String,
    Boolean,
    ForeignKey,
    DateTime,
    JSON,
    UniqueConstraint,
    Date,
)
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .db import Base

class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(Integer, unique=True, index=True, nullable=False)
    telegram_username = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    meals = relationship("Meal", back_populates="client")
    daily_metrics = relationship(
        "ClientDailyMetrics", back_populates="client", cascade="all, delete-orphan"
    )
    events = relationship(
        "ClientEvents", back_populates="client", cascade="all, delete-orphan"
    )

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


class ClientDailyMetrics(Base):
    __tablename__ = "client_daily_metrics"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, nullable=False)
    date = Column(Date, nullable=False, index=True)
    water_goal_met = Column(Boolean, default=False)
    steps = Column(Integer, nullable=True)
    protein_goal_met = Column(Boolean, default=False)
    fiber_goal_met = Column(Boolean, default=False)
    breakfast_logged_before_10 = Column(Boolean, default=False)
    dinner_logged = Column(Boolean, default=False)
    new_recipe_logged = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    client = relationship("Client", back_populates="daily_metrics")
    __table_args__ = (
        UniqueConstraint("client_id", "date", name="uq_client_daily_metrics"),
    )


class ClientEvents(Base):
    __tablename__ = "client_events"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), index=True, nullable=False)
    occurred_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    occurred_on = Column(Date, default=lambda: datetime.now(timezone.utc).date(), nullable=False, index=True)
    type = Column(String, nullable=False, index=True)
    payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    client = relationship("Client", back_populates="events")
    __table_args__ = (
        UniqueConstraint("client_id", "occurred_on", "type", name="uq_client_event_per_day"),
    )
