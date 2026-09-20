import datetime
import enum

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, Integer, String, Text, create_engine
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = "sqlite:///./hotwheels.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class EventType(str, enum.Enum):
    NEW = "NEW"
    RESTOCK = "RESTOCK"
    OOS = "OOS"
    PRICE_DROP = "PRICE_DROP"


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    price = Column(Float, nullable=True)
    original_price = Column(Float, nullable=True)
    url = Column(String, nullable=False)
    image_url = Column(String, nullable=True)
    is_available = Column(Boolean, default=True)
    series = Column(String, nullable=True)       # e.g. Premium / Mainline / Car Culture
    last_checked = Column(DateTime, default=datetime.datetime.utcnow)
    first_seen = Column(DateTime, default=datetime.datetime.utcnow)
    pincode_deliverable = Column(Boolean, nullable=True)


class StockEvent(Base):
    __tablename__ = "stock_events"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(String, index=True, nullable=False)
    product_name = Column(String, nullable=True)
    event_type = Column(String, nullable=False)  # EventType values
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    price = Column(Float, nullable=True)
    notified = Column(Boolean, default=False)
    details = Column(Text, nullable=True)


class AppSettings(Base):
    """Key-value store for runtime-editable settings."""
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(Text, nullable=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
