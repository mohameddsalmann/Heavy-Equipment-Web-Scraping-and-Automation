from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AlertEvent

logger = structlog.get_logger(__name__)


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_relative_time(dt: datetime) -> str:
    """Format datetime into human relative time e.g. '1m 14s' or '35s'."""
    now = get_utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = max(0, int((now - dt).total_seconds()))

    if delta < 60:
        return f"{delta}s"
    minutes = delta // 60
    seconds = delta % 60
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours = minutes // 60
    rem_minutes = minutes % 60
    return f"{hours}h {rem_minutes}m"


def format_price(amount: Optional[float], currency: str = "USD") -> str:
    if amount is None:
        return "Contact for Price"
    return f"${amount:,.2f}".rstrip("0").rstrip(".") + f" {currency}"


def format_telegram_message(payload: Dict[str, Any]) -> str:
    """
    Format outbox alert payload into standardized Telegram Markdown alert text.
    Contract matches n8n alert forwarding requirement.
    """
    event_type = payload.get("event_type", "NEW_LISTING")
    title = payload.get("title", "Heavy Equipment Listing")
    source = payload.get("source", "facebook_marketplace")
    source_display = (
        "Facebook Marketplace"
        if source == "facebook_marketplace"
        else "MachineryTrader"
        if source == "machinerytrader"
        else source.title()
    )

    location = payload.get("location") or "North America"
    url = payload.get("url", "")
    currency = payload.get("currency", "USD")

    detected_at_str = payload.get("detected_at")
    rel_time = "just now"
    if detected_at_str:
        try:
            detected_dt = datetime.fromisoformat(detected_at_str)
            rel_time = f"{format_relative_time(detected_dt)} ago"
        except Exception:
            pass

    if event_type == "PRICE_CHANGED":
        old_price = payload.get("old_price")
        new_price = payload.get("price")
        header = "📉 PRICE CHANGED"
        price_line = f"Price: {format_price(old_price, currency)} ➔ {format_price(new_price, currency)}"
    else:
        price = payload.get("price")
        header = "🚨 NEW MACHINE"
        price_line = f"Price: {format_price(price, currency)}"

    return (
        f"{header}\n\n"
        f"{title}\n"
        f"{price_line}\n"
        f"Location: {location}\n"
        f"Source: {source_display}\n\n"
        f"Detected: {rel_time}\n\n"
        f"Open Listing:\n{url}"
    )


class AlertService:
    """Service for managing the outbox table for Telegram / n8n notification events."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_pending_alerts(
        self, limit: int = 50, max_attempts: int = 5
    ) -> List[AlertEvent]:
        """Fetch undelivered alert events that have not exceeded max retry attempts."""
        stmt = (
            select(AlertEvent)
            .where(
                AlertEvent.delivered_at.is_(None),
                AlertEvent.attempt_count < max_attempts,
            )
            .order_by(AlertEvent.created_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def mark_delivered(self, alert_id: uuid.UUID) -> Optional[AlertEvent]:
        """Mark an alert event as successfully delivered."""
        stmt = select(AlertEvent).where(AlertEvent.id == alert_id)
        res = await self.session.execute(stmt)
        alert = res.scalar_one_or_none()
        if alert is None:
            return None

        alert.delivered_at = get_utc_now()
        await self.session.commit()
        return alert

    async def record_failure(
        self, alert_id: uuid.UUID, error_msg: str
    ) -> Optional[AlertEvent]:
        """Record delivery attempt failure and increment retry count."""
        stmt = select(AlertEvent).where(AlertEvent.id == alert_id)
        res = await self.session.execute(stmt)
        alert = res.scalar_one_or_none()
        if alert is None:
            return None

        alert.attempt_count += 1
        alert.last_error = error_msg
        await self.session.commit()
        return alert
