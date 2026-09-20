import datetime
from typing import Optional

from pydantic import BaseModel


# ── Product schemas ───────────────────────────────────────────────────────────

class ProductBase(BaseModel):
    product_id: str
    name: str
    price: Optional[float] = None
    original_price: Optional[float] = None
    url: str
    image_url: Optional[str] = None
    is_available: bool = True
    series: Optional[str] = None
    pincode_deliverable: Optional[bool] = None


class ProductOut(ProductBase):
    id: int
    last_checked: datetime.datetime
    first_seen: datetime.datetime

    class Config:
        from_attributes = True


# ── Stock event schemas ───────────────────────────────────────────────────────

class StockEventOut(BaseModel):
    id: int
    product_id: str
    product_name: Optional[str]
    event_type: str
    timestamp: datetime.datetime
    price: Optional[float]
    notified: bool
    details: Optional[str]

    class Config:
        from_attributes = True


# ── Settings schemas ──────────────────────────────────────────────────────────

class SettingsIn(BaseModel):
    pincode: Optional[str] = None
    notifications_enabled: Optional[bool] = None
    email_enabled: Optional[bool] = None
    email_to: Optional[str] = None
    telegram_enabled: Optional[bool] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    check_interval_minutes: Optional[int] = None


class SettingsOut(BaseModel):
    pincode: str
    notifications_enabled: bool
    email_enabled: bool
    email_to: str
    telegram_enabled: bool
    check_interval_minutes: int


# ── Response schemas ──────────────────────────────────────────────────────────

class CheckResponse(BaseModel):
    message: str
    products_found: int
    new_products: int
    restocked: int
    out_of_stock: int


class StatsOut(BaseModel):
    total_products: int
    in_stock: int
    out_of_stock: int
    deliverable: int
    last_check: Optional[datetime.datetime]
    next_check: Optional[datetime.datetime]
    total_events: int
