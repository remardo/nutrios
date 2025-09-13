from sqlalchemy import Column, Integer, Float, String, Boolean, ForeignKey, DateTime, JSON, UniqueConstraint
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
