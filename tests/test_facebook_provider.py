from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Dict, List
import pytest
import httpx

from app.providers.facebook.apify_provider import ApifyFacebookProvider
from app.providers.facebook.models import FacebookRegion, load_facebook_regions
from app.schemas.listing import RawListing


@pytest.fixture
def mock_region_us():
    return FacebookRegion(
        id="us_texas",
        name="Texas",
        country="US",
        location_id="dallas",
        search_url="https://www.facebook.com/marketplace/dallas/search",
        enabled=True,
        priority=1,
    )


@pytest.fixture
def mock_region_ca():
    return FacebookRegion(
        id="ca_alberta",
        name="Alberta",
        country="CA",
        location_id="calgary",
        search_url="https://www.facebook.com/marketplace/calgary/search",
        enabled=True,
        priority=1,
    )


def test_facebook_regions_load():
    """Verify that region grid configuration loads from YAML properly."""
    regions = load_facebook_regions()
    assert len(regions) >= 12
    us_regions = [r for r in regions if r.country == "US"]
    ca_regions = [r for r in regions if r.country == "CA"]
    assert len(us_regions) >= 7
    assert len(ca_regions) >= 5


def test_normalize_item_valid_us(mock_region_us):
    """Verify Apify payload normalization for US listing with standard USD price."""
    provider = ApifyFacebookProvider(token="test_token")
    raw_item = {
        "id": "1098234857234",
        "title": "2012 Caterpillar 966H Wheel Loader - Low Hours",
        "formattedPrice": "$89,500",
        "city": "Dallas",
        "location": "Dallas, TX",
        "listingUrl": "https://www.facebook.com/marketplace/item/1098234857234",
        "images": ["https://scontent.xx.fbcdn.net/v/t45.1600-4/test.jpg"],
        "description": "Clean machine ready for work. Serial: CAT966H012345.",
        "seller": {"name": "Texas Heavy Machinery LLC"},
    }

    normalized = provider.normalize_item(raw_item, region=mock_region_us)
    assert normalized is not None
    assert normalized.source == "facebook_marketplace"
    assert normalized.external_id == "1098234857234"
    assert normalized.title == "2012 Caterpillar 966H Wheel Loader - Low Hours"
    assert normalized.price == Decimal("89500")
    assert normalized.currency == "USD"
    assert normalized.location == "Dallas, TX"
    assert normalized.serial_number == "CAT966H012345"
    assert normalized.seller_name == "Texas Heavy Machinery LLC"


def test_normalize_item_currency_cad(mock_region_ca):
    """Test requirement: currency USD/CAD preserved correctly based on CA region or CAD symbol."""
    provider = ApifyFacebookProvider(token="test_token")
    raw_item = {
        "itemId": "992384729384",
        "title": "2008 Caterpillar 966H Wheel Loader",
        "formattedPrice": "CA$ 115,000",
        "listingUrl": "https://www.facebook.com/marketplace/item/992384729384",
    }

    normalized = provider.normalize_item(raw_item, region=mock_region_ca)
    assert normalized is not None
    assert normalized.currency == "CAD"
    assert normalized.price == Decimal("115000")


def test_normalize_item_id_fallback_from_url(mock_region_us):
    """Extracts external_id from Marketplace URL regex when ID field is absent."""
    provider = ApifyFacebookProvider(token="test_token")
    raw_item = {
        "title": "1998 CASE 580 Super L 4x4",
        "url": "https://www.facebook.com/marketplace/item/77112233445566/?ref=search",
        "price": "34500",
    }
    normalized = provider.normalize_item(raw_item, region=mock_region_us)
    assert normalized is not None
    assert normalized.external_id == "77112233445566"
    assert normalized.price == Decimal("34500")
    assert normalized.currency == "USD"


@pytest.mark.asyncio
async def test_duplicate_facebook_id_in_same_run_deduplicated(mock_region_us, mock_region_ca):
    """
    Test requirement: same listing returned from two regions is deduplicated inside provider run.
    """
    sample_listing = {
        "id": "duplicate_id_123",
        "title": "2010 CAT 966H Loader",
        "price": 95000,
        "url": "https://www.facebook.com/marketplace/item/duplicate_id_123",
    }

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[sample_listing])

    mock_client = httpx.AsyncClient(transport=MockTransport())
    provider = ApifyFacebookProvider(token="test_token", client=mock_client)

    results = await provider.fetch_latest(
        queries=["CAT 966H"],
        regions=[mock_region_us, mock_region_ca],
    )

    # 2 regions queried with identical item -> deduplicated to 1 listing
    assert len(results) == 1
    assert results[0].external_id == "duplicate_id_123"


@pytest.mark.asyncio
async def test_provider_partial_failure_does_not_fail_entire_scan(mock_region_us, mock_region_ca):
    """
    Test requirement: provider partial failure does not fail entire scan.
    One region returning 500 does not prevent successful results from the other region.
    """
    us_item = {
        "id": "us_success_1",
        "title": "CAT 966H in Texas",
        "price": 85000,
        "url": "https://www.facebook.com/marketplace/item/us_success_1",
    }

    class PartialFailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            # If request contains "calgary" (Canada region), fail with 500
            content = request.read().decode("utf-8")
            if "calgary" in content:
                return httpx.Response(500, text="Internal Server Error")
            return httpx.Response(200, json=[us_item])

    mock_client = httpx.AsyncClient(transport=PartialFailTransport())
    provider = ApifyFacebookProvider(token="test_token", client=mock_client)

    results = await provider.fetch_latest(
        queries=["CAT 966H"],
        regions=[mock_region_us, mock_region_ca],
    )

    assert len(results) == 1
    assert results[0].external_id == "us_success_1"


@pytest.mark.asyncio
async def test_timeout_is_isolated(mock_region_us, mock_region_ca):
    """
    Test requirement: timeout is isolated.
    A slow region timing out does not fail the other region or crash the run.
    """
    us_item = {
        "id": "fast_item_1",
        "title": "CAT 966H Fast Result",
        "price": 88000,
        "url": "https://www.facebook.com/marketplace/item/fast_item_1",
    }

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            content = request.read().decode("utf-8")
            if "calgary" in content:
                # Sleep longer than the provider timeout
                await asyncio.sleep(0.5)
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[us_item])

    mock_client = httpx.AsyncClient(transport=SlowTransport())
    # Configure short timeout of 0.1s for test
    provider = ApifyFacebookProvider(
        token="test_token",
        client=mock_client,
        timeout_seconds=0.1,
    )

    results = await provider.fetch_latest(
        queries=["CAT 966H"],
        regions=[mock_region_us, mock_region_ca],
    )

    assert len(results) == 1
    assert results[0].external_id == "fast_item_1"
