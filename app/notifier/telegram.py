import httpx

from app.config import settings


async def send_telegram_message(text: str) -> None:
    if not settings.telegram_enabled:
        raise RuntimeError("Telegram is not configured (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)")

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")


def format_price_alert(
    *,
    title: str | None,
    asin: str,
    url: str,
    price_eur: float,
    list_price_eur: float | None,
    discount_percent: float | None,
    reason: str,
) -> str:
    lines = [
        "🚨 POSSIBLE Amazon.ie price drop — research lead",
        "⚠️ Seller, condition and checkout price NOT verified. Confirm the exact offer before buying.",
        "",
        title or f"ASIN {asin}",
        f"Now: €{price_eur:.2f}",
    ]
    if list_price_eur:
        lines.append(f"Was: €{list_price_eur:.2f}")
    if discount_percent is not None:
        lines.append(f"Discount: ~{discount_percent:.0f}%")
    lines.extend(["", reason, "", url])
    return "\n".join(lines)


async def send_whatsapp_message(text: str) -> None:
    if not settings.whatsapp_enabled:
        raise RuntimeError("WhatsApp (Twilio) is not configured")

    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{settings.twilio_account_sid}/Messages.json"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            data={
                "From": settings.twilio_whatsapp_from,
                "To": settings.twilio_whatsapp_to,
                "Body": text,
            },
        )
        resp.raise_for_status()
