from __future__ import annotations

from typing import Any, Dict, List, Optional
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db_session
from app.services.alert_service import AlertService, format_telegram_message

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertFailureRequest(BaseModel):
    error: str = "Delivery failed"


class AlertResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    listing_id: uuid.UUID
    attempt_count: int
    created_at: str
    payload: Dict[str, Any]
    telegram_message: str


@router.get("/pending", response_model=List[AlertResponse])
async def get_pending_alerts(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> List[AlertResponse]:
    """
    Fetch pending alert events from the outbox for n8n to send via Telegram.
    Includes pre-formatted Telegram message markdown for zero-effort dispatch.
    """
    service = AlertService(session)
    pending = await service.get_pending_alerts(limit=limit)

    return [
        AlertResponse(
            id=alert.id,
            event_type=alert.event_type,
            listing_id=alert.listing_id,
            attempt_count=alert.attempt_count,
            created_at=alert.created_at.isoformat(),
            payload=alert.payload_json,
            telegram_message=format_telegram_message(alert.payload_json),
        )
        for alert in pending
    ]


@router.post("/{alert_id}/delivered", status_code=status.HTTP_200_OK)
async def mark_alert_delivered(
    alert_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Mark an alert event as delivered in the database outbox."""
    service = AlertService(session)
    alert = await service.mark_delivered(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert event not found")
    return {
        "status": "success",
        "alert_id": str(alert.id),
        "delivered_at": alert.delivered_at.isoformat() if alert.delivered_at else None,
    }


@router.post("/{alert_id}/fail", status_code=status.HTTP_200_OK)
async def mark_alert_failed(
    alert_id: uuid.UUID,
    req: AlertFailureRequest,
    session: AsyncSession = Depends(get_db_session),
) -> Dict[str, Any]:
    """Record an alert delivery failure and increment attempt count for retry."""
    service = AlertService(session)
    alert = await service.record_failure(alert_id, req.error)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert event not found")
    return {
        "status": "failed_recorded",
        "alert_id": str(alert.id),
        "attempt_count": alert.attempt_count,
        "last_error": alert.last_error,
    }
