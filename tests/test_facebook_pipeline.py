from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List
import pytest
from sqlalchemy import select

from app.database.models import AlertEvent, Listing
from app.providers.facebook.base import BaseFacebookProvider
from app.providers.facebook.models import FacebookRegion
from app.schemas.listing import RawListing
from app.workers.facebook_run_once import run_facebook_scan


class MockFacebookProvider(BaseFacebookProvider):
    """Deterministic mock provider returning test fixture items."""

    def __init__(self, items: List[RawListing]) -> None:
        super().__init__()
        self.mock_items = items

    async def search_region(
        self, query: str, region: FacebookRegion, max_results: int = 20
    ) -> List[RawListing]:
        return list(self.mock_items)

    def normalize_item(self, item: Dict[str, Any], region: FacebookRegion = None) -> RawListing:
        return item


@pytest.fixture
def mock_pipeline_region():
    return FacebookRegion(
        id="us_texas",
        name="Texas",
        country="US",
        location_id="dallas",
        search_url="https://www.facebook.com/marketplace/dallas/search",
        enabled=True,
        priority=1,
    )


@pytest.mark.asyncio
async def test_facebook_pipeline_equipment_matching_and_filtering(session_factory, test_db_session):
    """
    Test requirements:
    1. CAT 966H machine is accepted
    2. CAT 966H bucket is rejected
    3. CAT 966H toy is rejected
    4. duplicate Facebook product ID generates one listing
    """
    now = datetime.now(timezone.utc)
    mock_items = [
        # Real machine -> ACCEPT
        RawListing(
            source="facebook_marketplace",
            external_id="fb_machine_1",
            title="2014 Caterpillar 966H Wheel Loader - Ready for work",
            description="Good tires, enclosed cab with A/C, 8200 hours.",
            price=Decimal("110000.00"),
            currency="USD",
            location="Dallas, TX",
            url="https://www.facebook.com/marketplace/item/fb_machine_1",
            scraped_at=now,
        ),
        # Attachment -> REJECT
        RawListing(
            source="facebook_marketplace",
            external_id="fb_bucket_2",
            title="CAT 966H Loader Quick Coupler Bucket 5 yard",
            description="Heavy duty rock bucket for 966H loader.",
            price=Decimal("4500.00"),
            currency="USD",
            location="Houston, TX",
            url="https://www.facebook.com/marketplace/item/fb_bucket_2",
            scraped_at=now,
        ),
        # Toy / Model replica -> REJECT
        RawListing(
            source="facebook_marketplace",
            external_id="fb_toy_3",
            title="Diecast Masters CAT 966H Wheel Loader 1:50 Scale Model Toy",
            description="Collectible toy in original box, never opened.",
            price=Decimal("75.00"),
            currency="USD",
            location="Austin, TX",
            url="https://www.facebook.com/marketplace/item/fb_toy_3",
            scraped_at=now,
        ),
        # Duplicate of real machine -> DUPLICATE
        RawListing(
            source="facebook_marketplace",
            external_id="fb_machine_1",
            title="2014 Caterpillar 966H Wheel Loader - Ready for work",
            price=Decimal("110000.00"),
            currency="USD",
            location="Dallas, TX",
            url="https://www.facebook.com/marketplace/item/fb_machine_1",
            scraped_at=now,
        ),
    ]

    mock_provider = MockFacebookProvider(mock_items)

    metrics = await run_facebook_scan(
        queries=["CAT 966H"],
        region_ids=["us_texas"],
        persist=True,
        custom_provider=mock_provider,
        session_factory=session_factory,
    )

    # In-run deduplication reduced 4 items to 3 unique IDs
    assert metrics["total_raw_listings"] == 3
    assert metrics["unique_facebook_ids"] == 3

    # Verification:
    # Machine is ACCEPT
    # Bucket is REJECT
    # Toy is REJECT
    assert metrics["accepted"] == 1
    assert metrics["rejected"] == 2
    assert metrics["new_inserted"] == 1
    assert metrics["alerts_created"] == 1

    # Database state verification
    listings = (await test_db_session.execute(select(Listing))).scalars().all()
    assert len(listings) == 1
    assert listings[0].external_id == "fb_machine_1"
    assert listings[0].status == "ACCEPT"

    alerts = (await test_db_session.execute(select(AlertEvent))).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].listing_id == listings[0].id
    assert alerts[0].event_type == "NEW_LISTING"


