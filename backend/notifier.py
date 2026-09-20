"""
Notification dispatcher: Email, Telegram, and WhatsApp (Twilio).

Each channel is independently enabled via settings.
Messages use a consistent, human-readable format.
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.config import settings
from backend.database import StockEvent, Product

logger = logging.getLogger(__name__)


# ── Message formatting ────────────────────────────────────────────────────────

EVENT_EMOJI = {
    "NEW": "🆕",
    "RESTOCK": "🔄",
    "OOS": "❌",
    "PRICE_DROP": "💸",
}

EVENT_LABEL = {
    "NEW": "New Hot Wheels item listed!",
    "RESTOCK": "Back In Stock!",
    "OOS": "Out of Stock",
    "PRICE_DROP": "Price Drop!",
}


def _format_message(event: StockEvent, product: Product, pincode: str) -> str:
    emoji = EVENT_EMOJI.get(event.event_type, "🚗")
    label = EVENT_LABEL.get(event.event_type, event.event_type)

    price_str = f"₹{event.price:.0f}" if event.price else "N/A"
    if product.original_price and product.original_price > (event.price or 0):
        discount = int((1 - (event.price or 0) / product.original_price) * 100)
        price_str += f" (MRP ₹{product.original_price:.0f} — {discount}% off)"

    if product.pincode_deliverable is True:
        delivery_str = f"✅ Deliverable to {pincode}"
    elif product.pincode_deliverable is False:
        delivery_str = f"❌ Not deliverable to {pincode}"
    else:
        delivery_str = "📦 Delivery status unknown"

    series_str = f"Series: {product.series}" if product.series else ""

    lines = [
        f"{emoji} Hot Wheels Alert — {label}",
        "",
        f"Product : {product.name}",
        f"Price   : {price_str}",
        f"Stock   : {'In Stock ✅' if product.is_available else 'Out of Stock ❌'}",
        f"Delivery: {delivery_str}",
    ]
    if series_str:
        lines.append(f"Series  : {series_str}")
    lines += [
        f"Link    : {product.url}",
        "",
        f"Checked at {event.timestamp.strftime('%Y-%m-%d %H:%M')} UTC",
    ]
    return "\n".join(lines)


# ── Email ─────────────────────────────────────────────────────────────────────

async def _send_email(subject: str, body: str) -> bool:
    try:
        import aiosmtplib
        from email.mime.text import MIMEText

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = settings.email_from or settings.smtp_user
        msg["To"] = settings.email_to

        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_password,
            start_tls=True,
        )
        logger.info("Email sent to %s", settings.email_to)
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False


# ── Telegram ──────────────────────────────────────────────────────────────────

async def _send_telegram(text: str, product_url: Optional[str] = None) -> bool:
    try:
        import httpx

        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": settings.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if product_url:
            # One-tap button — faster than following a text link for a race against sellout.
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "🛒 Open Product", "url": product_url}]]
            }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        logger.info("Telegram message sent to chat %s", settings.telegram_chat_id)
        return True
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


# ── WhatsApp via Twilio ───────────────────────────────────────────────────────

async def _send_whatsapp(body: str) -> bool:
    try:
        import httpx
        import base64

        credentials = base64.b64encode(
            f"{settings.twilio_account_sid}:{settings.twilio_auth_token}".encode()
        ).decode()

        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.twilio_account_sid}/Messages.json"
        )
        data = {
            "From": settings.twilio_whatsapp_from,
            "To": settings.whatsapp_to,
            "Body": body,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                data=data,
                headers={"Authorization": f"Basic {credentials}"},
            )
            resp.raise_for_status()
        logger.info("WhatsApp message sent to %s", settings.whatsapp_to)
        return True
    except Exception as exc:
        logger.error("WhatsApp send failed: %s", exc)
        return False


# ── Public dispatcher ─────────────────────────────────────────────────────────

async def dispatch_notification(
    event: StockEvent,
    product: Product,
    pincode: str,
) -> bool:
    """
    Send notifications via all enabled channels.
    Returns True if at least one channel succeeded.
    """
    message = _format_message(event, product, pincode)
    subject = f"Hot Wheels {EVENT_LABEL.get(event.event_type, '')} — {product.name}"

    sent_any = False

    if settings.email_enabled and settings.smtp_user and settings.email_to:
        sent_any |= await _send_email(subject, message)

    if settings.telegram_enabled and settings.telegram_bot_token and settings.telegram_chat_id:
        sent_any |= await _send_telegram(message, product_url=product.url)

    if settings.whatsapp_enabled and settings.twilio_account_sid and settings.whatsapp_to:
        sent_any |= await _send_whatsapp(message)

    return sent_any
