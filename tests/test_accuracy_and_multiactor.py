from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List
import pytest
import httpx
from sqlalchemy import select

from app.database.models import AlertEvent, Listing
from app.matcher.equipment_matcher import EquipmentMatcher
from app.providers.facebook.apify_provider import ApifyFacebookProvider
from app.providers.facebook.models import FacebookRegion
from app.schemas.listing import MatchClassification, RawListing
from app.workers.facebook_run_once import run_facebook_scan


@pytest.fixture
def test_region_tx():
    return FacebookRegion(
        id="us_texas",
        name="Texas",
        country="US",
        location_id="dallas",
        search_url="https://www.facebook.com/marketplace/dallas/search",
        enabled=True,
        priority=1,
    )


def test_post_match_anomaly_gate_plausible_accept():
    """
    Phase 4: CAT 966D — $55,000 => plausible ACCEPT
    """
    matcher = EquipmentMatcher()
    raw = RawListing(
        source="facebook_marketplace",
        external_id="fb_966d_plausible",
        title="2012 Caterpillar 966D Wheel Loader",
        price=Decimal("55000.00"),
        url="https://www.facebook.com/marketplace/item/fb_966d_plausible",
    )
    result = matcher.match(raw)
    assert result.classification == MatchClassification.ACCEPT
    assert result.is_target_equipment is True
    assert "price_anomaly" not in result.matched_rules


def test_post_match_anomaly_gate_suspicious_price_demoted_to_review():
    """
    Phase 4: CAT 966E — $75 => REVIEW_REQUIRED / PRICE_ANOMALY
    A suspicious price must NOT automatically reject a potentially real machine.
    """
    matcher = EquipmentMatcher()
    raw = RawListing(
        source="facebook_marketplace",
        external_id="fb_966e_suspicious",
        title="2005 Caterpillar 966E Wheel Loader",
        price=Decimal("75.00"),
        url="https://www.facebook.com/marketplace/item/fb_966e_suspicious",
    )
    result = matcher.match(raw)
    # Must NOT reject outright
    assert result.classification != MatchClassification.REJECT
    # Must be demoted to REVIEW
    assert result.classification == MatchClassification.REVIEW
    assert result.is_target_equipment is False
    assert "price_anomaly" in result.matched_rules
    assert "PRICE_ANOMALY" in (result.rejection_reason or "")


def test_obvious_rejects_remain_strictly_reject():
    """
    Phase 4: Examples that must remain REJECT:
    966H bucket, 966H radiator, 966H tires, 966H engine for sale,
    966H toy, 966H diecast, 966H manual
    """
    matcher = EquipmentMatcher()
    must_reject = [
        ("966H bucket", "CAT 966H quick coupler bucket"),
        ("966H radiator", "CAT 966H heavy equipment radiator"),
        ("966H tires", "CAT 966H loader tires set of 4"),
        ("966H engine for sale", "CAT 966H diesel engine for sale"),
        ("966H toy", "CAT 966H wheel loader toy for kids"),
        ("966H diecast", "Diecast 1:50 scale CAT 966H replica"),
        ("966H manual", "CAT 966H workshop service repair manual"),
    ]

    for label, title in must_reject:
        raw = RawListing(
            source="facebook_marketplace",
            external_id=f"fb_reject_{label.replace(' ', '_')}",
            title=title,
            price=Decimal("5000.00"),
            url=f"https://www.facebook.com/marketplace/item/reject_{label}",
        )
        result = matcher.match(raw)
        assert result.classification == MatchClassification.REJECT, f"{label} was not rejected"
        assert result.is_target_equipment is False


@pytest.mark.asyncio
async def test_price_anomaly_does_not_create_alert_event(session_factory, test_db_session):
    """
    Phase 4: Verify that a PRICE_ANOMALY (e.g. CAT 966E - $75) is saved as REVIEW in DB
    but DOES NOT trigger a false alert event in the outbox.
    """
    from app.providers.facebook.base import BaseFacebookProvider

    class AnomalyMockProvider(BaseFacebookProvider):
        async def search_region(self, query: str, region: FacebookRegion, max_results: int = 20) -> List[RawListing]:
            return [
                RawListing(
                    source="facebook_marketplace",
                    external_id="fb_anomaly_1",
                    title="2005 Caterpillar 966E Wheel Loader",
                    price=Decimal("75.00"),
                    url="https://www.facebook.com/marketplace/item/fb_anomaly_1",
                ),
                RawListing(
                    source="facebook_marketplace",
                    external_id="fb_plausible_2",
                    title="2012 Caterpillar 966D Wheel Loader",
                    price=Decimal("55000.00"),
                    url="https://www.facebook.com/marketplace/item/fb_plausible_2",
                ),
            ]

        def normalize_item(self, item: Dict[str, Any], region: FacebookRegion = None) -> RawListing:
            return item

    metrics = await run_facebook_scan(
        queries=["CAT 966"],
        region_ids=["us_texas"],
        custom_provider=AnomalyMockProvider(),
        session_factory=session_factory,
    )

    assert metrics["accepted"] == 1
    assert metrics["review"] == 1
    assert metrics["price_anomaly"] == 1
    assert metrics["alerts_created"] == 1  # Only the plausible $55,000 listing created an alert!

    # Verify database listings
    listings = (await test_db_session.execute(select(Listing))).scalars().all()
    assert len(listings) == 2

    # Verify alert events: only 1 alert event created for the ACCEPT listing
    alerts = (await test_db_session.execute(select(AlertEvent))).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].payload_json["external_id"] == "fb_plausible_2"


@pytest.mark.asyncio
async def test_apify_provider_actor_fallback(test_region_tx):
    """
    Phase 1: When primary actor fails, provider gracefully falls back to configured fallback actor.
    """
    primary_actor = "blackfalcondata/facebook-marketplace-scraper"
    fallback_actor = "calm_builder/facebook-marketplace-scraper"

    sample_item = {
        "id": "fallback_item_1",
        "title": "2010 CAT 966H Wheel Loader",
        "price": 95000,
        "url": "https://www.facebook.com/marketplace/item/fallback_item_1",
    }

    class FallbackTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            # If request targets primary actor, fail with 500
            if "blackfalcondata" in url_str:
                return httpx.Response(500, text="Primary actor unavailable")
            # If request targets fallback actor, return success
            if "calm_builder" in url_str:
                return httpx.Response(200, json=[sample_item])
            return httpx.Response(404, text="Not found")

    mock_client = httpx.AsyncClient(transport=FallbackTransport())
    provider = ApifyFacebookProvider(
        token="test_token",
        actor_id=primary_actor,
        fallback_actors=[fallback_actor],
        client=mock_client,
    )

    results = await provider.search_region("CAT 966H", test_region_tx)
    assert len(results) == 1
    assert results[0].external_id == "fallback_item_1"
    assert provider.last_actor_id == fallback_actor