@pytest.mark.asyncio
async def test_facebook_pipeline_latency_metrics_computation(session_factory, test_db_session):
    """Verify that latency calculations (median, P95, scan duration) are computed accurately."""
    now = datetime.now(timezone.utc)
    mock_items = [
        RawListing(
            source="facebook_marketplace",
            external_id="fb_lat_1",
            title="2010 Caterpillar 966H Wheel Loader",
            price=Decimal("95000.00"),
            currency="USD",
            location="San Antonio, TX",
            url="https://www.facebook.com/marketplace/item/fb_lat_1",
            scraped_at=now,
        )
    ]
    mock_provider = MockFacebookProvider(mock_items)

    metrics = await run_facebook_scan(
        queries=["CAT 966H"],
        region_ids=["us_texas"],
        persist=True,
        custom_provider=mock_provider,
        session_factory=session_factory,
    )

    assert "latency" in metrics
    assert metrics["latency"]["scan_duration_seconds"] >= 0.0
    assert metrics["latency"]["median_detection_latency_seconds"] >= 0.0
    assert metrics["latency"]["p95_detection_latency_seconds"] >= 0.0
    assert metrics["scan_completed_at"] is not None


def test_print_facebook_report(capsys):
    from app.workers.facebook_run_once import print_facebook_report

    sample_metrics = {
        "source": "facebook_marketplace",
        "provider": "ApifyFacebookProvider",
        "queries_executed": ["CAT 966H"],
        "regions_scanned": ["Texas (US)", "Alberta (CA)"],
        "total_raw_listings": 12,
        "unique_facebook_ids": 10,
        "accepted": 2,
        "review": 1,
        "rejected": 7,
        "new_inserted": 3,
        "price_changed": 0,
        "duplicates": 0,
        "alerts_created": 2,
        "errors": [],
        "latency": {
            "scan_duration_seconds": 1.45,
            "median_detection_latency_seconds": 0.05,
            "p95_detection_latency_seconds": 0.12,
            "max_detection_latency_seconds": 0.20,
        },
        "examples": {
            "accepted": [],
            "review": [],
            "rejected": [],
            "all_sample": [
                {
                    "id": "fb_123",
                    "title": "2012 CAT 966H Wheel Loader",
                    "classification": "ACCEPT",
                    "model": "966H",
                    "confidence": 0.95,
                    "price": "89500",
                    "currency": "USD",
                    "location": "Dallas, TX",
                    "url": "https://www.facebook.com/marketplace/item/fb_123",
                    "reason": None,
                }
            ],
        },
    }

    print_facebook_report(sample_metrics)
    captured = capsys.readouterr()
    assert "FACEBOOK MARKETPLACE DISCOVERY REPORT" in captured.out
    assert "Accepted Matches:      2" in captured.out
    assert "Alert Events Created:  2" in captured.out
    assert "Median Latency:     0.05s" in captured.out


@pytest.mark.asyncio
async def test_continuous_monitor_run_cycles(monkeypatch):
    from unittest.mock import AsyncMock
    from app.workers.monitor import ContinuousMonitor

    mock_scan = AsyncMock(return_value={
        "latency": {"scan_duration_seconds": 0.01},
        "total_raw_listings": 2,
        "accepted": 1,
        "new_inserted": 1,
        "price_changed": 0,
        "alerts_created": 1,
    })
    monkeypatch.setattr("app.workers.monitor.run_facebook_scan", mock_scan)

    monitor = ContinuousMonitor(interval_seconds=1, max_cycles=2)
    await monitor.run()

    assert mock_scan.call_count == 2
