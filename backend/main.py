"""
FastAPI application entry point.

Serves:
  - REST API under /api/*
  - Static frontend files at /
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from backend import scheduler as sched_module
from backend.config import settings
from backend.database import AppSettings, Product, SessionLocal, StockEvent, get_db, init_db
from backend.models import CheckResponse, StatsOut
from backend.routers import products, settings as settings_router, events

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    _seed_default_settings()
    sched_module.start_scheduler()
    yield
    # Shutdown
    sched_module.stop_scheduler()


def _seed_default_settings() -> None:
    """Insert default settings if not already present."""
    db = SessionLocal()
    defaults = {
        "pincode": settings.default_pincode,
        "notifications_enabled": "true",
        "email_enabled": str(settings.email_enabled).lower(),
        "email_to": settings.email_to,
        "telegram_enabled": str(settings.telegram_enabled).lower(),
        "check_interval_minutes": str(settings.check_interval_minutes),
    }
    for key, value in defaults.items():
        if not db.query(AppSettings).filter(AppSettings.key == key).first():
            db.add(AppSettings(key=key, value=value))
    db.commit()
    db.close()


app = FastAPI(
    title="Hot Wheels Hunt",
    description="Monitors FirstCry for Hot Wheels stock changes and sends alerts.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API routers ───────────────────────────────────────────────────────────────

app.include_router(products.router)
app.include_router(settings_router.router)
app.include_router(events.router)


@app.post("/api/check", response_model=CheckResponse, tags=["check"])
async def manual_check():
    """Trigger an immediate scrape + diff + notify run."""
    summary = await sched_module.run_check()
    return CheckResponse(
        message="Check complete",
        products_found=summary.get("products_found", 0),
        new_products=summary.get("new", 0),
        restocked=summary.get("restock", 0),
        out_of_stock=summary.get("oos", 0),
    )


@app.get("/api/live", tags=["stats"])
def get_live_feed():
    return sched_module.get_live_feed()


@app.get("/api/stats", response_model=StatsOut, tags=["stats"])
def get_stats(db: Session = Depends(get_db)):
    total = db.query(Product).count()
    in_stock = db.query(Product).filter(Product.is_available == True).count()
    oos = db.query(Product).filter(Product.is_available == False).count()
    deliverable = db.query(Product).filter(Product.pincode_deliverable == True).count()

    # Primary source: persisted last-run timestamp (written by scheduler each cycle)
    last_check: datetime.datetime | None = None
    row = db.query(AppSettings).filter(AppSettings.key == "last_run_time").first()
    if row and row.value:
        try:
            last_check = datetime.datetime.fromisoformat(row.value)
        except ValueError:
            pass
    if last_check is None:
        last_product = db.query(Product).order_by(Product.last_checked.desc()).first()
        last_check = last_product.last_checked if last_product else None

    total_events = db.query(StockEvent).count()

    return StatsOut(
        total_products=total,
        in_stock=in_stock,
        out_of_stock=oos,
        deliverable=deliverable,
        last_check=last_check,
        next_check=sched_module.get_next_run_time(),
        total_events=total_events,
    )


# ── Frontend (serve static files) ─────────────────────────────────────────────

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def serve_frontend():
        return FileResponse(str(FRONTEND_DIR / "index.html"))
