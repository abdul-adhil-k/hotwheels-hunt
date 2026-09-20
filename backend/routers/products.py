from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional

from backend.database import get_db, Product, StockEvent
from backend.models import ProductOut, StockEventOut

router = APIRouter(prefix="/api/products", tags=["products"])


@router.get("", response_model=list[ProductOut])
def list_products(
    available_only: bool = Query(False),
    deliverable_only: bool = Query(False),
    series: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(Product)
    if available_only:
        q = q.filter(Product.is_available == True)
    if deliverable_only:
        q = q.filter(Product.pincode_deliverable == True)
    if series:
        q = q.filter(Product.series == series)
    # In-stock items first; within each group, most recently seen/restocked first
    return q.order_by(Product.is_available.desc(), Product.first_seen.desc()).all()


@router.get("/{product_id}/history", response_model=list[StockEventOut])
def product_history(product_id: str, db: Session = Depends(get_db)):
    return (
        db.query(StockEvent)
        .filter(StockEvent.product_id == product_id)
        .order_by(StockEvent.timestamp.desc())
        .limit(50)
        .all()
    )


@router.get("/series/list")
def list_series(db: Session = Depends(get_db)):
    rows = db.query(Product.series).distinct().all()
    return sorted([r[0] for r in rows if r[0]])
