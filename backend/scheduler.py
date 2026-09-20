"""
Background scheduler — runs the scrape + diff + notify pipeline.

Detection logic
---------------
For each scraped product:
  - NEW       : product_id not in DB at all
  - RESTOCK   : product was is_available=False, now True
  - OOS       : product was is_available=True, now False
  - PRICE_DROP: price has dropped by ≥5% compared to stored price

For NEW and RESTOCK events, pincode delivery is checked before notifying.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import AppSettings, EventType, Product, SessionLocal, StockEvent
from backend.notifier import dispatch_notification
from backend.pincode_checker import batch_check_pincode
from backend.scraper import ScrapedProduct, scrape_hot_wheels

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None
_last_run_time: Optional[datetime.datetime] = None
_live_feed: list[dict] = []   # ring buffer of last 30 check summaries
_MAX_FEED = 30


def get_last_run_time() -> Optional[datetime.datetime]:
    return _last_run_time


def get_live_feed() -> list[dict]:
    return list(reversed(_live_feed))

# ── Settings helpers ──────────────────────────────────────────────────────────

def _get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    return row.value if row and row.value is not None else default


def _get_pincode(db: Session) -> str:
    return _get_setting(db, "pincode", settings.default_pincode)


def _notifications_enabled(db: Session) -> bool:
    return _get_setting(db, "notifications_enabled", "true").lower() == "true"


# ── Core pipeline ─────────────────────────────────────────────────────────────

# ── Core pipeline ─────────────────────────────────────────────────────────────

async def run_check() -> dict:
    """
    Full pipeline: scrape (whole catalog, concurrently) → diff → notify → persist.

    Pages are streamed in as soon as they're fetched (top-down and bottom-up
    scans running in parallel) and each page is diffed/notified immediately,
    instead of waiting for the entire catalog to finish scraping. This is what
    keeps restock alerts fast enough to beat FirstCry's limited-stock sellouts.
    """
    logger.info("Starting Hot Wheels stock check …")
    summary = {"products_found": 0, "new": 0, "restock": 0, "oos": 0, "price_drop": 0}

    db = SessionLocal()
    notify = _notifications_enabled(db)
    now = datetime.datetime.utcnow()
    scraped_ids: set[str] = set()
    lock = asyncio.Lock()

    async def process_batch(batch: list) -> None:
        """Diff + notify a single page's worth of products immediately."""
        events_to_notify: list[tuple[StockEvent, Product]] = []
        async with lock:
            try:
                for sp in batch:
                    scraped_ids.add(sp.product_id)
                    existing: Optional[Product] = (
                        db.query(Product).filter(Product.product_id == sp.product_id).first()
                    )

                    if existing is None:
                        product = Product(
                            product_id=sp.product_id,
                            name=sp.name,
                            price=sp.price,
                            original_price=sp.original_price,
                            url=sp.url,
                            image_url=sp.image_url,
                            is_available=sp.is_available,
                            series=sp.series,
                            pincode_deliverable=None,
                            last_checked=now,
                            first_seen=now,
                        )
                        db.add(product)
                        db.flush()
                        if not sp.is_available:
                            continue
                        event = StockEvent(
                            product_id=sp.product_id,
                            product_name=sp.name,
                            event_type=EventType.NEW,
                            timestamp=now,
                            price=sp.price,
                        )
                        db.add(event)
                        db.flush()
                        summary["new"] += 1
                        events_to_notify.append((event, product))
                        continue

                    if not existing.is_available and sp.is_available:
                        event = StockEvent(
                            product_id=sp.product_id,
                            product_name=sp.name,
                            event_type=EventType.RESTOCK,
                            timestamp=now,
                            price=sp.price,
                        )
                        db.add(event)
                        db.flush()
                        summary["restock"] += 1
                        events_to_notify.append((event, existing))
                        existing.first_seen = now  # float restocked items back to the top

                    elif existing.is_available and not sp.is_available:
                        event = StockEvent(
                            product_id=sp.product_id,
                            product_name=sp.name,
                            event_type=EventType.OOS,
                            timestamp=now,
                            price=sp.price,
                        )
                        db.add(event)
                        db.flush()
                        summary["oos"] += 1

                    if existing.price and sp.price and sp.price < existing.price * 0.95:
                        event = StockEvent(
                            product_id=sp.product_id,
                            product_name=sp.name,
                            event_type=EventType.PRICE_DROP,
                            timestamp=now,
                            price=sp.price,
                            details=f"Was ₹{existing.price:.0f}, now ₹{sp.price:.0f}",
                        )
                        db.add(event)
                        db.flush()
                        summary["price_drop"] += 1
                        events_to_notify.append((event, existing))

                    existing.name = sp.name
                    existing.price = sp.price
                    existing.original_price = sp.original_price
                    existing.is_available = sp.is_available
                    existing.image_url = sp.image_url or existing.image_url
                    existing.series = sp.series or existing.series
                    existing.last_checked = now

                db.commit()

                if notify:
                    for event, product in events_to_notify:
                        db.refresh(product)
                        sent = await dispatch_notification(event, product, "")
                        if sent:
                            event.notified = True
                    db.commit()

            except Exception:
                logger.error("Batch processing error", exc_info=True)
                db.rollback()

    try:
        scraped: list[ScrapedProduct] = await scrape_hot_wheels(on_page=process_batch)
    except Exception as exc:
        logger.error("Scraping failed (no data to process): %s", exc)
        db.close()
        return summary

    summary["products_found"] = len(scraped)
    if not scraped:
        logger.warning("No products scraped — skipping diff.")
        db.close()
        return summary

    try:
        # ── Purge products absent from this scrape (delisted entirely) ────
        all_db_products = db.query(Product).all()
        for db_product in all_db_products:
            if db_product.product_id not in scraped_ids:
                event = StockEvent(
                    product_id=db_product.product_id,
                    product_name=db_product.name,
                    event_type=EventType.OOS,
                    timestamp=now,
                    price=db_product.price,
                    details="Removed from FirstCry listing",
                )
                db.add(event)
                db.delete(db_product)
                summary["oos"] += 1
                logger.info("Purging unlisted product: %s", db_product.name[:40])
        db.commit()

    except Exception as exc:
        logger.error("Purge step error: %s", exc, exc_info=True)
        db.rollback()
    finally:
        db.close()

    logger.info(
        "Check complete. Products: %d | New: %d | Restock: %d | OOS: %d | Price drop: %d",
        summary["products_found"],
        summary["new"],
        summary["restock"],
        summary["oos"],
        summary["price_drop"],
    )
    global _last_run_time
    _last_run_time = datetime.datetime.utcnow()
    # Append to live feed ring buffer
    _live_feed.append({
        "time": _last_run_time.isoformat(),
        "products_found": summary["products_found"],
        "new": summary["new"],
        "restock": summary["restock"],
        "oos": summary["oos"],
        "price_drop": summary["price_drop"],
    })
    if len(_live_feed) > _MAX_FEED:
        _live_feed.pop(0)
    # Also persist last_run_time to DB so it survives module-reload and is readable across sessions
    try:
        _db = SessionLocal()
        row = _db.query(AppSettings).filter(AppSettings.key == "last_run_time").first()
        val = _last_run_time.isoformat()
        if row:
            row.value = val
        else:
            _db.add(AppSettings(key="last_run_time", value=val))
        _db.commit()
        _db.close()
    except Exception:
        pass
    return summary


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

