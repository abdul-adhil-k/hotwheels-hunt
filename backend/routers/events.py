from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.database import get_db, StockEvent
from backend.models import StockEventOut

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("", response_model=list[StockEventOut])
def list_events(
    limit: int = Query(50, ge=1, le=500),
    event_type: str | None = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(StockEvent)
    if event_type:
        q = q.filter(StockEvent.event_type == event_type.upper())
    return q.order_by(StockEvent.timestamp.desc()).limit(limit).all()
