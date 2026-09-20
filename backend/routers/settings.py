from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import AppSettings, get_db
from backend.models import SettingsIn, SettingsOut
from backend.config import settings as env_settings
from backend import scheduler as sched_module

router = APIRouter(prefix="/api/settings", tags=["settings"])

DEFAULTS = {
    "pincode": env_settings.default_pincode,
    "notifications_enabled": "true",
    "email_enabled": str(env_settings.email_enabled).lower(),
    "email_to": env_settings.email_to,
    "telegram_enabled": str(env_settings.telegram_enabled).lower(),
    "check_interval_minutes": str(env_settings.check_interval_minutes),
}


def _get(db: Session, key: str) -> str:
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    return row.value if row and row.value is not None else DEFAULTS.get(key, "")


def _set(db: Session, key: str, value: str) -> None:
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AppSettings(key=key, value=value))


@router.get("", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return SettingsOut(
        pincode=_get(db, "pincode"),
        notifications_enabled=_get(db, "notifications_enabled") == "true",
        email_enabled=_get(db, "email_enabled") == "true",
        email_to=_get(db, "email_to"),
        telegram_enabled=_get(db, "telegram_enabled") == "true",
        check_interval_minutes=int(_get(db, "check_interval_minutes") or "10"),
    )


@router.put("", response_model=SettingsOut)
def update_settings(payload: SettingsIn, db: Session = Depends(get_db)):
    if payload.pincode is not None:
        if not payload.pincode.isdigit() or len(payload.pincode) != 6:
            raise HTTPException(status_code=422, detail="Pincode must be a 6-digit number.")
        _set(db, "pincode", payload.pincode)

    if payload.notifications_enabled is not None:
        _set(db, "notifications_enabled", str(payload.notifications_enabled).lower())

    if payload.email_enabled is not None:
        _set(db, "email_enabled", str(payload.email_enabled).lower())

    if payload.email_to is not None:
        _set(db, "email_to", payload.email_to)

    if payload.telegram_enabled is not None:
        _set(db, "telegram_enabled", str(payload.telegram_enabled).lower())

    if payload.telegram_bot_token is not None:
        env_settings.telegram_bot_token = payload.telegram_bot_token

    if payload.telegram_chat_id is not None:
        env_settings.telegram_chat_id = payload.telegram_chat_id

    if payload.check_interval_minutes is not None:
        if payload.check_interval_minutes < 1:
            raise HTTPException(status_code=422, detail="Interval must be at least 1 minute.")
        _set(db, "check_interval_minutes", str(payload.check_interval_minutes))
        sched_module.reschedule(payload.check_interval_minutes)

    db.commit()
    return get_settings(db)
