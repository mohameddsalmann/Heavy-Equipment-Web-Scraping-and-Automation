from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy import select

from app.database.models import AlertEvent, Listing
from app.schemas.listing import MatchClassification, MatchResult, RawListing
from app.services.alert_service import AlertService, format_telegram_message
from app.services.listing_service import ListingService, UpsertOutcome


def make_raw(
    external_id: str = "fb_101",
    price: Decimal = Decimal("89500.00"),
    currency: str = "USD",
    title: str = "2012 CAT 966H Wheel Loader",
) -> RawListing:
    return RawListing(
        source="facebook_marketplace",
        external_id=external_id,
        title=title,
        price=price,
        currency=currency,
        location="Dallas, TX",
        url=f"https://www.facebook.com/marketplace/item/{external_id}",
        scraped_at=datetime.now(timezone.utc),
    )


def make_accept_match() -> MatchResult:
    return MatchResult(
        is_target_equipment=True,
        classification=MatchClassification.ACCEPT,
        confidence=0.95,
        model="966H",
        manufacturer="Caterpillar",
        equipment_type="Wheel Loader",
    )


def make_review_match() -> MatchResult:
    return MatchResult(
        is_target_equipment=True,
        classification=MatchClassification.REVIEW,
        confidence=0.60,
        model="966",
        manufacturer="Caterpillar",
        rejection_reason="ambiguous_generation",
    )


@pytest.mark.asyncio
async def test_accept_new_creates_exactly_one_alert_event(test_db_session):
    """
    Test requirement: ACCEPT + NEW creates exactly one alert event.
    """
    listing_service = ListingService(test_db_session)
    raw = make_raw(external_id="alert_test_1")
    match = make_accept_match()

    listing, outcome = await listing_service.upsert_listing(raw, match)
    assert outcome == UpsertOutcome.NEW

    # Query outbox
    alerts_stmt = select(AlertEvent).where(AlertEvent.listing_id == listing.id)
    alerts = (await test_db_session.execute(alerts_stmt)).scalars().all()

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.event_type == "NEW_LISTING"
    assert alert.delivered_at is None
    assert alert.attempt_count == 0
    assert alert.payload_json["title"] == "2012 CAT 966H Wheel Loader"
    assert alert.payload_json["price"] == 89500.0
    assert alert.payload_json["currency"] == "USD"
    assert alert.payload_json["location"] == "Dallas, TX"
    assert alert.payload_json["source"] == "facebook_marketplace"


@pytest.mark.asyncio
async def test_review_does_not_create_alert_event(test_db_session):
    """
    Test requirement: REVIEW does not create Telegram alert event.
    Must be persisted with status REVIEW for human audit, but outbox remains empty.
    """
    listing_service = ListingService(test_db_session)
    raw = make_raw(external_id="review_test_1", title="CAT 966 Loader - Incomplete Model")
    match = make_review_match()

    listing, outcome = await listing_service.upsert_listing(raw, match)
    assert outcome == UpsertOutcome.NEW
    assert listing.status == "REVIEW"

    # Query outbox
    alerts_stmt = select(AlertEvent).where(AlertEvent.listing_id == listing.id)
    alerts = (await test_db_session.execute(alerts_stmt)).scalars().all()

    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_duplicate_does_not_create_new_alert_event(test_db_session):
    """
    Test requirement: Duplicate scan does not create additional alert event.
    """
    listing_service = ListingService(test_db_session)
    raw = make_raw(external_id="dup_test_1")
    match = make_accept_match()

    _, outcome1 = await listing_service.upsert_listing(raw, match)
    assert outcome1 == UpsertOutcome.NEW

    _, outcome2 = await listing_service.upsert_listing(raw, match)
    assert outcome2 == UpsertOutcome.DUPLICATE

    alerts_stmt = select(AlertEvent)
    alerts = (await test_db_session.execute(alerts_stmt)).scalars().all()
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_price_change_creates_price_changed_alert_event(test_db_session):
    """
    Test requirement: price change creates PRICE_CHANGED alert event.
    """
    listing_service = ListingService(test_db_session)
    raw1 = make_raw(external_id="price_drop_1", price=Decimal("95000.00"))
    match = make_accept_match()

    listing1, outcome1 = await listing_service.upsert_listing(raw1, match)
    assert outcome1 == UpsertOutcome.NEW

    # Price dropped to 87000
    raw2 = make_raw(external_id="price_drop_1", price=Decimal("87000.00"))
    listing2, outcome2 = await listing_service.upsert_listing(raw2, match)
    assert outcome2 == UpsertOutcome.PRICE_CHANGED

    alerts_stmt = (
        select(AlertEvent)
        .where(AlertEvent.listing_id == listing2.id)
        .order_by(AlertEvent.created_at.asc())
    )
    alerts = (await test_db_session.execute(alerts_stmt)).scalars().all()

    assert len(alerts) == 2
    assert alerts[0].event_type == "NEW_LISTING"
    assert alerts[1].event_type == "PRICE_CHANGED"
    assert alerts[1].payload_json["old_price"] == 95000.0
    assert alerts[1].payload_json["price"] == 87000.0


@pytest.mark.asyncio
async def test_n8n_delivery_retry_does_not_recreate_listing(test_db_session):
    """
    Test requirement: n8n delivery retry does not recreate listing.
    Failing or retrying delivery modifies only alert_events metadata (attempt_count, last_error),
    leaving the underlying listing intact.
    """
    listing_service = ListingService(test_db_session)
    alert_service = AlertService(test_db_session)
    raw = make_raw(external_id="retry_test_1")
    match = make_accept_match()

    listing, outcome = await listing_service.upsert_listing(raw, match)
    assert outcome == UpsertOutcome.NEW

    pending = await alert_service.get_pending_alerts()
    assert len(pending) == 1
    alert_id = pending[0].id

    # Simulate n8n attempt failure 1
    failed1 = await alert_service.record_failure(alert_id, "Telegram 429 Too Many Requests")
    assert failed1.attempt_count == 1
    assert failed1.last_error == "Telegram 429 Too Many Requests"
    assert failed1.delivered_at is None

    # Simulate n8n retry and successful delivery
    delivered = await alert_service.mark_delivered(alert_id)
    assert delivered.delivered_at is not None
    assert delivered.attempt_count == 1

    # Verify listings count in DB has not changed
    listings_count = (await test_db_session.execute(select(Listing))).scalars().all()
    assert len(listings_count) == 1
    assert listings_count[0].id == listing.id


def test_telegram_message_format():
    """Verify Telegram alert formatting matches client specification."""
    payload = {
        "event_type": "NEW_LISTING",
        "title": "2012 CAT 966H Wheel Loader",
        "price": 89500.0,
        "currency": "USD",
        "location": "Dallas, TX",
        "source": "facebook_marketplace",
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "url": "https://www.facebook.com/marketplace/item/1098234857234",
    }

    message = format_telegram_message(payload)
    assert "🚨 NEW MACHINE" in message
    assert "2012 CAT 966H Wheel Loader" in message
    assert "Price: $89,500 USD" in message
    assert "Location: Dallas, TX" in message
    assert "Source: Facebook Marketplace" in message
    assert "Open Listing:" in message
    assert "https://www.facebook.com/marketplace/item/1098234857234" in message