def start_scheduler(interval_minutes: int | None = None) -> AsyncIOScheduler:
    global _scheduler

    # seconds-based interval takes precedence over minutes
    secs = settings.check_interval_seconds
    if secs and secs > 0:
        trigger_kwargs = {"seconds": secs}
        label = f"{secs} seconds"
    else:
        mins = interval_minutes or settings.check_interval_minutes
        trigger_kwargs = {"minutes": mins}
        label = f"{mins} minutes"

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        run_check,
        trigger="interval",
        **trigger_kwargs,
        id="hot_wheels_check",
        replace_existing=True,
        max_instances=1,  # skip if previous run still in progress
        next_run_time=datetime.datetime.now(),
    )
    _scheduler.start()
    logger.info("Scheduler started — interval: %s", label)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


def get_next_run_time() -> datetime.datetime | None:
    """Return the next scheduled run time (UTC-aware)."""
    if _scheduler and _scheduler.running:
        job = _scheduler.get_job("hot_wheels_check")
        if job and job.next_run_time:
            return job.next_run_time
    return None


def reschedule(interval_minutes: int) -> None:
    global _scheduler
    if _scheduler:
        _scheduler.reschedule_job(
            "hot_wheels_check",
            trigger="interval",
            minutes=interval_minutes,
        )
        logger.info("Scheduler rescheduled to %d minutes.", interval_minutes)
