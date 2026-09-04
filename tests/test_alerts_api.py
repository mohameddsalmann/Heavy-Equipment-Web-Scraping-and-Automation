from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import pytest
from httpx import AsyncClient, ASGITransport

from app.database.models import AlertEvent
from app.database.session import get_db_session
from app.main import app
from app.schemas.listing import MatchClassification, MatchResult, RawListing
from app.services.listing_service import ListingService


@pytest.fixture
def make_accept_match():
    return MatchResult(
        is_target_equipment=True,
        classification=MatchClassification.ACCEPT,
        confidence=0.95,
        model="966H",
        manufacturer="Caterpillar",
        equipment_type="Wheel Loader",
    )


@pytest.mark.asyncio
async def test_alerts_api_endpoints(test_db_session, make_accept_match):
    """
    Test n8n API endpoints:
    1. GET /api/alerts/pending returns outbox alert with formatted Telegram message
    2. POST /api/alerts/{id}/fail records delivery error and increments attempt count
    3. POST /api/alerts/{id}/delivered marks alert as delivered
    4. GET /api/alerts/pending returns empty list after delivery
    """
    # Override FastAPI get_db_session dependency with test session
    async def override_get_db():
        yield test_db_session

    app.dependency_overrides[get_db_session] = override_get_db

    # Create listing & alert event in test DB
    listing_service = ListingService(test_db_session)
    raw = RawListing(
        source="facebook_marketplace",
        external_id="api_test_item_1",
        title="2012 Caterpillar 966H Wheel Loader",
        price=Decimal("89500.00"),
        currency="USD",
        location="Dallas, TX",
        url="https://www.facebook.com/marketplace/item/api_test_item_1",
        scraped_at=datetime.now(timezone.utc),
    )
    listing, _ = await listing_service.upsert_listing(raw, make_accept_match)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Test GET /api/alerts/pending
        res = await client.get("/api/alerts/pending")
        assert res.status_code == 200
        pending_data = res.json()
        assert len(pending_data) == 1
        alert = pending_data[0]
        alert_id = alert["id"]
        assert alert["event_type"] == "NEW_LISTING"
        assert "🚨 NEW MACHINE" in alert["telegram_message"]
        assert "Dallas, TX" in alert["telegram_message"]
        assert "https://www.facebook.com/marketplace/item/api_test_item_1" in alert["telegram_message"]

        # 2. Test POST /api/alerts/{id}/fail
        fail_res = await client.post(
            f"/api/alerts/{alert_id}/fail",
            json={"error": "Telegram timeout"},
        )
        assert fail_res.status_code == 200
        fail_data = fail_res.json()
        assert fail_data["attempt_count"] == 1
        assert fail_data["last_error"] == "Telegram timeout"

        # 3. Test POST /api/alerts/{id}/delivered
        deliv_res = await client.post(f"/api/alerts/{alert_id}/delivered")
        assert deliv_res.status_code == 200
        deliv_data = deliv_res.json()
        assert deliv_data["status"] == "success"
        assert deliv_data["delivered_at"] is not None

        # 4. Confirm it is no longer in pending
        res2 = await client.get("/api/alerts/pending")
        assert res2.status_code == 200
        assert len(res2.json()) == 0

    app.dependency_overrides.clear()
