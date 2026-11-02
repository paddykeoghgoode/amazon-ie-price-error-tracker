import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app import database as db
from app.services.monitor import add_watch_url, run_monitor_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_monitor_stop: asyncio.Event | None = None
_monitor_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _monitor_stop, _monitor_task
    db.init_db()
    _monitor_stop = asyncio.Event()
    _monitor_task = asyncio.create_task(run_monitor_loop(_monitor_stop))
    logger.info(
        "Monitor started (poll every %ss, Telegram=%s, WhatsApp=%s)",
        settings.poll_interval_seconds,
        settings.telegram_enabled,
        settings.whatsapp_enabled,
    )
    yield
    if _monitor_stop:
        _monitor_stop.set()
    if _monitor_task:
        await _monitor_task


app = FastAPI(title="Amazon.ie Price Error Tracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


class WatchRequest(BaseModel):
    url: str = Field(..., description="Amazon.ie product URL or 10-character ASIN")


class EnabledRequest(BaseModel):
    enabled: bool


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    from pathlib import Path

    return Path("templates/index.html").read_text(encoding="utf-8")


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "telegram_configured": settings.telegram_enabled,
        "whatsapp_configured": settings.whatsapp_enabled,
        "poll_interval_seconds": settings.poll_interval_seconds,
    }


@app.get("/api/products")
async def api_list_products():
    return db.list_products()


@app.post("/api/products")
async def api_add_product(body: WatchRequest):
    try:
        return add_watch_url(body.url.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/products/{product_id}")
async def api_set_enabled(product_id: int, body: EnabledRequest):
    db.set_product_enabled(product_id, body.enabled)
    return {"ok": True}


@app.delete("/api/products/{product_id}")
async def api_delete_product(product_id: int):
    db.delete_product(product_id)
    return {"ok": True}


@app.get("/api/alerts")
async def api_alerts(limit: int = 50):
    return db.recent_alerts(limit=min(limit, 200))


@app.get("/api/settings")
async def api_settings():
    return {
        "min_discount_percent": settings.min_discount_percent,
        "min_list_price_eur": settings.min_list_price_eur,
        "max_alert_price_eur": settings.max_alert_price_eur,
        "cooldown_minutes": settings.cooldown_minutes,
        "poll_interval_seconds": settings.poll_interval_seconds,
    }
