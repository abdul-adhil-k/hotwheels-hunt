import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import Product, WatchlistItem, get_db
from backend.models import WatchlistIn, WatchlistOut
from backend.scraper import _extract_product_id

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.get("", response_model=list[WatchlistOut])
def list_watchlist(db: Session = Depends(get_db)):
    return (
        db.query(WatchlistItem)
        .order_by(WatchlistItem.is_available.desc(), WatchlistItem.created_at.desc())
        .all()
    )


def _try_resolve(db: Session, item: WatchlistItem) -> None:
    """Match against products already scraped so far, so a favorite that's
    already in stock shows up as available immediately, without waiting for
    the next scheduled scrape cycle."""
    match = None
    if item.product_id:
        match = db.query(Product).filter(Product.product_id == item.product_id).first()
    elif item.query_type == "name":
        match = db.query(Product).filter(Product.name.ilike(f"%{item.query}%")).first()
        if match:
            item.product_id = match.product_id

    if match:
        item.name = match.name
        item.url = match.url
        item.image_url = match.image_url
        item.price = match.price
        item.is_available = match.is_available
        item.status = "available" if match.is_available else "found"
        item.last_checked = match.last_checked


@router.post("", response_model=WatchlistOut, status_code=201)
def add_watchlist_item(payload: WatchlistIn, db: Session = Depends(get_db)):
    query = payload.query.strip()
    if not query:
        raise HTTPException(400, "Please paste a product URL or name")

    is_url = query.lower().startswith("http")
    product_id = _extract_product_id(query) if is_url else None
    if is_url and not product_id:
        raise HTTPException(400, "Couldn't find a product id in that URL")

    existing = None
    if product_id:
        existing = db.query(WatchlistItem).filter(WatchlistItem.product_id == product_id).first()
    else:
        existing = db.query(WatchlistItem).filter(WatchlistItem.query.ilike(query)).first()
    if existing:
        raise HTTPException(409, "That product is already in your favorites list")

    item = WatchlistItem(
        query=query,
        query_type="url" if is_url else "name",
        product_id=product_id,
        name=None if is_url else query,
        status="watching",
        created_at=datetime.datetime.utcnow(),
    )
    _try_resolve(db, item)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{item_id}", status_code=204)
def delete_watchlist_item(item_id: int, db: Session = Depends(get_db)):
    item = db.query(WatchlistItem).filter(WatchlistItem.id == item_id).first()
    if not item:
        raise HTTPException(404, "Not found")
    db.delete(item)
    db.commit()
